"""The topic dimension: what a memory is *about*, next to where it came from.

Scope answers "in which context" and is inferred from the environment. It cannot
answer "about what topic", and one axis over it produces the same failure the
scope dimension exists to remove:

* a durable preference of the user ("likes coffee") gets bound to whatever
  project it happened to be said in, so it is invisible everywhere else;
* a life or travel fact bound to a project-scope the user is working in *is*
  visible there, and competes with the work that belongs there;
* nothing records that a claim is about teaching rather than about programming,
  so the two cannot be separated inside one context.

So this module adds a second, orthogonal dimension. Five decisions shape it, and
each of them is only correct given the failure it prevents:

* **Multi-label, unlike scope.** A fact has exactly one primary scope but may be
  about several topics at once ("teaching" *and* "programming" for a lecture on
  decorators). The primary label is the one ranking and conflict judgement read;
  the rest widen recall.
* **Registered, never free-form.** The extractor may *propose* a name; only a
  name that already exists (or whose ancestor does) is stored. A proposal with no
  ancestor is counted in ``domain_signal`` for a human to promote. A tag
  vocabulary that grows on every write stops being a vocabulary: it becomes one
  near-duplicate label per phrasing, and the filter it was built to power
  degenerates into "match everything" or "match nothing".
* **The label says where it came from.** Every row records the rule that chose it
  (see :data:`SOURCES`). "Semi-automatic tagging" that cannot be explained cannot
  be corrected, and a correction is the only thing that makes a wrong label
  harmless.
* **``general`` is a fallback, not a default.** The default is derived from the
  scope the session is in (:meth:`DomainStore.session_domains`); ``general`` is
  only what remains when even that says nothing. A dimension whose default bucket
  is the one every filter must let through isolates nothing.
* **Registered once, reused by path.** ``parent_id`` is the tree and ``path`` is
  a label, exactly as in :mod:`~atom_memory.scope`, because a canonical name may
  contain separators (``teaching/ds/ch3``) and parsing one back into a tree is
  ambiguous the first time it does.

Nothing here is a filter yet: this module owns the vocabulary, the labelling and
the management surface, and leaves recall untouched (``MemConfig.domain_recall``
defaults to ``"off"``).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import MemConfig
from .context import (
    SCOPE_CLIENT,
    SCOPE_DOCUMENT,
    SCOPE_ORG,
    SCOPE_PHASE,
    SCOPE_PROJECT,
    SCOPE_SERIES,
    SCOPE_TEAM,
    SCOPE_THREAD,
    SCOPE_USER,
    normalize_name,
)
from .db import now_ms, record_event

logger = logging.getLogger(__name__)

#: The canonical name every user starts with, and the last fallback before a
#: write is refused. Deliberately *not* the default: see the module docstring.
GENERAL_DOMAIN = "general"

#: Where a label came from, most trustworthy first. The order is the ranking used
#: when two rules propose different labels and is written to ``fact_domain.source``
#: so a receipt can explain itself.
SOURCE_USER = "user_explicit"
SOURCE_HINT = "hint"
SOURCE_SCOPED_MAP = "scoped_map"
SOURCE_KEYWORD = "keyword"
SOURCE_SESSION = "session_default"
SOURCE_SEED = "seed"

SOURCE_RANK: Tuple[str, ...] = (
    SOURCE_USER,
    SOURCE_HINT,
    SOURCE_SCOPED_MAP,
    SOURCE_KEYWORD,
    SOURCE_SESSION,
    SOURCE_SEED,
)
SOURCES = frozenset(SOURCE_RANK)


def source_rank(source: str) -> int:
    """Return how trustworthy a labelling source is (lower is better).

    Args:
        source: A value of :data:`SOURCES` (unknown values rank last).

    Returns:
        The index in :data:`SOURCE_RANK`, or ``len(SOURCE_RANK)`` when unknown.
    """
    try:
        return SOURCE_RANK.index(source)
    except ValueError:
        return len(SOURCE_RANK)


#: A canonical domain name: lower-case ASCII segments joined by ``/``.
#:
#: ASCII-only on purpose. The display name is a separate, free-form field, so
#: mixing scripts into the identity would buy nothing and cost a normalisation
#: step that has to agree across the host, the library and every migration.
_CANONICAL_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")

#: Maximum segments in a canonical name. The hierarchy exists to be *shallow*:
#: `teaching/ds/ch3` is useful, `teaching/ds/ch3/week2/slides/draft` is a folder
#: tree, and the deeper it gets the less any two facts share a label.
MAX_DOMAIN_DEPTH = 6

#: Bound on the ``IN`` clause in a batch lookup (same reasoning as ``scope``).
_SQL_CHUNK = 500


def normalize_canonical(name: Optional[str]) -> str:
    """Return the canonical form of a proposed domain name.

    Lower-cases, trims, collapses separators and drops any character that cannot
    appear in an identity. The result is what gets matched and stored; the raw
    proposal is only ever shown back to the user.

    Args:
        name: A raw proposal (``"Teaching/DS"``, ``" 教学 "``, ...).

    Returns:
        The canonical name, or ``""`` when nothing usable survives.
    """
    text = normalize_name(name or "")
    if not text:
        return ""
    parts = [part.strip() for part in text.replace("\\", "/").split("/")]
    kept = [part for part in parts if part]
    joined = "/".join(kept)
    return joined[:200]


def is_valid_canonical(name: str) -> bool:
    """Return whether ``name`` is a storable canonical domain name.

    Args:
        name: A canonical name (already normalised).

    Returns:
        ``True`` when every segment is lower-case ASCII and the depth is within
        :data:`MAX_DOMAIN_DEPTH`.
    """
    if not name or len(name) > 200:
        return False
    parts = name.split("/")
    if len(parts) > MAX_DOMAIN_DEPTH:
        return False
    return all(_CANONICAL_RE.match(part) for part in parts)


def ancestor_names(name: str) -> Tuple[str, ...]:
    """Return the ancestor chain of a canonical name, nearest first.

    Args:
        name: A canonical name (``teaching/ds/ch3``).

    Returns:
        The proper ancestors, excluding ``name`` itself (``teaching/ds``,
        ``teaching``).
    """
    parts = [part for part in (name or "").split("/") if part]
    return tuple("/".join(parts[:index]) for index in range(len(parts) - 1, 0, -1))


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    """Yield ``items`` in slices of at most ``size`` (empty input yields none)."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


# -- small read-only views ----------------------------------------------------


@dataclass(frozen=True)
class DomainRow:
    """One row of ``domain``."""

    id: int
    canonical_name: str
    display_name: str
    parent_id: Optional[int]
    path: str
    status: str = "active"
    system_seeded: bool = False

    def to_dict(self) -> dict:
        """Return the row as a plain dict for the RPC/UI surface."""
        return {
            "domain_id": self.id,
            "name": self.canonical_name,
            "display_name": self.display_name,
            "parent_id": self.parent_id,
            "path": self.path,
            "status": self.status,
            "system_seeded": self.system_seeded,
        }


@dataclass(frozen=True)
class DomainLabel:
    """One resolved label of a fact, with the rule that chose it."""

    domain_id: int
    name: str
    confidence: float
    is_primary: bool
    source: str

    def to_dict(self) -> dict:
        """Return the label as a plain dict."""
        return {
            "domain_id": self.domain_id,
            "name": self.name,
            "confidence": round(float(self.confidence), 4),
            "is_primary": bool(self.is_primary),
            "source": self.source,
        }


@dataclass(frozen=True)
class DomainAssignment:
    """What a write decided a fact is about.

    ``labels`` is never empty for a successful write: an empty assignment means
    the caller must refuse the write rather than store an unlabelled fact (see
    :meth:`DomainStore.assign`).
    """

    labels: Tuple[DomainLabel, ...] = ()
    signals: Tuple[str, ...] = ()
    detail: str = ""

    @property
    def primary(self) -> Optional[DomainLabel]:
        """The primary label, or ``None`` when nothing could be resolved."""
        for label in self.labels:
            if label.is_primary:
                return label
        return self.labels[0] if self.labels else None

    @property
    def names(self) -> Tuple[str, ...]:
        """The canonical names of every label, primary first."""
        return tuple(label.name for label in self.labels)

    @property
    def domain_ids(self) -> Tuple[int, ...]:
        """The domain ids of every label, primary first."""
        return tuple(label.domain_id for label in self.labels)

    def to_dict(self) -> dict:
        """Return the assignment as a plain dict for a write receipt."""
        primary = self.primary
        return {
            "domains": [label.to_dict() for label in self.labels],
            "primary": primary.name if primary is not None else None,
            "unregistered": list(self.signals),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SessionDomains:
    """The topics a session is assumed to be about, before the query says more.

    This is the *initial* set and a ranking bias, never a hard filter on its own:
    a query that turns out to be about something else must be able to say so.
    """

    domain_ids: Tuple[int, ...] = ()
    source: str = SOURCE_SESSION
    detail: str = ""
    #: Explicit labels the session was configured with, kept verbatim because
    #: they outrank everything the extractor proposes (they are the user's own
    #: statement about what the work is about).
    labels: Tuple[str, ...] = ()

    @property
    def primary_id(self) -> Optional[int]:
        """The most specific id in the set, or ``None``."""
        return self.domain_ids[0] if self.domain_ids else None

    def to_dict(self) -> dict:
        """Return the session set as a plain dict."""
        return {
            "domain_ids": list(self.domain_ids),
            "labels": list(self.labels),
            "source": self.source,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DomainSignal:
    """A proposed domain the store does not know yet."""

    canonical_name: str
    nearest_ancestor: Optional[int]
    seen_count: int = 1
    scope_id: Optional[int] = None

    def to_dict(self) -> dict:
        """Return the signal as a plain dict."""
        return {
            "name": self.canonical_name,
            "nearest_ancestor": self.nearest_ancestor,
            "seen_count": self.seen_count,
            "scope_id": self.scope_id,
        }


@dataclass
class DomainView:
    """Scores facts' domain labels against one query's topic set.

    Built once per query, like :class:`~atom_memory.scope.ScopeView`: the
    ancestor closure and the bridge set are the same for every candidate, so
    recomputing them per fact would turn a constant into an N-query cost.
    """

    store: "DomainStore"
    session: SessionDomains
    query_ids: Tuple[int, ...] = ()
    conditions: Mapping[str, Sequence[str]] = field(default_factory=dict)
    _weights: Dict[int, float] = field(default_factory=dict, init=False, repr=False)
    _bridges: Dict[int, float] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        """Precompute the weight of every id this query can reward."""
        ids: List[int] = list(self.query_ids) or list(self.session.domain_ids)
        if not ids:
            return
        # Nearest first: the first id is the query's own topic, and each further
        # one is an ancestor, so the weight decays down the chain.
        for index, domain_id in enumerate(ids):
            resolved = self.store.resolve_id(int(domain_id))
            weight = self.store.config.domain_primary_weight if index == 0 else (
                self.store.config.domain_secondary_weight
            )
            self._weights.setdefault(resolved, max(0.0, float(weight)))
            for ancestor in self.store.ancestors(resolved):
                self._weights.setdefault(
                    ancestor, float(self.store.config.domain_secondary_weight)
                )
        for domain_id, weight in self.store.bridges_from(ids).items():
            self._bridges.setdefault(domain_id, float(weight))

    def weight_for(self, fact_domain_ids: Sequence[int]) -> float:
        """Return the best topic-match weight of a fact's labels.

        An *unlabelled* fact (empty sequence) is a pre-domain fact: it scores 0
        rather than being penalised, because "we never recorded a topic" is not
        evidence of a wrong topic. The maximum over labels is used, mirroring the
        scope view: a fact about two topics is judged by its best one.
        """
        best = 0.0
        for domain_id in fact_domain_ids:
            resolved = self.store.resolve_id(int(domain_id))
            if resolved in self._weights:
                best = max(best, self._weights[resolved])
            if resolved in self._bridges:
                best = max(
                    best,
                    float(self.store.config.domain_secondary_weight)
                    * self._bridges[resolved],
                )
        return best


class DomainStore:
    """SQLite-backed topic vocabulary, labelling and the registry queue."""

    def __init__(self, conn: sqlite3.Connection, config: Optional[MemConfig] = None):
        """Initialise the store.

        Args:
            conn: An open connection.
            config: Configuration carrying the topic weights and limits. ``None``
                uses the library defaults.
        """
        self.conn = conn
        self.config = config or MemConfig()
        #: ``{(user_id, canonical_name): domain_id}`` for active domains. A
        #: labelling decision walks the ancestor chain of every proposal, so this
        #: map turns a per-candidate query into a per-candidate dict lookup —
        #: invalidated on every write that can change it.
        self._by_name: Dict[Tuple[str, str], int] = {}
        self._label_cache: Dict[int, str] = {}

    # -- reads ---------------------------------------------------------------

    def _cache_key(self, user_id: str, name: str) -> Tuple[str, str]:
        return (str(user_id or ""), str(name or ""))

    def find(self, user_id: str, canonical_name: str) -> Optional[int]:
        """Return the id of a registered domain, or ``None``.

        Args:
            user_id: Owner of the vocabulary.
            canonical_name: A canonical name (normalised by the caller).

        Returns:
            The domain id (merges resolved), or ``None``.
        """
        if not canonical_name:
            return None
        key = self._cache_key(user_id, canonical_name)
        if key in self._by_name:
            found = self._by_name[key]
            return self.resolve_id(found) if found else None
        row = self.conn.execute(
            "SELECT id FROM domain WHERE user_id = ? AND canonical_name = ?",
            (str(user_id or ""), canonical_name),
        ).fetchone()
        domain_id = int(row["id"]) if row is not None else 0
        self._by_name[key] = domain_id
        if not domain_id:
            return None
        resolved = self.resolve_id(domain_id)
        self._by_name[key] = resolved
        return resolved

    def get(self, domain_id: int) -> Optional[DomainRow]:
        """Return one domain row (merged ids resolved), or ``None``."""
        row = self.conn.execute(
            "SELECT id, canonical_name, display_name, parent_id, path, status, "
            "system_seeded FROM domain WHERE id = ?",
            (self.resolve_id(int(domain_id)),),
        ).fetchone()
        if row is None:
            return None
        return DomainRow(
            id=int(row["id"]),
            canonical_name=str(row["canonical_name"]),
            display_name=str(row["display_name"]),
            parent_id=int(row["parent_id"]) if row["parent_id"] is not None else None,
            path=str(row["path"]),
            status=str(row["status"]),
            system_seeded=bool(row["system_seeded"]),
        )

    def resolve_id(self, domain_id: int) -> int:
        """Follow ``merged_into`` to the domain that now owns this name.

        Args:
            domain_id: A possibly-merged id.

        Returns:
            The surviving id, or ``0`` when the id is unknown. ``0`` is not a
            valid domain (unlike ``GLOBAL_SCOPE_ID``, which is a real fallback):
            an unknown topic must not silently become a topic.
        """
        current = int(domain_id)
        for _ in range(MAX_DOMAIN_DEPTH + 2):
            row = self.conn.execute(
                "SELECT status, merged_into FROM domain WHERE id = ?", (current,)
            ).fetchone()
            if row is None:
                return 0
            if row["status"] != "merged" or row["merged_into"] is None:
                return current
            current = int(row["merged_into"])
        logger.warning("Domain %s has a cyclic merge chain; treating it as unknown", domain_id)
        return 0

    def exists(self, domain_id: int) -> bool:
        """Return whether ``domain_id`` names a registered domain."""
        return self.get(domain_id) is not None

    def name_of(self, domain_id: int) -> str:
        """Return a domain's canonical name (``""`` for an unknown id)."""
        resolved = self.resolve_id(int(domain_id))
        if not resolved:
            return ""
        if resolved in self._label_cache:
            return self._label_cache[resolved]
        row = self.conn.execute(
            "SELECT canonical_name FROM domain WHERE id = ?", (resolved,)
        ).fetchone()
        name = str(row["canonical_name"]) if row is not None else ""
        self._label_cache[resolved] = name
        return name

    def ancestors(self, domain_id: int) -> List[int]:
        """Return the ancestor ids of a domain, nearest first.

        Args:
            domain_id: Any registered id.

        Returns:
            The chain above it, excluding itself. The walk uses ``parent_id``
            rather than the materialised path.
        """
        out: List[int] = []
        current = self.resolve_id(int(domain_id))
        seen = {current}
        for _ in range(MAX_DOMAIN_DEPTH + 2):
            row = self.conn.execute(
                "SELECT parent_id FROM domain WHERE id = ?", (current,)
            ).fetchone()
            if row is None or row["parent_id"] is None:
                break
            parent = int(row["parent_id"])
            if parent in seen:
                break
            seen.add(parent)
            out.append(parent)
            current = parent
        return out

    def chain(self, domain_id: int) -> List[int]:
        """Return a domain and its ancestors as one ordered list, nearest first."""
        resolved = self.resolve_id(int(domain_id))
        if not resolved:
            return []
        return [resolved, *self.ancestors(resolved)]

    def list_domains(
        self, user_id: str, status: str = "active"
    ) -> List[DomainRow]:
        """Return a user's domains, ordered by path.

        Args:
            user_id: Owner of the vocabulary.
            status: The status to list (``active`` / ``merged`` / ``archived``).

        Returns:
            The matching rows, shallowest first.
        """
        rows = self.conn.execute(
            "SELECT id, canonical_name, display_name, parent_id, path, status, "
            "system_seeded FROM domain WHERE user_id = ? AND status = ? "
            "ORDER BY path",
            (str(user_id or ""), status),
        ).fetchall()
        return [
            DomainRow(
                id=int(row["id"]),
                canonical_name=str(row["canonical_name"]),
                display_name=str(row["display_name"]),
                parent_id=(
                    int(row["parent_id"]) if row["parent_id"] is not None else None
                ),
                path=str(row["path"]),
                status=str(row["status"]),
                system_seeded=bool(row["system_seeded"]),
            )
            for row in rows
        ]

    def children(self, user_id: str, domain_id: int) -> List[int]:
        """Return the active child ids of a domain."""
        rows = self.conn.execute(
            "SELECT id FROM domain WHERE user_id = ? AND parent_id = ? "
            "AND status = 'active' ORDER BY canonical_name",
            (str(user_id or ""), self.resolve_id(int(domain_id))),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def bridges_from(self, domain_ids: Sequence[int]) -> Dict[int, float]:
        """Return ``{target_id: weight}`` for bridges leaving the given ids.

        Bridges contribute ranking weight only. They never widen a filter set —
        that is the decision that keeps "isolate topics" and "reach across
        topics" from contradicting each other.
        """
        ids = sorted({self.resolve_id(int(d)) for d in domain_ids} - {0})
        if not ids:
            return {}
        out: Dict[int, float] = {}
        for chunk in _chunks(ids, _SQL_CHUNK):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT from_id, to_id, weight FROM domain_bridge "
                f"WHERE from_id IN ({placeholders})",
                list(chunk),
            ).fetchall()
            for row in rows:
                target = self.resolve_id(int(row["to_id"]))
                if not target:
                    continue
                out[target] = max(out.get(target, 0.0), float(row["weight"]))
        return out

    def unresolved(self, user_id: str, limit: int = 50) -> List[DomainSignal]:
        """Return the pending registration queue, most-seen first."""
        rows = self.conn.execute(
            "SELECT canonical_name, nearest_ancestor, seen_count, scope_id "
            "FROM domain_signal WHERE user_id = ? AND status = 'pending' "
            "ORDER BY seen_count DESC, last_seen DESC LIMIT ?",
            (str(user_id or ""), int(limit)),
        ).fetchall()
        return [
            DomainSignal(
                canonical_name=str(row["canonical_name"]),
                nearest_ancestor=(
                    int(row["nearest_ancestor"])
                    if row["nearest_ancestor"] is not None
                    else None
                ),
                seen_count=int(row["seen_count"]),
                # `-1` is the stored form of "no scope" (see record_signal).
                scope_id=(
                    int(row["scope_id"])
                    if row["scope_id"] is not None and int(row["scope_id"]) >= 0
                    else None
                ),
            )
            for row in rows
        ]

    # -- writes --------------------------------------------------------------

    def _invalidate(self, user_id: str = "") -> None:
        """Drop the name/label caches (whole user, or everything when blank)."""
        if not user_id:
            self._by_name.clear()
            self._label_cache.clear()
            return
        for key in [k for k in self._by_name if k[0] == str(user_id)]:
            self._by_name.pop(key, None)
        self._label_cache.clear()

    def create(
        self,
        user_id: str,
        canonical_name: str,
        display_name: str = "",
        parent_id: Optional[int] = None,
        system_seeded: bool = False,
    ) -> Optional[int]:
        """Register a domain, creating missing ancestors as needed.

        Idempotent on ``(user_id, canonical_name)``, like every other creation in
        this library: resolution is retried constantly and a second row for one
        name is the near-duplicate failure the registry exists to prevent.

        Missing ancestors are created **from the root down**, so
        ``teaching/ds/ch3`` produces ``teaching`` and ``teaching/ds`` first. A
        deep name that hung directly from the root would be unreachable from its
        own parent's other children, which is the one thing the hierarchy is for.

        Args:
            user_id: Owner of the vocabulary.
            canonical_name: A canonical name (normalised here).
            display_name: Free-form label for humans (may be CJK); only used for
                the deepest segment.
            parent_id: An explicit parent, used verbatim when given.
            system_seeded: Marks a name the system generated rather than the user
                (protected from deletion, allowed to be merged).

        Returns:
            The domain id, or ``None`` when the name is not storable (see
            :func:`is_valid_canonical`).
        """
        owner = str(user_id or "")
        name = normalize_canonical(canonical_name)
        if not owner or not is_valid_canonical(name):
            return None
        existing = self.find(owner, name)
        if existing:
            return existing

        parent = self._resolve_id_or_none(parent_id)
        if parent is None:
            parts = name.split("/")
            for index in range(1, len(parts)):
                ancestor = "/".join(parts[:index])
                found = self.find(owner, ancestor)
                if not found:
                    found = self._insert(owner, ancestor, ancestor, parent, True)
                parent = found
        return self._insert(owner, name, display_name or name, parent, system_seeded)

    def _resolve_id_or_none(self, parent_id: Optional[int]) -> Optional[int]:
        """Resolve an explicit parent id, or ``None`` when there is not one."""
        if parent_id is None:
            return None
        resolved = self.resolve_id(int(parent_id))
        return resolved or None

    def _insert(
        self,
        user_id: str,
        name: str,
        display_name: str,
        parent_id: Optional[int],
        system_seeded: bool,
    ) -> Optional[int]:
        """Insert one domain row (the caller has already handled ancestors).

        ``path`` is the **canonical name itself**, because a canonical name is
        already a full path from the root (``teaching/ds`` names the ``ds`` node
        under ``teaching``). Prefixing it with the parent's path would produce
        ``teaching/teaching/ds`` — the mistake that makes a "path" prefix match
        report a match for the wrong node.
        """
        now = now_ms()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO domain(user_id, canonical_name, display_name, parent_id, "
                "path, status, merged_into, system_seeded, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)",
                (
                    user_id,
                    name,
                    (display_name or name).strip() or name,
                    parent_id,
                    name,
                    1 if system_seeded else 0,
                    now,
                    now,
                ),
            )
            domain_id = int(cursor.lastrowid)
        self._invalidate(user_id)
        logger.info("Created domain %s (id=%d)", name, domain_id)
        return domain_id

    def path_of(self, domain_id: Optional[int]) -> str:
        """Return a domain's materialised path (``""`` for an unknown id)."""
        if domain_id is None:
            return ""
        row = self.get(int(domain_id))
        return row.path if row is not None else ""

    def set_bridge(
        self, user_id: str, from_id: int, to_id: int, weight: float = 0.5
    ) -> bool:
        """Record a cross-topic bridge (ranking only).

        Args:
            user_id: Owner of the vocabulary.
            from_id: The topic the user is working in.
            to_id: The topic to reward as well.
            weight: Multiplier applied to the secondary weight (0..1).

        Returns:
            ``True`` when the bridge was stored, ``False`` for unknown ids.
        """
        source = self.resolve_id(int(from_id))
        target = self.resolve_id(int(to_id))
        if not source or not target or source == target:
            return False
        with self.conn:
            self.conn.execute(
                "INSERT INTO domain_bridge(user_id, from_id, to_id, weight) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(user_id, from_id, to_id) "
                "DO UPDATE SET weight = excluded.weight",
                (str(user_id or ""), source, target, float(weight)),
            )
        return True

    def rename(self, user_id: str, domain_id: int, canonical_name: str) -> dict:
        """Rename a domain and every descendant path beneath it.

        `fact_domain` rows are keyed by id, so a rename rewrites **no fact
        labels** — only names. That is the whole reason the registry uses ids.

        Args:
            user_id: Owner of the vocabulary.
            domain_id: The domain to rename.
            canonical_name: The new canonical name.

        Returns:
            ``{"domain_id", "from", "to", "children_moved"}``.

        Raises:
            ValueError: For an unknown domain, an unstorable name, or a name
                already owned by another row.
        """
        row = self.get(domain_id)
        if row is None:
            raise ValueError(f"unknown domain: {domain_id}")
        owner = str(user_id or "")
        name = normalize_canonical(canonical_name)
        if not is_valid_canonical(name):
            raise ValueError(f"invalid domain name: {canonical_name!r}")
        if name == row.canonical_name:
            return {
                "domain_id": row.id,
                "from": row.canonical_name,
                "to": name,
                "children_moved": 0,
            }
        taken = self.find(owner, name)
        if taken and taken != row.id:
            raise ValueError(f"domain {name!r} already exists; merge instead")
        with self.conn:
            self._rewrite_subtree(owner, row.id, name)
        self._invalidate(owner)
        record_event(
            self.conn,
            "domain_renamed",
            {"domain_id": row.id, "from": row.canonical_name, "to": name},
            user_id=owner,
        )
        moved = len(self.children(owner, row.id))
        return {
            "domain_id": row.id,
            "from": row.canonical_name,
            "to": name,
            "children_moved": moved,
        }

    def _rewrite_subtree(self, user_id: str, domain_id: int, canonical_name: str) -> None:
        """Rewrite one domain's name and every descendant's path.

        The stored ``path`` is the canonical name itself (a canonical name is
        already a full path from the root — see :meth:`_insert`), so renaming a
        parent means rewriting each descendant's **prefix**, not prepending a
        parent name to an already-prefixed name.
        """
        row = self.get(domain_id)
        if row is None:
            return
        self.conn.execute(
            "UPDATE domain SET canonical_name = ?, path = ? WHERE id = ?",
            (canonical_name, canonical_name, row.id),
        )
        prefix = f"{row.canonical_name}/"
        for child_id in self.children(user_id, row.id):
            child = self.get(child_id)
            if child is None:
                continue
            relative = child.canonical_name
            if relative.startswith(prefix):
                relative = relative[len(prefix):]
            self._rewrite_subtree(user_id, child.id, f"{canonical_name}/{relative}")

    def merge(self, user_id: str, from_id: int, to_id: int) -> dict:
        """Fold one domain into another, keeping the source row as ``merged``.

        Facts are **not** rewritten: their ``fact_domain`` rows still point at
        the source id, which now resolves to the target. That keeps history
        readable and makes the merge reversible in principle — the alternative
        (rewriting every label) loses the information that a distinction was ever
        made.

        Args:
            user_id: Owner of the vocabulary.
            from_id: The domain to fold away.
            to_id: The domain to keep.

        Returns:
            ``{"from", "to", "labels_moved", "children_moved"}``.

        Raises:
            ValueError: For unknown ids or a self-merge.
        """
        source = self.resolve_id(int(from_id))
        target = self.resolve_id(int(to_id))
        if not source or not target:
            raise ValueError("merge requires two known domains")
        if source == target:
            raise ValueError("cannot merge a domain into itself")
        owner = str(user_id or "")
        children = self.children(owner, source)
        with self.conn:
            for child_id in children:
                self.conn.execute(
                    "UPDATE domain SET parent_id = ? WHERE id = ?",
                    (target, child_id),
                )
            labels = self.conn.execute(
                "SELECT COUNT(*) AS n FROM fact_domain WHERE domain_id = ?", (source,)
            ).fetchone()["n"]
            self.conn.execute(
                "UPDATE domain SET status = 'merged', merged_into = ? WHERE id = ?",
                (target, source),
            )
        for child_id in children:
            child = self.get(child_id)
            if child is not None:
                self._rewrite_subtree(owner, child_id, child.canonical_name)
        self._invalidate(owner)
        record_event(
            self.conn,
            "domain_merged",
            {
                "from": source,
                "from_name": self.name_of(source),
                "to": target,
                "labels_moved": int(labels or 0),
                "children_moved": len(children),
            },
            user_id=owner,
        )
        return {
            "from": source,
            "to": target,
            "labels_moved": int(labels or 0),
            "children_moved": len(children),
        }

    def archive(self, user_id: str, domain_id: int) -> bool:
        """Archive a domain so it stops being offered for new labels.

        Args:
            user_id: Owner of the vocabulary.
            domain_id: The domain to archive.

        Returns:
            ``True`` when a row changed.
        """
        resolved = self.resolve_id(int(domain_id))
        if not resolved:
            return False
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE domain SET status = 'archived' WHERE id = ? "
                "AND status = 'active'",
                (resolved,),
            )
        self._invalidate(str(user_id or ""))
        return bool(cursor.rowcount)

    # -- labelling -----------------------------------------------------------

    def resolve_chain(self, user_id: str, canonical_name: str) -> Tuple[Optional[int], List[str]]:
        """Resolve a proposed name to a registered domain, or to its ancestor.

        Args:
            user_id: Owner of the vocabulary.
            canonical_name: The proposal (normalised here).

        Returns:
            ``(domain_id, unresolved_names)``. The id is the exact match when one
            exists, otherwise the nearest registered ancestor, otherwise ``None``.
            ``unresolved_names`` are the proposals (deepest first) that exist in
            neither form and belong in the registration queue.
        """
        name = normalize_canonical(canonical_name)
        if not name or not is_valid_canonical(name):
            return None, []
        exact = self.find(user_id, name)
        if exact:
            return exact, []
        unresolved: List[str] = [name]
        for ancestor in ancestor_names(name):
            found = self.find(user_id, ancestor)
            if found:
                return found, unresolved
            unresolved.append(ancestor)
        return None, unresolved

    def session_domains(
        self,
        user_id: str,
        *,
        scope_ids: Sequence[int] = (),
        scope_paths: Sequence[str] = (),
        labels: Sequence[str] = (),
    ) -> SessionDomains:
        """Derive the topic set a session is assumed to be about.

        The order is the point: an explicit session label wins, then the mapping
        from the scope tree (a course project is about teaching — this is the
        rule that keeps ``general`` from becoming the default bucket), then
        ``general``.

        Args:
            user_id: Owner of the vocabulary.
            scope_ids: Scopes the session resolved to, most specific first.
            scope_paths: Their paths, positionally matching ``scope_ids``.
            labels: Explicit topic labels for the session.

        Returns:
            The session's domains, most specific first.
        """
        owner = str(user_id or "")
        chosen: List[int] = []
        source = SOURCE_SESSION
        detail = ""
        kept_labels: List[str] = []
        for label in labels or ():
            found, _ = self.resolve_chain(owner, label)
            if found and found not in chosen:
                chosen.append(found)
                source = SOURCE_USER
                detail = f"explicit session label {label!r}"
                kept_labels.append(str(label))
        if not chosen:
            for scope_id, path in zip(scope_ids, scope_paths):
                mapped = self._map_scope_path(owner, path)
                if mapped is None:
                    continue
                if mapped not in chosen:
                    chosen.append(mapped)
                    source = SOURCE_SCOPED_MAP
                    detail = f"scope {path} maps to {self.name_of(mapped)}"
                break
        if not chosen:
            general = self.find(owner, GENERAL_DOMAIN)
            if general:
                chosen.append(general)
                detail = "no session evidence; general"
        return SessionDomains(
            domain_ids=tuple(chosen),
            source=source,
            detail=detail,
            labels=tuple(kept_labels),
        )

    def _map_scope_path(self, user_id: str, path: str) -> Optional[int]:
        """Return the domain a scope path maps to, or ``None``.

        The mapping is by prefix, most specific first, so a deployment can say
        "everything under this course project is teaching" once. Matching is on
        the scope path because that is what a human writes down when they
        configure it.
        """
        mapping = getattr(self.config, "scope_domain_map", None) or ()
        best: Optional[Tuple[int, str]] = None
        for entry in mapping:
            try:
                prefix, domain_name = entry[0], entry[1]
            except (TypeError, IndexError):
                continue
            prefix = str(prefix or "")
            if not prefix or not path.startswith(prefix):
                continue
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), str(domain_name))
        if best is None:
            return None
        found = self.find(user_id, normalize_canonical(best[1]))
        if found:
            return found
        # A mapped name that was never registered is registered now: the
        # configuration is the user's own statement of intent, which is stronger
        # evidence than anything the extractor could propose.
        return self.create(user_id, best[1])

    def keywords_for(self, text: str) -> List[str]:
        """Return the topics whose configured keywords appear in ``text``.

        Args:
            text: The raw content being labelled.

        Returns:
            Candidate domain names, best (longest keyword) first.
        """
        lowered = str(text or "").casefold()
        if not lowered:
            return []
        hits: List[Tuple[int, str]] = []
        mapping = getattr(self.config, "domain_keywords", None) or ()
        for entry in mapping:
            try:
                keyword, domain_name = entry[0], entry[1]
            except (TypeError, IndexError):
                continue
            needle = str(keyword or "").casefold()
            if needle and needle in lowered:
                hits.append((len(needle), str(domain_name)))
        hits.sort(key=lambda item: -item[0])
        out: List[str] = []
        for _, name in hits:
            canonical = normalize_canonical(name)
            if canonical and canonical not in out:
                out.append(canonical)
        return out

    def assign(
        self,
        user_id: str,
        *,
        hints: Sequence[str] = (),
        text: str = "",
        session: Optional[SessionDomains] = None,
        scope_id: Optional[int] = None,
        source: str = SOURCE_HINT,
    ) -> DomainAssignment:
        """Decide which topics a fact is about, and record what it could not.

        The chain, in order: explicit/hinted proposals, the scope mapping, keyword
        rules, then the session default. An empty result is a *refusal*: the
        caller must not store an unlabelled fact, because a fact that no filter
        can classify is a fact that every filter has to let through.

        Args:
            user_id: Owner of the vocabulary.
            hints: Proposed canonical names, most important first.
            text: The content being labelled (used by the keyword rules).
            session: The session's derived topic set.
            scope_id: The scope the write is filed under (for the queue).
            source: Which rule the caller's hints came from (``hint`` /
                ``user_explicit``).

        Returns:
            The assignment (never with an empty ``labels`` when the vocabulary
            could say anything at all).
        """
        owner = str(user_id or "")
        session = session or self.session_domains(owner)
        # The session's own labels are the user speaking, so they enter at that
        # rank and *before* the extractor's proposals — the session says what the
        # work is about, which is the more reliable statement of the two. Both
        # end up as proposals: skipping the session set whenever a hint resolved
        # would silently drop the strongest signal the store has.
        proposals: List[Tuple[str, str]] = []
        for label in getattr(session, "labels", ()) or ():
            proposals.append((normalize_canonical(label), SOURCE_USER))
        proposals.extend(
            (normalize_canonical(hint), source) for hint in hints or () if hint
        )
        proposals.extend(
            (self.name_of(domain_id), session.source or SOURCE_SESSION)
            for domain_id in session.domain_ids
        )
        unresolved: List[str] = []
        chosen: List[Tuple[int, str, float]] = []

        def _add(domain_id: Optional[int], rule: str, weight: float) -> None:
            if not domain_id:
                return
            resolved = self.resolve_id(domain_id)
            if not resolved:
                return
            for index, (existing, existing_rule, _) in enumerate(chosen):
                if existing == resolved:
                    # Keep the more trustworthy rule for the same label.
                    if source_rank(rule) < source_rank(existing_rule):
                        chosen[index] = (resolved, rule, weight)
                    return
            chosen.append((resolved, rule, weight))

        for name, rule in proposals:
            if not name:
                continue
            found, missing = self.resolve_chain(owner, name)
            # A proposal that resolved to an ancestor is stored as that ancestor:
            # a label the vocabulary does not hold cannot be filtered on.
            _add(found, rule, 0.9 if rule == SOURCE_USER else 0.7)
            unresolved.extend(missing)

        if not chosen:
            for name in self.keywords_for(text):
                found, missing = self.resolve_chain(owner, name)
                _add(found, SOURCE_KEYWORD, 0.6)
                unresolved.extend(missing)

        if not chosen:
            for domain_id in session.domain_ids:
                _add(domain_id, session.source or SOURCE_SESSION, 0.5)

        if not chosen:
            general = self.create(owner, GENERAL_DOMAIN, "通用", system_seeded=True)
            _add(general, SOURCE_SESSION, 0.5)
            if not chosen:  # pragma: no cover - only when the name is unusable
                return DomainAssignment(
                    signals=tuple(dict.fromkeys(unresolved)),
                    detail="no domain could be resolved",
                )

        limit = max(1, int(getattr(self.config, "domain_max_per_fact", 5) or 5))
        capped = chosen[:limit]
        # A cap must never drop the label the fact is ranked by: the first entry
        # is the one the caller proposed or the session resolved to.
        labels = tuple(
            DomainLabel(
                domain_id=domain_id,
                name=self.name_of(domain_id),
                confidence=weight,
                is_primary=(index == 0),
                source=rule,
            )
            for index, (domain_id, rule, weight) in enumerate(capped)
        )
        for name in dict.fromkeys(unresolved):
            self.record_signal(owner, name, scope_id=scope_id)
        primary = labels[0].name if labels else None
        return DomainAssignment(
            labels=labels,
            signals=tuple(dict.fromkeys(unresolved)),
            detail=(
                f"primary {primary} ({labels[0].source})"
                + (f"; {len(labels) - 1} more" if len(labels) > 1 else "")
            ),
        )

    def record_signal(
        self,
        user_id: str,
        canonical_name: str,
        *,
        scope_id: Optional[int] = None,
        nearest_ancestor: Optional[int] = None,
    ) -> None:
        """Count a proposal the vocabulary does not hold yet.

        Registration is the user's decision, so a proposal is *queued*, never
        created. Repeated consistent sightings bump one row; a different scope
        under the same name is a separate row that cannot promote the first.
        """
        owner = str(user_id or "")
        name = normalize_canonical(canonical_name)
        if not owner or not name:
            return
        if nearest_ancestor is None:
            resolved, _ = self.resolve_chain(owner, name)
            nearest_ancestor = resolved
        now = now_ms()
        # The upsert targets `scope_key`, the generated `COALESCE(scope_id, -1)`
        # column, not `scope_id`: SQLite treats NULLs as *distinct* in a UNIQUE
        # index, so keying on the nullable column would insert a new row per
        # sighting instead of counting one — and that count is the whole basis
        # for offering a name for registration.
        with self.conn:
            self.conn.execute(
                "INSERT INTO domain_signal(user_id, canonical_name, nearest_ancestor, "
                "scope_id, seen_count, status, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, 1, 'pending', ?, ?) "
                "ON CONFLICT(user_id, canonical_name, scope_key) DO UPDATE SET "
                "seen_count = seen_count + 1, last_seen = excluded.last_seen",
                (owner, name, nearest_ancestor, scope_id, now, now),
            )

    # -- seeding -------------------------------------------------------------

    def ensure_root(self, user_id: str) -> Optional[int]:
        """Register ``general`` for a user if it is not there yet.

        ``general`` is the label of last resort, so it must exist before any
        write needs it — including a write that happens before the user has any
        scope or fact for :meth:`seed_from_scopes` to derive a vocabulary from.
        Creating it on demand inside :meth:`assign` instead would mean the first
        write of a fresh deployment both labels and restructures the vocabulary.

        Args:
            user_id: Owner of the vocabulary.

        Returns:
            The id of ``general`` (created if needed), or ``None``.
        """
        owner = str(user_id or "")
        if not owner:
            return None
        return self.create(owner, GENERAL_DOMAIN, "通用", system_seeded=True)

    def seed_from_scopes(self, user_id: str) -> List[str]:
        """Register a starting vocabulary from the scopes a user already has.

        The alternative — shipping a fixed list of root topics — is a guess about
        someone else's work. The scope tree, by contrast, is a record of what the
        user actually does: their projects, courses and clients. A project scope
        becomes a topic suggestion named after it, filed under the domain its
        configuration maps it to (or ``general``).

        ``general`` itself is registered even when there is nothing to derive, so
        a user's vocabulary always has a root.

        Idempotent and cheap enough to call at startup.

        Args:
            user_id: Owner of the vocabulary.

        Returns:
            The canonical names created, parents before children.
        """
        owner = str(user_id or "")
        if not owner:
            return []
        created: List[str] = []
        if not self.find(owner, GENERAL_DOMAIN):
            if self.ensure_root(owner):
                created.append(GENERAL_DOMAIN)
        scopes = self.conn.execute(
            "SELECT id, scope_type, canonical_name, path FROM scope WHERE scope_type IN "
            "(?, ?, ?, ?, ?, ?, ?) ORDER BY path",
            (
                SCOPE_ORG,
                SCOPE_TEAM,
                SCOPE_CLIENT,
                SCOPE_PROJECT,
                SCOPE_SERIES,
                SCOPE_PHASE,
                SCOPE_DOCUMENT,
            ),
        ).fetchall()
        for row in scopes:
            path = str(row["path"])
            if str(row["scope_type"]) == SCOPE_DOCUMENT:
                # A document is an artefact, not a topic: `project:x/document:ch3`
                # would give a topic per file, which is the label explosion the
                # registry exists to prevent. The *project* carries the topic.
                continue
            mapped = self._map_scope_path(owner, path)
            if mapped is not None:
                continue
            # The name comes from the scope's own `canonical_name` column, never
            # from parsing its path. A scope name routinely contains separators —
            # a remote URL becomes `github.com/acme/api`, a folder path keeps its
            # slashes — so `path.rsplit("/")` yields a basename, and two projects
            # both ending in `api` would collapse into one topic that labels
            # unrelated work identically.
            canonical = str(row["canonical_name"] or "")
            name = normalize_canonical(canonical)
            if not name or not is_valid_canonical(name):
                continue
            if self.find(owner, name):
                continue
            if self.create(
                owner,
                name,
                display_name=canonical.rsplit("/", 1)[-1] or name,
                system_seeded=True,
            ):
                created.append(name)
        # `user` scope is the topic of "about the person themselves".
        if not self.find(owner, SCOPE_USER):
            if self.create(owner, SCOPE_USER, "用户偏好", system_seeded=True):
                created.append(SCOPE_USER)
        return created

    # -- fact labels ---------------------------------------------------------

    def fact_domains(
        self, fact_ids: Sequence[str]
    ) -> Dict[str, Tuple[int, ...]]:
        """Return ``{fact_id: (domain_id, ...)}`` for a batch of facts.

        Facts with no label are present with an empty tuple, so a caller can tell
        "unlabelled (pre-domain row)" from "forgot to look" — the same contract
        :meth:`~atom_memory.scope.ScopeStore.fact_scopes` provides.
        """
        out: Dict[str, Tuple[int, ...]] = {fact_id: () for fact_id in fact_ids}
        grouped: Dict[str, List[int]] = {}
        for chunk in _chunks(list(fact_ids), _SQL_CHUNK):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT fact_id, domain_id FROM fact_domain "
                f"WHERE fact_id IN ({placeholders}) "
                f"ORDER BY is_primary DESC, confidence DESC, domain_id",
                list(chunk),
            ).fetchall()
            for row in rows:
                grouped.setdefault(str(row["fact_id"]), []).append(
                    int(row["domain_id"])
                )
        for fact_id, ids in grouped.items():
            out[fact_id] = tuple(ids)
        return out

    def labels_of(self, fact_id: str) -> List[DomainLabel]:
        """Return a fact's labels with their source and confidence.

        The name is read **through a merge**: a label whose domain was folded into
        another must report the name that is in force now, not the one it was
        written with, or a corrected vocabulary would keep showing the old name
        until every fact was relabelled by hand.
        """
        rows = self.conn.execute(
            "SELECT fd.domain_id, fd.confidence, fd.is_primary, fd.source "
            "FROM fact_domain fd WHERE fd.fact_id = ? "
            "ORDER BY fd.is_primary DESC, fd.confidence DESC",
            (fact_id,),
        ).fetchall()
        out: List[DomainLabel] = []
        for row in rows:
            resolved = self.resolve_id(int(row["domain_id"]))
            if not resolved:
                continue
            out.append(
                DomainLabel(
                    domain_id=resolved,
                    name=self.name_of(resolved),
                    confidence=float(row["confidence"] or 0.0),
                    is_primary=bool(row["is_primary"]),
                    source=str(row["source"]),
                )
            )
        return out

    def attach(
        self, fact_id: str, assignment: DomainAssignment
    ) -> List[DomainLabel]:
        """Write a fact's labels inside the caller's transaction.

        Merging is by **maximum confidence, never by union**: restating one claim
        in a new context must not grow its label set without limit, or a single
        generic sentence ends up labelled with every topic the user has, and
        "domains intersect" — the conflict rule — stops discriminating.

        Args:
            fact_id: The fact the labels belong to.
            assignment: The labels to attach.

        Returns:
            The labels stored.
        """
        if not assignment.labels:
            return []
        limit = max(1, int(getattr(self.config, "domain_max_per_fact", 5) or 5))
        existing = {label.domain_id: label for label in self.labels_of(fact_id)}
        now = now_ms()
        for label in assignment.labels:
            current = existing.get(label.domain_id)
            keep_primary = 0
            if current is None:
                if len(existing) >= limit:
                    continue
            else:
                keep_primary = 1 if current.is_primary and not label.is_primary else 0
            confidence = max(
                float(current.confidence) if current is not None else 0.0,
                float(label.confidence),
            )
            source = (
                current.source
                if current is not None
                and source_rank(current.source) <= source_rank(label.source)
                else label.source
            )
            is_primary = 1 if label.is_primary or keep_primary else 0
            with self.conn:
                self.conn.execute(
                    "INSERT INTO fact_domain(fact_id, domain_id, confidence, "
                    "is_primary, source, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(fact_id, domain_id) DO UPDATE SET "
                    "confidence = MAX(confidence, excluded.confidence), "
                    "is_primary = MAX(is_primary, excluded.is_primary), "
                    "source = excluded.source",
                    (
                        fact_id,
                        label.domain_id,
                        confidence,
                        is_primary,
                        source,
                        now,
                    ),
                )
        return self.labels_of(fact_id)

    def set_fact_domains(
        self,
        fact_id: str,
        names: Sequence[str],
        *,
        user_id: str = "",
        create_missing: bool = True,
    ) -> List[DomainLabel]:
        """Replace a fact's labels with exactly the given names.

        The user-facing correction. Unlike :meth:`attach` (which merges as the
        write path reinforces a fact), this is authoritative: what is not named is
        removed, because "correct this label" is a decision, not another
        observation.

        Args:
            fact_id: The fact to relabel.
            names: Canonical names, most important first (the first becomes
                primary).
            user_id: Owner of the vocabulary (required when creating names).
            create_missing: Register an unknown name instead of skipping it.

        Returns:
            The labels stored.

        Raises:
            ValueError: For an empty or unusable name list.
        """
        owner = str(user_id or "")
        cleaned = [normalize_canonical(name) for name in names or ()]
        cleaned = [name for name in cleaned if name]
        if not cleaned:
            raise ValueError("set_fact_domains requires at least one domain name")
        if len(cleaned) > max(1, int(getattr(self.config, "domain_max_per_fact", 5) or 5)):
            raise ValueError(
                f"at most {self.config.domain_max_per_fact} domains per fact"
            )
        resolved: List[int] = []
        for name in cleaned:
            if not is_valid_canonical(name):
                raise ValueError(f"invalid domain name: {name!r}")
            found = self.find(owner, name) if owner else None
            if not found and owner and create_missing:
                found = self.create(owner, name)
            if not found:
                raise ValueError(f"unknown domain: {name!r}")
            if found not in resolved:
                resolved.append(found)
        now = now_ms()
        with self.conn:
            self.conn.execute("DELETE FROM fact_domain WHERE fact_id = ?", (fact_id,))
            for index, domain_id in enumerate(resolved):
                self.conn.execute(
                    "INSERT INTO fact_domain(fact_id, domain_id, confidence, "
                    "is_primary, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        fact_id,
                        domain_id,
                        1.0 if index == 0 else 0.8,
                        1 if index == 0 else 0,
                        SOURCE_USER,
                        now,
                    ),
                )
        return self.labels_of(fact_id)
