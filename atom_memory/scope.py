"""Scope resolution, hierarchy and scoring.

This is the second half of scope awareness (the first is
:mod:`~atom_memory.context`, which normalises the observations). It answers three
questions, and they are separate on purpose:

1. **Which scope does this context mean?** :meth:`ScopeStore.resolve` matches the
   context's signals against registered identities and returns a decision:
   *bind* to an existing scope, *create* one, *queue* a weak candidate, or
   *degrade* to global. The decision is a value, not a side effect buried in the
   write path, so it can be tested, reported and audited.
2. **What can be seen from a scope?** :func:`expanded_scope_ids` walks the
   hierarchy outward: the scope itself, its ancestors, the root, and — when the
   query is not pinned to a phase — the phase descendants of the same project. A
   fact is visible from every scope at or above it, which is what makes a global
   rule reachable from a project without being copied into it.
3. **How relevant is another scope?** :class:`ScopeView` scores a fact's scope
   bindings against the current context (exact / parent / grandparent / global /
   sibling / unrelated) and its conditions against the current ones.

**The hierarchy is walked through ``parent_id``, never through ``path``.**
``path`` is a materialised, human-readable label (``/global/org:acme/project:x``)
that exists for display and debugging; canonical names can themselves contain
separators (a path signal, a remote URL), so parsing ``path`` back into a tree
would be ambiguous the first time a name contained a slash. Only
:meth:`ScopeStore._recompute_paths` (after a merge) writes it.

Three decisions are worth stating explicitly, because each looks like an
omission:

* **A matched signal always binds.** The thresholds in
  :class:`~atom_memory.config.MemConfig` govern *creation*
  (``scope_new_threshold``) and how much confidence a binding is *claimed* to
  have (``scope_bind_threshold`` / ``scope_pending_threshold``), not
  re-identification. A signal row is unique per ``(type, normalized_value)``, so
  seeing one again is proof of the identity rather than weak evidence of it — a
  low-reliability signal such as ``path`` can only ever have been registered on a
  scope that stronger evidence (or three consistent sightings) already created.
* **Resolution never restructures the tree.** If a matched scope sits under
  ``/global`` while the context also names a client, the client is *not* spliced
  in as its parent: doing that on a heuristic would silently move every fact
  already bound to it. An operator does that through :meth:`ScopeStore.merge` /
  :meth:`ScopeStore.split`, where the change is explicit and auditable.
* **A read never writes.** ``create=False`` (what a query uses) does not create
  scopes, register signals, refresh ``last_seen_at`` or extend the candidate
  queue. A recall that mutated the scope tree would make the answer depend on how
  often it had been asked.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import MemConfig
from .context import (
    GLOBAL_SCOPE_ID,
    SCOPE_DEPTH,
    SCOPE_DOCUMENT,
    SCOPE_GLOBAL,
    SCOPE_PHASE,
    SCOPE_THREAD,
    SCOPE_TYPES,
    ChainLevel,
    ScopeContext,
    ScopeSignal,
    display_name_for,
    normalize_conditions,
    normalize_signal,
)
from .db import now_ms, record_event

logger = logging.getLogger(__name__)

# -- resolution outcomes ------------------------------------------------------

STATUS_BOUND = "bound"
"""An existing scope was identified; facts bind to it."""
STATUS_CREATED = "created"
"""A new scope was created from high-confidence evidence."""
STATUS_PENDING = "pending_confirmation"
"""Bound on weaker evidence (a recorded alias rather than an observed signal)."""
STATUS_UNRESOLVED = "unresolved"
"""No scope: the fact binds to the nearest known ancestor, and the candidate is
queued until it has been seen consistently enough to promote."""
STATUS_GLOBAL = "global"
"""No usable evidence at all: the fact is a global fact."""

# -- scope distance weights (design §6.4) -------------------------------------

W_EXACT = 1.0
W_PARENT = 0.8
W_GRANDPARENT = 0.6
W_ANCESTOR = 0.5
W_GLOBAL = 0.5
W_PHASE = 0.5
W_SIBLING = 0.3
W_OTHER = 0.15

#: Weight of a fact whose conditions say nothing, or of a query that declares
#: none. Neutral rather than zero: silence about conditions must not be scored as
#: a mismatch.
W_CONDITION_NEUTRAL = 0.5

#: Weight of an alias match relative to an observed signal of the same type. An
#: alias records that someone *believed* two identities were the same; a signal
#: is the identity itself.
ALIAS_MATCH_FACTOR = 0.8

#: Relations recorded in ``fact_evolution`` / ``fact_origin``.
RELATION_EVOLVES_TO = "evolves_to"
RELATION_SUPERSEDES = "supersedes"
RELATION_EXCEPTION = "exception"
RELATION_ABSTRACTION = "abstraction"
RELATION_CROSS_SCOPE_SIMILAR = "cross_scope_similar"

#: How many fact ids go into one ``IN (...)`` clause. The digest asks for every
#: active fact of a user, so the batch is unbounded from here; SQLite's bound
#: variable limit is 32766 on current builds, and a query that crossed it would
#: fail the whole read rather than degrade.
_SQL_CHUNK = 500


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    """Yield ``items`` in slices of at most ``size`` (empty input yields none)."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


# -- small read-only views ----------------------------------------------------


@dataclass(frozen=True)
class ScopeRow:
    """One row of ``scope``."""

    id: int
    scope_type: str
    canonical_name: str
    parent_id: Optional[int]
    path: str
    status: str
    merged_into: Optional[int]
    confidence: float
    display_name: str = ""

    def to_dict(self) -> dict:
        """Return the JSON-serializable form used by the RPC surface."""
        return {
            "scope_id": self.id,
            "scope_type": self.scope_type,
            "name": self.canonical_name,
            "display_name": self.display_name or self.canonical_name,
            "parent_id": self.parent_id,
            "path": self.path,
            "status": self.status,
            "confidence": round(float(self.confidence), 4),
        }


@dataclass(frozen=True)
class ScopeCandidate:
    """A scope-shaped hypothesis that has not earned creation yet."""

    scope_type: str
    canonical_name: str
    parent_id: Optional[int]
    signal_type: str
    normalized_value: str
    confidence: float
    seen_count: int = 1

    def to_dict(self) -> dict:
        """Return the JSON-serializable form used by the RPC surface."""
        return {
            "scope_type": self.scope_type,
            "name": self.canonical_name,
            "parent_id": self.parent_id,
            "signal_type": self.signal_type,
            "signal_value": self.normalized_value,
            "confidence": round(float(self.confidence), 4),
            "seen_count": int(self.seen_count),
        }


@dataclass(frozen=True)
class ScopeResolution:
    """What a context resolved to.

    Attributes:
        scope_id: The scope facts from this context bind to. Always a real scope
            id — ``GLOBAL_SCOPE_ID`` when nothing better could be established.
        confidence: Evidence behind the decision.
        status: One of the ``STATUS_*`` constants.
        conditions: The context's conditions, carried here so a caller has one
            object to persist from.
        candidates: Sub-threshold hypotheses that were queued (empty otherwise).
        matched: Signal types that matched a registered identity.
        detail: Human-readable explanation, recorded in the audit log.
    """

    scope_id: int
    confidence: float
    status: str
    conditions: Tuple[Tuple[str, str], ...] = ()
    candidates: Tuple[ScopeCandidate, ...] = ()
    matched: Tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_global(self) -> bool:
        """Whether the resolution landed on the root scope."""
        return int(self.scope_id) == GLOBAL_SCOPE_ID

    def to_dict(self, store: Optional["ScopeStore"] = None) -> dict:
        """Return the JSON-serializable form used by the RPC surface."""
        return {
            "scope_id": self.scope_id,
            "scope_type": store.scope_type(self.scope_id) if store else None,
            "path": store.path_of(self.scope_id) if store else None,
            "display_name": store.label(self.scope_id) if store else None,
            "confidence": round(float(self.confidence), 4),
            "status": self.status,
            "conditions": [{"key": k, "value": v} for k, v in self.conditions],
            "candidates": [c.to_dict() for c in self.candidates],
            "matched": list(self.matched),
            "detail": self.detail,
        }


# -- pure scoring helpers -----------------------------------------------------


def distance_weight(a: int, b: int, ancestors: Mapping[int, int]) -> float:
    """Return the weight of the hierarchy relation between two scope ids.

    Args:
        a: The query's scope id.
        b: The fact's scope id.
        ancestors: ``{scope_id: distance}`` for ``a``'s ancestors, distance 1
            being ``a``'s parent (:func:`ancestor_distances` builds it).

    Returns:
        ``W_EXACT`` for the same scope, ``W_PARENT`` for a direct parent,
        ``W_GRANDPARENT`` for a grandparent, ``W_ANCESTOR`` above that, and
        ``W_GLOBAL`` for the root — which is checked before the ancestor map,
        because the root is also an ancestor and must keep its own weight.
    """
    if a == b:
        return W_EXACT
    if b == GLOBAL_SCOPE_ID:
        return W_GLOBAL
    steps = ancestors.get(b)
    if steps == 1:
        return W_PARENT
    if steps == 2:
        return W_GRANDPARENT
    if steps is not None:
        return W_ANCESTOR
    return W_OTHER


def ancestor_distances(conn: sqlite3.Connection, scope_id: int) -> Dict[int, int]:
    """Return ``{ancestor_id: distance}`` for a scope, excluding itself.

    Args:
        conn: Open SQLite connection.
        scope_id: The scope to walk up from.

    Returns:
        Ancestors keyed by id, nearest first (distance 1 = parent). The walk is
        bounded by the number of scope types, which is also what stops a
        corrupted ``parent_id`` cycle from looping forever.
    """
    distances: Dict[int, int] = {}
    seen = {scope_id}
    current = int(scope_id)
    for depth in range(1, len(SCOPE_TYPES) + 1):
        row = conn.execute(
            "SELECT parent_id FROM scope WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row["parent_id"] is None:
            break
        parent = int(row["parent_id"])
        if parent in seen:
            logger.warning("Scope %s has a cyclic parent chain; stopping", scope_id)
            break
        seen.add(parent)
        distances[parent] = depth
        current = parent
    return distances


def condition_match(
    fact_conditions: Sequence[Tuple[str, str]],
    current_conditions: Mapping[str, Sequence[str]],
) -> float:
    """Return how well a fact's conditions match the current context.

    Args:
        fact_conditions: The fact's ``(key, value)`` conditions.
        current_conditions: The query context's ``{key: [values]}``.

    Returns:
        ``W_CONDITION_NEUTRAL`` when either side is empty (nothing to compare,
        so nothing is punished), ``1.0`` when every condition of the fact is
        satisfied by the current context, and ``matched / total`` in between.
    """
    pairs = [pair for pair in (fact_conditions or ()) if pair]
    if not pairs:
        return W_CONDITION_NEUTRAL
    if not current_conditions:
        return W_CONDITION_NEUTRAL
    matched = 0
    for key, value in pairs:
        if value in (current_conditions.get(key) or ()):
            matched += 1
    return matched / len(pairs)


def phase_match(
    fact_scope_ids: Sequence[int],
    current_scope_id: Optional[int],
    scope_types: Mapping[int, str],
    phase_ids: Mapping[int, int],
) -> float:
    """Return how well a fact's phase bindings agree with the current phase.

    A fact bound to the current phase scores 1.0; a fact bound to *another* phase
    of the same project scores ``W_PHASE``. Everything else is neutral
    (``W_PHASE`` as well), because a global rule is not a phase mismatch — only
    an explicit disagreement is discounted.

    Args:
        fact_scope_ids: The scopes the fact is bound to.
        current_scope_id: The query's scope id, or ``None`` for a context-free
            query.
        scope_types: ``{scope_id: scope_type}`` covering the fact's scopes.
        phase_ids: ``{phase_scope_id: weight}`` for phases reachable from the
            current scope.

    Returns:
        A 0..1 weight.
    """
    if current_scope_id is None or current_scope_id == GLOBAL_SCOPE_ID:
        return W_PHASE
    if scope_types.get(current_scope_id) != SCOPE_PHASE:
        return W_PHASE
    if current_scope_id in fact_scope_ids:
        return 1.0
    for scope_id in fact_scope_ids:
        if scope_types.get(scope_id) == SCOPE_PHASE and scope_id in phase_ids:
            return W_PHASE
    return W_PHASE


def expanded_scope_ids(
    conn: sqlite3.Connection,
    scope_id: int,
    include_phases: bool = True,
) -> Tuple[List[int], List[int]]:
    """Return the scopes visible from a scope.

    Args:
        conn: Open SQLite connection.
        scope_id: The scope whose neighbourhood to expand.
        include_phases: Also include ``phase`` descendants of ``scope_id`` (the
            design's "default: recall every phase"). Off pins recall to the
            current phase.

    Returns:
        ``(upward, phases)``: the scope itself, its ancestors and the root, then
        its phase descendants (children, grandchildren, ... that are phases).
        Non-phase descendants are *not* included: a document-scoped fact is not
        visible from its sibling documents just because they share a project.
    """
    upward: List[int] = [int(scope_id)]
    current = int(scope_id)
    seen = {int(scope_id)}
    for _ in range(len(SCOPE_TYPES)):
        row = conn.execute(
            "SELECT parent_id FROM scope WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row["parent_id"] is None:
            break
        parent = int(row["parent_id"])
        if parent in seen:
            break
        seen.add(parent)
        upward.append(parent)
        current = parent
    if GLOBAL_SCOPE_ID not in upward:
        upward.append(GLOBAL_SCOPE_ID)

    phases: List[int] = []
    if include_phases:
        frontier = [int(scope_id)]
        visited = {int(scope_id)}
        for _ in range(len(SCOPE_TYPES)):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            rows = conn.execute(
                f"SELECT id, scope_type FROM scope WHERE parent_id IN ({placeholders}) "
                f"AND status = 'active'",
                frontier,
            ).fetchall()
            nxt: List[int] = []
            for row in rows:
                child = int(row["id"])
                if child in visited:
                    continue
                visited.add(child)
                if row["scope_type"] == SCOPE_PHASE:
                    phases.append(child)
                else:
                    nxt.append(child)
            frontier = nxt
    return upward, phases


# -- the store ----------------------------------------------------------------


class ScopeStore:
    """SQLite-backed scope hierarchy, resolution and scoring."""

    def __init__(
        self, conn: sqlite3.Connection, config: Optional[MemConfig] = None
    ) -> None:
        """Initialise the store.

        Args:
            conn: Open SQLite connection (the library's single writer, or any
                reader).
            config: Configuration carrying the resolution thresholds and the
                promotion policy. ``None`` uses library defaults.
        """
        self.conn = conn
        self.config = config or MemConfig()
        self._label_cache: Dict[int, str] = {}

    # -- reading ------------------------------------------------------------

    def get(self, scope_id: int) -> Optional[ScopeRow]:
        """Return one scope row exactly as stored, or ``None``.

        Deliberately does **not** follow ``merged_into``: this is the accessor
        that has to be able to say "this scope was merged into that one" (the
        merge bookkeeping itself, and ``scope_list --status merged``). The
        resolving accessors are :meth:`path_of`, :meth:`label`,
        :meth:`scope_type` and :meth:`parent_of`.
        """
        row = self.conn.execute(
            "SELECT id, scope_type, canonical_name, parent_id, path, status, "
            "merged_into, confidence FROM scope WHERE id = ?",
            (int(scope_id),),
        ).fetchone()
        if row is None:
            return None
        return ScopeRow(
            id=int(row["id"]),
            scope_type=str(row["scope_type"]),
            canonical_name=str(row["canonical_name"]),
            parent_id=row["parent_id"],
            path=str(row["path"]),
            status=str(row["status"]),
            merged_into=row["merged_into"],
            confidence=float(row["confidence"]),
            display_name=self._display_name_of(int(row["id"])),
        )

    def exists(self, scope_id: int) -> bool:
        """Whether a scope id is known."""
        return self.conn.execute(
            "SELECT 1 FROM scope WHERE id = ?", (int(scope_id),)
        ).fetchone() is not None

    def resolve_id(self, scope_id: int) -> int:
        """Follow ``merged_into`` to the scope that now owns this identity.

        Args:
            scope_id: A possibly-merged scope id.

        Returns:
            The surviving scope id, or ``GLOBAL_SCOPE_ID`` when the id is unknown
            — an unknown scope must never keep a fact out of every recall.
        """
        current = int(scope_id)
        for _ in range(len(SCOPE_TYPES) + 1):
            row = self.conn.execute(
                "SELECT status, merged_into FROM scope WHERE id = ?", (current,)
            ).fetchone()
            if row is None:
                return GLOBAL_SCOPE_ID
            if row["status"] != "merged" or row["merged_into"] is None:
                return current
            current = int(row["merged_into"])
        logger.warning("Scope %s has a cyclic merge chain; using global", scope_id)
        return GLOBAL_SCOPE_ID

    def scope_type(self, scope_id: int) -> str:
        """Return a scope's type (``global`` for an unknown id)."""
        row = self.conn.execute(
            "SELECT scope_type FROM scope WHERE id = ?", (self.resolve_id(scope_id),)
        ).fetchone()
        return str(row["scope_type"]) if row is not None else SCOPE_GLOBAL

    def path_of(self, scope_id: int) -> str:
        """Return a scope's materialised path (``/global`` for an unknown id)."""
        row = self.conn.execute(
            "SELECT path FROM scope WHERE id = ?", (self.resolve_id(scope_id),)
        ).fetchone()
        return str(row["path"]) if row is not None else "/global"

    def parent_of(self, scope_id: int) -> int:
        """Return a scope's parent id (``GLOBAL_SCOPE_ID`` when there is none)."""
        row = self.conn.execute(
            "SELECT parent_id FROM scope WHERE id = ?", (self.resolve_id(scope_id),)
        ).fetchone()
        if row is None or row["parent_id"] is None:
            return GLOBAL_SCOPE_ID
        return int(row["parent_id"])

    def label(self, scope_id: int) -> str:
        """Return the short human label of a scope, for block headings.

        ``[当前项目: dsh-atom-memory]`` rather than
        ``[当前项目: github.com/owner/dsh-atom-memory]``: the label is read by the
        model, and a full URL is noise in a heading. The complete identity stays
        available through :meth:`path_of`.
        """
        resolved = self.resolve_id(int(scope_id))
        cached = self._label_cache.get(resolved)
        if cached is not None:
            return cached
        label = "global" if resolved == GLOBAL_SCOPE_ID else self._display_name_of(resolved)
        self._label_cache[resolved] = label
        return label

    def _display_name_of(self, scope_id: int) -> str:
        """Return the best short name for a scope: its ``name`` alias, else derived."""
        row = self.conn.execute(
            "SELECT alias FROM scope_alias WHERE scope_id = ? AND alias_type = 'name' "
            "ORDER BY confidence DESC, id ASC LIMIT 1",
            (scope_id,),
        ).fetchone()
        if row is not None:
            return str(row["alias"])
        row = self.conn.execute(
            "SELECT canonical_name FROM scope WHERE id = ?", (scope_id,)
        ).fetchone()
        if row is None:
            return "global"
        name = str(row["canonical_name"])
        return display_name_for("", name) or name

    def list_scopes(
        self, parent_id: Optional[int] = None, status: str = "active"
    ) -> List[ScopeRow]:
        """List scopes under a parent, or every scope with the given status."""
        if parent_id is None:
            rows = self.conn.execute(
                "SELECT id FROM scope WHERE status = ? ORDER BY path", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id FROM scope WHERE parent_id = ? AND status = ? ORDER BY path",
                (self.resolve_id(parent_id), status),
            ).fetchall()
        return [row for row in (self.get(int(r["id"])) for r in rows) if row is not None]

    def children(self, scope_id: int, status: str = "active") -> List[int]:
        """Return a scope's direct children."""
        return [
            int(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM scope WHERE parent_id = ? AND status = ? ORDER BY id",
                (self.resolve_id(scope_id), status),
            ).fetchall()
        ]

    def unresolved(self, user_id: str, limit: int = 50) -> List[dict]:
        """Return a user's pending scope candidates, most-seen first.

        This is the queue the design asks a user to confirm: a project identified
        only by its folder path is not created on first sight, but the store
        remembers that it keeps coming up.
        """
        rows = self.conn.execute(
            "SELECT scope_type, canonical_name, parent_id, signal_type, "
            "normalized_value, confidence, seen_count FROM scope_candidate "
            "WHERE user_id = ? AND status = 'pending' "
            "ORDER BY seen_count DESC, last_seen DESC LIMIT ?",
            (user_id, max(1, int(limit))),
        ).fetchall()
        return [
            ScopeCandidate(
                scope_type=str(r["scope_type"]),
                canonical_name=str(r["canonical_name"]),
                parent_id=r["parent_id"],
                signal_type=str(r["signal_type"]),
                normalized_value=str(r["normalized_value"]),
                confidence=float(r["confidence"]),
                seen_count=int(r["seen_count"]),
            ).to_dict()
            for r in rows
        ]

    # -- resolution ---------------------------------------------------------

    def resolve(
        self, ctx: Optional[ScopeContext], create: bool = True
    ) -> ScopeResolution:
        """Decide which scope a context belongs to.

        The algorithm, in order:

        1. For each chain level, most specific first, look the level's signals up
           in ``scope_signal`` (exact identity) and then in ``scope_alias``
           (recorded equivalence). The first hit wins.
        2. On a hit: attach any newly observed signals that belong to that scope's
           own type, refresh ``last_seen_at`` up the ancestor chain, and return
           ``bound`` — or ``pending_confirmation`` for an alias-only hit whose
           evidence stays below ``scope_bind_threshold``.
        3. On no hit: create the levels whose evidence reaches
           ``scope_new_threshold``, creating their missing ancestors as well.
        4. Otherwise queue the best sub-threshold hypothesis and return
           ``unresolved`` (bound to the nearest known ancestor, so the fact is
           still reachable) — or ``global`` when there is not even a hypothesis.

        Args:
            ctx: The context to resolve, or ``None`` (returns a global
                resolution).
            create: Whether a high-confidence context may create scopes, register
                signals and extend the candidate queue. ``False`` makes
                resolution read-only, which is what a *query* wants.

        Returns:
            A :class:`ScopeResolution`.
        """
        if ctx is None or not ctx.signals:
            return ScopeResolution(
                scope_id=GLOBAL_SCOPE_ID,
                confidence=1.0,
                status=STATUS_GLOBAL,
                conditions=ctx.conditions if ctx is not None else (),
                detail="no scope signals in context",
            )

        chain = ctx.chain()
        for level in reversed(chain):
            hit = self._match_level(level)
            if hit is None:
                continue
            scope_id, confidence, matched, via_alias = hit
            scope_id = self.resolve_id(scope_id)
            # The scope's own standing raises the claim: a scope the user
            # confirmed (or that strong evidence created) resolves without
            # further doubt, while a weak signal matched to a provisionally
            # promoted scope keeps that provisional confidence — which is what
            # makes `pending_confirmation` mean something.
            confidence = max(confidence, self._stored_confidence(scope_id))
            if create:
                self._attach_signals(scope_id, level)
                self._touch(scope_id)
            status = (
                STATUS_PENDING
                if via_alias
                and confidence < float(self.config.scope_bind_threshold)
                else STATUS_BOUND
            )
            return ScopeResolution(
                scope_id=scope_id,
                confidence=confidence,
                status=status,
                conditions=ctx.conditions,
                matched=tuple(matched),
                detail=(
                    f"matched {matched[0] if matched else 'alias'} on "
                    f"{self.path_of(scope_id)} (confidence {confidence:.2f})"
                ),
            )

        if create:
            created = self._create_chain(chain, ctx)
            if created is not None:
                scope_id, confidence, level = created
                return ScopeResolution(
                    scope_id=scope_id,
                    confidence=confidence,
                    status=STATUS_CREATED,
                    conditions=ctx.conditions,
                    matched=tuple(s.signal_type for s in level.signals),
                    detail=(
                        f"created {self.path_of(scope_id)} from "
                        f"{level.signals[0].signal_type} "
                        f"(confidence {confidence:.2f})"
                    ),
                )

            queued = self._queue_candidates(chain, ctx)
            promoted, level = self._promote_matured(chain, ctx)
            if promoted is not None and level is not None:
                return ScopeResolution(
                    scope_id=promoted,
                    confidence=level.confidence,
                    status=STATUS_CREATED,
                    conditions=ctx.conditions,
                    matched=(level.signals[0].signal_type,),
                    detail=(
                        f"promoted {self.path_of(promoted)} after "
                        f"{int(self.config.scope_promote_after)} consistent sightings"
                    ),
                )
            if queued:
                parent = self._nearest_known(chain)
                return ScopeResolution(
                    scope_id=parent,
                    confidence=queued[0].confidence,
                    status=STATUS_UNRESOLVED,
                    conditions=ctx.conditions,
                    candidates=tuple(queued),
                    detail=(
                        f"no scope matched; queued {len(queued)} candidate(s) under "
                        f"{self.path_of(parent)}"
                    ),
                )

        return ScopeResolution(
            scope_id=GLOBAL_SCOPE_ID,
            confidence=1.0,
            status=STATUS_GLOBAL,
            conditions=ctx.conditions,
            detail="no scope evidence; bound to global",
        )

    def _stored_confidence(self, scope_id: int) -> float:
        """Return a scope's stored confidence (1.0 for an unknown id)."""
        row = self.conn.execute(
            "SELECT confidence FROM scope WHERE id = ?", (self.resolve_id(scope_id),)
        ).fetchone()
        return float(row["confidence"]) if row is not None else 1.0

    def _match_level(
        self, level: ChainLevel
    ) -> Optional[Tuple[int, float, List[str], bool]]:
        """Find the scope a chain level matches, if any.

        Args:
            level: The chain level to match.

        Returns:
            ``(scope_id, confidence, matched_signal_types, via_alias)`` or
            ``None``. Signals are tried in identity order, so the stable identity
            wins over a local path when both are registered.
        """
        for signal in level.signals:
            row = self.conn.execute(
                "SELECT scope_id FROM scope_signal "
                "WHERE signal_type = ? AND normalized_value = ?",
                (signal.signal_type, signal.normalized),
            ).fetchone()
            if row is not None:
                return (
                    int(row["scope_id"]),
                    signal.reliability,
                    [signal.signal_type],
                    False,
                )
            alias = self.conn.execute(
                "SELECT scope_id FROM scope_alias WHERE alias_type = ? AND alias = ?",
                (signal.signal_type, signal.normalized),
            ).fetchone()
            if alias is not None:
                return (
                    int(alias["scope_id"]),
                    signal.reliability * ALIAS_MATCH_FACTOR,
                    [signal.signal_type],
                    True,
                )
        return None

    def _attach_signals(self, scope_id: int, level: ChainLevel) -> None:
        """Register newly observed signals that belong to a scope's own type.

        A ``doc_id`` observed while resolving a *project* is not attached to it: a
        scope may not hold identities of another type, because that is exactly how
        two unrelated projects would collapse into one.
        """
        scope_type = self.scope_type(scope_id)
        now = now_ms()
        for signal in level.signals:
            if signal.scope_type != scope_type:
                continue
            existing = self.conn.execute(
                "SELECT scope_id FROM scope_signal WHERE signal_type = ? "
                "AND normalized_value = ?",
                (signal.signal_type, signal.normalized),
            ).fetchone()
            if existing is not None:
                if int(existing["scope_id"]) == scope_id:
                    with self.conn:
                        self.conn.execute(
                            "UPDATE scope_signal SET last_seen = ? WHERE signal_type = ? "
                            "AND normalized_value = ?",
                            (now, signal.signal_type, signal.normalized),
                        )
                # A signal registered to a *different* scope is left alone: the
                # unique index makes it that scope's identity, and stealing it
                # here would move facts between projects without a merge.
                continue
            try:
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO scope_signal(scope_id, signal_type, signal_value, "
                        "normalized_value, confidence, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            scope_id,
                            signal.signal_type,
                            signal.value,
                            signal.normalized,
                            signal.reliability,
                            now,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError:  # pragma: no cover - concurrent writer
                logger.debug(
                    "Signal %s=%s was registered concurrently",
                    signal.signal_type, signal.normalized,
                )

    def _touch(self, scope_id: int) -> None:
        """Refresh ``last_seen_at`` on a scope and its ancestors."""
        now = now_ms()
        ids = [self.resolve_id(scope_id), *ancestor_distances(self.conn, scope_id).keys()]
        placeholders = ",".join("?" for _ in ids)
        with self.conn:
            self.conn.execute(
                f"UPDATE scope SET last_seen_at = ? WHERE id IN ({placeholders})",
                [now, *ids],
            )

    def _creation_threshold(self, scope_type: str) -> float:
        """Return the evidence a *new* scope of this type must clear.

        Two thresholds, because "what counts as evidence" is not the same
        question for every scope type. A ``document`` is the case that matters:
        recall's candidate set is the scope's own path plus its ancestors, its
        phases and the root (see :func:`expanded_scope_ids` and
        :meth:`ScopeView.visible_ids`), so a fact bound to a document is
        *invisible from every sibling document* while the same fact bound to a
        project is visible from all of them. A weak signal that creates a
        document therefore buries the fact instead of filing it — the classic
        shape being a per-chapter file whose ``folder_path`` (0.50) is the only
        evidence present, which makes chapter 3's notes unreachable while
        working on chapter 4.

        Raising the bar for ``document`` / ``thread`` keeps those facts filed at
        the level that is actually reachable (the project) and lets the weak
        signal bind to a document that stronger evidence already created. The
        other levels keep the single general threshold: a project created from a
        bare ``path`` (0.50) is still a project, and it is reachable from every
        phase and document beneath it.

        Args:
            scope_type: The type a new scope would have.

        Returns:
            The minimum ``level.confidence`` that may create it.
        """
        if scope_type in (SCOPE_DOCUMENT, SCOPE_THREAD):
            return float(
                self.config.scope_new_threshold_document
                if self.config.scope_new_threshold_document is not None
                else self.config.scope_new_threshold
            )
        return float(self.config.scope_new_threshold)

    def _create_chain(
        self, chain: Sequence[ChainLevel], ctx: ScopeContext
    ) -> Optional[Tuple[int, float, ChainLevel]]:
        """Create the deepest level whose evidence clears its creation threshold.

        Args:
            chain: The context's chain, most general first.
            ctx: The owning context (unused today, kept for signature symmetry
                with :meth:`_queue_candidates`).

        Returns:
            ``(scope_id, confidence, level)`` for the deepest level created, or
            ``None`` when no level reaches its creation threshold (see
            :meth:`_creation_threshold`).
        """
        del ctx  # the chain already carries everything the walk needs
        parent_id: Optional[int] = GLOBAL_SCOPE_ID
        created: Optional[Tuple[int, float, ChainLevel]] = None
        for level in chain:
            if level.confidence < self._creation_threshold(level.scope_type):
                # A level below its threshold stops the walk rather than being
                # skipped: creating a *phase* without its project would file the
                # phase under the root, where every project's recall finds it.
                # The same applies to a document under a parent that was never
                # created — filing it under the root would make one chapter's
                # notes visible to every unrelated context.
                break
            scope_id = self.create(
                scope_type=level.scope_type,
                name=level.canonical_name,
                parent_id=parent_id,
                signals=level.signals,
                confidence=level.confidence,
                display_name=level.display_name,
            )
            parent_id = scope_id
            created = (scope_id, level.confidence, level)
        return created

    def find_by_signal(self, signal_type: str, value: str) -> Optional[int]:
        """Return the scope a signal currently identifies, or ``None``.

        Args:
            signal_type: A key of :data:`~atom_memory.context.SIGNAL_SPECS`.
            value: The raw signal value (normalised here).

        Returns:
            The owning scope id (merges resolved), or ``None``.
        """
        normalized = normalize_signal(signal_type, value)
        if not normalized:
            return None
        row = self.conn.execute(
            "SELECT scope_id FROM scope_signal WHERE signal_type = ? "
            "AND normalized_value = ?",
            (signal_type, normalized),
        ).fetchone()
        return self.resolve_id(int(row["scope_id"])) if row is not None else None

    def find_by_alias(self, alias_type: str, value: str) -> Optional[int]:
        """Return the scope an alias currently identifies, or ``None``."""
        normalized = normalize_signal(alias_type, value) or normalize_signal("name", value)
        if not normalized:
            return None
        row = self.conn.execute(
            "SELECT scope_id FROM scope_alias WHERE alias_type = ? AND alias = ?",
            (alias_type, normalized),
        ).fetchone()
        return self.resolve_id(int(row["scope_id"])) if row is not None else None

    def create(
        self,
        scope_type: str,
        name: str,
        parent_id: Optional[int] = None,
        signals: Sequence[ScopeSignal] = (),
        confidence: float = 0.5,
        display_name: str = "",
    ) -> int:
        """Create (or return) a scope and register its signals.

        Idempotent on ``(parent_id, scope_type, name)``: an existing scope is
        returned rather than duplicated, which is what makes resolution safe to
        retry.

        Args:
            scope_type: One of :data:`~atom_memory.context.SCOPE_TYPES`.
            name: The canonical name.
            parent_id: The parent scope (``None`` means the root).
            signals: Signals that identify this scope.
            confidence: Evidence behind the creation.
            display_name: Optional short label stored as a ``name`` alias.

        Returns:
            The scope id.

        Raises:
            ValueError: For an unknown scope type, an empty name, a parent that
                is not more general than the child, or a signal that already
                identifies a *different* scope. That last case is the one that
                matters: a signal is a unique identity, so creating a second scope
                for it would split the same project's history in two — which is
                exactly the near-duplicate-scope failure the design warns about.
                Resolution never hits it (it matches before it creates); a direct
                caller must merge or reparent instead.
        """
        if scope_type not in SCOPE_DEPTH or scope_type == SCOPE_GLOBAL:
            raise ValueError(f"invalid scope type: {scope_type!r}")
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("scope name must not be empty")
        parent = GLOBAL_SCOPE_ID if parent_id is None else self.resolve_id(parent_id)
        if parent != GLOBAL_SCOPE_ID:
            parent_row = self.get(parent)
            if parent_row is None:
                parent = GLOBAL_SCOPE_ID
            elif SCOPE_DEPTH[parent_row.scope_type] >= SCOPE_DEPTH[scope_type]:
                raise ValueError(
                    f"parent scope {parent_row.scope_type} is not more general "
                    f"than {scope_type}"
                )
            else:
                parent = parent_row.id

        level = ChainLevel(
            scope_type=scope_type,
            canonical_name=clean_name,
            signals=tuple(signals),
            confidence=confidence,
            display_name=display_name or clean_name,
        )
        existing = self.conn.execute(
            "SELECT id FROM scope WHERE parent_id = ? AND scope_type = ? "
            "AND canonical_name = ?",
            (parent, scope_type, clean_name),
        ).fetchone()
        if existing is not None:
            scope_id = int(existing["id"])
            self._attach_signals(scope_id, level)
            return scope_id

        for signal in level.signals:
            owner = self.find_by_signal(signal.signal_type, signal.value)
            if owner is None:
                # An alias with the same (type, normalized value) is an identity
                # claim too: `scope_alias` is unique on exactly that pair, so
                # creating a second scope for it would leave two scopes
                # answering to one name.
                owner = self.find_by_alias(signal.signal_type, signal.value)
            if owner is not None and owner != parent:
                raise ValueError(
                    f"signal {signal.signal_type}={signal.normalized!r} already "
                    f"identifies scope {owner}; merge or reparent instead"
                )

        now = now_ms()
        path = f"{self.path_of(parent)}/{scope_type}:{clean_name}"
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO scope(scope_type, canonical_name, parent_id, path, "
                "status, merged_into, confidence, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, 'active', NULL, ?, ?, ?)",
                (scope_type, clean_name, parent, path, confidence, now, now),
            )
            scope_id = int(cursor.lastrowid)
        self._attach_signals(scope_id, level)
        if display_name and display_name != clean_name:
            self.add_alias(scope_id, display_name, "name", confidence)
        self._label_cache.pop(scope_id, None)
        logger.info("Created scope %s (id=%d)", path, scope_id)
        return scope_id

    def _queue_candidates(
        self, chain: Sequence[ChainLevel], ctx: ScopeContext
    ) -> List[ScopeCandidate]:
        """Record sub-threshold hypotheses, counting consistent sightings.

        A candidate is promoted once
        :attr:`~atom_memory.config.MemConfig.scope_promote_after` *consistent*
        sightings have accumulated — the design's "N times in the unresolved queue
        with consistent signals". Consistency is enforced by the unique index: the
        same (type, name, signal) bumps ``seen_count``, while a different signal
        under the same name is a separate row that cannot promote the first.

        Counting and promoting are split (see :meth:`_promote_matured`) so that a
        sighting is recorded even when the promotion itself fails.
        """
        if not ctx.user_id:
            return []
        parent_id = self._nearest_known(chain)
        out: List[ScopeCandidate] = []
        now = now_ms()
        for level in chain:
            signal = level.signals[0]
            row = self.conn.execute(
                "SELECT id, seen_count FROM scope_candidate WHERE user_id = ? "
                "AND scope_type = ? AND canonical_name = ? AND signal_type = ? "
                "AND normalized_value = ?",
                (
                    ctx.user_id,
                    level.scope_type,
                    level.canonical_name,
                    signal.signal_type,
                    signal.normalized,
                ),
            ).fetchone()
            if row is None:
                with self.conn:
                    self.conn.execute(
                        "INSERT INTO scope_candidate(user_id, scope_type, "
                        "canonical_name, parent_id, signal_type, signal_value, "
                        "normalized_value, confidence, seen_count, status, "
                        "first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'pending', ?, ?)",
                        (
                            ctx.user_id,
                            level.scope_type,
                            level.canonical_name,
                            parent_id,
                            signal.signal_type,
                            signal.value,
                            signal.normalized,
                            level.confidence,
                            now,
                            now,
                        ),
                    )
                seen = 1
            else:
                seen = int(row["seen_count"]) + 1
                with self.conn:
                    self.conn.execute(
                        "UPDATE scope_candidate SET seen_count = ?, "
                        "confidence = MAX(confidence, ?), parent_id = ?, last_seen = ? "
                        "WHERE id = ?",
                        (seen, level.confidence, parent_id, now, int(row["id"])),
                    )
            out.append(
                ScopeCandidate(
                    scope_type=level.scope_type,
                    canonical_name=level.canonical_name,
                    parent_id=parent_id,
                    signal_type=signal.signal_type,
                    normalized_value=signal.normalized,
                    confidence=level.confidence,
                    seen_count=seen,
                )
            )
        return out

    def _promote_matured(
        self, chain: Sequence[ChainLevel], ctx: ScopeContext
    ) -> Tuple[Optional[int], Optional[ChainLevel]]:
        """Promote the first chain level whose candidate has been seen enough.

        Returns:
            ``(scope_id, level)`` for the promotion, or ``(None, None)``. The
            most specific matured level wins, because the walk in
            :meth:`resolve` already prefers the deepest match and promoting a
            parent first would file the child under a scope that a later
            promotion then has to move.
        """
        if not ctx.user_id:
            return None, None
        threshold = int(self.config.scope_promote_after)
        for level in reversed(chain):
            signal = level.signals[0]
            row = self.conn.execute(
                "SELECT seen_count FROM scope_candidate WHERE user_id = ? "
                "AND scope_type = ? AND canonical_name = ? AND signal_type = ? "
                "AND normalized_value = ? AND status = 'pending'",
                (
                    ctx.user_id,
                    level.scope_type,
                    level.canonical_name,
                    signal.signal_type,
                    signal.normalized,
                ),
            ).fetchone()
            if row is None or int(row["seen_count"]) < threshold:
                continue
            scope_id = self._promote_candidate(
                level, ctx.user_id, self._nearest_known(chain)
            )
            if scope_id is not None:
                return scope_id, level
        return None, None

    def _promote_candidate(
        self, level: ChainLevel, user_id: str, parent_id: int
    ) -> Optional[int]:
        """Create a scope from a candidate that has been seen often enough."""
        try:
            scope_id = self.create(
                scope_type=level.scope_type,
                name=level.canonical_name,
                parent_id=parent_id,
                signals=level.signals,
                confidence=level.confidence,
                display_name=level.display_name,
            )
        except ValueError as exc:
            # A signal that already identifies another scope means the queue entry
            # is stale (the scope was created or merged since). Point the
            # candidate at the scope that owns the identity instead of leaving it
            # pending forever.
            owner = None
            for signal in level.signals:
                owner = self.find_by_signal(signal.signal_type, signal.value)
                if owner is not None:
                    break
            if owner is None:
                logger.warning(
                    "Failed to promote scope candidate %s: %s",
                    level.canonical_name, exc,
                )
                return None
            scope_id = owner
        except sqlite3.IntegrityError:
            logger.exception(
                "Failed to promote scope candidate %s", level.canonical_name
            )
            return None
        with self.conn:
            self.conn.execute(
                "UPDATE scope_candidate SET status = 'promoted' "
                "WHERE user_id = ? AND scope_type = ? AND canonical_name = ?",
                (user_id, level.scope_type, level.canonical_name),
            )
        record_event(
            self.conn,
            "scope_promoted",
            {
                "scope_id": scope_id,
                "name": level.canonical_name,
                "scope_type": level.scope_type,
            },
            user_id=user_id,
        )
        logger.info(
            "Promoted scope candidate %s (%s) after repeated sightings",
            level.canonical_name, level.scope_type,
        )
        return scope_id

    def _nearest_known(self, chain: Sequence[ChainLevel]) -> int:
        """Return the deepest scope on the chain that already exists."""
        for level in reversed(chain):
            for signal in level.signals:
                row = self.conn.execute(
                    "SELECT scope_id FROM scope_signal "
                    "WHERE signal_type = ? AND normalized_value = ?",
                    (signal.signal_type, signal.normalized),
                ).fetchone()
                if row is not None:
                    return self.resolve_id(int(row["scope_id"]))
        return GLOBAL_SCOPE_ID

    # -- fact bindings ------------------------------------------------------

    def bind_fact(
        self, fact_id: str, scope_ids: Iterable[int], priority: int = 0
    ) -> List[int]:
        """Bind a fact to one or more scopes.

        Args:
            fact_id: The fact to bind.
            scope_ids: Scopes it belongs to. Unknown ids resolve to global;
                duplicates collapse.
            priority: Tie-break within :mod:`~atom_memory.retriever` when a fact
                carries several bindings.

        Returns:
            The scope ids actually bound, in the order supplied.
        """
        bound: List[int] = []
        for raw in scope_ids:
            scope_id = self.resolve_id(int(raw))
            if scope_id in bound:
                continue
            bound.append(scope_id)
            with self.conn:
                self.conn.execute(
                    "INSERT INTO fact_scope(fact_id, scope_id, priority) "
                    "VALUES (?, ?, ?) ON CONFLICT(fact_id, scope_id) "
                    "DO UPDATE SET priority = MAX(priority, excluded.priority)",
                    (fact_id, scope_id, priority),
                )
        return bound

    def promote_fact(self, fact_id: str, to_scope_id: int) -> dict:
        """Make ``to_scope_id`` a fact's primary binding, keeping the others.

        The correction that resolution deliberately refuses to make on its own.
        Recall's candidate set is a scope's path plus its *ancestors*, so a fact
        filed too deep — a teaching preference captured while writing one
        chapter's file, and therefore bound to that document — is unreachable
        from anywhere else. Nothing in the text says "this holds for every
        chapter"; only the user knows, so this is an explicit act.

        The general rule is enforced rather than assumed: the target must be
        *more general* than the current primary (a fact may be lifted out of a
        document to its project, never pushed down into one). Pushing down is
        what resolution already does with evidence, and doing it by hand would
        hide a fact from every context that used to see it.

        ``fact_scope.priority`` is the tie-break the retriever uses, so the new
        binding takes ``priority = 0`` and the previous primary is demoted to
        ``1`` rather than deleted: it stays as context, and the move is a
        re-prioritisation that a later promotion can undo.

        Args:
            fact_id: The fact to lift.
            to_scope_id: The scope it should primarily belong to.

        Returns:
            ``{"fact_id", "from_scope_id", "to_scope_id", "action"}`` where
            action is ``moved`` or ``already-primary``. ``from_scope_id`` is
            ``None`` when the fact had no primary binding to begin with.

        Raises:
            ValueError: For an unknown target scope, or a target that is not more
                general than the fact's current primary scope.
        """
        target = int(to_scope_id)
        if not self.exists(target):
            raise ValueError(f"unknown scope: {target}")
        target = self.resolve_id(target)
        row = self.conn.execute(
            "SELECT scope_id FROM fact_scope WHERE fact_id = ? "
            "ORDER BY priority ASC, scope_id ASC LIMIT 1",
            (fact_id,),
        ).fetchone()
        current = self.resolve_id(int(row["scope_id"])) if row is not None else None
        if current == target:
            return {
                "fact_id": fact_id,
                "from_scope_id": current,
                "to_scope_id": target,
                "action": "already-primary",
            }
        if current is not None:
            current_type = self.scope_type(current)
            target_type = self.scope_type(target)
            if SCOPE_DEPTH.get(target_type, 99) >= SCOPE_DEPTH.get(current_type, 99):
                raise ValueError(
                    f"scope {target} ({target_type}) is not more general than the "
                    f"fact's current scope {current} ({current_type}); promoting "
                    f"must lift a fact, not bury it deeper"
                )
        with self.conn:
            if current is not None:
                self.conn.execute(
                    "UPDATE fact_scope SET priority = 1 WHERE fact_id = ? "
                    "AND scope_id = ?",
                    (fact_id, current),
                )
            self.conn.execute(
                "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES (?, ?, 0) "
                "ON CONFLICT(fact_id, scope_id) DO UPDATE SET priority = 0",
                (fact_id, target),
            )
        record_event(
            self.conn,
            "fact_scope_promoted",
            {
                "fact_id": fact_id,
                "from_scope_id": current,
                "from_path": self.path_of(current) if current is not None else None,
                "to_scope_id": target,
                "to_path": self.path_of(target),
            },
        )
        return {
            "fact_id": fact_id,
            "from_scope_id": current,
            "to_scope_id": target,
            "action": "moved",
        }

    def set_conditions(
        self, fact_id: str, conditions: Iterable
    ) -> Tuple[Tuple[str, str], ...]:
        """Replace a fact's conditions.

        Args:
            fact_id: The fact to update.
            conditions: Mappings or pairs; unusable entries are dropped.

        Returns:
            The normalised conditions actually stored.
        """
        pairs = normalize_conditions(conditions)
        with self.conn:
            self.conn.execute("DELETE FROM fact_condition WHERE fact_id = ?", (fact_id,))
            for key, value in pairs:
                self.conn.execute(
                    "INSERT INTO fact_condition(fact_id, key, value) VALUES (?, ?, ?)",
                    (fact_id, key, value),
                )
        return pairs

    def fact_scopes(self, fact_ids: Sequence[str]) -> Dict[str, Tuple[int, ...]]:
        """Return ``{fact_id: (scope_id, ...)}`` for a batch of facts.

        Facts with no binding are present with an empty tuple: a caller has to be
        able to tell "global by compatibility" from "forgot to look".

        The lookup is chunked because the batch is not bounded by the caller: the
        digest asks for *every* active fact of a user, and a single ``IN`` clause
        with tens of thousands of placeholders would hit SQLite's bound-variable
        limit. The chunk size is well under it on every build.
        """
        out: Dict[str, Tuple[int, ...]] = {fact_id: () for fact_id in fact_ids}
        grouped: Dict[str, List[int]] = {}
        for chunk in _chunks(fact_ids, _SQL_CHUNK):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT fact_id, scope_id FROM fact_scope "
                f"WHERE fact_id IN ({placeholders}) "
                f"ORDER BY priority DESC, scope_id ASC",
                list(chunk),
            ).fetchall()
            for row in rows:
                grouped.setdefault(str(row["fact_id"]), []).append(
                    self.resolve_id(int(row["scope_id"]))
                )
        for fact_id, scope_ids in grouped.items():
            out[fact_id] = tuple(scope_ids)
        return out

    def fact_conditions(
        self, fact_ids: Sequence[str]
    ) -> Dict[str, Tuple[Tuple[str, str], ...]]:
        """Return ``{fact_id: ((key, value), ...)}`` for a batch of facts."""
        out: Dict[str, Tuple[Tuple[str, str], ...]] = {fid: () for fid in fact_ids}
        grouped: Dict[str, List[Tuple[str, str]]] = {}
        for chunk in _chunks(fact_ids, _SQL_CHUNK):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT fact_id, key, value FROM fact_condition "
                f"WHERE fact_id IN ({placeholders}) ORDER BY key, value",
                list(chunk),
            ).fetchall()
            for row in rows:
                grouped.setdefault(str(row["fact_id"]), []).append(
                    (str(row["key"]), str(row["value"]))
                )
        for fact_id, pairs in grouped.items():
            out[fact_id] = tuple(pairs)
        return out

    # -- provenance and evolution -------------------------------------------

    def link_origin(
        self, derived_fact_id: str, source_fact_id: str, relation: str
    ) -> None:
        """Record that ``derived_fact_id`` was derived from ``source_fact_id``.

        Best-effort: a provenance link is an explanation, never a precondition
        for the fact itself, so a duplicate insert is a no-op rather than an error
        the write path has to handle.
        """
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO fact_origin(derived_fact_id, source_fact_id, "
                    "relation, created_at) VALUES (?, ?, ?, ?)",
                    (derived_fact_id, source_fact_id, relation, now_ms()),
                )
        except sqlite3.Error:  # pragma: no cover - defensive
            logger.exception(
                "Failed to link origin %s <- %s", derived_fact_id, source_fact_id
            )

    def record_evolution(
        self,
        from_fact_id: str,
        to_fact_id: str,
        relation: str,
        scope_id: Optional[int] = None,
        reason: str = "",
    ) -> None:
        """Record how one fact evolved into another (best-effort, idempotent)."""
        if from_fact_id == to_fact_id:
            return
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO fact_evolution(from_fact_id, to_fact_id, "
                    "relation, scope_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (from_fact_id, to_fact_id, relation, scope_id, reason, now_ms()),
                )
        except sqlite3.Error:  # pragma: no cover - defensive
            logger.exception(
                "Failed to record evolution %s -> %s", from_fact_id, to_fact_id
            )

    def evolution_of(self, fact_id: str) -> List[dict]:
        """Return the evolution edges touching a fact, in either direction."""
        rows = self.conn.execute(
            "SELECT from_fact_id, to_fact_id, relation, scope_id, reason, created_at "
            "FROM fact_evolution WHERE from_fact_id = ? OR to_fact_id = ? "
            "ORDER BY created_at",
            (fact_id, fact_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def origins_of(self, fact_id: str) -> List[dict]:
        """Return the provenance edges of a fact, in both directions."""
        rows = self.conn.execute(
            "SELECT derived_fact_id, source_fact_id, relation, created_at "
            "FROM fact_origin WHERE derived_fact_id = ? OR source_fact_id = ? "
            "ORDER BY created_at",
            (fact_id, fact_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def relate_cross_scope(
        self,
        new_fact_id: str,
        new_scope_id: int,
        other_fact_id: str,
        other_scope_id: int,
        same_object: bool,
        reason: str = "",
    ) -> str:
        """Link two facts that describe the same key in *different* scopes.

        The design's rule for a cross-scope collision is "do not judge a
        contradiction": a project that overrides a global rule is not evidence
        that the global rule is wrong, and silently retiring one of the two is how
        a store loses either the rule or its exception. So the two statements are
        kept and a relation is recorded instead:

        * same object → ``cross_scope_similar`` in ``fact_origin``: an
          independent restatement, which is also what
          :meth:`abstraction_candidates` looks for.
        * different object, one scope an ancestor of the other →
          ``exception``: the specific scope refines the general one.
        * different object, otherwise → ``evolves_to``: two scopes at the same
          level disagree, which is a change of practice rather than an exception.

        Args:
            new_fact_id: The fact just written.
            new_scope_id: Its scope.
            other_fact_id: The existing fact under the same key.
            other_scope_id: The existing fact's scope.
            same_object: Whether both hold the same object.
            reason: Human-readable note stored on the evolution edge.

        Returns:
            The relation recorded.
        """
        new_id = self.resolve_id(new_scope_id)
        other_id = self.resolve_id(other_scope_id)
        if same_object:
            self.link_origin(new_fact_id, other_fact_id, RELATION_CROSS_SCOPE_SIMILAR)
            return RELATION_CROSS_SCOPE_SIMILAR

        new_ancestors = set(ancestor_distances(self.conn, new_id))
        other_ancestors = set(ancestor_distances(self.conn, other_id))
        if other_id in new_ancestors:
            # The new fact is more specific than the stored one.
            self.record_evolution(
                other_fact_id, new_fact_id, RELATION_EXCEPTION, new_id,
                reason or "specific scope refines the general rule",
            )
            return RELATION_EXCEPTION
        if new_id in other_ancestors:
            self.record_evolution(
                new_fact_id, other_fact_id, RELATION_EXCEPTION, other_id,
                reason or "stored fact is an exception to the new general rule",
            )
            return RELATION_EXCEPTION
        self.record_evolution(
            other_fact_id, new_fact_id, RELATION_EVOLVES_TO, new_id,
            reason or "two scopes disagree under the same key",
        )
        return RELATION_EVOLVES_TO

    # -- management ---------------------------------------------------------

    def add_alias(
        self, scope_id: int, alias: str, alias_type: str, confidence: float = 0.5
    ) -> bool:
        """Register an alternative name for a scope.

        Args:
            scope_id: The scope the alias belongs to.
            alias: The alias text.
            alias_type: Its kind (``name`` / ``path`` / ``remote`` / ``doc_id`` /
                ``folder_id`` / ``email_thread`` / ``project_code``, or any signal
                type the context module knows).
            confidence: How reliable the equivalence is.

        Returns:
            ``True`` when the alias is (now) registered to this scope, ``False``
            when it already belongs to a different one — an alias is a global
            identity and cannot point at two scopes at once.
        """
        normalized = normalize_signal(alias_type, alias) or normalize_signal("name", alias)
        if not normalized:
            return False
        scope_id = self.resolve_id(scope_id)
        existing = self.conn.execute(
            "SELECT scope_id FROM scope_alias WHERE alias_type = ? AND alias = ?",
            (alias_type, normalized),
        ).fetchone()
        now = now_ms()
        if existing is not None:
            if int(existing["scope_id"]) != scope_id:
                return False
            with self.conn:
                self.conn.execute(
                    "UPDATE scope_alias SET last_seen = ?, "
                    "confidence = MAX(confidence, ?) "
                    "WHERE alias_type = ? AND alias = ?",
                    (now, confidence, alias_type, normalized),
                )
            self._label_cache.pop(scope_id, None)
            return True
        with self.conn:
            self.conn.execute(
                "INSERT INTO scope_alias(scope_id, alias, alias_type, confidence, "
                "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (scope_id, normalized, alias_type, confidence, now, now),
            )
        self._label_cache.pop(scope_id, None)
        return True

    def confirm(self, scope_id: int, confidence: float = 1.0) -> bool:
        """Mark a scope as user-confirmed, raising its standing.

        Confirmation is what turns a promoted-but-uncertain scope (a folder path
        seen three times) into one that resolves without further doubt.

        Returns:
            ``True`` when a scope was updated.
        """
        resolved = self.resolve_id(scope_id)
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE scope SET confidence = MAX(confidence, ?), status = 'active' "
                "WHERE id = ?",
                (float(confidence), resolved),
            )
        self._label_cache.pop(resolved, None)
        return cursor.rowcount > 0

    def archive(self, scope_id: int) -> bool:
        """Archive a scope so it stops being matched, keeping its history."""
        resolved = self.resolve_id(scope_id)
        if resolved == GLOBAL_SCOPE_ID:
            return False
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE scope SET status = 'archived' WHERE id = ?", (resolved,)
            )
        self._label_cache.pop(resolved, None)
        return cursor.rowcount > 0

    def merge(self, from_id: int, to_id: int) -> dict:
        """Merge two scopes that turned out to be the same thing.

        The source scope is kept (``status='merged'``, ``merged_into`` pointing at
        the target) rather than deleted: its facts, its aliases and the audit
        trail reference it, and :meth:`resolve_id` follows the pointer, so history
        stays readable and every read path keeps working.

        Args:
            from_id: The scope to fold away.
            to_id: The scope that survives.

        Returns:
            ``{"from", "to", "facts_moved", "aliases_moved", "signals_moved",
            "children_moved"}``.

        Raises:
            ValueError: When either id is unknown, they are the same, or either is
                the root.
        """
        source = self.resolve_id(int(from_id))
        target = self.resolve_id(int(to_id))
        if source == target:
            raise ValueError("cannot merge a scope into itself")
        if source == GLOBAL_SCOPE_ID or target == GLOBAL_SCOPE_ID:
            raise ValueError("the global scope cannot be merged")

        with self.conn:
            rows = self.conn.execute(
                "SELECT fact_id, priority FROM fact_scope WHERE scope_id = ?",
                (source,),
            ).fetchall()
            facts_moved = 0
            for row in rows:
                self.conn.execute(
                    "INSERT INTO fact_scope(fact_id, scope_id, priority) "
                    "VALUES (?, ?, ?) ON CONFLICT(fact_id, scope_id) "
                    "DO UPDATE SET priority = MAX(priority, excluded.priority)",
                    (row["fact_id"], target, row["priority"]),
                )
                facts_moved += 1
            self.conn.execute("DELETE FROM fact_scope WHERE scope_id = ?", (source,))

            aliases = self.conn.execute(
                "SELECT alias, alias_type, confidence, first_seen, last_seen "
                "FROM scope_alias WHERE scope_id = ?",
                (source,),
            ).fetchall()
            aliases_moved = 0
            for row in aliases:
                taken = self.conn.execute(
                    "SELECT scope_id FROM scope_alias WHERE alias_type = ? "
                    "AND alias = ? AND scope_id != ?",
                    (row["alias_type"], row["alias"], source),
                ).fetchone()
                if taken is None:
                    self.conn.execute(
                        "INSERT INTO scope_alias(scope_id, alias, alias_type, "
                        "confidence, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(alias_type, alias) DO NOTHING",
                        (target, row["alias"], row["alias_type"], row["confidence"],
                         row["first_seen"], row["last_seen"]),
                    )
                    aliases_moved += 1
            self.conn.execute("DELETE FROM scope_alias WHERE scope_id = ?", (source,))

            signals = self.conn.execute(
                "SELECT signal_type, signal_value, normalized_value, confidence, "
                "first_seen, last_seen FROM scope_signal WHERE scope_id = ?",
                (source,),
            ).fetchall()
            signals_moved = 0
            for row in signals:
                taken = self.conn.execute(
                    "SELECT scope_id FROM scope_signal WHERE signal_type = ? "
                    "AND normalized_value = ? AND scope_id != ?",
                    (row["signal_type"], row["normalized_value"], source),
                ).fetchone()
                if taken is None:
                    self.conn.execute(
                        "INSERT INTO scope_signal(scope_id, signal_type, signal_value, "
                        "normalized_value, confidence, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(signal_type, normalized_value) DO NOTHING",
                        (target, row["signal_type"], row["signal_value"],
                         row["normalized_value"], row["confidence"],
                         row["first_seen"], row["last_seen"]),
                    )
                    signals_moved += 1
            self.conn.execute("DELETE FROM scope_signal WHERE scope_id = ?", (source,))

            child_rows = self.conn.execute(
                "SELECT id FROM scope WHERE parent_id = ?", (source,)
            ).fetchall()
            children_moved = 0
            for row in child_rows:
                child = int(row["id"])
                if child == target:
                    continue
                self.conn.execute(
                    "UPDATE scope SET parent_id = ? WHERE id = ?", (target, child)
                )
                children_moved += 1

            self.conn.execute(
                "UPDATE scope SET status = 'merged', merged_into = ? WHERE id = ?",
                (target, source),
            )

        for row in child_rows:
            self._recompute_paths(int(row["id"]))
        self._label_cache.pop(source, None)
        self._label_cache.pop(target, None)
        record_event(
            self.conn,
            "scope_merged",
            {"from": source, "to": target, "facts_moved": facts_moved},
        )
        return {
            "from": source,
            "to": target,
            "facts_moved": facts_moved,
            "aliases_moved": aliases_moved,
            "signals_moved": signals_moved,
            "children_moved": children_moved,
        }

    def split(
        self,
        from_id: int,
        name: str,
        scope_type: str,
        fact_ids: Iterable[str],
    ) -> int:
        """Split facts out of a scope into a new child scope.

        Args:
            from_id: The scope to split from.
            name: The new scope's name.
            scope_type: The new scope's type (must be more specific).
            fact_ids: Facts to move into the new scope.

        Returns:
            The new scope id.
        """
        source = self.resolve_id(int(from_id))
        targets = [str(fid) for fid in (fact_ids or [])]
        new_id = self.create(scope_type=scope_type, name=name, parent_id=source)
        for fact_id in targets:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM fact_scope WHERE fact_id = ? AND scope_id = ?",
                    (fact_id, source),
                )
            self.bind_fact(fact_id, [new_id])
        record_event(
            self.conn,
            "scope_split",
            {"from": source, "to": new_id, "facts": len(targets)},
        )
        return new_id

    def reparent(self, scope_id: int, parent_id: int) -> ScopeRow:
        """Move a scope under a different parent.

        The design's scope management is merge and split, and neither can express
        the correction that actually comes up: a project discovered before its
        client was ever named sits under ``/global``, and the client that is
        declared later cannot be spliced in automatically — resolution must not
        restructure the tree, because that would silently move every fact already
        bound to it. This is that correction, as an explicit operator action.

        Facts are untouched: only the ancestry changes, and recall's ancestor walk
        picks the new path up immediately. The subtree's materialised paths are
        rewritten.

        A cycle cannot arise: :data:`~atom_memory.context.SCOPE_DEPTH` is a strict
        order, the new parent must be strictly more general, and a scope's
        descendants are by construction strictly more specific than it — so no
        descendant can ever satisfy the parent test.

        Args:
            scope_id: The scope to move.
            parent_id: Its new parent.

        Returns:
            The moved scope's row.

        Raises:
            ValueError: When either id is unknown, the scope is the root, or the
                parent is not strictly more general.
        """
        scope = self.get(int(scope_id))
        if scope is None:
            raise ValueError(f"unknown scope: {scope_id}")
        if scope.id == GLOBAL_SCOPE_ID:
            raise ValueError("the global scope cannot be reparented")
        parent = self.get(int(parent_id))
        if parent is None:
            raise ValueError(f"unknown parent scope: {parent_id}")
        if parent.id == scope.id:
            raise ValueError("cannot reparent a scope under itself")
        if SCOPE_DEPTH[parent.scope_type] >= SCOPE_DEPTH[scope.scope_type]:
            raise ValueError(
                f"parent scope {parent.scope_type} is not more general than "
                f"{scope.scope_type}"
            )

        with self.conn:
            self.conn.execute(
                "UPDATE scope SET parent_id = ? WHERE id = ?", (parent.id, scope.id)
            )
        self._recompute_paths(scope.id)
        self._label_cache.pop(scope.id, None)
        record_event(
            self.conn,
            "scope_reparented",
            {"scope_id": scope.id, "parent_id": parent.id},
        )
        moved = self.get(scope.id)
        assert moved is not None  # just written
        return moved

    def _recompute_paths(self, scope_id: int) -> None:
        """Re-derive ``path`` for a subtree after a re-parent."""
        current = int(scope_id)
        row = self.conn.execute(
            "SELECT canonical_name, scope_type FROM scope WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            return
        path = (
            f"{self.path_of(self.parent_of(current))}"
            f"/{row['scope_type']}:{row['canonical_name']}"
        )
        with self.conn:
            self.conn.execute("UPDATE scope SET path = ? WHERE id = ?", (path, current))
        for child in [
            int(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM scope WHERE parent_id = ?", (current,)
            ).fetchall()
        ]:
            self._recompute_paths(child)

    # -- cross-scope promotion ----------------------------------------------

    def abstraction_candidates(
        self, user_id: str, min_scopes: Optional[int] = None
    ) -> List[dict]:
        """Find claims that ≥ N distinct scopes hold independently.

        Args:
            user_id: Owner to inspect.
            min_scopes: Threshold; defaults to
                :attr:`~atom_memory.config.MemConfig.scope_abstraction_min_scopes`.

        Returns:
            One entry per promotable claim: ``{"fingerprint", "fact_ids",
            "scope_ids", "representative"}``, oldest fact first. A claim already
            present at the root is excluded — it has been abstracted already.
        """
        threshold = int(
            min_scopes
            if min_scopes is not None
            else self.config.scope_abstraction_min_scopes
        )
        rows = self.conn.execute(
            "SELECT f.fact_id, f.content_fingerprint, fs.scope_id "
            "FROM facts f JOIN fact_scope fs ON fs.fact_id = f.fact_id "
            "WHERE f.user_id = ? AND f.status = 'active' "
            "AND f.content_fingerprint IS NOT NULL AND fs.scope_id != ? "
            "ORDER BY f.created_at ASC",
            (user_id, GLOBAL_SCOPE_ID),
        ).fetchall()
        grouped: Dict[str, Dict] = {}
        for row in rows:
            entry = grouped.setdefault(
                str(row["content_fingerprint"]),
                {
                    "fact_ids": [],
                    "scope_ids": set(),
                    "representative": str(row["fact_id"]),
                },
            )
            entry["fact_ids"].append(str(row["fact_id"]))
            entry["scope_ids"].add(self.resolve_id(int(row["scope_id"])))

        out: List[dict] = []
        for fingerprint, entry in grouped.items():
            scope_ids = sorted(entry["scope_ids"])
            if len(scope_ids) < threshold:
                continue
            if self._has_root_fact(user_id, fingerprint):
                continue
            out.append(
                {
                    "fingerprint": fingerprint,
                    "fact_ids": entry["fact_ids"],
                    "scope_ids": scope_ids,
                    "representative": entry["representative"],
                }
            )
        return out

    def _has_root_fact(self, user_id: str, fingerprint: str) -> bool:
        """Whether the root already holds an active fact with this fingerprint."""
        row = self.conn.execute(
            "SELECT f.fact_id FROM facts f "
            "LEFT JOIN fact_scope fs ON fs.fact_id = f.fact_id "
            "WHERE f.user_id = ? AND f.status = 'active' "
            "AND f.content_fingerprint = ? "
            "AND (fs.scope_id IS NULL OR fs.scope_id = ?) LIMIT 1",
            (user_id, fingerprint, GLOBAL_SCOPE_ID),
        ).fetchone()
        return row is not None

    # -- scoring view -------------------------------------------------------

    def view(
        self,
        resolution: Optional[ScopeResolution],
        conditions: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> "ScopeView":
        """Build the scoring view for one query."""
        return ScopeView(self, resolution, conditions or {})


@dataclass
class ScopeView:
    """Scores facts' scope bindings and conditions against one query context.

    Built once per query: the ancestor walk, the phase set and the sibling set are
    the same for every candidate, so recomputing them per fact would turn a
    constant into an N-query cost.
    """

    store: ScopeStore
    resolution: Optional[ScopeResolution]
    conditions: Mapping[str, Sequence[str]] = field(default_factory=dict)
    _upward: Dict[int, float] = field(default_factory=dict, init=False, repr=False)
    _extra: Dict[int, float] = field(default_factory=dict, init=False, repr=False)
    _phases: Dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _types: Dict[int, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        """Precompute the neighbourhood of the query's scope."""
        if self.resolution is None:
            return
        conn = self.store.conn
        scope_id = self.store.resolve_id(self.resolution.scope_id)
        distances = ancestor_distances(conn, scope_id)
        self._upward[scope_id] = W_EXACT
        for ancestor_id in distances:
            self._upward[ancestor_id] = distance_weight(scope_id, ancestor_id, distances)
        self._upward[GLOBAL_SCOPE_ID] = W_GLOBAL

        upward, phases = expanded_scope_ids(
            conn, scope_id, bool(self.store.config.scope_all_phases)
        )
        for phase_id in phases:
            self._extra[phase_id] = W_PHASE
            self._phases[phase_id] = W_PHASE
        for sibling in self.store.children(self.store.parent_of(scope_id)):
            if sibling != scope_id:
                self._extra.setdefault(sibling, W_SIBLING)

        ids = set(self._upward) | set(self._extra) | {scope_id}
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id, scope_type FROM scope WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
        for row in rows:
            self._types[int(row["id"])] = str(row["scope_type"])

    @property
    def current_id(self) -> Optional[int]:
        """The query's scope id, or ``None`` for a context-free query."""
        if self.resolution is None:
            return None
        return self.store.resolve_id(self.resolution.scope_id)

    def visible_ids(self) -> List[int]:
        """Return the scope ids whose facts are candidates for this query.

        Deliberately *not* the same set as :meth:`weight_for` knows about: the
        candidate set is the scope's own path plus its phases, while the scoring
        knows about siblings too. A sibling project's fact therefore only reaches
        recall through an explicit condition match (where ``W_OTHER``/``W_SIBLING``
        then ranks it low) rather than being pulled in wholesale — which is the
        difference between "a related project's rule" and cross-project pollution.
        """
        ids = set(self._upward)
        ids.update(self._phases)
        return sorted(ids)

    def weight_for(self, fact_scope_ids: Sequence[int]) -> float:
        """Return the best scope-distance weight of a fact's bindings.

        An *unbound* fact (empty sequence) is a pre-scope fact and counts as
        global. The maximum over bindings is used, because a fact bound to both a
        project and the root should be judged by its closest relation rather than
        penalised for also being general.
        """
        if self.resolution is None:
            return W_GLOBAL
        if not fact_scope_ids:
            return W_GLOBAL
        best = W_OTHER
        for scope_id in fact_scope_ids:
            resolved = self.store.resolve_id(int(scope_id))
            if resolved in self._upward:
                best = max(best, self._upward[resolved])
            elif resolved in self._extra:
                best = max(best, self._extra[resolved])
        return best

    def condition_weight(self, fact_conditions: Sequence[Tuple[str, str]]) -> float:
        """Return the condition-match weight of a fact."""
        return condition_match(fact_conditions, self.conditions)

    def phase_weight(self, fact_scope_ids: Sequence[int]) -> float:
        """Return the phase-match weight of a fact's bindings."""
        return phase_match(
            fact_scope_ids, self.current_id, self._types, self._phases
        )


def resolution_for(
    conn: sqlite3.Connection,
    config: MemConfig,
    payload: Optional[Mapping],
    user_id: str,
    session_id: str = "",
    create: bool = True,
) -> Tuple[Optional[ScopeContext], ScopeResolution]:
    """Parse a context payload and resolve it in one call.

    The single entry point both the write path and the read path use, so a payload
    is interpreted identically on both sides — the failure mode of two parsing
    call sites is a fact written under one scope and read under another.

    Args:
        conn: Open SQLite connection.
        config: Configuration carrying the resolution thresholds and the master
            switch.
        payload: The context payload (see
            :func:`~atom_memory.context.context_from_payload`).
        user_id: Owner of the context.
        session_id: Session the context came from.
        create: Whether resolution may create scopes (``False`` for a query).

    Returns:
        ``(context, resolution)``. The context is ``None`` when the payload
        carried nothing usable, in which case the resolution is a global one.

    ``MemConfig.scope_aware`` is honoured here, so the master switch is enforced
    at the single entry point the automatic paths share rather than at each of
    them: with awareness off, nothing this function returns can create a scope.
    The *management* methods (:meth:`ScopeStore.create`, ``merge``, ``split``,
    ``reparent``, ...) deliberately do not consult it — repairing a scope tree
    has to be possible whether or not automation is switched on.
    """
    from .context import context_from_payload

    ctx = context_from_payload(payload, user_id=user_id, session_id=session_id)
    if not config.scope_aware:
        return ctx, ScopeResolution(
            scope_id=GLOBAL_SCOPE_ID,
            confidence=1.0,
            status=STATUS_GLOBAL,
            conditions=ctx.conditions if ctx is not None else (),
            detail="scope awareness disabled",
        )
    store = ScopeStore(conn, config)
    return ctx, store.resolve(ctx, create=create)
