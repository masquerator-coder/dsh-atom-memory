"""Stage-by-stage latency attribution for the recall path.

Measures every stage of a real recall against the live store, so optimisation
decisions rest on measured cost rather than assumed cost.

Stages covered, in call order:

    embed(query)                FastEmbed inference for the query text
    _scope_view                 scope resolution + predicate construction
    _vector_knn                 sqlite-vec exact KNN (scope-filtered)
    _fts_search                 FTS5 lexical leg (scope-filtered)
    rrf_merge                   Reciprocal Rank Fusion
    _fetch_facts                row fetch for the fused ids
    _rerank                     the four-term + scope-term ranking formula
    _load_conflicts             api.recall's conflict scan (outside search())
    ---------------------------
    search() total              retriever.search end to end
    recall() total              api.recall end to end
    rpc round trip              stdio NDJSON through a live child process

Run:  python bench/stage_timing.py [--db PATH] [--repeat N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atom_memory.config import MemConfig  # noqa: E402
from atom_memory.db import open_db  # noqa: E402
from atom_memory.embedder import Embedder  # noqa: E402
from atom_memory.retriever import Retriever, rrf_merge  # noqa: E402

DEFAULT_DB = str(Path.home() / ".dsh" / "atom-memory" / "memory.db")

# A mix of shapes a real session produces: Chinese prose, code identifiers,
# short keyword queries and a long natural-language one.
QUERIES = [
    "用户偏好 项目约定",
    "TypeScript 严格模式要开启",
    "低空物流 专业申报",
    "检索优化 方案设计",
    "天津市 人工智能 示范课",
    "记忆 去重 策略",
    "插件 配置 加载 失败",
    "作用域 解析 失败 降级",
    "向量 索引 性能 优化",
    "摘要 注入 预算 估算",
]


class Timer:
    """Accumulates per-stage wall-clock samples."""

    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = {}

    def add(self, name: str, ms: float) -> None:
        self.samples.setdefault(name, []).append(ms)

    def timeit(self, name: str, fn, *a, **kw):
        t = time.perf_counter()
        out = fn(*a, **kw)
        self.add(name, (time.perf_counter() - t) * 1000.0)
        return out

    def stat(self, name: str) -> tuple[float, float, float]:
        """Return (p50, p95, mean) in ms."""
        vals = sorted(self.samples.get(name, []))
        if not vals:
            return (0.0, 0.0, 0.0)
        p50 = statistics.median(vals)
        idx = min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))
        return (p50, vals[idx], statistics.fmean(vals))


async def run_local(db: str, repeat: int) -> Timer:
    """Exercise the pipeline stage by stage on a real store."""
    cfg = MemConfig(db_path=db)
    conn = open_db(cfg)
    embedder = Embedder()
    retriever = Retriever(conn, embedder.embed_one, config=cfg)
    timer = Timer()

    ctx = {
        "cwd": str(Path(__file__).resolve().parent.parent),
        "session_id": "bench-session",
    }

    # Warm the model and the page cache so we measure steady state, not
    # first-call model construction (which is ~40x the per-call cost).
    for q in QUERIES[:3]:
        embedder.embed_one(q)
    await retriever.search("global", "warm", top_k=10, scope_context=ctx)

    pool = 10 * cfg.candidate_pool_multiplier

    for i in range(repeat):
        q = QUERIES[i % len(QUERIES)]

        blob = timer.timeit("embed", embedder.embed_one, q)
        view, ssql, sargs = timer.timeit(
            "_scope_view", retriever._scope_view, "global", ctx, None
        )
        vec = timer.timeit(
            "_vector_knn",
            retriever._vector_knn,
            "global",
            blob,
            pool,
            cfg.max_vector_distance,
            ssql,
            sargs,
        )
        fts = timer.timeit(
            "_fts_search", retriever._fts_search, "global", q, pool, ssql, sargs
        )
        fused = timer.timeit("rrf_merge", rrf_merge, fts, vec, cfg.rrf_k)
        facts = timer.timeit(
            "_fetch_facts",
            retriever._fetch_facts,
            "global",
            [fid for fid, _ in fused],
        )
        rrf_scores = dict(fused)
        timer.timeit("_rerank", retriever._rerank, facts, rrf_scores, view)

        # Whole-pipeline cost, measured separately so it includes the glue the
        # per-stage numbers omit.
        t = time.perf_counter()
        await retriever.search("global", q, top_k=10, scope_context=ctx)
        timer.add("search_total", (time.perf_counter() - t) * 1000.0)

    return timer


async def run_recall(db: str, repeat: int) -> tuple[Timer, dict]:
    """Measure api.recall end to end on a started AtomMem."""
    from atom_memory.api import AtomMem

    cfg = MemConfig(db_path=db)
    mem = AtomMem(cfg)
    await mem.start()
    timer = Timer()
    ctx = {
        "cwd": str(Path(__file__).resolve().parent.parent),
        "session_id": "bench-session",
    }
    n_conflicts = 0
    try:
        await mem.recall("global", "warm", scope_context=ctx)
        for i in range(repeat):
            q = QUERIES[i % len(QUERIES)]
            t = time.perf_counter()
            out = await mem.recall("global", q, scope_context=ctx)
            timer.add("recall_total", (time.perf_counter() - t) * 1000.0)
            n_conflicts = len(out.get("conflicts") or [])
    finally:
        await mem.stop()
    return timer, {"conflicts": n_conflicts}


def run_rpc_roundtrip(db: str, repeat: int) -> Timer:
    """Measure the true stdio NDJSON round trip against a live child process.

    This is the boundary the host actually pays for: spawn ``python -m
    atom_memory.rpc``, start it, then time each request/response line.
    """
    import subprocess
    import tempfile

    timer = Timer()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["PYTHONIOENCODING"] = "utf-8"

    # stderr must NOT be a pipe the parent never drains: the child logs on
    # stderr, and once the ~64KB pipe buffer fills the child blocks forever
    # writing to it — a deadlock that looks exactly like a slow RPC. Send it to
    # a file so nothing can block.
    err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")

    proc = subprocess.Popen(
        [sys.executable, "-m", "atom_memory.rpc"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=err_file,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )

    def send(obj: dict) -> dict:
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            err_file.seek(0)
            tail = err_file.read()[-2000:]
            raise RuntimeError(f"RPC child closed stdout. stderr tail:\n{tail}")
        return json.loads(line)

    try:
        t = time.perf_counter()
        resp = send({"id": 0, "method": "start", "params": {"db_path": db}})
        timer.add("rpc_start", (time.perf_counter() - t) * 1000.0)
        if not resp.get("ok"):
            raise RuntimeError(f"rpc start failed: {resp}")

        ctx = {
            "cwd": str(Path(__file__).resolve().parent.parent),
            "session_id": "bench-session",
        }
        send(
            {
                "id": 1,
                "method": "recall",
                "params": {"user_id": "global", "query": "warm", "scope_context": ctx},
            }
        )
        for i in range(repeat):
            q = QUERIES[i % len(QUERIES)]
            t = time.perf_counter()
            out = send(
                {
                    "id": 100 + i,
                    "method": "recall",
                    "params": {
                        "user_id": "global",
                        "query": q,
                        "top_k": 10,
                        "scope_context": ctx,
                    },
                }
            )
            timer.add("rpc_recall", (time.perf_counter() - t) * 1000.0)
            if not out.get("ok"):
                raise RuntimeError(f"rpc recall failed: {out}")
    finally:
        try:
            send({"id": 9999, "method": "stop", "params": {}})
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    return timer


def report(timer: Timer, order: list[str]) -> None:
    print(f"  {'stage':<22} {'p50':>9} {'p95':>9} {'mean':>9}   n")
    print("  " + "-" * 58)
    for name in order:
        if name not in timer.samples:
            continue
        p50, p95, mean = timer.stat(name)
        print(f"  {name:<22} {p50:>8.3f}m {p95:>8.3f}m {mean:>8.3f}m  {len(timer.samples[name]):>3}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--repeat", type=int, default=30)
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return

    print(f"db      : {args.db}")
    print(f"repeat  : {args.repeat}")
    print(f"python  : {sys.version.split()[0]}", flush=True)
    print(flush=True)

    print("== stage attribution (retriever internals) ==", flush=True)
    t1 = await run_local(args.db, args.repeat)
    report(
        t1,
        [
            "embed",
            "_scope_view",
            "_vector_knn",
            "_fts_search",
            "rrf_merge",
            "_fetch_facts",
            "_rerank",
            "search_total",
        ],
    )

    print()
    print("== api.recall end to end ==", flush=True)
    t2, extra = await run_recall(args.db, args.repeat)
    report(t2, ["recall_total"])
    print(f"  (active conflict pairs returned: {extra['conflicts']})")

    print()
    print("== live stdio RPC round trip ==", flush=True)
    try:
        t3 = run_rpc_roundtrip(args.db, args.repeat)
        report(t3, ["rpc_start", "rpc_recall"])
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"  RPC measurement failed: {exc}")

    print()
    print("== attribution of search_total ==")
    p50 = {n: t1.stat(n)[0] for n in t1.samples}
    total = p50.get("search_total", 0.0)
    if total:
        parts = [
            "embed",
            "_scope_view",
            "_vector_knn",
            "_fts_search",
            "rrf_merge",
            "_fetch_facts",
            "_rerank",
        ]
        acc = 0.0
        for n in parts:
            v = p50.get(n, 0.0)
            acc += v
            print(f"  {n:<22} {v:>8.3f} ms  {v / total:>6.1%}")
        print(f"  {'-' * 40}")
        print(f"  {'sum of stages':<22} {acc:>8.3f} ms  {acc / total:>6.1%}")
        print(f"  {'search_total':<22} {total:>8.3f} ms  100.0%")
        print(f"  {'unattributed':<22} {total - acc:>8.3f} ms  {(total - acc) / total:>6.1%}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())