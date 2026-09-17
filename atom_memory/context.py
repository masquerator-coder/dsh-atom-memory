"""Session-context signals and the :class:`ScopeContext` they resolve into.

Scope resolution has two halves, and this module is the first one: turning a
heterogeneous pile of hints about "where am I right now" — a git remote, a
session working directory, an explicit ``client=acme`` tag, an LLM's
``scope_hint`` — into a normalised, reliability-weighted description. The second
half (:mod:`~atom_memory.scope`) decides what to do with it.

Three rules shape everything here, and each exists because of a specific way
scope resolution goes wrong:

1. **Normalise, then compare.** ``git@GitHub.com:Team/Repo.git``,
   ``https://github.com/team/repo/`` and ``ssh://git@github.com/team/repo`` are
   one project, not three. Every signal carries both the raw value (for audit)
   and a ``normalized_value`` (for identity), and only the latter is ever
   compared or stored as a key.
2. **Reliability is per signal type, not per caller.** A git remote (high) and a
   repository's display name (very low) are not equally good evidence that two
   sessions are the same project. The table below is the whole policy; a caller
   cannot talk a weak signal into being strong by asserting it confidently.
3. **A signal names at most one scope type.** ``doc_id`` identifies a document;
   it can never create a ``client``. :data:`SIGNAL_SPECS` is the mapping, and a
   signal type absent from it is rejected outright rather than guessed at — an
   unknown signal silently interpreted as "a project" is how cross-project
   pollution starts.

Conditions are the second, orthogonal axis: "this is true *for proposals*" is not
a scope, it is a constraint on when a fact applies. They are normalised the same
way (lower-cased, trimmed, key-shaped) so that ``doc_type=Proposal`` and
``doc_type= proposal`` are one condition instead of two.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# -- scope types --------------------------------------------------------------

SCOPE_GLOBAL = "global"
SCOPE_USER = "user"
SCOPE_ORG = "org"
SCOPE_TEAM = "team"
SCOPE_CLIENT = "client"
SCOPE_PROJECT = "project"
SCOPE_SERIES = "series"
SCOPE_PHASE = "phase"
SCOPE_DOCUMENT = "document"
SCOPE_THREAD = "thread"

#: Every scope type the schema accepts, in *increasing* specificity. The order is
#: the hierarchy's spine: a fact bound to a specific scope is reachable from any
#: more general one (see :mod:`~atom_memory.scope`), so the list doubles as the
#: ancestor order and as the guard that a parent is never more specific than its
#: child.
SCOPE_TYPES: Tuple[str, ...] = (
    SCOPE_GLOBAL,
    SCOPE_USER,
    SCOPE_ORG,
    SCOPE_TEAM,
    SCOPE_CLIENT,
    SCOPE_PROJECT,
    SCOPE_SERIES,
    SCOPE_PHASE,
    SCOPE_DOCUMENT,
    SCOPE_THREAD,
)

#: Depth of each scope type. ``global`` is the root (0); larger is more specific.
SCOPE_DEPTH: Dict[str, int] = {name: i for i, name in enumerate(SCOPE_TYPES)}

#: The reserved root scope id written by migration 011.
GLOBAL_SCOPE_ID = 1

#: Values of the ``scope_hint`` field that name a *level* rather than a place.
#:
#: The field is normally a content anchor — a phrase the model copied out of the
#: text ("第3章课件", "acme 的 api 项目") — and it is 0.25 evidence that can only
#: corroborate a resolution or accumulate in the candidate queue. These two
#: values are different in kind: the extractor emits them for a fact that belongs
#: to the *user* rather than to whatever document the session happens to be in
#: (a durable preference, a stable attribute). They are markers, so they must
#: never become a scope name: a content anchor is a name, and a project called
#: "user" would be created once per session that mentioned a preference.
#:
#: Honouring ``user`` fully — filing such a fact at the user scope, so that every
#: project sees it — needs the topic dimension to exist first: lifting every
#: preference to a level visible from everywhere is precisely how a preference
#: about programming would start leaking into teaching work. Until then the
#: marker is recognised and *ignored*, which keeps the write where the session is
#: and creates no bogus scope.
RESERVED_SCOPE_HINTS: frozenset = frozenset({SCOPE_GLOBAL, SCOPE_USER})

# -- signal types -------------------------------------------------------------


@dataclass(frozen=True)
class SignalSpec:
    """What one kind of context signal is worth, and what it can name.

    Attributes:
        scope_type: The scope type this signal identifies. A signal can only ever
            create or match a scope of this type.
        reliability: 0..1 evidence weight of the signal, from the design's
            reliability table. It is the *ceiling* a match can reach: a match
            whose reliability is below the auto-bind threshold can corroborate a
            resolution but never cause one.
        identity_rank: Which signal represents a scope when several describe the
            same level (lower wins). The order prefers the identity that survives
            a move: an explicit user tag, then a durable id or a remote URL,
            then a local path, and only then a display name.
    """

    scope_type: str
    reliability: float
    identity_rank: int


#: Reliability of each signal type, and what it can identify.
#:
#: The numbers are the design's table (极高 / 高 / 中 / 低 / 极低) made explicit:
#: only a signal at or above ``MemConfig.scope_new_threshold`` can bring a scope
#: into existence, which is why `path` (0.50) needs three consistent
#: observations while `git_root` (0.90) does not.
SIGNAL_SPECS: Dict[str, SignalSpec] = {
    # Explicit metadata (session configuration, user tags): the user said so.
    "explicit_org": SignalSpec(SCOPE_ORG, 0.95, 0),
    "explicit_team": SignalSpec(SCOPE_TEAM, 0.95, 0),
    "explicit_client": SignalSpec(SCOPE_CLIENT, 0.95, 0),
    "explicit_project": SignalSpec(SCOPE_PROJECT, 0.95, 0),
    "explicit_series": SignalSpec(SCOPE_SERIES, 0.95, 0),
    "explicit_phase": SignalSpec(SCOPE_PHASE, 0.95, 0),
    # Durable ids: a cloud document, a folder, a mail thread.
    "doc_id": SignalSpec(SCOPE_DOCUMENT, 0.90, 1),
    "folder_id": SignalSpec(SCOPE_DOCUMENT, 0.90, 1),
    "email_thread": SignalSpec(SCOPE_THREAD, 0.90, 1),
    # Remote identity: survives cloning the repository somewhere else.
    "git_remote": SignalSpec(SCOPE_PROJECT, 0.80, 1),
    "share_link": SignalSpec(SCOPE_DOCUMENT, 0.75, 2),
    "org_domain": SignalSpec(SCOPE_ORG, 0.75, 1),
    # Local / project identifiers.
    "git_root": SignalSpec(SCOPE_PROJECT, 0.90, 2),
    "package": SignalSpec(SCOPE_PROJECT, 0.55, 3),
    "doc_title": SignalSpec(SCOPE_DOCUMENT, 0.50, 4),
    "project_code": SignalSpec(SCOPE_PROJECT, 0.50, 4),
    "path": SignalSpec(SCOPE_PROJECT, 0.50, 5),
    "folder_path": SignalSpec(SCOPE_DOCUMENT, 0.50, 5),
    "participants": SignalSpec(SCOPE_TEAM, 0.45, 6),
    # Content anchors: what the text is *about*. Never enough to bind, but
    # exactly what the unresolved queue waits to see repeated.
    "content_anchor": SignalSpec(SCOPE_PROJECT, 0.25, 7),
    "name": SignalSpec(SCOPE_PROJECT, 0.15, 8),
}

#: Signal types whose value the *extractor* supplies (the LLM's ``scope_hint``).
#: Kept as its own name so a hint is distinguishable from a real observation in
#: the audit trail.
SIGNAL_HINT = "content_anchor"

#: Upper bound on a stored raw signal value. The raw value exists for audit and
#: for a human to recognise; it is never what identity is computed from, so a
#: value longer than this is truncated rather than kept whole.
MAX_SIGNAL_VALUE_CHARS = 512

#: Signal types that carry credentials when written as a URL. Their raw value is
#: reduced to the part that can be shown safely (see :func:`safe_raw_value`).
_URL_LIKE_SIGNALS = frozenset({"git_remote", "share_link"})


def safe_raw_value(signal_type: str, value: str) -> str:
    """Return the raw signal value in the form that is safe to store.

    A signal's *raw* value is kept for the audit trail — a human reading
    ``scope_signal`` should recognise ``git@github.com:acme/api.git`` for what it
    is — but a remote URL is routinely written with a credential in it, and in
    more places than the obvious one: ``https://user:token@host/o/r`` carries a
    password in its userinfo, and ``https://host/o/r?access_token=…`` carries one
    in its query string. The identity is computed from the *normalised* value
    (which already drops both), so the raw copy is stripped here too: a token
    that reached this table would sit in the store, and in every backup of it,
    for no benefit at all.

    Non-URL signals are returned as-is (bounded), because a filesystem path or a
    document title has nothing to strip and mangling it would make the audit
    trail lie.

    Args:
        signal_type: The signal's type (a key of :data:`SIGNAL_SPECS`).
        value: The raw value as the caller supplied it.

    Returns:
        The storable raw value, at most :data:`MAX_SIGNAL_VALUE_CHARS` long.
    """
    text = str(value or "").strip()
    if signal_type in _URL_LIKE_SIGNALS:
        text = text.split("#", 1)[0].split("?", 1)[0]
        # Drop userinfo (`user:password@` / `token@`) without touching the host.
        scheme = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text)
        if scheme is not None:
            rest = text[scheme.end():]
            slash = rest.find("/")
            authority = rest if slash == -1 else rest[:slash]
            if "@" in authority:
                text = (
                    scheme.group(0)
                    + authority.rsplit("@", 1)[1]
                    + rest[len(authority):]
                )
    return text[:MAX_SIGNAL_VALUE_CHARS]


# -- conditions ---------------------------------------------------------------

#: Condition keys the design names, kept for documentation and for the extraction
#: prompt. Any other key is still accepted (the schema does not constrain them),
#: because an office deployment's useful dimensions are its own.
KNOWN_CONDITION_KEYS: Tuple[str, ...] = (
    "language",
    "doc_type",
    "audience",
    "industry",
    "stage",
    "tool",
    "vcs",
)

#: Shape an accepted condition key must have. Deliberately narrow: a condition
#: key is a small machine-readable dimension, not a sentence, and a free-form key
#: would let two deployments store the same dimension under different names.
_CONDITION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Upper bound on conditions stored per fact. Each one is a row and a ranking
#: term; an unbounded list is a way to make a single fact unfalsifiable.
MAX_CONDITIONS = 8

#: Longest accepted condition value.
MAX_CONDITION_VALUE_CHARS = 64


# -- normalisation ------------------------------------------------------------


def normalize_path(value: str) -> str:
    """Return the identity form of a filesystem path signal.

    Separators are unified, ``~`` is expanded, redundant segments are dropped and
    the result is case-folded. Case-folding is deliberate even on case-sensitive
    filesystems: the same project reached through a differently-cased spelling is
    the same project, and the cost of being wrong (two scopes merged) is far
    lower here than the cost of a permanent duplicate (the same fact stored twice
    under two scopes that never match).

    Args:
        value: A raw path.

    Returns:
        A normalised, forward-slashed, lower-cased path; ``""`` for a blank input.
    """
    import os

    text = (value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    text = os.path.expanduser(text)
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    # Manual normalisation instead of os.path.normpath: normpath is
    # platform-specific (it would keep a Windows drive letter on Windows and
    # treat it as a segment on POSIX), and the stored identity must not depend on
    # which machine happened to write it.
    parts: List[str] = []
    absolute = text.startswith("/")
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    joined = "/".join(parts)
    if absolute:
        joined = "/" + joined
    return joined.casefold()


def normalize_remote(value: str) -> str:
    """Return the identity form of a git remote URL.

    ``git@github.com:Team/Repo.git``, ``https://user:token@github.com/team/repo``
    and ``ssh://git@github.com/team/repo/`` all normalise to
    ``github.com/team/repo``. Credentials are dropped rather than kept — a remote
    URL with an embedded token must never be stored as a signal value — and the
    host is kept, because ``team/repo`` on two hosts is two projects.

    Args:
        value: A raw remote URL (scp-like or URL form).

    Returns:
        ``host/path`` lower-cased with no scheme, credentials, port or ``.git``
        suffix; ``""`` when nothing usable is left.
    """
    text = (value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    # scp-like syntax: [user@]host:path
    scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", text)
    if scp and "://" not in text:
        host, path = scp.group(1), scp.group(2)
    else:
        rest = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
        rest = rest.split("#", 1)[0].split("?", 1)[0]
        if "@" in rest.split("/", 1)[0]:
            rest = rest.split("@", 1)[1]
        host, _, path = rest.partition("/")
        host = host.split(":", 1)[0]  # drop a port
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    if not host and not path:
        return ""
    return f"{host}/{path}".casefold() if path else host.casefold()


def normalize_name(value: str) -> str:
    """Return the identity form of a free-form name or title.

    Whitespace is collapsed, surrounding quotes dropped, separators unified and
    the result case-folded, so a document titled ``"Q3   Report"`` and
    ``q3 report`` are one name.
    """
    text = (value or "").strip().strip('"').strip("'")
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


#: Normaliser per signal type. A signal type missing here is either handled by
#: :func:`normalize_signal`'s default or rejected as unknown.
_NORMALIZERS = {
    "path": normalize_path,
    "folder_path": normalize_path,
    "git_root": normalize_path,
    "git_remote": normalize_remote,
    "share_link": normalize_remote,
    "org_domain": normalize_name,
    "package": normalize_name,
    "doc_id": normalize_name,
    "folder_id": normalize_name,
    "email_thread": normalize_name,
    "doc_title": normalize_name,
    "project_code": normalize_name,
    "participants": normalize_name,
}


def normalize_signal(signal_type: str, value: str) -> str:
    """Return the identity form of one signal value.

    Args:
        signal_type: A key of :data:`SIGNAL_SPECS`.
        value: The raw signal value.

    Returns:
        The normalised value (``""`` when the input carries nothing usable).
    """
    normalizer = _NORMALIZERS.get(signal_type)
    if normalizer is not None:
        return normalizer(value)
    if signal_type.startswith("explicit_"):
        return normalize_name(value)
    return normalize_name(value)


def canonical_name_for(signal_type: str, normalized: str) -> str:
    """Return the scope name a signal would create.

    A fingerprint-like signal names the scope by its normalised value (a remote,
    a path, a document id): those are unique by construction, so two repositories
    that happen to share a basename cannot collide. An explicit tag uses the
    user's own text — the user owns that namespace, and a name they recognise is
    the whole point of writing it down.

    Args:
        signal_type: A key of :data:`SIGNAL_SPECS`.
        normalized: The signal's normalised value.

    Returns:
        A non-empty canonical name, or ``""`` when the value is empty.
    """
    if not normalized:
        return ""
    if signal_type.startswith("explicit_"):
        return normalized
    return normalized


def display_name_for(signal_type: str, normalized: str) -> str:
    """Return a short human label for a scope created from this signal.

    Used for the injected digest's block labels (``[当前项目]`` carries the
    repository's own name, not its remote URL).

    Args:
        signal_type: A key of :data:`SIGNAL_SPECS`.
        normalized: The signal's normalised value.

    Returns:
        The last meaningful segment of the value, or the value itself.
    """
    if not normalized:
        return ""
    if signal_type.startswith("explicit_"):
        return normalized
    trimmed = normalized.rstrip("/")
    tail = trimmed.replace("\\", "/").rsplit("/", 1)[-1]
    return tail or trimmed


def reliability_of(signal_type: str) -> float:
    """Return the evidence weight of a signal type (0.0 when unknown)."""
    spec = SIGNAL_SPECS.get(signal_type)
    return spec.reliability if spec is not None else 0.0


@dataclass(frozen=True)
class ScopeSignal:
    """One normalised observation about the current context.

    Attributes:
        signal_type: The kind of observation (a key of :data:`SIGNAL_SPECS`).
        value: The raw value, kept only for audit and for the user to recognise.
        normalized: The identity form actually compared and stored.
        scope_type: The scope type this signal can identify.
        reliability: Its evidence weight.
        identity_rank: Preference among signals describing the same level.
    """

    signal_type: str
    value: str
    normalized: str
    scope_type: str
    reliability: float
    identity_rank: int

    def display_name(self) -> str:
        """Return the short human label for a scope created from this signal."""
        return display_name_for(self.signal_type, self.normalized)


def make_signal(signal_type: str, value: str) -> Optional[ScopeSignal]:
    """Build a :class:`ScopeSignal`, or ``None`` when it carries no evidence.

    Args:
        signal_type: The candidate signal type.
        value: The raw value.

    Returns:
        The normalised signal, or ``None`` for an unknown signal type or a value
        that normalises to nothing. Returning ``None`` rather than raising is
        deliberate: a host may send a signal this version does not know yet, and
        that must degrade to "less evidence", never to a failed write.
    """
    spec = SIGNAL_SPECS.get((signal_type or "").strip())
    if spec is None:
        if signal_type:
            logger.debug("Ignoring unknown scope signal type %r", signal_type)
        return None
    normalized = normalize_signal(signal_type, value)
    if not normalized:
        return None
    return ScopeSignal(
        signal_type=signal_type,
        # The raw copy is what a human reads in the audit trail, so it is stored
        # in the safe form (no URL credentials) and bounded.
        value=safe_raw_value(signal_type, value),
        normalized=normalized,
        scope_type=spec.scope_type,
        reliability=spec.reliability,
        identity_rank=spec.identity_rank,
    )


# -- conditions ---------------------------------------------------------------


def normalize_condition(key: str, value: str) -> Optional[Tuple[str, str]]:
    """Normalise one ``(key, value)`` condition pair.

    Args:
        key: The condition key (``language``, ``doc_type``, ...).
        value: The condition value.

    Returns:
        The normalised pair, or ``None`` when the key is not key-shaped or the
        value is empty after trimming. An unusable condition is dropped rather
        than repaired: a condition nobody can match is worse than no condition,
        because it silently narrows the fact's reach.
    """
    clean_key = (key or "").strip().casefold()
    if not _CONDITION_KEY_RE.match(clean_key):
        return None
    clean_value = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    if not clean_value:
        return None
    return (clean_key, clean_value[:MAX_CONDITION_VALUE_CHARS])


def normalize_conditions(pairs: Iterable) -> Tuple[Tuple[str, str], ...]:
    """Normalise a collection of conditions, dropping unusable ones.

    Accepts ``{"key": ..., "value": ...}`` mappings (the extractor's shape) and
    ``(key, value)`` sequences, so both the LLM payload and an internal call site
    can use it unchanged.

    Args:
        pairs: Condition mappings or pairs.

    Returns:
        A deduplicated tuple of ``(key, value)`` pairs, capped at
        :data:`MAX_CONDITIONS`, in a deterministic order.
    """
    out: List[Tuple[str, str]] = []
    for entry in pairs or ():
        pair: Optional[Tuple[str, str]] = None
        if isinstance(entry, Mapping):
            pair = normalize_condition(
                str(entry.get("key", "")), str(entry.get("value", ""))
            )
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            pair = normalize_condition(str(entry[0]), str(entry[1]))
        if pair is None or pair in out:
            continue
        out.append(pair)
        if len(out) >= MAX_CONDITIONS:
            break
    out.sort()
    return tuple(out)


def conditions_from_mapping(mapping: Mapping) -> Tuple[Tuple[str, str], ...]:
    """Normalise a ``{key: value}`` condition mapping."""
    return normalize_conditions(list(mapping.items())) if mapping else ()


# -- the context --------------------------------------------------------------


@dataclass(frozen=True)
class ChainLevel:
    """One level of the specificity chain a context resolves to.

    Attributes:
        scope_type: The scope type at this level.
        canonical_name: The name a scope at this level would carry.
        signals: Every signal observed for this level, most identifying first.
        confidence: The best evidence behind the level.
        display_name: Short human label for the block headings.
    """

    scope_type: str
    canonical_name: str
    signals: Tuple[ScopeSignal, ...]
    confidence: float
    display_name: str


@dataclass(frozen=True)
class ScopeContext:
    """Where the current session is, as far as the store can tell.

    Attributes:
        user_id: Owner the context belongs to. Resolution is per user: two users
            sharing a git remote still have separate scope trees.
        signals: Normalised signals, in the order they were supplied.
        conditions: Normalised ``(key, value)`` conditions of the *current*
            context, used to score condition-bearing facts.
        session_id: Session the context came from (provenance and audit).
        phase: The explicitly declared phase, when the context names one. Kept
            separately because the write path needs it to decide whether a
            different value under a single-valued key is an evolution or a
            contradiction.
    """

    user_id: str
    signals: Tuple[ScopeSignal, ...] = ()
    conditions: Tuple[Tuple[str, str], ...] = ()
    session_id: str = ""
    phase: str = ""
    _chain: Optional[Tuple[ChainLevel, ...]] = field(default=None, repr=False, compare=False)

    def chain(self) -> Tuple[ChainLevel, ...]:
        """Return the specificity chain, most general level first.

        Levels are grouped by scope type and ordered by
        :data:`SCOPE_DEPTH`, so a parent always exists before the child that
        references it. A type with no signal is absent from the chain rather than
        represented by a guess.
        """
        if self._chain is not None:
            return self._chain
        return build_chain(self.signals)

    def condition_map(self) -> Dict[str, List[str]]:
        """Return the conditions as a ``{key: [values]}`` mapping."""
        out: Dict[str, List[str]] = {}
        for key, value in self.conditions:
            out.setdefault(key, []).append(value)
        return out

    def primary_signal(self) -> Optional[ScopeSignal]:
        """Return the signal furthest down the chain (the most specific one)."""
        chain = self.chain()
        return chain[-1].signals[0] if chain else None


def build_chain(signals: Sequence[ScopeSignal]) -> Tuple[ChainLevel, ...]:
    """Group signals into ordered, per-type chain levels.

    Args:
        signals: Normalised signals.

    Returns:
        One :class:`ChainLevel` per scope type present, ordered most general
        first. Within a level the signals are ordered by identity rank, so
        ``level.signals[0]`` is the one that names a scope created from it.
    """
    grouped: Dict[str, List[ScopeSignal]] = {}
    for signal in signals:
        grouped.setdefault(signal.scope_type, []).append(signal)
    levels: List[ChainLevel] = []
    for scope_type, members in grouped.items():
        members.sort(key=lambda s: (s.identity_rank, -s.reliability, s.normalized))
        head = members[0]
        levels.append(
            ChainLevel(
                scope_type=scope_type,
                canonical_name=canonical_name_for(head.signal_type, head.normalized),
                signals=tuple(members),
                confidence=max(s.reliability for s in members),
                display_name=head.display_name(),
            )
        )
    levels.sort(key=lambda level: SCOPE_DEPTH.get(level.scope_type, 99))
    return tuple(levels)


def context_from_payload(
    payload: Optional[Mapping],
    user_id: str,
    session_id: str = "",
) -> Optional[ScopeContext]:
    """Build a :class:`ScopeContext` from the wire shape a host sends.

    The accepted shape is deliberately tolerant — the dsh side evolves on its own
    release cadence, and a context the store refuses to parse would silently
    disable scope awareness for a whole deployment::

        {
          "signals": {"git_remote": "git@github.com:o/r.git", "path": "D:/x"},
          "conditions": {"language": "typescript"},
          "phase": "draft"
        }

    ``signals`` may equally be a list of ``{"type", "value"}`` mappings, and
    ``conditions`` a list of ``{"key", "value"}`` mappings.

    Args:
        payload: The raw payload, or ``None``.
        user_id: Owner of the context.
        session_id: Session the context came from.

    Returns:
        The parsed context, or ``None`` when the payload carries no usable
        evidence at all (in which case callers keep their existing, global
        behaviour rather than inventing a scope).
    """
    if not payload or not isinstance(payload, Mapping):
        return None

    signals: List[ScopeSignal] = []
    raw_signals = payload.get("signals")
    if isinstance(raw_signals, Mapping):
        candidates = list(raw_signals.items())
    elif isinstance(raw_signals, (list, tuple)):
        candidates = [
            (str(item.get("type", "")), str(item.get("value", "")))
            for item in raw_signals
            if isinstance(item, Mapping)
        ]
    else:
        candidates = []
    for signal_type, value in candidates:
        signal = make_signal(str(signal_type), str(value))
        if signal is not None and signal not in signals:
            signals.append(signal)

    raw_conditions = payload.get("conditions")
    if isinstance(raw_conditions, Mapping):
        conditions = conditions_from_mapping(raw_conditions)
    elif isinstance(raw_conditions, (list, tuple)):
        conditions = normalize_conditions(raw_conditions)
    else:
        conditions = ()

    # An LLM's `scope_hint` is a content anchor: it can corroborate a resolution
    # or accumulate in the unresolved queue, but it can never bind on its own
    # (0.25 reliability is far below every auto-bind threshold). The two reserved
    # level markers are not names at all — see RESERVED_SCOPE_HINTS — so they are
    # recognised and dropped here rather than turned into a content anchor.
    hint = payload.get("scope_hint")
    if isinstance(hint, str) and hint.strip():
        normalized_hint = hint.strip().casefold()
        if normalized_hint not in RESERVED_SCOPE_HINTS:
            signal = make_signal(SIGNAL_HINT, hint)
            if signal is not None:
                signals.append(signal)

    phase = ""
    for signal in signals:
        if signal.scope_type == SCOPE_PHASE:
            phase = signal.display_name()
            break
    if phase:
        conditions = tuple(
            sorted(set(conditions) | {("stage", phase)})
        )[:MAX_CONDITIONS]

    if not signals and not conditions:
        return None
    return ScopeContext(
        user_id=user_id,
        signals=tuple(signals),
        conditions=conditions,
        session_id=session_id,
        phase=phase,
        _chain=build_chain(signals),
    )


def context_from_signals(
    user_id: str,
    signals: Mapping[str, str],
    conditions: Optional[Iterable] = None,
    session_id: str = "",
) -> Optional[ScopeContext]:
    """Build a context from a plain ``{signal_type: value}`` mapping.

    Convenience wrapper for callers that already hold a mapping (the RPC layer,
    tests); see :func:`context_from_payload` for the authoritative shape.

    Args:
        user_id: Owner of the context.
        signals: Signal type to raw value.
        conditions: Optional conditions in any accepted shape.
        session_id: Session the context came from.

    Returns:
        The context, or ``None`` when nothing usable was supplied.
    """
    payload: Dict[str, object] = {"signals": dict(signals or {})}
    if conditions is not None:
        payload["conditions"] = list(conditions)
    return context_from_payload(payload, user_id=user_id, session_id=session_id)
