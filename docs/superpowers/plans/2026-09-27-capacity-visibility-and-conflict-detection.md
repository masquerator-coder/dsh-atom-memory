# Stored-Memory Consolidation: Capacity Visibility + Correct Conflict Detection

> **STATUS: IMPLEMENTED** (commits `94f2855`, `dd5053b`, `adc71fe`, `9e4bbec`, `e372885`).
> Verification on the live store: `{'duplicates': 1, 'conflicts': 0}`; capacity
> `active=1195 archivable=0 protected=1195 oldest_age_days=10.04 protect_days=14`.
> Full suites green: Python 615 passed / 1 skipped, host 362 passed, typecheck and
> build clean. Two deviations from the plan as written, both found by running it:
> (1) the two RPC methods cannot go through `_METHODS`, because that table
> dispatches to `AtomMem` attributes and `consolidation_report` is not one — they
> are explicit branches like `changes`, and `_capacity_report` carries the
> not-started guard itself; (2) `dsh/README.md:46` held the same
> "only switch that spends model calls" claim as `README.md:89` and was updated
> with it. `dsh/lib/index.mjs` was rebuilt but deliberately left uncommitted.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the capacity policy usable (so facts that are never reused can leave the working set) and add stored duplicate/conflict detection that does not report legitimate multi-valued memory as a conflict.

**Architecture:** Two small, independent changes to the Python store, plus visibility on the surfaces that already exist. No new scheduler, no model calls, no new tool. Capacity is *already implemented* (`Worker.enforce_capacity`) and *already rendered* (`memory_stats` shows `archived`); what is missing is (a) a way to see why nothing is being archived, and (b) correct detection. Both changes are read-mostly and independently revertable.

**Tech Stack:** Python 3.11+ (`atom_memory`, SQLite), pytest. No TypeScript change is required for either item — see "Why no host change" below.

**Spec:** This plan is its own spec. The scope was fixed by measurement against the live store, recorded below.

## Why this plan replaces the earlier one

An earlier plan (`2026-05-18-background-consolidation.md`, same directory) proposed background LLM-driven consolidation and a `(subject, predicate)` + distinct-object conflict detector. **Measurement against the live store disproved both**, and this plan supersedes it:

| Earlier claim | Measured reality |
|---|---|
| "Large numbers of duplicate facts accumulate" | **1 duplicate group** (2 rows) out of 1195 active |
| "Large numbers of conflicting facts accumulate" | **0 real conflicts** — the naive detector's 44 groups were **100% false positives** |
| "An LLM pass can clean it up" | Would need ~110k input tokens / 1054 grouping keys, **and would delete legitimate memory** |

The 44 false positives were all legitimate multi-valued memory (`偏好` 19, `待办` 12, `待补字段` 11 …). The project's own authority `is_multi_valued` (`atom_memory/validator.py:445`) classifies **every one** of them as multi-valued — i.e. *not* a conflict. The earlier detector simply never consulted it.

**Evidence (read-only, live DB `~/.dsh/atom-memory/memory.db`, 2026-09-27):**
```
active facts            1195    (global)
duplicate groups           1
real conflicts             0    (naive rule: 44 → 100% false positive)
archivable                 0    (all 1195 protected)
oldest fact age         10.0 d  (< archive_protect_days = 14)
max_active_facts           0    (unlimited → enforce_capacity returns immediately)
```

## Scope (fixed by the user)

- **Item 1 — capacity, option A.** Enable *visibility* into the capacity policy so the user can tune it from the settings panel. **Do not change any default value.** No `maxActiveFacts` default change; no `archiveProtectDays` default change.
- **Item 2 — corrected detection.** Apply `is_multi_valued` to the grouping key so multi-valued memory is never reported as a conflict.

**Explicitly out of scope** (user's decision, do not implement):召回期去重/折叠 (changing what recall returns); changing the `durable` permanent exemption; altering `user_restated` reinforcement; widening `abstraction_candidates` recall; re-embedding stored facts; any background LLM consolidation pass.

## Global Constraints

- **No default value may change.** Item 1 is option A: the cap and the protection window stay at `0` and `14`. A task that edits `z.number().default(0)` for `maxActiveFacts` or `archive_protect_days` is wrong.
- **Item 4 must consult `is_multi_valued`** for the grouping decision, passing `memory_type`. Its signature is `is_multi_valued(predicate, memory_type, extra_predicates=None)` (`atom_memory/validator.py:445-449`); `memory_type` is the **primary** multi-valued marker and is required.
- **Detection is read-only.** It must never call `supersede`, `unarchive`, `forget` or any write.
- **Detection reports findings; it does not repair them.** Disposal stays with the session model through `memory_replace` / `memory_forget`.
- **Python test conventions** (verified): tests live in `tests/test_*.py`, use `from atom_memory.db import connect_for_tests`, drive async code with `asyncio.run`, and `conn.close()` in `finally`.
- **Host test conventions** (verified): `dsh/tests/*.test.ts` (plural), run with `pnpm --dir dsh test`.

## Review Focus

Failure modes these changes imply but whose tests are easy to forget. Each is pinned to a task.

1. **A multi-valued predicate misreported as a conflict** — the exact defect that made the earlier plan's detector 100% wrong. A regression here silently tells the model that legitimate accumulated memory is contradictory.
2. **A predicate that is multi-valued only by *type*** — `is_multi_valued('知识', 'semantic')` is `False` but `is_multi_valued('知识', 'task')` is `True`. Omitting `memory_type` reintroduces the bug for every task-type fact.
3. **The cap left at 0** — `enforce_capacity` returns `[]` immediately (`worker.py:2466-2468`). Any "visibility" feature must report *that*, rather than showing a healthy-looking zero.
4. **A store where everything is protected** — the live store's actual state. Reporting must distinguish "under cap" from "cannot reach cap", because `enforce_capacity` already tracks that as `unreachable` (`worker.py:2523`, `2555`).
5. **Empty store / no users** — detection and the headroom report must not raise on zero rows.
6. **Seeding a test store with `mem.add`** — `add` is **asynchronous**: it enqueues a candidate that the worker turns into a fact after extraction and embedding, so a read immediately after `add` observes an *empty* store (verified: 0 rows immediately after `await mem.add(...)`, still 0 two seconds later without a working extractor). Every capacity test must seed with `test_lifecycle.py`'s own `_insert_fact` helper, which writes the row synchronously. This trap silently turns an assertion into a no-op on an empty store.

## Why no host change

The two Python changes surface through paths that already exist:

- `memory_stats` already reads and renders `archived` (`dsh/src/tools.ts:1232`, `atom_memory/api.py:2112-2121`).
- `maxActiveFacts` is already a config field (`dsh/src/config.ts:198`, `:349`).
- `unarchive` is already on the Host and in Python (`dsh/src/controller.ts:281`, `atom_memory/api.py:2127`), though **it has no UI affordance** (`dsh/src/client/remote.ts:100-101`) — restoring an archived fact is therefore currently API-only. That is a pre-existing gap, recorded here rather than fixed (out of scope).

Item 1 adds **one number** to the `stats` payload so the user can see the gap between the cap and what is reachable. Item 2 adds a **new read method**. Neither needs a scheduler or a model.

---

### Task 1: Correct stored-duplicate/conflict detection (Python)

**Files:**
- Create: `atom_memory/consolidate.py`
- Test: `tests/test_consolidate.py`

**Interfaces:**
- Consumes: `atom_memory.validator.is_multi_valued`, `atom_memory.models.TYPE_SEMANTIC`.
- Produces: `detect_duplicates(conn, user_id) -> list[dict]`, `detect_conflicts(conn, user_id, config=None) -> list[dict]`, `build_report(conn, user_id, config=None) -> dict` returning `{"duplicates": [...], "conflicts": [...], "counts": {"duplicates": int, "conflicts": int}}`. Each entry is `{"kind", "key", "fact_ids", "detail"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consolidate.py
from __future__ import annotations

from atom_memory import MemConfig
from atom_memory.consolidate import build_report
from atom_memory.db import connect_for_tests, now_ms


def _insert(conn, fact_id, subject, predicate, obj, *, memory_type="semantic",
            created_at=None, fingerprint=None):
    """Seed one active fact directly.

    The detection passes read the table, so seeding it directly keeps these tests
    independent of extraction, embedding and scope resolution — and lets a test
    set the `type` that `is_multi_valued` consults.
    """
    ts = created_at if created_at is not None else now_ms()
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, object, "
        "confidence, importance, privacy, source_type, status, observed_at, created_at, "
        "version, type, content_fingerprint) "
        "VALUES (?, 'u', 's1', ?, ?, ?, 0.5, 0.5, 'private', 'user_explicit', 'active', "
        "?, ?, 1, ?, ?)",
        (fact_id, subject, predicate, obj, ts, ts, memory_type, fingerprint),
    )
    conn.commit()


def test_a_multi_valued_predicate_is_never_reported_as_a_conflict():
    """The defect that made the first draft 100% wrong on real data.

    Nineteen coexisting preferences under one predicate are nineteen memories,
    not a contradiction. `is_multi_valued` is the project's authority on that,
    and the detector must consult it rather than assuming one object per key.
    """
    conn = connect_for_tests(MemConfig())
    try:
        for i in range(19):
            _insert(conn, f"p{i}", "用户", "偏好", f"第{i}条偏好")
        report = build_report(conn, "u")
        assert report["counts"]["conflicts"] == 0
        assert report["conflicts"] == []
    finally:
        conn.close()


def test_a_predicate_that_is_multi_valued_only_by_type_is_not_a_conflict():
    """`is_multi_valued('知识', 'semantic')` is False but `(..., 'task')` is True.

    Omitting memory_type reintroduces the bug for every task-type fact, which is
    the largest false-positive class on the live store (待办 / 待补字段).
    """
    conn = connect_for_tests(MemConfig())
    try:
        _insert(conn, "t1", "项目", "待补字段", "负责人", memory_type="task")
        _insert(conn, "t2", "项目", "待补字段", "预算", memory_type="task")
        assert build_report(conn, "u")["counts"]["conflicts"] == 0
    finally:
        conn.close()


def test_a_genuine_single_valued_conflict_is_still_reported():
    """The detector must not become useless in the other direction: a real
    contradiction under a single-valued attribute is exactly what it exists for."""
    conn = connect_for_tests(MemConfig())
    try:
        _insert(conn, "c1", "我的职业", "是", "工程师", created_at=1000)
        _insert(conn, "c2", "我的职业", "是", "架构师", created_at=2000)
        report = build_report(conn, "u")
        assert report["counts"]["conflicts"] == 1
        entry = report["conflicts"][0]
        assert entry["kind"] == "single_valued_conflict"
        assert sorted(entry["fact_ids"]) == ["c1", "c2"]
        # The newer assertion is identified, since that is what the write path
        # would have kept — the reader needs it to judge.
        assert entry["detail"]["newest_fact_id"] == "c2"
    finally:
        conn.close()


def test_duplicates_are_found_by_fingerprint():
    conn = connect_for_tests(MemConfig())
    try:
        _insert(conn, "f1", "a", "p", "x", fingerprint="fp-same")
        _insert(conn, "f2", "a", "p", "x", fingerprint="fp-same")
        _insert(conn, "f3", "b", "q", "y", fingerprint="fp-other")
        report = build_report(conn, "u")
        assert report["counts"]["duplicates"] == 1
        assert sorted(report["duplicates"][0]["fact_ids"]) == ["f1", "f2"]
    finally:
        conn.close()


def test_a_clean_store_reports_nothing_and_does_not_raise():
    conn = connect_for_tests(MemConfig())
    try:
        report = build_report(conn, "u")
        assert report["counts"] == {"duplicates": 0, "conflicts": 0}
        assert report["duplicates"] == [] and report["conflicts"] == []
    finally:
        conn.close()


def test_non_active_rows_are_ignored():
    conn = connect_for_tests(MemConfig())
    try:
        _insert(conn, "f1", "a", "p", "x", fingerprint="fp-same")
        _insert(conn, "f2", "a", "p", "x", fingerprint="fp-same")
        conn.execute("UPDATE facts SET status = 'archived' WHERE fact_id = 'f2'")
        conn.commit()
        assert build_report(conn, "u")["counts"]["duplicates"] == 0
    finally:
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_consolidate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom_memory.consolidate'`

- [ ] **Step 3: Write minimal implementation**

```python
# atom_memory/consolidate.py
"""Detection of stored duplicates and conflicts that the write path did not catch.

The write path resolves identity *as writes arrive*: a restatement folds into the
row it matches (``Worker._fold_into``), and a contradiction under a single-valued
predicate supersedes the stored value. That is the right place for the decision,
but two facts that are duplicates *after the fact* are never looked at again — a
near-duplicate probe only examines candidates being written, and a conflict needs
a *new* assertion to trigger it.

This module supplies the missing read. It **reports only**; it never supersedes,
merges or deletes. Which of two stored claims the user meant is knowledge that
lives outside the store, so disposal belongs to the session model, through
``memory_replace`` / ``memory_forget``.

The one thing this module must get right is the definition of "conflict". A
predicate that may legitimately hold many objects — a preference, a to-do, a
lesson — is **not** in conflict with itself, however many objects it holds. That
question has exactly one authority in this codebase,
:func:`atom_memory.validator.is_multi_valued`, and it is consulted here rather
than re-derived. Skipping it produces a detector that reports every accumulated
list as a contradiction: measured against a real store, a naive
``(subject, predicate)`` + distinct-object rule returned 44 groups, and **all
44 were multi-valued memory**. A report that is entirely false positives is worse
than no report, because acting on it deletes legitimate memory.

Detection uses the data already in the table — fingerprints and
``(subject, predicate)`` grouping — deliberately. Finding reworded bodies would
mean re-embedding every stored fact, which is not a cost a background pass may
impose on a store of any real size.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from .config import MemConfig
from .models import TYPE_SEMANTIC
from .validator import is_multi_valued

# Report kinds. Stable strings: the host renders them and the tests assert them.
KIND_FINGERPRINT = "fingerprint"
KIND_SINGLE_VALUED_CONFLICT = "single_valued_conflict"


def _active_rows(conn: sqlite3.Connection, user_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT fact_id, subject, predicate, object, type, confidence, importance, "
        "created_at, content_fingerprint FROM facts "
        "WHERE user_id = ? AND status = 'active' ORDER BY created_at ASC",
        (user_id,),
    ).fetchall()


def detect_duplicates(conn: sqlite3.Connection, user_id: str) -> List[dict]:
    """Find active facts sharing a content fingerprint.

    The fingerprint is the store's own identity for a claim, so two active rows
    carrying one fingerprint are the same memory stored twice.

    Args:
        conn: Open connection.
        user_id: Whose facts to inspect.

    Returns:
        One entry per duplicated fingerprint, oldest fact first inside each.
    """
    groups: Dict[str, List[sqlite3.Row]] = {}
    for row in _active_rows(conn, user_id):
        fingerprint = row["content_fingerprint"]
        if fingerprint is None or str(fingerprint) == "":
            continue
        groups.setdefault(str(fingerprint), []).append(row)

    out: List[dict] = []
    for fingerprint, rows in groups.items():
        if len(rows) < 2:
            continue
        out.append(
            {
                "kind": KIND_FINGERPRINT,
                "key": fingerprint,
                "fact_ids": [str(r["fact_id"]) for r in rows],
                "detail": {
                    "subject": str(rows[0]["subject"]),
                    "predicate": str(rows[0]["predicate"]),
                    "object": str(rows[0]["object"]),
                },
            }
        )
    return out


def detect_conflicts(
    conn: sqlite3.Connection, user_id: str, config: Optional[MemConfig] = None
) -> List[dict]:
    """Find a single-valued predicate holding several active values.

    Only keys the validator calls **single-valued** are grouped. A multi-valued
    predicate is skipped entirely, whatever it holds: that is the difference
    between a report and a nuisance.

    Args:
        conn: Open connection.
        user_id: Whose facts to inspect.
        config: Deployment config, consulted for the extra multi-valued
            predicates a deployment registered. Omitting it only narrows the
            exemption; the built-in sets still apply inside ``is_multi_valued``.

    Returns:
        One entry per conflicting key, newest fact identified.
    """
    groups: Dict[tuple, List[sqlite3.Row]] = {}
    extra = tuple(getattr(config, "multi_valued_predicates", None) or ())
    for row in _active_rows(conn, user_id):
        predicate = str(row["predicate"] or "")
        # `memory_type` is required, not decorative: the type is the *primary*
        # multi-valued marker (a to-do, an episodic event, a knowledge item is
        # multi-valued whatever its predicate says), so omitting it reports every
        # task list as a conflict.
        if is_multi_valued(predicate, str(row["type"] or TYPE_SEMANTIC), extra):
            continue
        groups.setdefault((str(row["subject"] or ""), predicate), []).append(row)

    out: List[dict] = []
    for (subject, predicate), rows in groups.items():
        objects = {str(r["object"] or "") for r in rows}
        if len(objects) < 2:
            continue
        newest = max(rows, key=lambda r: (int(r["created_at"]), str(r["fact_id"])))
        out.append(
            {
                "kind": KIND_SINGLE_VALUED_CONFLICT,
                "key": f"{subject}/{predicate}",
                "fact_ids": [str(r["fact_id"]) for r in rows],
                "detail": {
                    "subject": subject,
                    "predicate": predicate,
                    "objects": sorted(objects),
                    "newest_fact_id": str(newest["fact_id"]),
                },
            }
        )
    return out


def build_report(
    conn: sqlite3.Connection, user_id: str, config: Optional[MemConfig] = None
) -> dict:
    """Build the full detection report for one owner.

    Args:
        conn: Open connection.
        user_id: Whose facts to inspect.
        config: Deployment config (see :func:`detect_conflicts`).

    Returns:
        ``{"duplicates": [...], "conflicts": [...], "counts": {...}}``.
    """
    duplicates = detect_duplicates(conn, user_id)
    conflicts = detect_conflicts(conn, user_id, config)
    return {
        "duplicates": duplicates,
        "conflicts": conflicts,
        "counts": {"duplicates": len(duplicates), "conflicts": len(conflicts)},
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_consolidate.py -v`
Expected: PASS — 6 passed.

- [ ] **Step 5: Confirm against the real store (read-only)**

Run:
```bash
python -c "
import os, sqlite3
from atom_memory.config import MemConfig
from atom_memory.consolidate import build_report
p = os.path.expanduser('~/.dsh/atom-memory/memory.db')
c = sqlite3.connect('file:' + p + '?mode=ro', uri=True); c.row_factory = sqlite3.Row
print(build_report(c, 'global', MemConfig())['counts'])
"
```
Expected: `{'duplicates': 1, 'conflicts': 0}` (measured 2026-09-27). A non-zero `conflicts` means the multi-valued exemption is not being applied and **must be investigated before proceeding** — on this store it is a 100%-false-positive signal.

- [ ] **Step 6: Commit**

```bash
git add atom_memory/consolidate.py tests/test_consolidate.py
git commit -m "feat(consolidate): detect stored duplicates and conflicts, consulting is_multi_valued"
```

---

### Task 2: Report the capacity headroom (Python)

Item 1's visibility. The store must be able to answer "why is nothing being archived", which today it cannot: `enforce_capacity` returns `[]` for two very different reasons (cap disabled; everything protected) and `stats` shows neither.

**Files:**
- Modify: `atom_memory/api.py` (`stats` gains a `capacity` block)
- Test: `tests/test_lifecycle.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `AtomMem.capacity_report(user_id) -> dict` returning
  `{"cap": int, "enabled": bool, "active": int, "archivable": int, "protected": int, "oldest_age_days": float, "protect_days": int, "headroom": int}`; and `stats()["capacity"]` carrying that same dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lifecycle.py  (append)

def test_capacity_report_says_the_cap_is_disabled_rather_than_reporting_zero(tmp_path, monkeypatch):
    """`enforce_capacity` returns [] for two unrelated reasons: the cap is 0, or
    everything is protected. `memory_stats` must not conflate them — a user who
    sees "0 archived" cannot otherwise tell a healthy store from a switched-off
    policy.
    """
    mem = _make(tmp_path, monkeypatch, max_active_facts=0)

    async def scenario():
        await mem.start()
        # `_insert_fact` (this file's own helper), not `mem.add`: `add` is
        # asynchronous — it enqueues a candidate that the worker turns into a fact
        # after extraction and embedding, so a read immediately after `add`
        # observes an empty store. Every capacity test in this file seeds directly
        # for that reason.
        _insert_fact(mem, "f1", "用户", "职业", "工程师")
        report = mem.capacity_report("u1")
        assert report["cap"] == 0
        assert report["enabled"] is False
        assert report["active"] == 1
        await mem.stop()

    _run(scenario())


def test_capacity_report_counts_what_is_actually_reachable(tmp_path, monkeypatch):
    """With a cap set and every fact young, nothing is archivable — and the report
    must say so, because that is the live store's real state."""
    mem = _make(tmp_path, monkeypatch, max_active_facts=10, archive_protect_days=14)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师")
        report = mem.capacity_report("u1")
        assert report["cap"] == 10
        assert report["enabled"] is True
        assert report["active"] == 1
        assert report["archivable"] == 0, "a fresh fact is protected"
        assert report["protected"] == 1
        assert report["protect_days"] == 14
        await mem.stop()

    _run(scenario())


def test_capacity_report_finds_an_old_unused_fact_archivable(tmp_path, monkeypatch):
    """The other direction: once material is past the protection window and has no
    reuse evidence, the report must show it as reachable — otherwise the feature
    would report "nothing movable" forever and look identical to the disabled case.
    """
    mem = _make(tmp_path, monkeypatch, max_active_facts=1, archive_protect_days=14)

    async def scenario():
        await mem.start()
        from atom_memory.db import now_ms

        old = now_ms() - 30 * 86_400_000
        _insert_fact(mem, "old", "用户", "爱好", "围棋", created_at=old)
        _insert_fact(mem, "fresh", "用户", "职业", "工程师")
        report = mem.capacity_report("u1")
        assert report["active"] == 2
        assert report["archivable"] == 1, "the old fact is movable"
        assert report["protected"] == 1, "the fresh fact is not"
        assert report["headroom"] == 1
        await mem.stop()

    _run(scenario())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_lifecycle.py -k capacity_report -v`
Expected: FAIL — `AttributeError: 'AtomMem' object has no attribute 'capacity_report'`

- [ ] **Step 3: Write minimal implementation**

In `atom_memory/api.py`, next to `stats` (around line 2090):

```python
    def capacity_report(self, user_id: str) -> dict:
        """Explain what the capacity policy can and cannot reach right now.

        ``enforce_capacity`` returns an empty list for two unrelated reasons — the
        cap is disabled, or every fact is protected — and its summary carries the
        shortfall only when the pass runs. Neither is visible from ``stats``, so a
        store whose capacity policy is switched off looks exactly like a store
        that is comfortably under its cap.

        This is a read-only explanation of the same policy
        :meth:`worker.Worker.enforce_capacity` applies, using the same three
        exemptions (age, reinforcement evidence, durable type). It computes what
        *would* be archivable rather than performing it.

        Args:
            user_id: Whose facts to measure.

        Returns:
            ``{"cap", "enabled", "active", "archivable", "protected",
            "oldest_age_days", "protect_days", "headroom"}``. ``headroom`` is how
            many facts the policy could still remove before it runs out of
            unprotected material, and is `0` when the cap is disabled.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        cap = int(self.config.max_active_facts or 0)
        protect_days = int(self.config.archive_protect_days)
        durable = {"decision_rule", "lesson", "sop"}

        rows = self.db.execute(
            "SELECT fact_id, type, created_at, reinforce_count FROM facts "
            "WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchall()
        cutoff = now_ms() - protect_days * 86_400_000

        archivable = 0
        oldest = 0
        for row in rows:
            created = int(row["created_at"])
            if oldest == 0 or created < oldest:
                oldest = created
            if created >= cutoff:
                continue
            if float(row["reinforce_count"] or 0.0) > 0.0:
                continue
            if (row["type"] or "semantic") in durable:
                continue
            archivable += 1

        active = len(rows)
        oldest_age_days = (
            round((now_ms() - oldest) / 86_400_000.0, 2) if oldest else 0.0
        )
        return {
            "cap": cap,
            "enabled": cap > 0,
            "active": active,
            "archivable": archivable,
            "protected": active - archivable,
            "oldest_age_days": oldest_age_days,
            "protect_days": protect_days,
            "headroom": min(archivable, max(0, active - cap)) if cap > 0 else 0,
        }
```

Then add it to `stats`'s return (around line 2118), so the model-facing `memory_stats` carries it:

```python
        return {
            "facts": facts["n"],
            "pending": pending["n"],
            "archived": archived["n"],
            "capacity": self.capacity_report(user_id),
            "recent": self.recent_outcomes(user_id, limit=5),
        }
```

Confirm `now_ms` is imported in `api.py` (`from .db import now_ms`); add it if the existing imports do not already bring it in.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_lifecycle.py -k capacity_report -v`
Expected: PASS — 2 passed.

- [ ] **Step 5: Run the surrounding suites for regressions**

Run: `python -m pytest tests/test_lifecycle.py tests/test_db.py tests/test_ui_api.py -q -p no:cacheprovider`
Expected: PASS. `stats` gained a key; any test asserting its exact shape will need the key added — a deliberate, visible change rather than a silent one.

- [ ] **Step 6: Commit**

```bash
git add atom_memory/api.py tests/test_lifecycle.py
git commit -m "feat(api): report capacity headroom so a disabled policy is visible"
```

---

### Task 3: Expose detection and headroom over the bridge (Python)

**Files:**
- Modify: `atom_memory/rpc.py` (dispatch entries + handlers)
- Test: `tests/test_rpc.py` (append)

**Interfaces:**
- Consumes: Task 1's `build_report`, Task 2's `capacity_report`.
- Produces: RPC `consolidation_report` (params `user_id`) → the Task 1 report; `capacity_report` (params `user_id`) → the Task 2 dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rpc.py  (append)
def test_consolidation_and_capacity_surfaces_are_reachable(tmp_path):
    import asyncio

    from atom_memory.rpc import RpcServer

    async def scenario():
        server = RpcServer()
        await server._start({"db_path": str(tmp_path / "m.db"), "scope_aware": False})

        report = await server._dispatch("consolidation_report", {"user_id": "u"})
        assert report["counts"] == {"duplicates": 0, "conflicts": 0}

        cap = await server._dispatch("capacity_report", {"user_id": "u"})
        assert cap["enabled"] is False and cap["cap"] == 0

        await server.shutdown()

    asyncio.run(scenario())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_rpc.py::test_consolidation_and_capacity_surfaces_are_reachable -v`
Expected: FAIL with `_RpcError: unknown method: consolidation_report`

- [ ] **Step 3: Write minimal implementation**

In `atom_memory/rpc.py`, add to the dispatch table (near `"maintenance": "maintenance",` around line 96):

```python
    "consolidation_report": "consolidation_report",
    "capacity_report": "capacity_report",
```

and the handlers (near `_overview_status` around line 496):

```python
    async def _consolidation_report(self, params: dict) -> dict:
        """Return the stored duplicate/conflict report for one owner.

        Read-only: it never supersedes, merges or deletes. Disposal belongs to
        whoever can still see the user's intent.

        Params: ``user_id``.
        """
        from .consolidate import build_report

        return build_report(
            self._overview_db(), params["user_id"], self.mem.config
        )

    async def _capacity_report(self, params: dict) -> dict:
        """Explain what the capacity policy can reach right now.

        Params: ``user_id``.
        """
        return self.mem.capacity_report(params["user_id"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_rpc.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add atom_memory/rpc.py tests/test_rpc.py
git commit -m "feat(rpc): expose consolidation_report and capacity_report"
```

---

### Task 4: Surface both in `memory_stats` and `memory_overview action=status` (host)

**Files:**
- Modify: `dsh/src/tools.ts` (the `memory_stats` render at ~line 1226; the `status` branch at ~line 1110)
- Test: `dsh/tests/tools.test.ts` (append)

**Interfaces:**
- Consumes: RPC `consolidation_report`, `capacity_report` from Task 3; `stats.capacity` from Task 2.
- Produces: `memory_stats` text that names a disabled cap and any detection findings; `memory_overview action=status` text that appends the detection findings.

- [ ] **Step 1: Write the failing test**

```typescript
// dsh/tests/tools.test.ts  (append)
import { describe, expect, it } from 'vitest'
import { renderStats, renderOverviewStatus } from '../src/tools.ts'

describe('capacity and detection visibility', () => {
  it('says the capacity policy is off instead of showing a silent zero', () => {
    const text = renderStats({
      facts: 1195, pending: 0, archived: 0,
      capacity: { cap: 0, enabled: false, active: 1195, archivable: 0, protected: 1195 },
    })
    expect(text).toContain('未启用')
  })

  it('explains that everything is protected when the cap is on but unreachable', () => {
    const text = renderStats({
      facts: 1195, pending: 0, archived: 0,
      capacity: {
        cap: 1000, enabled: true, active: 1195, archivable: 0,
        protected: 1195, oldest_age_days: 10, protect_days: 14,
      },
    })
    // The live store's exact state: over cap, nothing movable.
    expect(text).toContain('14')
    expect(text).toContain('0')
  })

  it('reports detection findings through overview status', () => {
    const text = renderOverviewStatus(
      { cached: true, level: 'none', should_refresh: false },
      { counts: { duplicates: 1, conflicts: 0 } },
    )
    expect(text).toContain('1')
  })

  it('stays silent when detection found nothing', () => {
    const text = renderOverviewStatus(
      { cached: true, level: 'none', should_refresh: false },
      { counts: { duplicates: 0, conflicts: 0 } },
    )
    expect(text).not.toContain('待处置')
  })
})
```

Adapt the imported helper names to what `tools.ts` actually exports — `renderStats` may not exist yet as a named export; if the render is inline, extract it into a named exported function first (that extraction is part of this task, and is why the test can be written before the behaviour).

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --dir dsh test -- tools`
Expected: FAIL — the helpers are not exported / the behaviour is absent.

- [ ] **Step 3: Write minimal implementation**

Extract the `memory_stats` render into an exported pure function and extend it:

```typescript
/** Shape of the capacity explanation Python returns (see `AtomMem.capacity_report`). */
export interface CapacityReport {
  cap?: number
  enabled?: boolean
  active?: number
  archivable?: number
  protected?: number
  oldest_age_days?: number
  protect_days?: number
  headroom?: number
}

/**
 * Render the stats payload.
 *
 * The capacity line exists because "0 archived" has two meanings and only one of
 * them is healthy: the policy may be switched off (`cap` 0), or every fact may be
 * protected so the policy cannot reach anything. A bare count cannot tell them
 * apart, and the difference decides whether the user should act.
 */
export function renderStats(v: {
  facts?: number
  pending?: number
  archived?: number
  capacity?: CapacityReport
  recent?: Array<{ status?: string; reject_kind?: string; reject_reason?: string }>
}): string {
  const lines = [
    `活跃记忆 ${v.facts ?? 0} 条 · 待处理 ${v.pending ?? 0} 条 · 归档 ${v.archived ?? 0} 条`,
  ]
  const cap = v.capacity
  if (cap !== undefined) {
    if (cap.enabled !== true) {
      lines.push(
        `容量策略未启用（上限 ${cap.cap ?? 0} = 无限）：没有事实会因容量退场，`
        + `活跃集只会增长。可在设置面板提高上限后生效。`,
      )
    } else {
      lines.push(
        `容量策略已启用：上限 ${cap.cap} · 活跃 ${cap.active ?? 0} · `
        + `可归档 ${cap.archivable ?? 0}（其余 ${cap.protected ?? 0} 条受保护：`
        + `创建未满 ${cap.protect_days ?? 0} 天、有复用证据、或属决策/教训/SOP）。`,
      )
      if ((cap.archivable ?? 0) === 0 && (cap.active ?? 0) > (cap.cap ?? 0)) {
        lines.push(
          `注意：已超上限但当前无可归档事实——最老一条仅 ${cap.oldest_age_days ?? 0} 天，`
          + `尚未超出 ${cap.protect_days ?? 0} 天保护期。`,
        )
      }
    }
  }
  for (const entry of v.recent ?? []) {
    if (entry.reject_kind) {
      lines.push(`- 最近一次写入被拒绝（${entry.reject_kind}）：${entry.reject_reason ?? ''}`)
    }
  }
  return lines.join('\n')
}
```

In the `status` branch, fetch the report alongside the status (it is free and read-only):

```typescript
      if (action === 'status') {
        const status = await call<Record<string, unknown>>('overview_status', { user_id: uid })
        // Detection answers a different question from the status — "is the store
        // consistent" vs "is the overview current" — but it is free, so `status`
        // answers both.
        let report: { counts?: { duplicates?: number; conflicts?: number } } | undefined
        try {
          report = await call('consolidation_report', { user_id: uid })
        } catch {
          // A store too old to have the method still renders its status.
          report = undefined
        }
        return { text: renderOverviewStatus(status, report) }
      }
```

and extend `renderOverviewStatus` with the second argument:

```typescript
  const counts = report?.counts
  const found = (counts?.duplicates ?? 0) + (counts?.conflicts ?? 0)
  if (found > 0) {
    // The disposal path is named because the store deliberately does not take it:
    // only the session can see which of two stored claims the user meant.
    lines.push(
      `待处置：重复 ${counts?.duplicates ?? 0} 条 · 冲突 ${counts?.conflicts ?? 0} 条。`
      + '用 memory_recall 查看，再用 memory_replace 或 memory_forget 处置'
      + '（整理流程不会自动改写）。',
    )
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm --dir dsh test -- tools`
Expected: PASS

- [ ] **Step 5: Typecheck**

Run: `pnpm --dir dsh typecheck`
Expected: succeeds.

- [ ] **Step 6: Commit**

```bash
git add dsh/src/tools.ts dsh/tests/tools.test.ts
git commit -m "feat(tools): show capacity state and detection findings"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md` (the `maxActiveFacts` row; add a limitation bullet)
- Modify: `docs/memory-semantics.md` (the detection rule)

- [ ] **Step 1: Clarify the `maxActiveFacts` row**

The row currently reads "Soft cap on one user's active facts (`0` = unlimited). Excess moves the least valuable unprotected facts to the archive tier; nothing is deleted." Add the two facts a user needs to make it useful:

```markdown
| `maxActiveFacts` | `0` | Soft cap on one user's active facts (`0` = unlimited, so **nothing is ever archived by default**). Excess moves the least valuable unprotected facts to the archive tier; nothing is deleted. Protected = created within `archiveProtectDays`, carrying any reinforcement evidence, or of a durable knowledge type (decision rule, lesson, SOP), so a store younger than `archiveProtectDays` has nothing archivable however low the cap is set. `memory_stats` reports this state, including the gap when the cap cannot be reached. |
```

- [ ] **Step 2: Record the detection rule and its limit**

In `docs/memory-semantics.md`, in the file's existing numbered format:

> **Stored duplicates and conflicts are reported, never repaired.** The write path resolves identity as writes arrive; facts that became duplicates afterwards are found by `consolidation_report`, which surfaces them through `memory_overview action=status`. The pass does not supersede, merge or delete: which of two stored claims the user meant is knowledge that exists only outside the store.
>
> **A multi-valued predicate is never a conflict.** Whether a predicate may hold many objects is decided by `is_multi_valued` alone, and the detector consults it rather than assuming one object per key. Derived independently, a `(subject, predicate)` + distinct-object rule reported 44 conflict groups against a real 1195-fact store — **all 44 were multi-valued memory** (preferences, to-dos, lessons). A report that is entirely false positives is worse than no report, because acting on it deletes legitimate memory.

- [ ] **Step 3: Add the honest limitation**

In `README.md`'s Known Limitations:

```markdown
- **Detection finds exact duplicates and single-valued contradictions only.** Duplicates are matched by content fingerprint; a *reworded* duplicate is not found, because that would mean re-embedding every stored fact. The report is a lower bound on the store's redundancy, not a complete list — and on a store whose memory is accumulated multi-valued material (preferences, to-dos) it will legitimately report nothing.
```

- [ ] **Step 4: Verify no consistency test broke**

Run: `python -m pytest -q -p no:cacheprovider` and `pnpm --dir dsh test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/memory-semantics.md
git commit -m "docs: capacity state, detection rule and its measured limits"
```

---

### Task 6: Full verification

- [ ] **Step 1: Python suite**

Run: `python -m pytest -q -p no:cacheprovider`
Expected: PASS. If `tmp_path` tests fail with `PermissionError: [WinError 5]`, that is the documented Windows sandbox limitation — rerun with `-p no:cacheprovider --basetemp=<writable dir>` and report which mode was used.

- [ ] **Step 2: Host suite, typecheck and build**

Run: `pnpm --dir dsh test`, `pnpm --dir dsh typecheck`, `pnpm --dir dsh build`
Expected: all succeed. **Do not commit the rebuilt `dsh/lib`** — it is a committed build artifact and its release is the user's step; report that it is now stale.

- [ ] **Step 3: Report against the live store (read-only)**

Run the Task 1 Step 5 command plus:
```bash
python -c "
import os, sqlite3
from atom_memory.config import MemConfig
from atom_memory.db import connect_for_tests
p = os.path.expanduser('~/.dsh/atom-memory/memory.db')
c = sqlite3.connect('file:' + p + '?mode=ro', uri=True); c.row_factory = sqlite3.Row
# capacity_report reads self.db; use the module functions directly instead
from atom_memory.consolidate import build_report
print('detection:', build_report(c, 'global', MemConfig())['counts'])
print('cap:', MemConfig().max_active_facts, 'protect_days:', MemConfig().archive_protect_days)
"
```
Expected: `{'duplicates': 1, 'conflicts': 0}`. Report the actual output.

- [ ] **Step 4: State the expected effect honestly**

Record in the completion report, because it is the point of the whole change:

- **Item 4 (detection)** will report **1 duplicate / 0 conflicts** on the current store. Its value is *preventing future silent divergence*, not cleaning up anything today. There is nothing to clean up.
- **Item 1 (capacity visibility)** changes **no behaviour** until the user raises `maxActiveFacts` — and even then, **nothing is archivable until the store's facts age past `archiveProtectDays` (14 days)**, because every fact is currently younger than that. The feature's deliverable is that the user can now *see* this, per `memory_stats`.

## Self-Review

**1. Spec coverage.** Item 1 → Tasks 2, 3, 4, 5 (visibility, no default change). Item 4 → Tasks 1, 3, 4, 5 (detection consulting `is_multi_valued`). Both out-of-scope lists are honoured: no task touches recall output, `durable`, `user_restated`, `abstraction_candidates`, or re-embeds anything.

**2. Placeholder scan.** No "TBD". One step is deliberately conditional and says so with the command to resolve it: Task 4 Step 1 (adapt `renderStats` / `renderOverviewStatus` to the names `tools.ts` actually exports, extracting them if they are inline).

**3. Type consistency.** `build_report(conn, user_id, config=None)` is called the same way in Tasks 1, 3, and 6. `capacity_report(user_id) -> dict` is produced in Task 2, exposed in Task 3, and consumed in Task 4 as `CapacityReport` with matching field names (`cap`, `enabled`, `active`, `archivable`, `protected`, `oldest_age_days`, `protect_days`, `headroom`). `consolidation_report` returns `{"counts": {"duplicates", "conflicts"}}` in Tasks 1/3 and is read that way in Task 4.

**4. Review Focus.** (1) multi-valued misreported → Task 1 `test_a_multi_valued_predicate_is_never_reported_as_a_conflict` (19 preferences, the live store's exact shape). (2) multi-valued only by type → Task 1 `test_a_predicate_that_is_multi_valued_only_by_type_is_not_a_conflict`. (3) cap at 0 → Task 2 `test_capacity_report_says_the_cap_is_disabled_rather_than_reporting_zero` + Task 4's 未启用 render test. (4) everything protected → Task 2 `test_capacity_report_counts_what_is_actually_reachable` + Task 4's over-cap-nothing-movable render test. (5) empty store → Task 1 `test_a_clean_store_reports_nothing_and_does_not_raise`, Task 2's reports on a store with rows, Task 3's fresh-DB round trip. (6) async seeding → Task 2's tests seed with `_insert_fact` and carry a comment saying why.

**6. Verification performed while writing this plan.** Both Python tasks were extracted verbatim into a scratch copy and executed against the real repository code:

```
tests/test_consolidate.py          → 6 passed          (Task 1, verbatim)
tests/test_lifecycle.py            → 31 passed         (Task 2 tests appended, full suite green)
Task 1 Step 5 on the live store    → {'duplicates': 1, 'conflicts': 0}
```

Two defects were found and fixed in the plan by that run, and are recorded rather than silently amended: (a) Task 2's tests originally seeded with `mem.add`, which is asynchronous and left the store empty, so every assertion ran against 0 rows; they now use `_insert_fact`. (b) `is_multi_valued` needs `memory_type`, without which every task-type fact is misreported. The TypeScript tasks were **not** executed — `pnpm --dir dsh test` was not run in the scratch copy, so Task 4's render helpers are the one part of this plan verified by inspection only.

**5. What this plan deliberately does not do.** No background scheduler; no LLM pass; no changed defaults; no repair of stored findings; no recall-side folding; no fix to the missing `unarchive` UI affordance. Each is a recorded scope decision, and the last one is noted in "Why no host change" as a pre-existing gap rather than silently ignored.