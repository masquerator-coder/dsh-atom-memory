"""Generate a user's ``summary`` from active atomic facts.

``summary`` is a *derived view* over the authoritative facts table, rendered at
two depths from one implementation:

- **compact** (``detail=False``) — what the dsh host freezes into the session
  system prompt. Facts are grouped by memory type, semantic attributes collapse
  to ``predicate: value`` one-liners and multi-valued ones fold into a single
  line, so the whole picture fits a small token budget. No ``fact_id``: the long
  UUIDs cost more tokens than they carry information for the model, and every
  fact stays addressable through ``recall`` and the settings editor.
- **detail** (``detail=True``) — the full list, one bullet per fact with its
  ``fact_id``, used by the ``memory_summary_detail`` tool and the settings modal.

Ordering blends **importance and recency** into one score (see
:func:`_blend`): a fact's ``importance`` only counts when the extractor actually
supplied a signal; the neutral default (:data:`NEUTRAL_SCORE`) means "unknown"
and falls back to the type's default rank
(:func:`~atom_memory.models.default_importance`). Without that fallback every
fact ties at 0.5 and the order degenerates to plain recency.

Recency is a real dimension rather than a tie-break, because a memory view is
read at whatever budget the user configured: when the budget is tight, whatever
the ordering puts last is what gets dropped. Ranking by importance alone would
therefore always sacrifice the newest material — a fact recorded minutes ago
would lose to durable knowledge from months back — which is exactly the wrong
trade for "what is going on right now". The blend keeps importance primary
while letting a sufficiently fresh fact outrank a stale one.

The budget is spent **globally best-first** (:func:`_select`), not
section-by-section: trimming a whole low-priority section before touching any
line of a high-priority one would discard a top-scoring fresh fact merely
because it lives in the section that sorts last.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Sequence, Tuple

from .config import MemConfig
from .context import GLOBAL_SCOPE_ID
from .db import now_ms, recency_credit
from .models import (
    NEUTRAL_SCORE,
    PRED_EVENT,
    TYPE_SEMANTIC,
    default_importance,
)
from .retriever import CHARS_PER_TOKEN, estimate_tokens, token_cost
from .reinforce import adjust, effective_importance
from .scope import ScopeStore, resolution_for
from .validator import PREFERENCE_PREDICATES

# Maximum characters of a rendered *content line* in the compact digest.
#
# This is the per-line cap, and it is what keeps the view 精炼 (concise): every
# line the model reads — a knowledge body, an attribute ``predicate: value``, a
# folded preference list — is bounded, so one runaway value can never crowd
# several other memories out of the token budget, and every entry is forced to
# stay a recognisable headline rather than a paragraph.
#
# The full text always stays in the store and is reachable through ``recall``
# (which returns ``content``) and the detail depth, so clipping here costs the
# model nothing but the padding.
_MAX_COMPACT_LINE_CHARS = 80

# Maximum characters of one value *inside* a folded line, applied before the
# values are joined. Without it a single long value would consume the whole line
# and the cap would silently hide every sibling value; clipping each value first
# keeps them all visible (each shorter) and the line cap then bounds the total.
_MAX_FOLDED_VALUE_CHARS = 40

# Maximum characters of a knowledge body rendered on the detail depth's folded
# ``> 知识内容`` sub-line.
_DETAIL_CONTENT_CHARS = 120

# Maximum characters of a single *field* (subject / predicate / object) on the
# detail depth. The detail depth exists so a human can locate a fact by its
# ``fact_id`` and edit it, so its structure is preserved and only the payloads of
# its fields are clipped — a long field must not push the ``fact_id`` out of view.
_MAX_DETAIL_FIELD_CHARS = 120

# -- ranking ------------------------------------------------------------------
#
# One score per fact decides both the render order and (through it) what a tight
# token budget keeps. The two weights are deliberately explicit module constants
# rather than magic numbers inline: they are the one knob that trades "durable
# knowledge stays" against "what just happened gets in".
#
# Importance is primary (0.7) because the type defaults already encode what stays
# valuable longest; recency (0.3) is strong enough to promote a fresh fact past
# materially staler material without letting recency alone decide the view.
_IMPORTANCE_WEIGHT = 0.7
_RECENCY_WEIGHT = 0.3

# Age at which a fact's recency credit halves, measured *relative to the newest
# fact in the set* rather than against the wall clock, so the ranking is
# deterministic (no clock dependency, no test flakiness) and still means what it
# should: "the newest thing I know" always gets full recency credit, and
# everything else is discounted by how much older it is than that.
#
# The name says "seconds" but the value is in **milliseconds**, matching the
# timestamps it divides (every `*_at` column is ms). It is spelled that way for
# backward compatibility; db.recency_credit is unit-agnostic and takes whatever
# the caller passes, so the only thing that must hold is that this and the age
# share a unit.
#
# The anchor deliberately differs from the retriever's: this view renders one
# user's whole memory, so the newest memory is exactly the right definition of
# "now". The retriever sees a handful of query-matched candidates and caps its
# reference offset with a window instead, because one very fresh candidate would
# otherwise make every other candidate look ancient. Only the anchor and the
# half-life differ; the decay shape is shared (db.recency_credit).
_RECENCY_HALF_LIFE_SECONDS = 14 * 24 * 60 * 60 * 1000

# Section titles in rendering order. Sections present in the data always render
# (even when a budget only allows a heading), so the reader can see *which kinds*
# of memory exist — the failing this view used to have.
_SECTION_TITLES = {
    "decision_rule": "决策规则",
    "lesson": "教训",
    "sop": "流程（SOP）",
    "procedural": "流程",
    "preference": "偏好",
    "task": "待办",
    "attribute": "属性",
    "few_shot": "示例",
    "episodic": "事件",
}

_SECTION_ORDER = [
    "decision_rule",
    "lesson",
    "sop",
    "procedural",
    "preference",
    "task",
    "attribute",
    "few_shot",
    "episodic",
]

_EMPTY_NOTICE = "_暂无持久化的原子记忆。_ (No active atomic facts yet.)"

# -- the work-overview head (the compact depth's leading sections) --------------
#
# The compact depth used to be nothing but a type-grouped list of atomic facts,
# and that answered the wrong question. A session opening with ``技术栈: Flask``
# and ``内置斜杠命令: 仅 /compact`` learned a handful of disconnected details and
# still could not say what had been *worked on* — or that anything had been.
#
# So the compact depth now leads with two sections that do answer it:
#
#   * the **work overview** — what has been worked on, per work unit, written out
#     of band by a model (see `overview.py` and `dsh/src/overview.ts`) and cached,
#     with a deterministic render as the fallback when nothing is cached;
#   * the **lookup guide** — how to get at the detail: which tools to call and
#     with what, including example queries built from this store's own labels.
#
# The type-grouped digest is still rendered, demoted to "参考明细" below both, and
# only while the budget allows. It is the honest fallback (it is the only view
# that cannot be wrong about the store) and it is what makes an empty cache
# degrade to *something* rather than nothing.
_OVERVIEW_TITLE = "以前做过的工作"
_RECALL_TITLE = "要了解细节"

#: Floor reserved for the overview section, in tokens. The overview is the reason
#: this view exists, so it is the last content given up under a tight budget — the
#: detail digest is sacrificed first, and a budget too small even for this floor
#: drops to the guide alone rather than to a fact list.
_OVERVIEW_MIN_TOKENS = 40

#: Cap on the overview text taken from the cache. A model-written overview is
#: unbounded by construction (it is prose), so it is clipped to this before it
#: can crowd out everything else. Generous enough for several work units.
_MAX_OVERVIEW_CHARS = 1600

#: Cap on the rendered lookup guide. It is a fixed block plus example queries;
#: this bounds the examples, not the tool wording.
_MAX_GUIDE_CHARS = 700

#: How many example queries the guide carries, drawn from the skeleton's own
#: work-unit labels and topic names. Two or three show the *shape* of a useful
#: query; more turns a guide into a list the model skims.
_GUIDE_EXAMPLE_LIMIT = 4

#: The lookup guide's fixed text, in decreasing order of usefulness. Each line
#: names one tool and what it reaches, because the failure this section exists to
#: fix is a model that knows memory exists but not which call reaches which depth
#: of it. Ordered so the guide can be truncated from the tail under a tight
#: budget without losing the mechanism for the convenience.
_RECALL_GUIDE_LINES = [
    "- 细节检索：memory_recall（名词短语最佳）；全文用 memory_get factId=…",
    "- 完整清单：memory_summary_detail（含 fact_id，用于定位与编辑）",
    "- 相关范围：memory_scope action=list / resolve",
    "- 近期变动：memory_overview action=changes",
]

# Marker opening a compact section label (``## 决策规则``). Held as a constant
# because it is measured as well as rendered: `_select` accounts for the label's
# cost while it trims the artifact, so a marker that only existed inside
# `_render_body` would let the budget under-count every surviving section — by
# one token per label, which is exactly the kind of leak the hard cap forbids.
#
# Two hashes, not one: the dsh host prefixes every injected line with ``| ``
# (`dsh/src/memory-data.ts`) so that no stored line can occupy column zero. That
# prefix costs the artifact its first-character hierarchy — under it a label
# (``| # …``) and a bullet (``| - …``) differ in one character out of two, which
# reads as one flat list. Widening the label to ``## `` restores the distinction
# *after* the prefix, at one extra character per label (a Chinese title pair, so
# the estimate does not move). The marker must stay a hash rather than a longer
# banner: it is paid for once per section on every request of every session.
_COMPACT_LABEL_MARKER = "## "

# Separator between two scope blocks, replacing the blank line the single-block
# artifact uses.
#
# A blank line does not survive injection: the host prefixes it too, so the block
# boundary arrives as ``| `` — a line that looks like a content line carrying
# nothing. A single colon is the cheapest mark that reads as structure (1 token,
# measured) and, under the ``| `` prefix, gives the four line kinds four distinct
# first characters: ``[`` block heading, ``#`` section label, ``-`` fact, ``:``
# separator.
_BLOCK_SEPARATOR = ":"

# -- scope blocks (design §6.7) -----------------------------------------------
#
# With a scope context the compact digest is rendered as independent blocks —
# the current scope, its ancestors, its phases, the global rules and the
# condition-matching rules from elsewhere. Each block has its own budget, so a
# large project memory cannot crowd the global rules out of the prompt, and the
# headings tell the model *which* level a rule belongs to (which is what makes
# "the project overrides the company rule" readable instead of contradictory).
#
# Blocks are ordered most-specific-first, and the global block's minimum is
# reserved before anything else is allocated: "全局规则始终包含，但可压缩" is only
# true if a big current-scope block cannot spend the reserve first.
_BLOCK_SHARE_CURRENT = 0.5
_BLOCK_SHARE_ANCESTOR = 0.25
_BLOCK_SHARE_GLOBAL = 0.3
_BLOCK_SHARE_PHASE = 0.25
_BLOCK_SHARE_CONDITION = 0.25

#: Floor for a rendered block: enough for its heading and at least one line. A
#: block that gets less than this is skipped rather than rendered as a bare
#: heading, which would cost tokens and say nothing.
_BLOCK_MIN_TOKENS = 24

#: Budget below which a squeezed block is dropped outright instead of being
#: re-rendered: a block that cannot hold even one line is a heading.
_BLOCK_DROP_TOKENS = 10

#: How many times the overflow is taken back from the blocks before the footer
#: and whole blocks are given up. Each pass at least halves one block's budget,
#: so this reaches the fixpoint of any realistic layout; the cap exists so a
#: pathological budget cannot make rendering loop.
_BLOCK_SHRINK_PASSES = 8

#: Passes allowed for the block heading / selection fixpoint (see
#: :func:`_assemble_blocks`). Two would do for any heading whose width changes by
#: a few characters; the third is slack so a pathological case settles rather
#: than exits mid-loop.
_BLOCK_HEADING_PASSES = 3

#: Short Chinese label per scope type, used in block headings.
_SCOPE_LABELS = {
    "global": "全局",
    "user": "用户",
    "org": "组织",
    "team": "团队",
    "client": "客户",
    "project": "项目",
    "series": "系列",
    "phase": "阶段",
    "document": "文档",
    "thread": "线程",
}


def generate_summary(
    conn: sqlite3.Connection,
    user_id: str,
    max_tokens: int = 1500,
    detail: bool = True,
    scope_context: Optional[Mapping] = None,
    config: Optional[MemConfig] = None,
    overview: Optional[str] = None,
    use_overview: bool = False,
) -> str:
    """Build the ``summary`` text for a user.

    The compact depth renders three things, in this order:

    1. **the work overview** — what has been worked on, per work unit. ``overview``
       carries the cached, model-written text when the caller has one; otherwise
       it is aggregated and rendered deterministically (:func:`_fallback_overview`).
       Either way this is where the view's budget goes first.
    2. **the lookup guide** — which tools reach the detail, with example queries
       drawn from this store (:func:`_render_guide`).
    3. **the reference detail** — the type-grouped fact digest, demoted and
       rendered only while the budget still allows.

    Args:
        conn: The SQLite connection.
        user_id: The user whose memory is rendered (hard isolation scope).
        max_tokens: Upper bound on the estimated token count of the body.
        detail: ``True`` renders the full fact list with ``fact_id`` references;
            ``False`` renders the compact digest injected into the session system
            prompt.
        scope_context: The session context payload. When given (and scope
            awareness is on), the compact depth's *detail* section is rendered as
            scope blocks — current scope, ancestors, phases, global rules,
            condition-matching rules — instead of one flat digest. Ignored by the
            detail depth, which exists to locate and edit facts and therefore
            lists everything. It also orders the overview's work units (the
            session's own project first) without filtering them.
        config: Configuration carrying the scope thresholds and the master
            switch. ``None`` uses library defaults.
        overview: Cached overview text to lead with. ``None`` means "none
            cached" and the deterministic fallback is rendered instead — this
            parameter is never a request to *generate* one, because generating is
            a model call and this function runs on the prompt-freeze path.
        use_overview: Whether to render the overview head at all. Defaults to
            ``False`` so an existing caller's output is unchanged; the injection
            path and ``memory_summary`` pass ``True``.

    Returns:
        The rendered markdown string. An empty (but non-blank) notice is returned
        when the user has no active facts, in both depths.
    """
    buckets = _collect(conn, user_id)
    if not buckets:
        return _EMPTY_NOTICE if not detail else (
            f"# 记忆 (Memory) — {user_id}\n\n{_EMPTY_NOTICE}\n"
        )

    if detail:
        return _render_detail(buckets, user_id, max_tokens)
    cfg = config or MemConfig()
    if use_overview:
        return _render_with_overview(
            conn, user_id, buckets, max_tokens, scope_context, cfg, overview
        )
    if scope_context is not None and cfg.scope_aware:
        scoped = _render_scoped(conn, user_id, max_tokens, scope_context, cfg)
        if scoped is not None:
            return scoped
    return _render_compact(buckets, max_tokens)


# -- the overview head ---------------------------------------------------------


def _render_with_overview(
    conn: sqlite3.Connection,
    user_id: str,
    buckets: dict,
    max_tokens: int,
    scope_context: Optional[Mapping],
    config: MemConfig,
    overview: Optional[str],
) -> str:
    """Render the compact depth as overview + guide + reference detail.

    The budget is spent in that order, and the ordering is the whole design:

    * the overview gets ``_OVERVIEW_MIN_TOKENS`` reserved before anything else,
      because it is the section that answers "what has been done here";
    * the detail digest is what shrinks — it goes through the same
      :func:`_select` it always did, at whatever budget is left, so the
      most-important-and-most-recent material still survives a squeeze;
    * if even the reserve cannot be honoured, the overview is clipped rather than
      dropped and the detail disappears entirely: half an overview is worth more
      here than a fact list, which is the whole premise of the change.

    Args:
        conn: Open connection.
        user_id: Owner whose memory is rendered.
        buckets: ``section -> [fact, ...]``, already score-sorted.
        max_tokens: Budget for the whole artifact.
        scope_context: Session context, for unit ordering and the scoped detail.
        config: Configuration.
        overview: Cached overview text, or ``None``.

    Returns:
        The rendered artifact, clipped to ``max_tokens``. Never ``""`` unless the
        budget cannot even hold one line: the guide is the last thing to go,
        because a model that cannot see the overview can still be told how to
        look one up.
    """
    guide = _render_guide(conn, user_id, scope_context, config)
    guide_tokens = estimate_tokens(
        f"{_COMPACT_LABEL_MARKER}{_RECALL_TITLE}\n{guide}"
    ) if guide else 0

    if overview:
        overview_text = _clip_overview(overview)
    else:
        # Built to fit rather than clipped afterwards: the fallback is dense
        # Chinese, and shrinking it post-hoc cost it its structure. The reserve
        # left for the guide is capped, so at a mid budget the overview is not
        # squeezed down to accommodate a hint block that costs more than it does.
        reserve = min(guide_tokens, max(0, max_tokens // 3))
        allowance = max_tokens - reserve - 6
        overview_text = _fallback_overview(
            conn, user_id, scope_context, config, max_tokens=max(0, allowance)
        )

    head = _compose_head(overview_text, guide, max_tokens)
    head_tokens = estimate_tokens(head)

    # The detail digest gets what is left, and only if that is worth rendering: a
    # heading plus one line is the minimum at which a section is information
    # rather than decoration.
    remaining = max_tokens - head_tokens
    detail_text = ""
    if remaining >= _OVERVIEW_MIN_TOKENS:
        detail_text = _render_detail_section(
            conn, user_id, buckets, remaining, scope_context, config
        )
    return head + "\n" + detail_text if detail_text else head


def _compose_head(overview_text: str, guide: str, max_tokens: int) -> str:
    """Assemble the overview and the guide within ``max_tokens``.

    The priority order is **overview first, guide second, and within the guide
    the examples last**. That is the reverse of what a naive "keep the short
    fixed block" rule produces, and the earlier version had it backwards: at a
    mid budget it emitted the static hint block and *no* overview, which is the
    one outcome this whole change exists to prevent. A model that reads a guide
    tells you it has memory; a model that reads an overview knows what was done.

    Degradation is therefore staged, and each stage is a whole unit rather than a
    character slice, so nothing is ever half-said:

    1. overview + full guide (with examples)
    2. overview + guide without its example line
    3. overview + the tool lines only (the examples are the first thing dropped:
       they are a convenience, the tool names are the mechanism)
    4. clipped overview + tool lines
    5. tool lines alone, if the overview has been clipped to nothing

    Every stage is measured, so ``max_tokens`` stays a hard cap on the returned
    artifact at any budget — including one too small for the guide, where the
    guide is itself clipped line by line (and, if need be, cut to the first line
    plus an ellipsis) rather than returned over budget.

    Args:
        overview_text: The overview body, already character-clipped.
        guide: The rendered guide, or ``""``.
        max_tokens: Budget for the assembled head.

    Returns:
        The largest head that fits, or ``""`` when not even one line does.
    """
    guide_lines = guide.splitlines() if guide else []
    # The example line is optional and is dropped first: it is a convenience,
    # whereas the tool names are the mechanism.
    tool_lines = [line for line in guide_lines if not line.startswith("- 例：")]
    has_examples = len(tool_lines) != len(guide_lines)

    candidates: List[str] = []
    if overview_text:
        # Most informative first, and **the overview is never the thing that
        # gives way**: the guide shrinks from the tail (examples, then its last
        # tool lines) while the overview stays whole, and only a budget that
        # cannot hold both costs the overview its highlights, then its type
        # breakdown. The guide is short, static and re-derivable; the overview is
        # the answer this whole view was rebuilt to give, so an artifact that
        # dropped it to make room for a hint block would be exactly backwards.
        for lines in _guide_ladder(guide_lines, tool_lines, has_examples):
            block = _head_block(overview_text, lines)
            if block:
                candidates.append(block)
        for keep_chars in (600, 320, 160, 80):
            clipped = _clip(overview_text, keep_chars)
            if not clipped or clipped == overview_text:
                continue
            block = _head_block(clipped, tool_lines[:2])
            if block and block not in candidates:
                candidates.append(block)
    if tool_lines:
        candidates.append(_head_block("", tool_lines))
    candidates.append(_head_block("", tool_lines[:1]))

    for block in candidates:
        if block and estimate_tokens(block) <= max_tokens:
            # Candidates are ordered most-informative first, so the first that
            # fits wins; there is nothing to optimise beyond that.
            return block

    # Nothing fit whole. This is the only path that cuts inside a line, and it
    # exists so ``max_tokens`` holds even at a budget too small for one tool
    # line. ``estimate_tokens`` charges CJK one token per character, so a
    # character room of ``max_tokens`` minus the heading is never more than the
    # estimate will ask for.
    smallest = next((block for block in reversed(candidates) if block), "")
    if not smallest:
        return ""
    room = max_tokens - estimate_tokens(_RECALL_TITLE) - 2
    if room < 1:
        return ""
    keep = [
        line for line in smallest.splitlines()
        if not line.startswith(_COMPACT_LABEL_MARKER)
    ]
    clipped = _clip("\n".join(keep), room)
    if not clipped:
        return ""
    return f"{_COMPACT_LABEL_MARKER}{_RECALL_TITLE}\n{clipped}"


def _guide_ladder(
    guide_lines: Sequence[str],
    tool_lines: Sequence[str],
    has_examples: bool,
) -> List[List[str]]:
    """Return the guide's progressively shorter forms, longest first.

    Used to pair a whole overview with each form in turn, so the caller's
    most-informative-first scan naturally trades guide detail for keeping the
    overview instead of the other way round.
    """
    forms: List[List[str]] = []
    if has_examples:
        forms.append(list(guide_lines))
    for count in (len(tool_lines), 3, 2, 1):
        form = list(tool_lines[:count])
        if form and form not in forms:
            forms.append(form)
    return forms


def _head_block(overview_text: str, guide_lines: Sequence[str]) -> str:
    """Join an optional overview section and an optional guide section."""
    parts: List[str] = []
    if overview_text:
        parts.append(f"{_COMPACT_LABEL_MARKER}{_OVERVIEW_TITLE}\n{overview_text}")
    if guide_lines:
        parts.append(
            f"{_COMPACT_LABEL_MARKER}{_RECALL_TITLE}\n" + "\n".join(guide_lines)
        )
    return "\n".join(parts)


def _clip_overview(overview: str) -> str:
    """Normalise a cached overview and bound it to :data:`_MAX_OVERVIEW_CHARS`.

    Bounded rather than trusted: the text is model-written prose, so its length
    is not something this renderer controls, and an unbounded head would consume
    the entire budget before the detail section was ever considered. The window
    is applied per line with a marker so a clipped overview says so instead of
    just stopping.
    """
    text = overview.strip()
    if not text:
        return ""
    if len(text) <= _MAX_OVERVIEW_CHARS:
        return text
    kept: List[str] = []
    spent = 0
    for line in text.splitlines():
        if spent + len(line) > _MAX_OVERVIEW_CHARS - 12:
            kept.append("…（总览已裁剪）")
            break
        kept.append(line)
        spent += len(line) + 1
    return "\n".join(kept)


def _fallback_overview(
    conn: sqlite3.Connection,
    user_id: str,
    scope_context: Optional[Mapping],
    config: MemConfig,
    max_tokens: Optional[int] = None,
) -> str:
    """Render the deterministic overview used when no cached text exists.

    This is what a deployment that never runs the out-of-band job sees, and what
    a brand-new store sees before its first refresh. It is the skeleton rendered
    directly (see `overview.render_skeleton_overview`): terser than prose, but
    true, derived from the same grouping, and requiring no model call — which is
    the only kind of fallback a prompt-freeze path can afford.

    ``max_tokens`` is forwarded so the render is *built* to fit. Clipping
    afterwards was measurably worse: the fallback is dense Chinese, and a version
    rendered to a character cap then shrunk to a token budget lost its structure
    and came out as one severed line.

    A failure here returns ``""`` rather than raising: this runs while a session
    is being assembled, and a store whose aggregation is broken must still yield
    a usable prompt.
    """
    from .overview import build_overview_skeleton, render_skeleton_overview

    try:
        skeleton = build_overview_skeleton(
            conn, user_id, scope_context=scope_context, config=config
        )
        return render_skeleton_overview(
            skeleton, max_chars=_MAX_OVERVIEW_CHARS, max_tokens=max_tokens
        )
    except Exception:  # pragma: no cover - defensive: freeze must not fail
        import logging

        logging.getLogger(__name__).exception(
            "Failed to build fallback overview for %s", user_id
        )
        return ""


def _render_guide(
    conn: sqlite3.Connection,
    user_id: str,
    scope_context: Optional[Mapping],
    config: MemConfig,
) -> str:
    """Render the lookup guide: which calls reach the detail, and with what.

    The example queries are generated from this store's own labels (the work
    units' names and the topic vocabulary) rather than being generic, because a
    model that has just read an overview naming ``dsh-atom-memory`` can use
    ``memory_recall「dsh-atom-memory 决定」`` immediately, whereas
    ``memory_recall「<关键词>」`` leaves it to guess. The examples are omitted
    entirely when the store yields no labels — a template with an empty slot is
    worse than no example.
    """
    from .overview import build_overview_skeleton

    lines = list(_RECALL_GUIDE_LINES)
    examples: List[str] = []
    try:
        skeleton = build_overview_skeleton(
            conn, user_id, scope_context=scope_context, config=config,
            max_units=3, highlights_per_unit=0,
        )
        labels = [
            str(unit.get("label") or "")
            for unit in skeleton.get("units") or []
            if unit.get("label") and unit.get("type") != "global"
        ]
        for label in labels[:2]:
            examples.append(f"memory_recall「{label} 决定」")
            examples.append(f"memory_recall「{label} 待办」")
        for topic in (skeleton.get("topics") or [])[:1]:
            if topic.get("label"):
                examples.append(f"memory_recall「{topic['label']} 教训」")
    except Exception:  # pragma: no cover - defensive
        examples = []

    if examples:
        lines.append(
            "- 例：" + "、".join(examples[:_GUIDE_EXAMPLE_LIMIT])
        )
    text = "\n".join(lines)
    return _clip_text_block(text, _MAX_GUIDE_CHARS)


def _clip_text_block(text: str, limit: int) -> str:
    """Bound a multi-line block to ``limit`` characters, dropping whole lines.

    Line-wise rather than character-wise: the guide is a list, and half a line of
    tool guidance is worse than one line fewer.
    """
    if len(text) <= limit:
        return text
    kept: List[str] = []
    spent = 0
    for line in text.splitlines():
        if spent + len(line) > limit:
            break
        kept.append(line)
        spent += len(line) + 1
    return "\n".join(kept)


def _render_detail_section(
    conn: sqlite3.Connection,
    user_id: str,
    buckets: dict,
    max_tokens: int,
    scope_context: Optional[Mapping],
    config: MemConfig,
) -> str:
    """Render the demoted reference-detail section under a leftover budget.

    Reuses the scope-blocked renderer when a context is available and the flat
    digest otherwise — the same two paths the compact depth always used, so the
    detail section cannot drift from what it rendered before this change. It is
    only the *placement* that moved: the section now competes for a leftover
    budget instead of owning the whole one.

    Returns:
        The rendered detail, or ``""`` when the budget holds nothing worth
        showing.
    """
    if max_tokens <= 0 or not buckets:
        return ""
    if scope_context is not None and config.scope_aware:
        scoped = _render_scoped(conn, user_id, max_tokens, scope_context, config)
        if scoped:
            return scoped
    return _render_compact(buckets, max_tokens)


# -- scope-blocked rendering --------------------------------------------------


@dataclass
class _Block:
    """One rendered block of the scoped digest."""

    key: str
    title: str
    share: float
    facts: List[dict] = field(default_factory=list)


def _render_scoped(
    conn: sqlite3.Connection,
    user_id: str,
    max_tokens: int,
    scope_context: Mapping,
    config: MemConfig,
) -> Optional[str]:
    """Render the compact digest as scope blocks.

    Returns ``None`` when the context carries nothing scope-shaped after all (an
    unknown project), in which case the caller falls back to the flat digest —
    block headings naming a scope nobody resolved would be noise, not
    information.

    Args:
        conn: Open connection.
        user_id: Owner whose facts are rendered.
        max_tokens: Budget for the whole artifact, headings and footer included.
        scope_context: The context payload.
        config: Configuration carrying the scope thresholds.

    Returns:
        The rendered artifact, or ``None`` to fall back to the flat digest.
    """
    ctx, resolution = resolution_for(
        conn, config, scope_context, user_id=user_id, create=False
    )
    if ctx is None:
        return None
    store = ScopeStore(conn, config)
    view = store.view(resolution, ctx.condition_map())

    facts = _score_facts(conn, user_id)
    if not facts:
        return None
    scope_map = store.fact_scopes([f["fact_id"] for f in facts])
    cond_map = store.fact_conditions([f["fact_id"] for f in facts])

    blocks = _partition_blocks(
        facts, scope_map, cond_map, view, resolution, store
    )
    if not blocks:
        return None
    return _compose_blocks(blocks, max_tokens)


def _partition_blocks(
    facts: List[dict],
    scope_map: Mapping[str, Sequence[int]],
    cond_map: Mapping[str, Sequence[Tuple[str, str]]],
    view,
    resolution,
    store: ScopeStore,
) -> List[_Block]:
    """Sort facts into the design's blocks, in render order.

    Order is most-specific-first (current scope, then each ancestor nearest
    first, then the project's phases, then the global rules, then rules from
    other scopes that matched the current conditions). Facts bound to a scope
    that is neither visible nor condition-matched go to no block at all: the
    design's "其他项目参考默认不注入" — they stay reachable through ``memory_recall``
    instead of being paid for on every request.

    ``visible`` gates the phase branch as well as the condition branch. A *phase*
    is only a phase *of this project*: without that check another project's
    phase would render under a "阶段" heading inside this project's digest, which
    is cross-project pollution wearing a helpful-looking label.
    """
    current_id = view.current_id
    visible = set(view.visible_ids())
    blocks: dict = {}

    def _block(key: str, title: str, share: float) -> _Block:
        if key not in blocks:
            blocks[key] = _Block(key=key, title=title, share=share)
        return blocks[key]

    ancestors = dict(_ancestor_depths(store, current_id)) if current_id else {}
    current_type = store.scope_type(current_id) if current_id else "global"

    for fact in facts:
        scope_ids = [store.resolve_id(int(s)) for s in scope_map.get(fact["fact_id"], ())]
        conditions = cond_map.get(fact["fact_id"], ())

        target: Optional[_Block] = None
        for scope_id in scope_ids:
            if scope_id == GLOBAL_SCOPE_ID:
                continue
            if current_id is not None and scope_id == current_id:
                label = store.label(scope_id)
                target = _block(
                    "current",
                    f"当前{_SCOPE_LABELS.get(current_type, '作用域')}: {label}",
                    _BLOCK_SHARE_CURRENT,
                )
                break
            if scope_id in ancestors:
                label = store.label(scope_id)
                kind = store.scope_type(scope_id)
                target = _block(
                    f"ancestor:{scope_id}",
                    f"{_SCOPE_LABELS.get(kind, kind)}: {label}",
                    _BLOCK_SHARE_ANCESTOR,
                )
                break
            if store.scope_type(scope_id) == "phase" and scope_id in visible:
                label = store.label(scope_id)
                target = _block(
                    f"phase:{scope_id}", f"阶段: {label}", _BLOCK_SHARE_PHASE
                )
                break
        if target is None:
            if not scope_ids or all(s == GLOBAL_SCOPE_ID for s in scope_ids):
                target = _block("global", "全局规则", _BLOCK_SHARE_GLOBAL)
            elif conditions and view.condition_weight(conditions) > 0.0:
                target = _block(
                    "condition",
                    "条件规则: " + "、".join(
                        f"{key}={value}" for key, value in list(conditions)[:2]
                    ),
                    _BLOCK_SHARE_CONDITION,
                )
        if target is not None:
            target.facts.append(fact)

    ordered = ["current"]
    ordered.extend(
        sorted(
            (key for key in blocks if key.startswith("ancestor:")),
            key=lambda key: ancestors.get(int(key.split(":", 1)[1]), 99),
        )
    )
    ordered.extend(sorted(key for key in blocks if key.startswith("phase:")))
    ordered.extend(["global", "condition"])
    return [blocks[key] for key in ordered if key in blocks and blocks[key].facts]


def _ancestor_depths(store: ScopeStore, scope_id: Optional[int]) -> List[Tuple[int, int]]:
    """Return ``[(ancestor_id, distance), ...]`` for a scope, nearest first."""
    if scope_id is None or scope_id == GLOBAL_SCOPE_ID:
        return []
    from .scope import ancestor_distances

    distances = ancestor_distances(store.conn, scope_id)
    return sorted(
        ((sid, depth) for sid, depth in distances.items() if sid != GLOBAL_SCOPE_ID),
        key=lambda item: item[1],
    )


def _compose_blocks(blocks: List[_Block], max_tokens: int) -> str:
    """Render blocks under per-block budgets, most specific first.

    Each block starts with a share of the budget (the current scope gets the
    largest, the global rules a reserved share of their own), and the artifact is
    then checked against the cap as a whole. If it overshoots, the overflow is
    taken back from blocks in **reverse specificity order** — condition rules,
    phases, distant ancestors, then the current scope, then the global rules
    last. So the two properties the design asks for hold even at a budget too
    small for everything: the current scope's material is what survives a squeeze,
    and the global rules are the last thing given up.

    The cap is unconditional. Once nothing is left to shrink, the aggregate footer
    is dropped (it is the one line that carries no memory) and then whole blocks
    from that same order.

    Args:
        blocks: The blocks to render, in order.
        max_tokens: Total budget for the artifact.

    Returns:
        The rendered artifact, or ``""`` when nothing fits at all.
    """
    sections_by_block = {block.key: _titled_sections(block.facts) for block in blocks}
    active = [block for block in blocks if sections_by_block[block.key]]
    if not active:
        return ""
    budgets = {
        block.key: max(_BLOCK_MIN_TOKENS, int(max_tokens * block.share))
        for block in active
    }
    # Least specific first, global last: the order in which material is given up.
    shrink_order = [block.key for block in reversed(active) if block.key != "global"]
    if any(block.key == "global" for block in active):
        shrink_order.append("global")

    assembled = _assemble_blocks(active, sections_by_block, budgets, footer=False)
    for _attempt in range(_BLOCK_SHRINK_PASSES):
        if estimate_tokens(assembled) <= max_tokens:
            break
        victim = next(
            (key for key in shrink_order if key in budgets and budgets[key] > 0), None
        )
        if victim is None:
            break
        # Halve rather than subtract the overflow: the overflow is measured on the
        # whole artifact, so subtracting it from one block's budget would zero out
        # a block far cheaper than the overflow and give up more memory than the
        # cap requires. Halving converges in a few passes and gives up the least
        # material that fits.
        budgets[victim] //= 2
        if budgets[victim] < _BLOCK_DROP_TOKENS:
            budgets.pop(victim)
            active = [block for block in active if block.key != victim]
        assembled = _assemble_blocks(active, sections_by_block, budgets, footer=False)

    # The blocks are the content; the footer is bookkeeping. So the footer is what
    # goes when the two cannot both fit — shrinking further to make room for a
    # count would trade a memory for a statistic.
    body, footer = _assemble_blocks(active, sections_by_block, budgets, split=True)
    if footer:
        with_footer = _join_footer(body, footer)
        if estimate_tokens(with_footer) <= max_tokens:
            return with_footer
    if body and estimate_tokens(body) <= max_tokens:
        return body
    if estimate_tokens(assembled) <= max_tokens:
        return assembled

    # Last resort: whole blocks, in the same order.
    remaining_active = list(active)
    for key in shrink_order:
        if key not in budgets:
            continue
        budgets.pop(key)
        remaining_active = [b for b in remaining_active if b.key in budgets]
        candidate = _assemble_blocks(
            remaining_active, sections_by_block, budgets, footer=False
        )
        if candidate and estimate_tokens(candidate) <= max_tokens:
            return candidate
    return ""


def _assemble_blocks(
    active: List[_Block],
    sections_by_block: Mapping[str, dict],
    budgets: Mapping[str, int],
    split: bool = False,
    footer: bool = True,
) -> "str | Tuple[str, str]":
    """Render every active block at its budget and join them with one footer.

    Args:
        active: Blocks to render, in order, with a budget each.
        sections_by_block: ``{block key: sections}``.
        budgets: ``{block key: token budget}``; a missing key is not rendered.
        split: Return ``(body, footer)`` instead of the joined artifact, for the
            caller that needs to try the footer on its own.
        footer: Append the aggregate footer (ignored when ``split``).

    Returns:
        The assembled artifact, or ``(body, footer)`` when ``split``.
    """
    parts: List[str] = []
    kept_totals: dict = {}
    omitted_total = 0
    hidden: List[str] = []
    for block in active:
        sections = sections_by_block.get(block.key)
        budget = budgets.get(block.key)
        if not sections or budget is None or budget <= 0:
            continue
        # The heading is derived from the selection, and the selection is charged
        # for the heading — a circularity that a single pass cannot satisfy. Two
        # passes with a fixpoint check: select under the whole budget, build the
        # real heading from that selection, and if the two together do not fit,
        # re-select with the heading charged. `_BLOCK_HEADING_PASSES` bounds the
        # loop; each pass can only shrink the selection, and a heading's own width
        # changes by at most a few characters when it does, so the second pass is
        # the one that normally settles it.
        #
        # A heading quoting the block's *original* fact count (``len(block.facts)``)
        # needs none of this and is what the code used to do — but it contradicts
        # the lines below it whenever the budget trims the block, by an amount that
        # grows as the squeeze gets tighter, which is precisely when the reader is
        # relying on the counts to see what was lost.
        kept = _select(sections, budget, footer=False)
        for _pass in range(_BLOCK_HEADING_PASSES):
            if not kept:
                break
            heading = _render_block_heading(block, sections, kept)
            heading_cost = estimate_tokens(heading)
            if budget <= heading_cost:
                kept = {}
                break
            if estimate_tokens(_render_body(sections, kept)) + heading_cost <= budget:
                break
            smaller = _select(sections, budget - heading_cost, footer=False)
            if smaller == kept:
                # Charging for the heading no longer changes the selection, so
                # this is the fixpoint: the heading describes `kept` exactly.
                break
            kept = smaller
        body = _render_body(sections, kept) if kept else ""
        if not body:
            continue
        # The heading must be rebuilt from the final selection: the loop above can
        # exit on an iteration that changed `kept`.
        heading = _render_block_heading(block, sections, kept)
        parts.append(heading + "\n" + body)
        for title, lines in sections.items():
            count = len(kept.get(title) or ())
            omitted_total += len(lines) - count
            if count:
                kept_totals[title] = kept_totals.get(title, 0) + count
            elif title not in hidden:
                hidden.append(title)
    body = ("\n" + _BLOCK_SEPARATOR + "\n").join(parts)
    footer_text = _render_footer(omitted_total, hidden, kept_totals) if parts else ""
    if split:
        return body, footer_text
    if not footer:
        return body
    return _join_footer(body, footer_text)


def _join_footer(body: str, footer_text: str) -> str:
    """Append the footer to the body with a separator that survives injection.

    Never a blank line: the dsh host prefixes every line, blank ones included, so
    a blank separator reaches the prompt as ``| `` — a line that looks like
    content carrying nothing. :data:`_BLOCK_SEPARATOR` is the same mark the blocks
    use, so there is exactly one way this artifact separates its parts.
    """
    if body and footer_text:
        return body + "\n" + _BLOCK_SEPARATOR + "\n" + footer_text
    return body or footer_text


def _render_block_heading(block: "_Block", sections: Mapping[str, dict], kept: Mapping) -> str:
    """Render one block's heading: what the block is, and what it actually holds.

    ``[当前项目: dsh-atom-memory · 8 条 · 决策规则 6 · 教训 1]`` — the scope label,
    the number of lines the block renders, and its type breakdown. Three facts in
    one line, because the alternative (a heading plus one breakdown line per
    block) pays the per-line cost twice on every request of every session.

    The count is the **rendered** line count, not ``len(block.facts)``. That
    distinction is the whole point of deriving the heading from ``kept``: the
    original count is what the block *started* with, and at a tight budget it is
    a number the block visibly does not deliver.

    Only sections that survived are named, so the breakdown and the body below it
    always describe the same set.
    """
    counts = [
        (title, len(kept.get(title) or ()))
        for title in sections
    ]
    counts = [(title, count) for title, count in counts if count]
    rendered = sum(count for _title, count in counts)
    breakdown = " · ".join(f"{title} {count}" for title, count in counts)
    tail = f" · {breakdown}" if breakdown else ""
    return f"[{block.title} · {rendered} 条{tail}]"


# -- loading ------------------------------------------------------------------


def _load_active_facts(conn: sqlite3.Connection, user_id: str) -> List[dict]:
    """Load active facts for a user, normalising the fields the views need."""
    rows = conn.execute(
        "SELECT fact_id, subject, predicate, object, qualifiers, confidence, "
        "importance, type, content, created_at, reinforce_count, last_used_at "
        "FROM facts WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()

    at = now_ms()
    facts: List[dict] = []
    for row in rows:
        fact = dict(row)
        memory_type = fact["type"] or TYPE_SEMANTIC
        fact["type"] = memory_type
        # ``importance`` is only a signal when the extractor supplied one; the
        # neutral default means "unknown" and defers to the type's rank.
        stated = float(fact["importance"] or 0.0)
        # Reuse then strengthens that rank, so a fact the user keeps returning to
        # outranks an equally-stated one they never touch. The reinforcement
        # snapshot is decayed to now, exactly as retrieval does — reading the
        # columns raw would keep a long-unused fact at its old strength.
        fact["rank"] = effective_importance(
            _effective_rank(memory_type, stated),
            adjust(
                float(fact.get("reinforce_count") or 0.0),
                fact.get("last_used_at"),
                at,
            ),
        )
        facts.append(fact)
    return facts


def _effective_rank(memory_type: str, stated_importance: float) -> float:
    """Return the priority used for ordering: stated signal, else type default.

    Args:
        memory_type: The fact's type discriminator.
        stated_importance: The stored importance value.

    Returns:
        ``stated_importance`` when it carries a signal, otherwise the type's
        fallback rank.
    """
    if _has_signal(stated_importance):
        return stated_importance
    return default_importance(memory_type)


def _has_signal(value: float) -> bool:
    """Whether a stored 0..1 score carries an explicit signal.

    Every persistence path writes the neutral default when the extractor omits
    the field, so a stored 0.5 is indistinguishable from "unknown" and must not
    be treated as a real ranking.

    Args:
        value: The stored score.

    Returns:
        ``True`` when the value differs from the neutral default.
    """
    return abs(float(value) - NEUTRAL_SCORE) > 1e-9


def _recency_score(created_at: int, newest_created_at: int) -> float:
    """Return the 0..1 recency credit of a fact, newest-first.

    A thin wrapper over the shared decay in
    :func:`~atom_memory.db.recency_credit` so this view and the retriever cannot
    drift apart on the *shape* — both are exponential decay with a half-life.
    They differ only in their reference anchor and half-life, which is exactly
    the knob that should differ.

    Args:
        created_at: The fact's creation timestamp (ms).
        newest_created_at: The newest timestamp in the same fact set; this view
            treats it as "now" rather than the wall clock, so the ranking is
            deterministic.

    Returns:
        ``1.0`` for the newest fact, halving per
        :data:`_RECENCY_HALF_LIFE_SECONDS` (which is in ms; see its comment) of
        age relative to it.
    """
    age_ms = max(0, int(newest_created_at) - int(created_at))
    # The newest fact scores 1.0: here the reference *is* the newest timestamp,
    # with no window cap, because the set is one user's whole memory rather than
    # the handful of query-matched candidates the retriever sees.
    return recency_credit(age_ms, float(_RECENCY_HALF_LIFE_SECONDS), 0.0)


def _blend(rank: float, recency: float) -> float:
    """Combine importance and recency into the single ranking score.

    Args:
        rank: The fact's importance signal (stated, else its type default).
        recency: The fact's recency credit from :func:`_recency_score`.

    Returns:
        The blended 0..1 score used for ordering and budget allocation.
    """
    return _IMPORTANCE_WEIGHT * rank + _RECENCY_WEIGHT * recency


def _sort_key(fact: dict) -> tuple:
    """Rank facts by blended score, then recency, then a stable id tiebreak."""
    return (-fact["score"], -int(fact["created_at"]), str(fact["fact_id"]))


def _collect(conn: sqlite3.Connection, user_id: str) -> dict:
    """Bucket a user's active facts by rendered section, score-sorted.

    Sections are ordered by the blended score of their **best** fact, not by a
    fixed type list: a fresh, high-importance fact must be able to outrank a
    whole section of staler ones, otherwise "top of the view" would mean
    "luckiest type" rather than "most worth reading now". ``_SECTION_ORDER`` only
    breaks ties between sections whose best facts score equally.

    Returns:
        An ordered mapping ``section -> [fact, ...]``. Empty when the user has
        no active facts.
    """
    facts = _score_facts(conn, user_id)
    if not facts:
        return {}
    return _sections_of(facts)


def _score_facts(conn: sqlite3.Connection, user_id: str) -> List[dict]:
    """Load a user's active facts and attach their blended rank and score.

    Split out from :func:`_collect` because the scoped renderer partitions facts
    into blocks *before* grouping them into sections: it needs the scored fact
    list, not the finished buckets.

    Returns:
        Facts carrying ``rank`` and ``score``. Empty when nothing is active.
    """
    facts = _load_active_facts(conn, user_id)
    if not facts:
        return []
    newest = max(int(fact["created_at"]) for fact in facts)
    for fact in facts:
        fact["score"] = _blend(fact["rank"], _recency_score(fact["created_at"], newest))
    return facts


def _sections_of(facts: List[dict]) -> dict:
    """Group scored facts into ordered, score-sorted sections.

    Sections are ordered by the blended score of their best fact (see
    :func:`_collect`); facts keep score order within a section.
    """
    grouped: dict = {}
    for fact in facts:
        grouped.setdefault(_section_of(fact), []).append(fact)
    for items in grouped.values():
        items.sort(key=_sort_key)

    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            -max(fact["score"] for fact in item[1]),
            _SECTION_ORDER.index(item[0]),
        ),
    )
    return dict(ranked)


def _titled_sections(facts: List[dict]) -> dict:
    """Group facts into ``section title -> [(line, score), ...]``.

    The shape :func:`_select` and :func:`_render_body` consume, so both the flat
    digest and each scope block go through exactly the same grouping, clipping
    and folding. A second hand-written copy of this would be the place where the
    two renderings silently diverge (one clipping to 80 characters, the other
    not).
    """
    out: dict = {}
    for section, group in _sections_of(facts).items():
        if not group:
            continue
        rendered = _render_section_lines(section, group)
        if rendered:
            out[_SECTION_TITLES[section]] = rendered
    return out


def _section_of(fact: dict) -> str:
    """Return the rendered section a fact belongs to."""
    memory_type = fact["type"]
    if memory_type == TYPE_SEMANTIC:
        predicate = fact["predicate"]
        # The *preference* set, not the multi-valued one: a to-do list is
        # multi-valued too, and rendering it as a preference would both mislabel
        # it and fold it into "X（喜欢）".
        if predicate in PREFERENCE_PREDICATES:
            return "preference"
        if predicate == PRED_EVENT:
            return "episodic"
        return "attribute"
    if memory_type not in _SECTION_TITLES:
        return "attribute"
    return memory_type


# -- rendering ----------------------------------------------------------------


def _render_compact(buckets: dict, max_tokens: int, footer: bool = True) -> str:
    """Render the type-grouped digest injected into the system prompt.

    Section labels carry a single ``# `` and the footer a ``-- `` marker, so the
    three line kinds (label / ``- `` fact / footer) differ in their *first*
    character rather than their second. That matters because the dsh host fences
    every injected line with ``| `` (`dsh/src/memory-data.ts`): under that prefix a bare label and a bullet used to look all but identical, and a
    footer inherited markdown's quote marker (``| > …``) inside a plain-text
    block where it means nothing. One ``#`` — not ``##``/``###`` — is deliberate:
    this text is never rendered *as* markdown (it is injected into the prompt and
    shown verbatim in a ``<pre>`` block), and it mirrors the ``# 记忆 (Memory)``
    heading the detail depth already uses, so deeper hashes would only surface as
    punctuation. The label stays cheap: one character per section.

    There is likewise no document title here; the injection site supplies its own
    header and the user scope never varies, so a ``# 记忆 (Memory) — global`` line
    would be pure payload.

    The rendered artifact — footer and labels included — fits ``max_tokens``.
    The only exception is a budget too small to hold even the one-line
    "omitted" notice (~18 tokens): then that notice is returned as the shortest
    honest answer, because returning empty text would be indistinguishable from
    a failed read at the injection site.

    Args:
        buckets: ``section -> [fact, ...]``, already score-sorted.
        max_tokens: Budget for the artifact.
        footer: Whether to append the ``-- `` footer. ``False`` is used by the
            scope-blocked renderer, which emits one aggregate footer for the
            whole artifact instead of one per block.

    Returns:
        The rendered digest, or ``""`` when nothing fits.
    """
    sections: dict = {}  # section title -> [(line, score), ...] in render order
    for section, facts in buckets.items():
        if not facts:
            continue
        rendered = _render_section_lines(section, facts)
        if not rendered:
            continue
        sections[_SECTION_TITLES[section]] = rendered

    rendered = _compose(sections, _select(sections, max_tokens, footer=footer), footer=footer)
    if estimate_tokens(rendered) <= max_tokens:
        return rendered
    # The budget cannot hold even the empty digest and its footer. There is no
    # shorter *honest* thing to say than nothing: a notice would itself cost
    # tokens, would overshoot the cap it is announcing, and would repeat what the
    # caller already knows (it set the budget). Returning "" makes the empty case
    # explicit — the injection path injects nothing — instead of leaving a branch
    # that no realistic budget can reach.
    return ""


def _render_footer(omitted: int, hidden: List[str], kept: dict) -> str:
    """Render the digest footer: what is kept, and what was left out.

    Args:
        omitted: How many content lines were dropped.
        hidden: Titles of the sections dropped in full.
        kept: The surviving ``section title -> line count`` mapping.

    The count/inventory line says **按行** (by line) and not *条事实* (facts). The
    numbers in it are line counts: a folded preference list stands for several
    facts on one line and a to-do list renders one line per item, so summing it
    and calling the result a fact count was simply wrong — and it invited the
    reader to compare it against a fact count elsewhere that is counted a
    different way. What the line *is* good for is the breakdown, which says
    which kinds of memory this digest is spending its budget on; the per-block
    share of the same information now lives in the block headings, so the footer
    only carries the whole-artifact total and what was dropped.

    Each block heading already names the types *that block* holds, so repeating
    them here would pay for the same words twice; this line is the artifact-wide
    view, which is why it can disagree with an individual block without either
    being wrong.

    The count/inventory lines use ``-- `` rather than markdown's ``> ``: the
    footer is summary material, not a quotation, and once the dsh host prefixes
    every line with ``| `` a quote marker renders as the meaningless ``| > ``.

    Returns:
        The footer text.
    """
    breakdown = " · ".join(f"{title} {count}" for title, count in kept.items())
    lines = [f"-- （按行）{breakdown}" if breakdown else "-- （按行）"]
    if omitted or hidden:
        lines.append(
            f"-- 已省略 {omitted} 条低优先级记忆"
            + (f"（{'、'.join(hidden)}分组已隐藏）" if hidden else "")
            + "；需要时用 memory_recall 检索"
        )
    return "\n".join(lines)


def _render_section_lines(section: str, facts: List[dict]) -> List[Tuple[str, float]]:
    """Render one section's facts to scored bullet lines, highest score first.

    Each line carries the blended score of its best contributing fact, because
    that score is what decides whether the line survives a tight budget. Folded
    lines (see :func:`_fold_preferences` / :func:`_attribute_lines`) therefore
    inherit their strongest member's score: a line is worth as much as the best
    thing it stands for.

    Every line is finally clipped to :data:`_MAX_COMPACT_LINE_CHARS`. This single
    choke point is deliberate — it is the one place that guarantees *any* line the
    model reads is bounded, whatever it was built from.
    """
    if section == "preference":
        lines = [(f"- {text}", score) for text, score in _fold_preferences(facts)]
    elif section == "attribute":
        lines = [(f"- {text}", score) for text, score in _attribute_lines(facts)]
    elif section == "task":
        lines = [(_render_task_line(fact), fact["score"]) for fact in facts]
    else:
        lines = [(_render_fact_line(section, fact), fact["score"]) for fact in facts]
    return [(_clip(text, _MAX_COMPACT_LINE_CHARS), score) for text, score in lines]


def _render_task_line(fact: dict) -> str:
    """Render one to-do, keeping the subject it belongs to.

    The attribute fold uses ``predicate: value``, which is right for a
    single-valued attribute (the subject is the user, always) and wrong for a
    to-do list: "待办: 手机真机实测" does not say *which project* still owes the
    test, and that is the whole content of the item. One line per item also
    keeps two open to-dos from being folded into one unreadable line.
    """
    subject = " ".join(str(fact.get("subject") or "").split())
    title = _fact_title(fact)
    if not subject:
        return f"- {title}"
    return f"- {subject}：{title}"


def _fold_preferences(facts: List[dict]) -> List[Tuple[str, float]]:
    """Fold multi-valued preferences into one ``- X（喜欢）`` line per polarity."""
    liked: List[str] = []
    disliked: List[str] = []
    liked_score = 0.0
    disliked_score = 0.0
    for fact in facts:
        value = _clip(str(fact["object"]), _MAX_FOLDED_VALUE_CHARS)
        if not value or value in liked or value in disliked:
            continue
        if _negated(fact.get("qualifiers")):
            disliked.append(value)
            disliked_score = max(disliked_score, fact["score"])
        else:
            liked.append(value)
            liked_score = max(liked_score, fact["score"])

    lines: List[Tuple[str, float]] = []
    if liked:
        lines.append(("、".join(liked), liked_score))
    if disliked:
        lines.append(("、".join(disliked) + "（不喜欢）", disliked_score))
    return lines


def _attribute_lines(facts: List[dict]) -> List[Tuple[str, float]]:
    """Render single-valued attributes as ``predicate: value`` one-liners.

    Several values for the same predicate are merged into one line in score
    order, so a mistyped or re-stated attribute does not burn a whole
    token budget line on its own. Values are clipped individually first (see
    :data:`_MAX_FOLDED_VALUE_CHARS`) so a long one cannot hide its siblings; the
    line cap in :func:`_render_section_lines` then bounds the total.
    """
    merged: dict = {}
    scores: dict = {}
    for fact in facts:
        value = _clip(str(fact["object"]), _MAX_FOLDED_VALUE_CHARS)
        if not value:
            continue
        predicate = fact["predicate"]
        merged.setdefault(predicate, []).append(value)
        scores[predicate] = max(scores.get(predicate, 0.0), fact["score"])

    lines: List[Tuple[str, float]] = []
    for predicate, values in merged.items():
        unique: List[str] = []
        for value in values:
            if value not in unique:
                unique.append(value)
        lines.append((f"{predicate}: {'、'.join(unique)}", scores[predicate]))
    return lines


def _render_fact_line(section: str, fact: dict) -> str:
    """Render one fact as a compact bullet line.

    No clipping happens here: the line (including the ``- `` marker and any
    ``[when]`` prefix) is clipped as a whole by :func:`_render_section_lines`, so
    the cap applies to exactly what the reader sees.
    """
    title = _fact_title(fact)
    if section == "episodic":
        when = _qualifier(fact.get("qualifiers"), "when")
        return f"- [{when}] {title}" if when else f"- {title}"
    return f"- {title}"


def _fact_title(fact: dict) -> str:
    """Return the human-facing value of a fact, body-backed when available.

    Facts carrying a knowledge body render that body rather than the
    placeholder-ish ``object`` headline, which for a long-form fact is often
    just a repeat of the predicate ("构建发布流程" → "dsh-atom-memory"). Only
    whitespace is normalised here; length is bounded once, by the line cap.
    """
    body = (fact.get("content") or "").strip()
    value = body if body else str(fact["object"]).strip()
    return " ".join(value.split())


def _clip(text: str, limit: int) -> str:
    """Normalise whitespace and truncate to *at most* ``limit`` characters.

    The ellipsis is counted **inside** the limit, so this is a real cap: a caller
    that bounds a rendered line by ``limit`` can rely on ``len(result) <= limit``.
    (Appending the ellipsis on top of ``limit`` characters, as this used to do,
    made every "cap" one character larger than advertised.)

    A value that ends in a filesystem path is truncated from the **front**
    instead of the back. Head-clipping is right for prose — the opening words say
    what the sentence is about — and exactly wrong for a path, where the
    discriminating part is the last component: ``C:\\Users\\fuqia\\.dsh\\profiles\\web\\node_m…``
    ends at the one segment the reader cannot guess, so it survives the cap as a
    prefix of the real location while carrying none of its identity. Both forms
    still fit ``limit`` exactly, so the budget arithmetic is untouched.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:max(limit, 0)]
    match = _TRAILING_PATH_RE.search(text) or _TRAILING_PATH_AT_START_RE.search(text)
    if match is not None:
        prefix = text[: match.start()]
        # `limit - len(prefix) - 1` leaves room for the ellipsis this branch
        # prepends; _tail_segments never adds one of its own.
        tail = _tail_segments(match.group(0), limit - len(prefix) - 1)
        if tail:
            return prefix + "…" + tail
    return text[:limit - 1] + "…"


# A filesystem path occupying the **end** of a rendered line.
#
# Matched by shape rather than by first splitting the line at its delimiter: the
# line the clip sees is the whole rendered line (`- 实现包路径: C:\…\client.js`),
# so a test anchored at position 0 never fires, and a split-then-test approach
# has to get the split offset right against drive letters
# (`C:\` contains a delimiter-shaped colon). One regex has neither problem: it
# finds the path wherever it sits, and everything before it is prefix.
#
# Three conditions keep prose out, and each one was needed:
#
#  * the match must run to the end of the line;
#  * it must start at a value boundary — a delimiter, the bullet, or the start of
#    the line — rather than mid-word, so `吞吐/延迟调优` inside a sentence is not a
#    candidate at all;
#  * a relative path's first component may not contain CJK. A bare `a/b` segment
#    is far more often a slash-joined pair of Chinese nouns (`稳定性/速度`) than a
#    directory, whereas an *anchored* path may contain CJK freely — Chinese
#    directory names are the norm here (`…\AI智慧课程\scripts\templates`).
_TRAILING_PATH_RE = re.compile(
    r"""(?<=[\s:：])
        (?:
          [A-Za-z]:[\\/][^\s]*                  # a drive-letter path: C:\...
        | \\\\[^\s\\/]+[\\/][^\s]*              # a UNC path: \\host\share\...
        | [\\/][^\s\\/]+[\\/][^\s]*             # a rooted path: \Users\... or /usr/...
        | [A-Za-z0-9._-]+[\\/][^\s]*            # a relative path: src/channels/...
        )
        $
    """,
    re.VERBOSE,
)

# The same shapes at the very start of the line, where the `(?<=…)` lookbehind
# above has nothing to match. Kept separate because Python's `re` cannot express
# "start of string or one of these characters" as a single fixed-width lookbehind.
_TRAILING_PATH_AT_START_RE = re.compile(
    r"""^
        (?:
          [A-Za-z]:[\\/][^\s]*                  # C:\...
        | \\\\[^\s\\/]+[\\/][^\s]*              # \\host\share\...
        | [\\/][^\s\\/]+[\\/][^\s]*             # \Users\... or /usr/...
        | [A-Za-z0-9._-]+[\\/][^\s]*            # src/channels/...
        )
        $
    """,
    re.VERBOSE,
)


# How many trailing path segments a tail-clipped path keeps. One segment is the
# discriminating part, but a directory whose own name is generic (``web``,
# ``src``) reads better with its parent.
_MAX_CLIPPED_PATH_SEGMENTS = 2


def _tail_segments(text: str, limit: int) -> str:
    """Return the last path segments of ``text``, fitting ``limit`` characters.

    Walks the components from the right and keeps the final
    :data:`_MAX_CLIPPED_PATH_SEGMENTS` of them, joined by their original
    separators, until ``limit`` is reached — whichever comes first. A
    caller-prepended ellipsis is not this function's business, so it never renders
    one, and the result never *begins* with a separator (a leading separator would
    read as a rooted path that the ellipsis already replaced).

    Falls back to a plain tail slice when not even one component fits, so the
    result is never longer than ``limit``.
    """
    parts = [part for part in re.split(r"([\\/])", text) if part]
    kept: List[str] = []
    length = 0
    segments = 0
    index = len(parts) - 1
    while index >= 0:
        part = parts[index]
        if part in "\\/":
            # A separator is kept only when a component already sits to its right,
            # and never as the leftmost thing in the result.
            if kept:
                kept.insert(0, part)
                length += 1
            index -= 1
            continue
        if segments >= _MAX_CLIPPED_PATH_SEGMENTS or length + len(part) > limit:
            break
        kept.insert(0, part)
        length += len(part)
        segments += 1
        index -= 1
    # A separator that survived without a component to its left is dead weight.
    while kept and kept[0] in "\\/":
        kept.pop(0)
    while kept and kept[-1] in "\\/":
        kept.pop()
    tail = "".join(kept)
    return tail if tail else text[-limit:]


def _select(sections: dict, max_tokens: int, footer: bool = True) -> dict:
    """Choose the lines a token budget can hold, globally best-scoring first.

    Nothing is dropped while the render fits — the dense preference/attribute
    folds are cheap and the whole point of this view is a complete picture.

    On overflow the artifact is shrunk one line at a time, always giving up the
    **globally lowest-scoring** line still present, until it fits. Two properties
    follow, and both matter:

    * The surviving set is "the most important and most recent memory" at *any*
      budget. The previous implementation instead emptied one section after
      another from the tail, so a top-scoring fact recorded minutes ago could be
      thrown away purely because it lived in the section that sorts last.
    * The budget is measured on the **assembled artifact** — the body plus the
      footer that this very selection produces — never on an estimate of its
      parts, so the token budget is a hard cap on what is actually returned.

    A section that loses *every* line loses its label too (labels are part of the
    artifact being measured), so no bare ``流程`` stub is rendered.

    Cost is linear-ish rather than quadratic: the candidate order is fixed once,
    and each removal updates the artifact's *character totals* instead of
    re-rendering it. ``estimate_tokens`` is a pure function of those totals
    (``cjk + max(other // 5, 1)``), so the incremental measurement is exactly
    what a full re-render would report — which the differential test in
    ``tests/test_summary.py`` pins. Re-measuring the whole artifact per dropped
    line cost O(n²): 3000 facts took 8.2 s *inside* prompt assembly, blocking
    every other request on the same event loop.

    Args:
        sections: ``section title -> [(line, score), ...]`` in render order.
        max_tokens: The budget for the complete rendered artifact.
        footer: Whether the artifact this selection is measured against includes
            the footer. ``False`` when the caller renders its own aggregate
            footer, so the fit check measures exactly what that caller emits.

    Returns:
        ``section title -> surviving line indices`` (ascending). Sections that
        kept nothing are absent; :func:`_compose` derives everything else
        (footer counts, hidden sections) from this one mapping, so there is a
        single source of truth for what was kept.
    """
    titles = list(sections)
    kept: dict = {title: list(range(len(lines))) for title, lines in sections.items()}

    # Lowest score first: the order in which material is given up. Ties resolve
    # against render order so the outcome is deterministic.
    give_up = [
        (score, order_index, line_index)
        for order_index, lines in enumerate(sections.values())
        for line_index, (_line, score) in enumerate(lines)
    ]
    give_up.sort(key=lambda item: (item[0], -item[1], -item[2]))

    # -- exact, incremental measurement ---------------------------------------
    #
    # The artifact's token count is a pure function of its two character totals
    # (``cjk + max(other // 5, 1)``), and character decomposition is additive, so
    # the totals of the assembled text can be maintained as parts are removed
    # instead of re-rendering the text. The newline separators count as "other"
    # characters and are accounted for explicitly:
    #
    #   body   = "\n".join(parts)        -> sum(parts) + (parts - 1) newlines
    #   result = body + "\n\n" + footer  -> + 2 more when the body is non-empty
    line_cost: dict = {}
    title_cost: dict = {}
    totals = {"cjk": 0, "other": 0, "parts": 0, "lines": 0}
    for title, lines in sections.items():
        cost = token_cost(_COMPACT_LABEL_MARKER + title)
        title_cost[title] = (cost[1], cost[2])
        totals["cjk"] += cost[1]
        totals["other"] += cost[2]
        totals["parts"] += 1
        for index, (text, _score) in enumerate(lines):
            cost = token_cost(text)
            line_cost[(title, index)] = (cost[1], cost[2])
            totals["cjk"] += cost[1]
            totals["other"] += cost[2]
            totals["parts"] += 1
            totals["lines"] += 1
    # The newlines joining the parts.
    totals["other"] += max(0, totals["parts"] - 1)

    def _drop(title: str, line_index: int) -> None:
        """Remove one kept line and update the totals it contributed."""
        kept[title] = [index for index in kept[title] if index != line_index]
        cjk, other = line_cost[(title, line_index)]
        totals["cjk"] -= cjk
        totals["other"] -= other
        totals["parts"] -= 1
        totals["other"] -= 1 if totals["parts"] > 0 else 0
        if not kept[title]:
            cjk, other = title_cost[title]
            totals["cjk"] -= cjk
            totals["other"] -= other
            totals["parts"] -= 1
            totals["other"] -= 1 if totals["parts"] > 0 else 0

    def _artifact_totals() -> tuple:
        """The ``(cjk, other)`` totals of body (and footer) for the current state."""
        if not footer:
            if totals["parts"] == 0:
                return (0, 0)
            return (totals["cjk"], totals["other"])
        counts = {title: len(indices) for title, indices in kept.items() if indices}
        hidden = [title for title in sections if not kept.get(title)]
        omitted = totals["lines"] - sum(counts.values())
        foot = token_cost(_render_footer(omitted, hidden, counts))
        if totals["parts"] == 0:
            return (foot[1], foot[2])
        # The body/footer join is ``"\n" + _BLOCK_SEPARATOR + "\n"``: two newlines,
        # "other" characters like any other, plus the separator itself. Derived
        # from the constants rather than written as a literal "+2", because a
        # hard-coded count is how the incremental measurement would silently stop
        # agreeing with the artifact the caller receives — the one thing this
        # function exists to prevent. Asserted by
        # `test_incremental_selection_matches_the_reference_exactly`.
        gap_cjk, gap_other = token_cost(_BLOCK_SEPARATOR + "\n\n")[1:]
        return (totals["cjk"] + foot[1] + gap_cjk, totals["other"] + foot[2] + gap_other)

    for _score, order_index, line_index in give_up:
        if _tokens_for_totals(_artifact_totals()) <= max_tokens:
            break
        title = titles[order_index]
        if line_index in kept[title]:
            _drop(title, line_index)

    return {title: indices for title, indices in kept.items() if indices}


def _tokens_for_totals(totals) -> int:
    """Return the token estimate for accumulated ``(cjk, other)`` totals.

    Mirrors :func:`~atom_memory.retriever.estimate_tokens` exactly (including
    its "empty text is zero" case) so an incremental measurement and a full
    re-render can never disagree. The divisor is the shared
    :data:`~atom_memory.retriever.CHARS_PER_TOKEN`: if this file kept its own
    constant, retuning the estimate would silently split the two paths.
    """
    cjk, other = totals
    if cjk <= 0 and other <= 0:
        return 0
    return cjk + max(other // CHARS_PER_TOKEN, 1 if other else 0)


def _kept_indices(kept: dict, title: str) -> List[int]:
    """Return a section's kept line indices (empty list when it kept none)."""
    return kept.get(title) or []


def _render_body(sections: dict, kept: dict) -> str:
    """Assemble the surviving lines, grouped under their section labels.

    Labels are emitted only for sections that kept at least one line, and lines
    keep their original (score-descending) order inside a section: the selection
    chooses *what* survives, never the layout.

    Each label gets the ``## `` marker rather than a single hash: under the dsh
    host's per-line ``| `` prefix a one-hash label and a ``- `` bullet differ in
    one character out of two and read as one flat list (see
    :data:`_COMPACT_LABEL_MARKER`), so the label must stay visibly wider than the
    bullets it heads, and a section stays findable by jumping to the next
    ``## `` line.
    """
    parts: List[str] = []
    for title, lines in sections.items():
        indices = _kept_indices(kept, title)
        if not indices:
            continue
        parts.append(_COMPACT_LABEL_MARKER + title)
        parts.extend(lines[index][0] for index in indices)
    return "\n".join(parts)


def _compose(sections: dict, kept: dict, footer: bool = True) -> str:
    """Render the complete artifact (body + footer) for a candidate selection.

    The footer is derived from the same selection, so what the fit check measures
    is exactly what the caller receives — including the "what was left out" line,
    whose length depends on how much was dropped. ``footer=False`` returns the
    body alone, for a caller that emits its own aggregate footer.
    """
    body = _render_body(sections, kept)
    if not footer:
        return body
    counts = {title: len(indices) for title, indices in kept.items() if indices}
    hidden = [title for title in sections if not kept.get(title)]
    omitted = sum(len(lines) for lines in sections.values()) - sum(counts.values())
    foot = _render_footer(omitted, hidden, counts)
    return _join_footer(body, foot)


def _render_detail(buckets: dict, user_id: str, max_tokens: int) -> str:
    """Render the full fact list with ``fact_id`` references.

    The heading is kept here (unlike the compact view): this text is read by a
    human in a raw ``<pre>`` block, where the title is what orients them. The
    token budget bounds the **facts**; this depth is a drill-down list whose
    heading exists to orient a human reader and is therefore not trimmed away.
    """
    facts: List[dict] = []
    for section in buckets:
        facts.extend(buckets.get(section, []))
    facts.sort(key=_sort_key)

    lines = [f"# 记忆 (Memory) — {user_id}", ""]
    budget = max_tokens
    kept = 0
    budget_exhausted = False

    for fact in facts:
        line = f"- {_format_fact(fact)}"
        cost = estimate_tokens(line)
        if cost > budget and kept > 0:
            budget_exhausted = True
            break
        lines.append(line)
        budget -= cost
        kept += 1

    lines.append("")
    lines.append(f"> {kept} 条事实 (facts) · 含 fact_id 作为唯一引用")
    if budget_exhausted:
        lines.append(f"> ⚠ 超出 token 预算，已裁剪（限制 {max_tokens}）")
    return "\n".join(lines)


def _format_fact(fact: dict) -> str:
    """Render a single fact as a markdown bullet for the detail view.

    The full ``fact_id`` is included as the unique, stable reference. Facts that
    carry structured knowledge content (SOP, decision rule, few-shot, lesson)
    render the body on a folded sub-line so the full text stays addressable
    without bloating the bullet. The stored scores are deliberately *not*
    rendered: they are uniform in practice and read as false precision.

    Each of ``subject`` / ``predicate`` / ``object`` is clipped individually, so
    a runaway field cannot push the ``fact_id`` (the whole reason this depth
    exists) out of sight.
    """
    subject = _clip(str(fact["subject"]), _MAX_DETAIL_FIELD_CHARS)
    predicate = _clip(str(fact["predicate"]), _MAX_DETAIL_FIELD_CHARS)
    obj = _clip(str(fact["object"]), _MAX_DETAIL_FIELD_CHARS)
    head = f"[{fact['fact_id']}] **{subject}** — {predicate}: {obj}"
    content = fact.get("content")
    if content:
        snippet = _clip(content, _DETAIL_CONTENT_CHARS)
        if snippet and snippet != obj:
            return f"{head}\n    > **知识内容** {snippet}"
    return head


# -- qualifier helpers --------------------------------------------------------


def _qualifiers(raw: Optional[str]) -> dict:
    """Parse a qualifiers JSON string into a dict (best-effort)."""
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _qualifier(raw: Optional[str], key: str) -> str:
    """Return one qualifier value as a stripped string."""
    return str(_qualifiers(raw).get(key) or "").strip()


def _negated(raw: Optional[str]) -> bool:
    """Whether a qualifiers JSON string carries a negation marker."""
    return bool(_qualifiers(raw).get("negation"))
