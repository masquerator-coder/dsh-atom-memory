"""P3 scale scan: where does exact KNN stop being viable?

Answers the question the plan defers to measurement: at what fact count does
sqlite-vec's brute-force scan become the problem, and what is actually available
to replace it?

Constraints found while building this (sqlite-vec v0.1.9, the pinned version):

* There is **no ANN index**. `vec0` accepts no `index=`/`partitions=` option, and
  the build exports no index-building function. So "switch to HNSW/IVF" is not a
  plan that can be executed against this dependency -- P3 cannot be what the
  original proposal assumed.
* What *is* available: `vec_quantize_binary` / `vec_quantize_int8` and the
  `bit[N]` / `int8[N]` column types, i.e. a coarse pre-filter followed by an
  exact re-rank. That is the only lever.

Method: build synthetic stores at 1k / 5k / 20k / 50k facts with realistic
512-d vectors, and time the *production* `_vector_knn` (same scope filter, same
pool) at each size. Scaling shape matters more than absolute numbers: a linear
curve means the cost is intrinsic to the scan, a flat one would mean overhead.

Run:  python bench/scale_scan.py [--sizes 1000,5000,20000,50000]
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import statistics
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite_vec  # noqa: E402

from atom_memory.config import MemConfig  # noqa: E402
from atom_memory.db import open_db  # noqa: E402
from atom_memory.retriever import Retriever  # noqa: E402

DIM = 512
USER = "scale"


def ser(values) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unit(rng: random.Random):
    v = [rng.gauss(0, 1) for _ in range(DIM)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


class _FixedEmbed:
    """Embedder returning a preset vector, so no model is needed."""

    def __init__(self, blob: bytes) -> None:
        self._b = blob

    def embed_one(self, _text: str) -> bytes:
        return self._b


def build_store(n_facts: int, seed: int = 5) -> sqlite3.Connection:
    """Create a temp store with ``n_facts`` active facts and vectors."""
    path = os.path.join(tempfile.mkdtemp(prefix="scale"), "mem.db")
    cfg = MemConfig(db_path=path)
    conn = open_db(cfg)
    rng = random.Random(seed)
    conn.execute("BEGIN")
    for i in range(n_facts):
        fid = f"f{i:07d}"
        conn.execute(
            "INSERT INTO facts(fact_id,user_id,session_id,subject,predicate,object,"
            "confidence,importance,source_type,status,observed_at,created_at,version,type) "
            "VALUES(?,?,'s','用户','偏好',?,0.8,0.6,'user_explicit','active',1,1,1,'semantic')",
            (fid, USER, f"值{i}"),
        )
        conn.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES(?, ?)",
            (fid, ser(unit(rng))),
        )
    conn.commit()
    return conn


def timeit(fn, n: int = 20) -> tuple[float, float]:
    fn()
    samples = []
    for _ in range(n):
        s = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - s) * 1000)
    samples.sort()
    return statistics.median(samples), samples[min(len(samples) - 1, int(0.95 * (len(samples) - 1)))]


def scan_one(n_facts: int, pool: int, repeat: int) -> dict:
    conn = build_store(n_facts)
    try:
        query_blob = ser(unit(random.Random(99)))
        cfg = MemConfig(db_path=":memory:")
        r = Retriever(conn, _FixedEmbed(query_blob).embed_one, config=cfg)

        p50, p95 = timeit(lambda: r._vector_knn(USER, query_blob, pool), repeat)
        raw_p50, raw_p95 = timeit(
            lambda: conn.execute(
                "SELECT fact_id FROM facts_vec WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?", (query_blob, pool)
            ).fetchall(),
            repeat,
        )
        # FTS for comparison: is it scale-sensitive at all?
        fts_p50, _ = timeit(
            lambda: conn.execute(
                "SELECT f.fact_id FROM facts_fts fts JOIN facts f ON f.fact_id = fts.fact_id "
                "WHERE f.user_id = ? AND f.status = 'active' AND facts_fts MATCH ? "
                "ORDER BY rank LIMIT ?", (USER, '"用户"', pool)
            ).fetchall(),
            repeat,
        )
        rows = conn.execute("SELECT COUNT(*) FROM facts_vec").fetchone()[0]
        return {
            "facts": n_facts,
            "vec_rows": rows,
            "knn_p50": p50,
            "knn_p95": p95,
            "raw_p50": raw_p50,
            "raw_p95": raw_p95,
            "fts_p50": fts_p50,
            "size_mb": os.path.getsize(conn.execute("PRAGMA database_list").fetchone()[2]) / 1e6,
        }
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1000,5000,20000,50000")
    ap.add_argument("--repeat", type=int, default=15)
    ap.add_argument("--pool", type=int, default=40)
    args = ap.parse_args()
    sizes = [int(x) for x in args.sizes.split(",") if x.strip()]

    print("sqlite-vec build: v%s (no ANN index available)" % sqlite_vec.__dict__.get("__version__", "?"))
    print("pool=%d dim=%d repeat=%d" % (args.pool, DIM, args.repeat))
    print()
    print("%9s %9s %10s %10s %10s %9s %8s"
          % ("facts", "vec_rows", "knn_p50", "knn_p95", "raw_p50", "fts_p50", "db_MB"))
    print("-" * 72)
    results = []
    for n in sizes:
        r = scan_one(n, args.pool, args.repeat)
        results.append(r)
        print("%9d %9d %9.2fm %9.2fm %9.2fm %8.2fm %8.1f"
              % (r["facts"], r["vec_rows"], r["knn_p50"], r["knn_p95"],
                 r["raw_p50"], r["fts_p50"], r["size_mb"]))

    print()
    print("scaling shape (KNN p50 per 1k facts, relative to the smallest store):")
    base = results[0]
    for r in results:
        per_k = r["knn_p50"] / (r["facts"] / 1000.0)
        ratio = r["knn_p50"] / base["knn_p50"]
        factor = r["facts"] / base["facts"]
        print("   %7d facts: %7.3f ms/1k   %5.2fx slower at %4.1fx the size"
              % (r["facts"], per_k, ratio, factor))

    # Straight-line fit for extrapolation.
    if len(results) >= 2:
        xs = [r["facts"] for r in results]
        ys = [r["knn_p50"] for r in results]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0
        intercept = my - slope * mx
        print()
        print("linear fit: knn_p50 ~= %.4f ms + %.6f ms/fact" % (intercept, slope))
        for target in (20, 50, 100):
            if slope > 0:
                facts = (target - intercept) / slope
                print("   reaches %3d ms at ~%s facts" % (target, f"{int(facts):,}"))
        print("   (extrapolation only -- treat as an order-of-magnitude guide)")


if __name__ == "__main__":
    main()