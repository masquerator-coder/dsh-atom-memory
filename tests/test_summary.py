"""Tests for ``summary`` rendering (``summary.py``).

Covers both render depths and, above all, the properties the old implementation
got wrong: memory types stay distinct instead of collapsing into one flat list;
ordering follows a real priority signal rather than plain recency (``importance``
is only a signal when the extractor supplied one; the neutral default defers to
the type's rank); and a tight token budget keeps the **most important and most
recent** memory rather than gutting whichever section sorts last.
"""

from __future__ import annotations

import re

import pytest

from atom_memory.db import connect_for_tests
from atom_memory.models import NEUTRAL_SCORE
from atom_memory.config import MemConfig
from atom_memory.overview import write_overview, read_overview
from atom_memory.scope import ScopeStore
from atom_memory.summary import (
    _COMPACT_LABEL_MARKER,
    _DETAIL_CONTENT_CHARS,
    _MAX_COMPACT_LINE_CHARS,
    _MAX_DETAIL_FIELD_CHARS,
    _MAX_FOLDED_VALUE_CHARS,
    _RECENCY_HALF_LIFE_SECONDS,
    _SECTION_TITLES,
    generate_summary,
)
from atom_memory.retriever import estimate_tokens

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Ages are expressed in half-lives so the tests read as "N half-lives staler".
HALF_LIFE = _RECENCY_HALF_LIFE_SECONDS


def _insert_fact(
    conn,
    fact_id: str,
    predicate: str,
    obj: str,
    memory_type: str = "semantic",
    importance: float = NEUTRAL_SCORE,
    confidence: float = NEUTRAL_SCORE,
    content: str | None = None,
    qualifiers: str | None = None,
    user_id: str = "u1",
    created_at: int = 1000,
    subject: str = "用户",
):
    """Insert one active fact directly, bypassing the worker pipeline."""
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, qualifiers, confidence, importance, type, content, status, "
        "observed_at, created_at, version) "
        "VALUES (?, ?, 's1', ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1)",
        (
            fact_id, user_id, subject, predicate, obj, qualifiers,
            confidence, importance, memory_type, content, created_at, created_at,
        ),
    )
    conn.commit()


def _md(conn, max_tokens: int = 1500, detail: bool = False, user_id: str = "u1"):
    return generate_summary(conn, user_id, max_tokens, detail)


# ---- compact depth -----------------------------------------------------------


def test_compact_omits_fact_id_and_scores():
    """The injected view carries no UUIDs and none of the uniform score noise."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "aaaa1111-2222-3333-4444-555566667777", "职业", "工程师")
        md = _md(conn)

        assert "aaaa1111" not in md
        assert not UUID_RE.search(md)
        assert "置信" not in md
        assert "重要" not in md
    finally:
        conn.close()


def test_compact_has_no_markdown_title():
    """No ``# 记忆 (Memory)`` heading: the injection site supplies its own."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        md = _md(conn)
        assert not md.splitlines()[0].startswith("# 记忆")
        assert "# 记忆" not in md
        assert "global" not in md
    finally:
        conn.close()


def test_compact_groups_every_present_type():
    """All memory kinds the user actually has get their own section."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        _insert_fact(conn, "f2", "教训", "上线前必须测试", memory_type="lesson")
        _insert_fact(conn, "f3", "发布流程", "步骤", memory_type="procedural")
        _insert_fact(conn, "f4", "职业", "工程师", memory_type="semantic")
        _insert_fact(conn, "f5", "偏好", "黑咖啡", memory_type="semantic")
        md = _md(conn)
        lines = md.splitlines()

        labels = {
            _COMPACT_LABEL_MARKER + title
            for title in ("决策规则", "教训", "流程", "偏好", "属性")
        }
        for label in labels:
            assert label in lines, label
        # A section label is never rendered without content under it.
        for index, line in enumerate(lines[:-1]):
            if line in labels:
                assert lines[index + 1].startswith("- "), line
    finally:
        conn.close()


def test_a_todo_list_keeps_its_subject_and_its_items():
    """To-dos render one line per item, each naming what it belongs to.

    The attribute fold would render ``待办: A、B`` — right for a single-valued
    attribute (whose subject is always the user) and useless for a to-do list,
    where "which project still owes this" is the item's whole content.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "t1", "待办", "真机挂载验证", memory_type="task",
                     subject="dsh-memory", created_at=3000)
        _insert_fact(conn, "t2", "待办", "写接口实弹验证", memory_type="task",
                     subject="超星图谱", created_at=2000)
        md = _md(conn)
        lines = md.splitlines()

        assert _COMPACT_LABEL_MARKER + "待办" in lines
        assert "- dsh-memory：真机挂载验证" in lines
        assert "- 超星图谱：写接口实弹验证" in lines
    finally:
        conn.close()


def test_a_todo_is_not_rendered_as_a_preference():
    """Cardinality and preference are different questions.

    A to-do is multi-valued *and* not a preference; routing it through the
    preference set would label the user's outstanding work as a taste.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "t1", "待办", "真机挂载验证", memory_type="task",
                     subject="dsh-memory")
        md = _md(conn)
        lines = md.splitlines()

        assert _COMPACT_LABEL_MARKER + "偏好" not in lines
        assert "（不喜欢）" not in md
    finally:
        conn.close()


# ---- detail depth ------------------------------------------------------------


def test_detail_keeps_fact_id_and_title():
    """The settings/tool view still exposes ``fact_id`` so facts stay editable."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        md = _md(conn, detail=True)

        assert md.splitlines()[0].startswith("# 记忆 (Memory) — u1")
        assert "[f1]" in md
        assert "fact_id" in md
    finally:
        conn.close()


def test_detail_renders_knowledge_body():
    """A knowledge fact's body is addressable from the detail view."""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "f1", "教训", "不能在没测试的情况下直接上线",
            memory_type="lesson", content="不能在没测试的情况下直接上线",
        )
        md = _md(conn, detail=True)
        assert "不能在没测试的情况下直接上线" in md
    finally:
        conn.close()


# ---- per-line length cap -----------------------------------------------------


def _bullet_lines(md: str) -> list[str]:
    """Return the rendered content lines (the ones the reader/model actually reads)."""
    return [line for line in md.splitlines() if line.startswith("- ")]


def test_compact_line_cap_bounds_every_rendered_line():
    """每条注入行都被限制在 80 字符内，无论它由什么拼成。

    Covers every compact line shape at once: a folded attribute, a folded
    preference list, a knowledge-body headline, and an episodic line with a
    ``[when]`` prefix.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "a", "属性", "值" * 400)
        _insert_fact(conn, "b", "偏好", "长偏好" * 200)
        _insert_fact(
            conn, "c", "教训", "标题", memory_type="lesson", content="正文" * 500,
        )
        _insert_fact(
            conn, "d", "事件", "发生了一件很长的" * 40,
            memory_type="episodic", qualifiers='{"when": "2026-02-01"}',
        )
        md = _md(conn)

        lines = _bullet_lines(md)
        assert len(lines) >= 4
        for line in lines:
            assert len(line) <= _MAX_COMPACT_LINE_CHARS, (len(line), line)
    finally:
        conn.close()


def test_compact_line_cap_does_not_touch_short_folds():
    """短值的折叠行不受影响：每个值都仍然可见。"""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "偏好", "黑咖啡", created_at=3000)
        _insert_fact(conn, "f2", "偏好", "少糖", created_at=2000)
        _insert_fact(conn, "f3", "偏好", "深色主题", created_at=1000)
        md = _md(conn)

        line = next(l for l in _bullet_lines(md) if l.startswith("- 黑咖啡"))
        assert "少糖" in line
        assert "深色主题" in line
        assert len(line) <= _MAX_COMPACT_LINE_CHARS
    finally:
        conn.close()


def test_compact_line_cap_clips_a_runaway_value_before_folding():
    """超长值先被单独截断，不会独占整行而让同谓词的其他值彻底消失。

    The point of clipping values *before* joining them: without it the line cap
    would consume the entire line with the first value and the reader would never
    learn that a second value exists.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "部署路径", "A" * 400, created_at=2000)
        _insert_fact(conn, "f2", "部署路径", "B" * 400, created_at=1000)
        md = _md(conn)

        line = next(l for l in _bullet_lines(md) if l.startswith("- 部署路径"))
        assert len(line) <= _MAX_COMPACT_LINE_CHARS
        values = line.split(": ", 1)[1].split("、")
        assert len(values) >= 2, line
        for value in values:
            assert len(value) <= _MAX_FOLDED_VALUE_CHARS, value
    finally:
        conn.close()


def test_detail_clips_each_field_and_keeps_the_fact_id_readable():
    """完整版逐字段限长，但 fact_id 与结构保持完整——它才是这一层存在的理由。"""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "f1", "P" * 400, "O" * 400, subject="S" * 400,
        )
        md = _md(conn, detail=True)

        line = next(l for l in md.splitlines() if l.startswith("- ["))
        assert line.startswith("- [f1] ")
        assert "S" * (_MAX_DETAIL_FIELD_CHARS + 1) not in line
        assert "P" * (_MAX_DETAIL_FIELD_CHARS + 1) not in line
        assert "O" * (_MAX_DETAIL_FIELD_CHARS + 1) not in line
        # The clipped fields are still marked as clipped.
        assert line.count("…") == 3
    finally:
        conn.close()


def test_detail_clips_the_knowledge_body_subline():
    """完整版的知识正文折叠行同样限长。"""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "f1", "教训", "短", memory_type="lesson", content="正文" * 300,
        )
        md = _md(conn, detail=True)

        sub = next(l for l in md.splitlines() if "知识内容" in l)
        snippet = sub.split("知识内容** ", 1)[1]
        assert len(snippet) <= _DETAIL_CONTENT_CHARS
    finally:
        conn.close()


def test_line_cap_keeps_one_long_memory_from_crowding_out_others():
    """限长的实际收益：一条超长记忆不再挤掉同一预算下的其他记忆。"""
    conn = connect_for_tests()
    try:
        # One runaway knowledge body plus four ordinary facts.
        _insert_fact(
            conn, "big", "教训", "巨大教训", memory_type="lesson",
            content="很长的正文" * 500, created_at=9000,
        )
        for index in range(4):
            _insert_fact(
                conn, f"n{index}", "属性", f"普通值{index}",
                memory_type="semantic", created_at=1000 + index,
            )
        md = _md(conn, max_tokens=200)

        lines = _bullet_lines(md)
        assert all(len(line) <= _MAX_COMPACT_LINE_CHARS for line in lines)
        # The four ordinary facts are all still represented.
        for index in range(4):
            assert f"普通值{index}" in md, index
        assert estimate_tokens(md) <= 200
    finally:
        conn.close()


# ---- priority ----------------------------------------------------------------


def test_type_default_breaks_the_neutral_tie():
    """Facts with no explicit signal rank by type, not by insertion order."""
    conn = connect_for_tests()
    try:
        # The semantic fact is *newer*: recency ordering would put it first.
        _insert_fact(conn, "f1", "职业", "工程师", created_at=2000)
        _insert_fact(
            conn, "f2", "决定", "先回滚再排查",
            memory_type="decision_rule", created_at=1000,
        )
        md = _md(conn)
        assert md.index("先回滚再排查") < md.index("工程师")
    finally:
        conn.close()


def test_stated_importance_outranks_the_type_default():
    """An explicit signal from the extractor wins over the type fallback."""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "f1", "决定", "先回滚再排查",
            memory_type="decision_rule", importance=NEUTRAL_SCORE,
        )
        _insert_fact(
            conn, "f2", "部署路径", "C:\\dsh",
            memory_type="semantic", importance=0.95,
        )
        md = _md(conn)
        assert md.index("C:\\dsh") < md.index("先回滚再排查")
    finally:
        conn.close()


# ---- folding -----------------------------------------------------------------


def test_multi_valued_values_fold_onto_one_line():
    """Repeated preferences do not each burn a bullet."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "偏好", "黑咖啡", created_at=3000)
        _insert_fact(conn, "f2", "偏好", "少糖", created_at=2000)
        md = _md(conn)

        preference_lines = [
            line for line in md.splitlines()
            if line.startswith("- ") and "黑咖啡" in line
        ]
        assert len(preference_lines) == 1
        assert "少糖" in preference_lines[0]
    finally:
        conn.close()


def test_negated_preference_is_marked():
    """A disliked value is rendered separately from liked ones."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "偏好", "黑咖啡", created_at=2000)
        _insert_fact(
            conn, "f2", "偏好", "奶茶",
            qualifiers='{"negation": true}', created_at=1000,
        )
        md = _md(conn)
        assert "奶茶（不喜欢）" in md
        assert "黑咖啡（不喜欢）" not in md
    finally:
        conn.close()


# ---- budgeting ---------------------------------------------------------------


def test_budget_is_a_hard_cap_including_the_footer():
    """The rendered artifact never exceeds the caller's token budget."""
    conn = connect_for_tests()
    try:
        for index in range(40):
            _insert_fact(
                conn, f"f{index}", f"属性{index}",
                "值" * 20,
                memory_type="semantic",
                content="正文" * 40,
                created_at=1000 + index,
            )
        # 30 is below the cost of the one-line "omitted" notice (~18 tokens plus
        # its section text), so the documented floor is the lowest budget at
        # which the cap is meaningful for real configuration.
        for budget in (30, 60, 120, 200, 300, 500, 800):
            md = _md(conn, max_tokens=budget)
            assert estimate_tokens(md) <= budget, (budget, estimate_tokens(md))
    finally:
        conn.close()


# ---- the label a scope block renders under injection -------------------------


def test_the_section_label_marker_is_two_hashes():
    """标签标记是 ``## ``：注入端每行加 ``| `` 后，一个 ``#`` 与条目无法区分。

    The dsh host prefixes every injected line with ``| `` so that no stored line
    can occupy column zero. That prefix costs the artifact its first-character
    hierarchy: under it ``| # 决策规则`` and ``| - 事实`` differ in one character
    out of two and read as one flat list, which is exactly the complaint this
    marker answers. The assertion is on the marker itself rather than on a
    rendered string, because the property that matters is the *width relative to
    a bullet*, and a test that only checked ``"## 决策规则" in md`` would pass
    again the day someone rendered a two-hash heading by accident.
    """
    assert _COMPACT_LABEL_MARKER == "## "
    assert not _COMPACT_LABEL_MARKER.startswith("- "), "a label must not look like a bullet"

    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        md = _md(conn)
        labels = [line for line in md.splitlines() if line.startswith("## ")]
        assert labels == ["## 决策规则"]
    finally:
        conn.close()


def test_a_path_value_keeps_its_tail_and_prose_keeps_its_head():
    """路径从**尾部**截断，正文仍从头部截断——两者都仍受同一个字符上限。

    ``C:\\Users\\fuqia\\.dsh\\profiles\\web\\node_m…`` ends at the one segment the
    reader cannot guess, so head-clipping preserved a prefix of the location while
    discarding its identity. The second half of the test is the guard on that
    fix: front-clipping content would be strictly worse than the behaviour it
    replaces, so prose must stay head-clipped.

    The values are deliberately long enough to cross
    :data:`_MAX_COMPACT_LINE_CHARS` — a short value is returned untouched by both
    branches, so a test that used one would assert nothing.
    """
    conn = connect_for_tests()
    try:
        path = r"C:\Users\fuqia\.dsh\profiles\web\node_modules\dsh-im-gateway\lib\client.js"
        prose = "稳定性：不崩、不乱码、可复现；速度：吞吐/延迟调优，另外还要考虑并发下的表现与退化路径"
        _insert_fact(conn, "p1", "实现包路径", path)
        _insert_fact(conn, "p2", "调研重点", prose)
        md = _md(conn)
        lines = md.splitlines()

        path_line = next(line for line in lines if "实现包路径" in line)
        # The last `_MAX_CLIPPED_PATH_SEGMENTS` components survive — the directory
        # that names the install and the file itself. The drive letter is kept as
        # part of the prefix, so the ellipsis lands *inside* the path rather than
        # at the delimiter.
        assert "lib" in path_line and "client.js" in path_line, path_line
        assert path_line.startswith("- 实现包路径: "), path_line
        assert "…" in path_line and path_line.index("…") < path_line.index("lib"), path_line
        assert "fuqia" not in path_line, "the head of the path is what gets dropped"

        prose_line = next(line for line in lines if "调研重点" in line)
        assert "稳定性" in prose_line, "prose must not be tail-clipped"
        assert "吞吐/延迟调优" in prose_line, "a slash pair in prose is not a path"

        # Both forms still respect the one cap that bounds every rendered line.
        for line in lines:
            assert len(line) <= _MAX_COMPACT_LINE_CHARS, line
    finally:
        conn.close()


def test_a_path_with_chinese_directory_names_is_still_a_path():
    """带中文目录名的路径仍判为路径：本部署的项目目录普遍是中文。

    Anchoring has to be decided before the "contains CJK" prose signal, because
    ``C:\\Users\\…\\AI智慧课程\\…`` is a path this user actually stores and a
    prose-first check silently sent it back to head-clipping.
    """
    conn = connect_for_tests()
    try:
        path = r"C:\Users\fuqia\Documents\AIWorkspace\AI智慧课程\scripts\templates\render.py"
        _insert_fact(conn, "p1", "脚本模板位置", path)
        md = _md(conn)
        line = next(line for line in md.splitlines() if "脚本模板位置" in line)

        assert "templates" in line and "render.py" in line, line
        assert "AI智慧课程" not in line, "the head is what gets dropped, CJK or not"
        assert "…" in line, line
    finally:
        conn.close()


def test_prose_with_slashes_is_not_mistaken_for_a_path():
    """含斜杠的正文不是路径：``…/ 下一步：X`` 这类行曾被前端截断，信息归零。

    The first version of the path branch keyed on "contains two separators",
    which swept up memory bodies that quote other predicates. Tail-clipping those
    keeps the sentence's tail and throws away what it was about, so the test pins
    the negative case as hard as the positive one.
    """
    conn = connect_for_tests()
    try:
        body = "Python 规则引擎新增「待办：X / 下一步：X / TODO: X」兜底（type=task），并且迁移期不改任何 schema"
        _insert_fact(conn, "b1", "抽取端", "占位", memory_type="sop", content=body)
        md = _md(conn)
        line = next(line for line in md.splitlines() if "Python 规则引擎" in line)

        assert line.startswith("- Python 规则引擎"), line
        assert not line.startswith("- …"), line
    finally:
        conn.close()


def test_the_documented_path_example_is_the_rendered_one():
    """文档里引用的截断例子必须就是真实输出，否则文档会静默变成谎言。

    `docs/scopes.md` §6, `README.md` and `README.zh.md` all quote this exact
    rewrite, so it is pinned here: a change to the path branch that keeps the
    tests green while invalidating the quoted example would leave the docs
    describing a shape the renderer no longer produces.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "p1", "实现包路径",
            r"C:\Users\fuqia\.dsh\profiles\web\node_modules",
        )
        md = _md(conn)
        line = next(line for line in md.splitlines() if "实现包路径" in line)
        assert line == r"- 实现包路径: C:…web\node_modules", line
    finally:
        conn.close()


def test_an_unusable_budget_yields_nothing_not_a_notice():
    """A budget below the footer's own cost renders empty text, on purpose.

    The previous behaviour emitted a one-line notice ("N memories omitted,
    budget too small") — reachable only below ~18 tokens, which no shipping
    budget can produce (the plugin's floor is 100). A notice is also the wrong
    answer: it costs tokens, overshoots the cap it is announcing, and tells the
    caller what it already knows. Empty is the honest and cheaper form, and the
    injection path reads it as "inject nothing".
    """
    conn = connect_for_tests()
    try:
        for index in range(30):
            _insert_fact(conn, f"f{index}", f"属性{index}", "值", created_at=index)
        assert _md(conn, max_tokens=1) == ""
        # One notch above the floor, the render is real again.
        md = _md(conn, max_tokens=40)
        assert md.strip()
        assert estimate_tokens(md) <= 40
    finally:
        conn.close()


def test_tight_budget_shrinks_tail_sections_first():
    """Durable knowledge survives a squeeze; the tail is what gets trimmed."""
    conn = connect_for_tests()
    try:
        for index in range(30):
            _insert_fact(
                conn, f"e{index}", f"属性{index}", "值" * 10,
                memory_type="semantic", created_at=500 + index,
            )
        _insert_fact(
            conn, "d1", "决定", "先回滚再排查",
            memory_type="decision_rule", created_at=100,
        )
        md = _md(conn, max_tokens=200)

        assert "先回滚再排查" in md
        assert _COMPACT_LABEL_MARKER + "决策规则" in md.splitlines()
        assert "已省略" in md
        # The durable section comes first, and the tail was trimmed hard.
        body = md.splitlines()
        assert body.index(_COMPACT_LABEL_MARKER + "决策规则") < body.index(
            _COMPACT_LABEL_MARKER + "属性"
        )
        assert md.count("- 属性") < 30
        assert estimate_tokens(md) <= 200
    finally:
        conn.close()


# ---- a tight budget must keep what is important *and* recent -----------------


def test_fresh_fact_outranks_a_stale_higher_rank_fact():
    """近期是一等维度：陈旧的决策规则不再无条件压过最新的事实。

    Importance stays primary for modest age gaps (see the test below); this
    pins the other end of the trade — after ~8 half-lives of staleness, a fresh
    fact is worth more to the reader than a durable rule nobody has touched for
    months, and the render order says so.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "d1", "决定", "先回滚再排查",
            memory_type="decision_rule", created_at=1000,
        )
        _insert_fact(
            conn, "f1", "部署路径", "D:\\dsh",
            memory_type="semantic", created_at=1000 + 8 * HALF_LIFE,
        )
        md = _md(conn)
        assert md.index("D:\\dsh") < md.index("先回滚再排查")
    finally:
        conn.close()


def test_modest_staleness_does_not_flip_the_type_rank():
    """重要度仍是主信号：仅陈旧一倍半衰期不足以让 durable 知识退位。"""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "d1", "决定", "先回滚再排查",
            memory_type="decision_rule", created_at=1000,
        )
        _insert_fact(
            conn, "f1", "部署路径", "D:\\dsh",
            memory_type="semantic", created_at=1000 + HALF_LIFE,
        )
        md = _md(conn)
        assert md.index("先回滚再排查") < md.index("D:\\dsh")
    finally:
        conn.close()


def test_tight_budget_keeps_the_newest_fact_of_the_tail_section():
    """预算收紧时最新的一条必须活下来，而不是随所在分组被整段砍掉。

    Regression: the previous trim walked sections from the tail inward and
    emptied one before touching the next. The 属性 section outranks 事件 by type,
    so a squeeze used to clear the 事件 section wholesale — even though the event
    recorded minutes ago scores above every stale attribute. The budget is now
    spent globally, best score first.
    """
    conn = connect_for_tests()
    try:
        for index in range(30):
            _insert_fact(
                conn, f"a{index}", f"属性{index}", "值" * 10,
                memory_type="semantic", created_at=1000 + index,
            )
        _insert_fact(
            conn, "e1", "事件", "刚刚完成了插件部署",
            memory_type="episodic", created_at=1000 + 2 * HALF_LIFE,
        )
        md = _md(conn, max_tokens=150)

        assert "刚刚完成了插件部署" in md
        assert estimate_tokens(md) <= 150
    finally:
        conn.close()


def test_tight_budget_drops_the_stalest_lines_of_a_kept_section():
    """同类型事实按新旧取舍：最新的留下，最陈旧的让出预算。"""
    conn = connect_for_tests()
    try:
        for index in range(20):
            _insert_fact(
                conn, f"l{index}", "教训", f"教训正文{index}",
                memory_type="lesson", created_at=1000 + index * HALF_LIFE,
            )
        # The whole set costs ~111 tokens, so 100 forces a real trade.
        md = _md(conn, max_tokens=100)

        # The newest lesson is present, the oldest ones are gone.
        assert "教训正文19" in md
        assert "教训正文0" not in md
        assert "已省略" in md
        assert estimate_tokens(md) <= 100
    finally:
        conn.close()


def test_the_newest_survives_at_every_budget_down_to_one_line():
    """无论预算压到多小，最新的一条都是最后被放弃的。"""
    conn = connect_for_tests()
    try:
        for index in range(20):
            _insert_fact(
                conn, f"l{index}", "教训", f"教训正文{index}",
                memory_type="lesson", created_at=1000 + index * HALF_LIFE,
            )
        for budget in (200, 160, 120, 100):
            md = _md(conn, max_tokens=budget)
            assert "教训正文19" in md, budget
            assert estimate_tokens(md) <= budget, budget
        # Below what the footer alone costs there is nothing to give up *to*:
        # the render is empty rather than a truncated fragment or a notice.
        assert _md(conn, max_tokens=5) == ""
    finally:
        conn.close()


def test_tight_budget_never_leaves_a_section_label_without_content():
    """紧预算下也不渲染空的分组标签（旧实现同样保证，这里钉住）。"""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "d1", "决定", "先回滚再排查", memory_type="decision_rule")
        for index in range(30):
            _insert_fact(
                conn, f"a{index}", f"属性{index}", "值" * 10,
                memory_type="semantic", created_at=1000 + index,
            )
        md = _md(conn, max_tokens=100)
        lines = md.splitlines()
        titles = {
            _COMPACT_LABEL_MARKER + title
            for title in ("决策规则", "教训", "流程（SOP）", "流程", "偏好", "属性", "示例", "事件")
        }
        for index, line in enumerate(lines[:-1]):
            if line in titles:
                assert lines[index + 1].startswith("- "), line
    finally:
        conn.close()


# ---- edge cases --------------------------------------------------------------


def test_empty_memory_notice_in_both_depths():
    """An empty store renders a non-blank notice, so callers can detect it."""
    conn = connect_for_tests()
    try:
        compact = _md(conn)
        detail = _md(conn, detail=True)
        assert compact.strip()
        assert "暂无" in compact
        assert "暂无" in detail
        assert not compact.startswith("# 记忆")
    finally:
        conn.close()


def test_facts_are_isolated_per_user():
    """Another user's facts never leak into a render."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师", user_id="u1")
        assert "暂无" in _md(conn, user_id="u2")
    finally:
        conn.close()


def test_retracted_facts_are_excluded():
    """Soft-deleted facts do not appear in either depth."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        conn.execute("UPDATE facts SET status='retracted' WHERE fact_id='f1'")
        conn.commit()
        assert "暂无" in _md(conn)
        assert "工程师" not in _md(conn, detail=True)
    finally:
        conn.close()


def test_compact_is_meaningfully_smaller_than_detail():
    """The whole point: the injected view is much cheaper than the full list."""
    conn = connect_for_tests()
    try:
        for index in range(20):
            _insert_fact(
                conn, f"f{index}", f"属性{index}", "值" * 12,
                memory_type="semantic",
                content="一段不算短的知识正文" * 6,
                created_at=1000 + index,
            )
        compact = _md(conn, max_tokens=4000)
        detail = _md(conn, max_tokens=4000, detail=True)
        assert estimate_tokens(compact) < estimate_tokens(detail)
    finally:
        conn.close()


# ---- budget selection: fast path must equal the reference exactly ------------

def _reference_select(sections: dict, max_tokens: int) -> dict:
    """The original selection: recompose and re-measure after every removal.

    Kept here as the oracle for the incremental implementation. It is O(n²), so
    it is only ever run on the small inputs of this test.
    """
    from atom_memory.summary import _compose

    titles = list(sections)
    kept = {title: list(range(len(lines))) for title, lines in sections.items()}
    give_up = [
        (score, order_index, line_index)
        for order_index, lines in enumerate(sections.values())
        for line_index, (_line, score) in enumerate(lines)
    ]
    give_up.sort(key=lambda item: (item[0], -item[1], -item[2]))
    for _score, order_index, line_index in give_up:
        if estimate_tokens(_compose(sections, kept)) <= max_tokens:
            break
        title = titles[order_index]
        if line_index in kept[title]:
            kept[title] = [index for index in kept[title] if index != line_index]
    return {title: indices for title, indices in kept.items() if indices}


def _random_sections(seed: int, sections: int, per_section: int):
    import random

    from atom_memory.summary import _SECTION_TITLES

    rng = random.Random(seed)
    titles = list(_SECTION_TITLES.values())[:sections]
    out = {}
    for title in titles:
        lines = []
        for index in range(per_section):
            width = rng.choice([1, 3, 8, 30, 80])
            alphabet = rng.choice(["字", "ab", "字a", " "])
            text = "- " + "".join(
                rng.choice(alphabet) for _ in range(width)
            ) + str(index)
            lines.append((text, round(rng.random(), 4)))
        out[title] = sorted(lines, key=lambda item: -item[1])
    return out


@pytest.mark.parametrize("seed", range(12))
def test_incremental_selection_matches_the_reference_exactly(seed):
    """The O(n log n) selection must pick the *same* lines as the O(n²) oracle.

    This is the contract that makes the speed-up safe: the budget decides which
    memories the model sees, so "faster but slightly different" would silently
    change what is remembered.
    """
    from atom_memory.summary import _compose, _select

    sections = _random_sections(seed, sections=4, per_section=12)
    for budget in (5, 20, 40, 80, 120, 200, 400, 1200):
        fast = _select(sections, budget)
        slow = _reference_select(sections, budget)
        assert fast == slow, f"seed={seed} budget={budget}"

        rendered = _compose(sections, fast)
        assert estimate_tokens(rendered) <= budget or not fast


def test_selection_does_not_re_render_the_artifact(monkeypatch):
    """The cost fix, asserted structurally rather than by wall-clock.

    The old loop called ``_compose`` (a full re-render plus a full re-measure)
    once per dropped line, which is what made freezing a snapshot quadratic.
    """
    import atom_memory.summary as summary_mod

    calls = {"compose": 0, "real": summary_mod._compose}

    def _counting_compose(sections, kept):
        calls["compose"] += 1
        return calls["real"](sections, kept)

    monkeypatch.setattr(summary_mod, "_compose", _counting_compose)
    sections = _random_sections(7, sections=3, per_section=40)
    summary_mod._select(sections, 150)
    assert calls["compose"] == 0, "selection must not re-render the artifact"


def test_incremental_selection_charges_each_label_what_it_renders():
    """标签的标记字符必须计入预算，否则硬上限会被撑破。

    `_select` 逐行回收预算时用的是自维护的字符计数，而 `_compose` 是重渲染测量。
    两者必须对**同一段文本**计数：标签在 `_render_body` 里带标记渲染，测量端如果
    只算裸标题，每个存活分组就会少算标记字符。构造恰好落在边界上的最小例子（两个
    分组各一行）后，未计入标记的实现会把 28 token 的产物交给 27 token 的预算——正是
    这个上限承诺不允许的泄漏。预算逐 token 扫过边界，因为泄漏只在差 1~2 token 处
    显形，稀疏采样会漏掉它。
    """
    from atom_memory.summary import _compose, _select

    sections = {
        _SECTION_TITLES["attribute"]: [("- alpha", 0.9)],
        _SECTION_TITLES["decision_rule"]: [("- beta", 0.8)],
    }
    ceilings = set()
    for budget in range(1, 64):
        kept = _select(sections, budget)
        if not kept:
            continue
        rendered = _compose(sections, kept)
        assert estimate_tokens(rendered) <= budget, (
            budget,
            estimate_tokens(rendered),
            rendered,
        )
        ceilings.add(estimate_tokens(rendered))
    # The sweep must actually cross the artifact's full size, or the assertion
    # above would hold vacuously (an empty render fits every budget). 23 is the
    # full two-label artifact: 12 tokens of body plus the 11-token footer. The
    # bound is deliberately the *measured* size rather than a loose one — a loose
    # bound stops being a check the day the artifact shrinks under it, which is
    # exactly what happened when the footer lost its "N 条事实 · 类型分布：" prefix.
    assert max(ceilings) >= 23


def test_selecting_a_large_render_is_not_quadratic():
    """A store with thousands of facts must not stall prompt assembly."""
    import time

    from atom_memory.summary import _select

    sections = _random_sections(11, sections=6, per_section=300)  # 1800 lines
    start = time.perf_counter()
    kept = _select(sections, 800)
    elapsed = time.perf_counter() - start
    assert kept, "a large store still renders something"
    # The measured O(n²) implementation took ~2 s at 1500 lines and ~8 s at
    # 3000; this bound is far above the fast path and far below the old one.
    assert elapsed < 1.0, f"selection took {elapsed:.2f}s"


# ---- the work-overview head --------------------------------------------------
#
# The compact depth used to be nothing but a type-grouped fact list, which
# answered the wrong question: a model could read every attribute it held and
# still not know what had been worked on. These tests pin the replacement — an
# overview section, and the old digest demoted to a reference section that only
# renders while the budget allows. There is no tool-usage section: the tool
# definitions already carry that, and these tests pin its absence.


def _overview_md(conn, max_tokens=600, overview=None, scope_context=None):
    """Render the compact depth with the overview head enabled."""
    return generate_summary(
        conn, "u1", max_tokens, False,
        scope_context=scope_context, overview=overview, use_overview=True,
    )


def test_the_head_leads_with_the_work_overview():
    """The head is the overview, and nothing else: no tool-usage section above it."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        md = _overview_md(conn)

        assert _COMPACT_LABEL_MARKER + "以前做过的工作" in md
        assert _COMPACT_LABEL_MARKER + "要了解细节" not in md
    finally:
        conn.close()


def test_a_cached_overview_is_rendered_verbatim():
    """The cached text is model-written prose; the renderer does not touch it."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        prose = "- 完成了记忆基数分层修复，并沉淀了沙箱测试的教训。"
        md = _overview_md(conn, overview=prose)
        assert prose in md
    finally:
        conn.close()


def test_without_a_cache_the_overview_is_derived_not_omitted():
    """An empty cache degrades to the deterministic render, never to nothing.

    This is the state of every deployment that has not run the out-of-band job
    yet, so an overview that only appeared once the model had run would leave
    exactly those users with the old fact-list experience.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        md = _overview_md(conn, max_tokens=400)
        head = md.split(_COMPACT_LABEL_MARKER + "以前做过的工作")[-1]
        assert "决定" in head or "条" in head, head
    finally:
        conn.close()


def test_the_guide_is_gone_and_tool_usage_is_not_repeated():
    """Tool usage lives in the tool definitions, not in the injected snapshot.

    The ``memory_*`` schemas already state what each tool does and how to call it,
    and the system prompt's awareness note points the model at them. Repeating
    that here spent the same sentences twice on every request of every session.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        md = _overview_md(conn)
        assert _COMPACT_LABEL_MARKER + "要了解细节" not in md
        for leaked in (
            "memory_recall",
            "memory_summary_detail",
            "memory_get",
            "memory_scope",
            "memory_overview",
            "action=",
            "factId=",
        ):
            assert leaked not in md, leaked
    finally:
        conn.close()


def test_the_overview_survives_a_tight_budget_without_the_guide_to_shrink():
    """The guide used to be the pick-up-the-slack section; now the overview is.

    Removing it must not reintroduce the failure the head was built to fix: at a
    mid budget the artifact still leads with what was worked on rather than
    dropping to a bare fact list.
    """
    conn = connect_for_tests()
    try:
        store = ScopeStore(conn, MemConfig())
        scope_id = store.create("project", "dsh-atom-memory", parent_id=1, confidence=1.0)
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        conn.execute(
            "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES ('f1', ?, 0)",
            (scope_id,),
        )
        conn.commit()
        for budget in (60, 100, 200, 400):
            md = _overview_md(conn, max_tokens=budget)
            assert "以前做过的工作" in md, budget
    finally:
        conn.close()


def test_the_reference_digest_is_demoted_below_the_head():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        md = _overview_md(conn, max_tokens=600)
        assert md.index("以前做过的工作") < md.index("属性")
    finally:
        conn.close()


def test_the_overview_survives_a_budget_that_kills_the_detail():
    """The detail digest is what gives way, not the overview.

    The earlier implementation had this backwards and emitted the static guide
    with no overview at all at a mid budget — the one outcome the change exists
    to prevent.
    """
    conn = connect_for_tests()
    try:
        for index in range(30):
            _insert_fact(conn, f"f{index}", f"属性{index}", "值" * 10, created_at=1000 + index)
        md = _overview_md(conn, max_tokens=120)
        assert "以前做过的工作" in md
    finally:
        conn.close()


def test_a_squeezed_head_drops_whole_bullets_not_half_a_line():
    """Degradation keeps the structure: fewer bullets, never a severed one.

    Clipping the body as one string would flatten the bullet list onto a single
    line (``_clip`` normalises whitespace) and then cut it mid-sentence, which
    loses the structure *and* costs more tokens. Whole bullets in order are what
    stays useful: the first names the biggest work unit, so one bullet still
    answers "what has been worked on".

    The overview has to be multi-line for this to mean anything — the
    deterministic fallback for a one-project store renders a single bullet, which
    both implementations output identically. So this drives the cached (prose)
    path, which is the realistic one anyway.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        overview = "\n".join(
            f"- 工作单元{i}：推进了第 {i} 项内容，包含若干决定与教训" for i in range(6)
        )

        seen: list[int] = []
        for budget in (400, 300, 200, 120, 80, 50, 30):
            md = _overview_md(conn, max_tokens=budget, overview=overview)
            assert estimate_tokens(md) <= budget, (budget, estimate_tokens(md))
            if "以前做过的工作" not in md:
                continue
            head = md.split("\n## ", 1)[0]
            body_lines = [l for l in head.splitlines()[1:] if l.strip()]
            seen.append(len(body_lines))
            # Every surviving bullet is whole: a clipped one would end in the
            # ellipsis `_clip` appends.
            assert not any(l.endswith("…") for l in body_lines), (budget, body_lines)
            # And it is still a bullet list, not one flattened paragraph.
            assert all(l.startswith("- ") for l in body_lines), (budget, body_lines)

        # The head survives whole at the roomy end and shrinks as the budget does,
        # rather than vanishing at the first squeeze.
        assert seen, "the head never rendered at any budget"
        assert seen[0] == 6, seen
        assert seen == sorted(seen, reverse=True), seen
        assert len(set(seen)) > 1, f"the head never actually degraded: {seen}"
    finally:
        conn.close()


def test_the_overview_outlives_the_detail_when_the_budget_tightens():
    """The overview is the payload; the reference digest is what gives way.

    This used to be phrased as "the guide gives up its examples first", because
    the guide was the disposable section. With the guide gone the same property
    has to hold against the only remaining competitor: at a tight budget the head
    survives whole and the detail digest is what shortens or disappears.
    """
    conn = connect_for_tests()
    try:
        store = ScopeStore(conn, MemConfig())
        scope_id = store.create("project", "demo", parent_id=1, confidence=1.0)
        for index in range(20):
            _insert_fact(conn, f"f{index}", f"属性{index}", "值" * 8, created_at=1000 + index)
            conn.execute(
                "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES (?, ?, 0)",
                (f"f{index}", scope_id),
            )
        conn.commit()

        roomy = _overview_md(conn, max_tokens=400)
        tight = _overview_md(conn, max_tokens=90)

        assert "以前做过的工作" in roomy
        assert "以前做过的工作" in tight
        # The detail digest is the section that pays: fewer of its lines survive
        # a tight budget than a roomy one.
        assert roomy.count("\n") > tight.count("\n")
    finally:
        conn.close()


def test_the_head_is_a_hard_cap_at_every_budget():
    """``max_tokens`` bounds the assembled artifact, head included."""
    conn = connect_for_tests()
    try:
        store = ScopeStore(conn, MemConfig())
        scope_id = store.create("project", "demo", parent_id=1, confidence=1.0)
        for index in range(25):
            _insert_fact(conn, f"f{index}", f"属性{index}", "值" * 8, created_at=1000 + index)
            conn.execute(
                "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES (?, ?, 0)",
                (f"f{index}", scope_id),
            )
        conn.commit()
        for budget in (10, 15, 25, 40, 60, 90, 130, 200, 400, 800):
            md = _overview_md(conn, max_tokens=budget)
            assert estimate_tokens(md) <= budget, (budget, estimate_tokens(md))
    finally:
        conn.close()


def test_the_cache_keeps_rendering_after_a_detail_only_change():
    """A detail change does not regenerate the overview, so it keeps serving.

    The end-to-end consequence of the refresh gate: ``read_overview`` given the
    *stored* fingerprint still returns the text, which is what the freeze path
    passes when it has decided not to wait for a new one.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        prose = "- 做过记忆基数分层修复。"
        stored = "fixed-fingerprint"
        write_overview(conn, "u1", prose, stored, 1)

        # The freeze path uses the cached text it holds rather than recomputing.
        assert read_overview(conn, "u1", stored) == prose
        md = _overview_md(conn, overview=read_overview(conn, "u1", stored))
        assert prose in md
    finally:
        conn.close()


def test_the_overview_head_is_off_by_default():
    """An existing caller's output is unchanged unless it opts in."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        md = generate_summary(conn, "u1", 600, False)
        assert "以前做过的工作" not in md
        assert "要了解细节" not in md
    finally:
        conn.close()


def test_an_empty_store_is_still_the_notice():
    """No facts means no head either: the notice is the whole artifact."""
    conn = connect_for_tests()
    try:
        md = _overview_md(conn)
        assert "暂无" in md
        assert "以前做过的工作" not in md
    finally:
        conn.close()


def test_the_overview_head_never_injects_a_fact_id():
    """The head shares the compact depth's rule: no UUIDs in the prompt."""
    conn = connect_for_tests()
    try:
        store = ScopeStore(conn, MemConfig())
        scope_id = store.create("project", "demo", parent_id=1, confidence=1.0)
        _insert_fact(conn, "aaaa1111-2222-3333-4444-555566667777", "决定", "x")
        conn.execute(
            "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES "
            "('aaaa1111-2222-3333-4444-555566667777', ?, 0)",
            (scope_id,),
        )
        conn.commit()
        md = _overview_md(conn, max_tokens=800)
        assert not UUID_RE.search(md)
    finally:
        conn.close()
