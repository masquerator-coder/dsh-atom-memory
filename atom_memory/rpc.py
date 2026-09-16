"""NDJSON stdio RPC service — the bridge entry point for the dsh plugin.

The dsh plugin (``dsh/`` npm subpackage) spawns this module as a long-lived
child process (``python -m atom_memory.rpc``) and talks to it over plain
stdio using newline-delimited JSON. This keeps the pure-Python library fully
process-isolated from the Node host: the library stays free of any dsh / Node
dependency, and the dsh side never imports Python directly.

Wire protocol (one JSON object per line):

* Request (dsh -> Python, read from stdin)::

    {"id": 1, "method": "recall", "params": {...}}

  ``id`` is an arbitrary opaque token (an integer or string) that the response
  echoes. ``method`` is one of the supported RPC methods below.

* Response (Python -> dsh, written to stdout, one per line)::

    {"id": 1, "ok": true, "result": {...}}
    {"id": 1, "ok": false, "error": "human readable message"}

* Background event (Python -> dsh, written to stderr, prefixed so the host can
  filter it)::

    {"evt": "task_done", "candidate_id": "..."}

  Events are best-effort monitoring lines (the library worker has no callback
  hook), surfaced so the dsh side can satisfy the "model-visible <=> logged"
  invariant without inventing out-of-repo event types.

Lifecycle:

- The host sends ``{"method": "start"}`` with startup configuration; the service
  builds an :class:`~atom_memory.api.AtomMem` and starts it.
- The host sends ``{"method": "stop"}``; the service stops AtomMem (flushing the
  worker / closing the DB) and exits.

Exposure is a thin mapping over the public ``AtomMem`` API and deliberately
**never touches library internals** beyond read-only inspection for bulk
operations (``forget_all`` reads active ``fact_id``s then enqueues public
``forget`` calls).

Run directly (debug)::

    python -m atom_memory.rpc
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import sys
import uuid
from typing import Any, Dict, Optional

from .api import AtomMem
from .config import MemConfig
from .db import now_ms

logger = logging.getLogger(__name__)

# RPC methods mapped to AtomMem's public surface. Each entry is a tuple
# ``(callable, requires_started)``. ``None`` callables are handled specially.
_METHODS: Dict[str, str] = {
    "add": "add",
    "recall": "recall",
    "replace": "replace",
    "forget": "forget",
    "summary": "summary",
    "user_md": "user_md",
    "stats": "stats",
    # UI-facing edit / backup / restore surface.
    "list_facts": "list_facts",
    "get_fact": "get_fact",
    "edit_fact": "edit_fact",
    "reinforce": "reinforce",
    "list_profile": "list_profile",
    "upsert_profile": "upsert_profile",
    "delete_profile": "delete_profile",
    # The profile is user-owned: candidates are read out of the facts for the
    # dsh-side LLM to turn into suggestions, and the accepted ones are written
    # back in one batch.
    "profile_candidates": "profile_candidates",
    "write_profile": "write_profile",
    "backup": "backup",
    "restore": "restore",
    # Lifecycle: the archive tier, erasure, housekeeping, and the write outcomes
    # a caller can pull instead of waiting for.
    "unarchive": "unarchive",
    "purge": "purge",
    "vacuum": "vacuum",
    "maintenance": "maintenance",
    "index_health": "index_health",
    "candidate_outcome": "candidate_outcome",
    "recent_outcomes": "recent_outcomes",
    "outcomes": "recent_outcomes",
    # Scope surface: the hierarchy, its resolution, and the operator actions that
    # repair it (merge / split / reparent / confirm / alias).
    "scope_list": "scope_list",
    "scope_resolve": "scope_resolve",
    "scope_create": "scope_create",
    "scope_alias_add": "scope_alias_add",
    "scope_confirm": "scope_confirm",
    "scope_merge": "scope_merge",
    "scope_split": "scope_split",
    "scope_reparent": "scope_reparent",
    "scope_unresolved": "scope_unresolved",
    "scope_promote": "scope_promote",
    "fact_scope_bind": "fact_scope_bind",
    "fact_condition_set": "fact_condition_set",
    "fact_scope_get": "fact_scope_get",
}


class _RpcError(Exception):
    """A structured error returned to the caller instead of crashing the loop."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RpcServer:
    """Reads NDJSON requests on stdin and dispatches them to an AtomMem.

    The server owns one :class:`AtomMem` instance whose life follows the
    ``start`` / ``stop`` RPC protocol. Requests are processed serially on a
    single event loop; long-running or background work (worker extraction,
    embedding) never blocks request dispatch because the library runs those on
    its own worker tasks.
    """

    def __init__(self) -> None:
        self.mem: Optional[AtomMem] = None
        self._started = False

    # -- public dispatch -----------------------------------------------------

    async def run_forever(self) -> None:
        """Serve stdin until EOF ("stop" request closes the loop).

        stdin/stdout are blocking file objects, and the Windows Proactor event
        loop cannot drive a pipe read through ``connect_read_pipe`` (that path
        fails with ``WinError 6``), so requests are read on a worker thread via
        ``run_in_executor`` — the portable cross-platform pattern.
        """
        loop = asyncio.get_running_loop()

        async def _read_line() -> Optional[str]:
            return await loop.run_in_executor(None, sys.stdin.readline)

        emit_task = asyncio.create_task(self._emit_background_events())
        try:
            while True:
                line = await _read_line()
                if not line:
                    break  # stdin closed -> host gone -> exit
                text = line.decode("utf-8", errors="replace").strip() if isinstance(line, bytes) else line.strip()
                if not text:
                    continue
                await self._handle_line(text)
                if getattr(self, "_stop_requested", False):
                    break
        finally:
            emit_task.cancel()
            try:
                await emit_task
            except asyncio.CancelledError:
                pass
            await self.shutdown()

    async def _handle_line(self, text: str) -> None:
        """Parse and dispatch a single NDJSON request line."""
        try:
            req = json.loads(text)
        except (ValueError, TypeError):
            self._write({"id": None, "ok": False, "error": "invalid JSON"})
            return
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        if not isinstance(method, str):
            self._write({"id": rid, "ok": False, "error": "missing method"})
            return
        try:
            result = await self._dispatch(method, params)
        except _RpcError as e:
            self._write({"id": rid, "ok": False, "error": e.message})
        except Exception as e:  # noqa: BLE001 - surface any failure to the host
            logger.exception("RPC method %s failed", method)
            self._write({"id": rid, "ok": False, "error": str(e)})
        else:
            self._write({"id": rid, "ok": True, "result": result})

    async def _dispatch(self, method: str, params: dict) -> Any:
        """Route one method call to AtomMem (or the lifecycle handlers)."""
        if method == "start":
            return await self._start(params)
        if method == "stop":
            self._stop_requested = True  # type: ignore[attr-defined]
            return {"stopped": True}
        if method == "health":
            return self._health()
        if method == "summary":
            return await self._summary(params)
        if method == "forget_all":
            return await self._forget_all(params)
        if method == "persist_candidates":
            return await self._persist_candidates(params)

        attr = _METHODS.get(method)
        if attr is None:
            raise _RpcError(f"unknown method: {method}")
        if not self._started or self.mem is None:
            raise _RpcError("not started; call start first")
        fn = getattr(self.mem, attr)
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn(**params)
            return fn(**params)
        except TypeError as e:
            # A wrong-argument call is a host bug, not a provider failure.
            raise _RpcError(f"bad arguments for {method}: {e}") from e

    # -- lifecycle -----------------------------------------------------------

    def _health(self) -> dict:
        """Report whether the store is actually usable, not merely alive.

        A health endpoint that always answers ``ok`` cannot be used to gate
        anything — the bridge would keep talking to a process whose database is
        unreachable or whose indexes have drifted. This one answers from the
        store itself: the queue state, an index consistency check, and the last
        maintenance result, so a caller can distinguish "no memory" from
        "memory is broken".

        Returns:
            The health payload. ``ok`` is ``False`` when the store cannot answer
            a trivial query or when a fact is missing an index entry.
        """
        payload: Dict[str, Any] = {
            "started": self._started,
            "ok": False,
            "db_path": None,
            "queue": {},
            "index": {},
            "last_maintenance": None,
            "error": None,
        }
        if not self._started or self.mem is None or self.mem.db is None:
            payload["error"] = "not started"
            return payload
        try:
            payload["db_path"] = self.mem.config.resolved_db_path()
            # A trivial read proves the file is reachable and readable.
            self.mem.db.execute("SELECT 1").fetchone()
            queue = self.mem.db.execute(
                "SELECT status, COUNT(*) AS n FROM task_queue GROUP BY status"
            ).fetchall()
            payload["queue"] = {r["status"]: r["n"] for r in queue}
            health = self.mem.index_health()
            payload["index"] = {
                "ok": health["ok"],
                "counts": health["counts"],
                "orphans": {
                    key: len(value)
                    for key, value in health["orphans"].items()
                },
            }
            payload["last_maintenance"] = (
                self.mem._worker.last_maintenance_result
                if self.mem._worker is not None
                else None
            )
            payload["ok"] = bool(health["ok"])
        except Exception as exc:  # noqa: BLE001 - health must answer, not raise
            payload["error"] = str(exc)
            payload["ok"] = False
        return payload

    async def _start(self, params: dict) -> dict:
        """Build and start AtomMem from the startup configuration."""
        if self._started:
            return {"already_started": True}
        try:
            if "llm_extractor" in params:
                # A callable cannot cross the wire. Silently dropping it used to
                # sit next to a comment claiming the dsh host injects the
                # extractor through the child's environment — which no code ever
                # read, so the parameter looked supported while doing nothing.
                # Refusing loudly is the honest version: LLM extraction happens in
                # the dsh host (llm-extractor.ts) and reaches this store as
                # pre-extracted candidates over `persist_candidates`, while
                # `MemConfig.llm_extractor` stays usable only when the library is
                # embedded in-process (see tests/test_extractor.py).
                raise _RpcError(
                    "llm_extractor cannot be sent over the wire; extraction runs in "
                    "the dsh host and is delivered through persist_candidates"
                )
            config = MemConfig(**params)
        except TypeError as e:
            raise _RpcError(f"invalid start params: {e}") from e

        mem = AtomMem(config)
        await mem.start()
        self.mem = mem
        self._started = True
        return {"started": True, "db_path": config.resolved_db_path()}

    async def _forget_all(self, params: dict) -> dict:
        """Bulk retract (or erase) one user's memory.

        Soft-deletes by default — facts are never physically deleted, this
        honours the library's soft-delete invariant, and it keeps the provenance
        of what was removed. Pass ``purge=True`` for an actual erasure request:
        the content, its index entries and its reinforcement log are deleted.

        Args:
            params: ``user_id`` and optional ``purge``.

        Returns:
            ``{"fact_ids", "queued", "purged": bool}``.
        """
        user_id = params.get("user_id")
        if not user_id:
            raise _RpcError("forget_all requires user_id")
        if not self._started or self.mem is None or self.mem.db is None:
            raise _RpcError("not started; call start first")
        purge = bool(params.get("purge"))
        rows = self.mem.db.execute(
            "SELECT fact_id FROM facts WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchall()
        if purge:
            result = self.mem.purge(user_id)
            return {
                "fact_ids": result["purged_ids"],
                "queued": 0,
                "purged": True,
            }
        for r in rows:
            await self.mem.forget(user_id, fact_id=r["fact_id"])
        return {"fact_ids": [r["fact_id"] for r in rows], "queued": len(rows),
                "purged": False}

    async def _summary(self, params: dict) -> Any:
        """Render the compact or detail digest, optionally with its metadata.

        ``include_meta`` is for the injection path: the caller needs to know
        whether the store holds *anything* before spending prompt tokens on a
        snapshot, and asking in the same round trip is what keeps freezing a
        single call.

        ``scope_context`` selects the scope-blocked rendering (current scope,
        ancestors, phases, global rules, condition rules) for the compact depth.
        """
        user_id = params["user_id"]
        text = await self.mem.summary(
            user_id,
            max_tokens=int(params.get("max_tokens", 1500)),
            detail=bool(params.get("detail", False)),
            scope_context=params.get("scope_context"),
        )
        if not params.get("include_meta"):
            return text
        row = self.mem.db.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
        return {"text": text, "facts": int(row["n"] if row is not None else 0)}

    async def _persist_candidates(self, params: dict) -> dict:
        """Persist pre-extracted candidates (from the dsh-side LLM extractor).

        Enqueues a ``persist_pre`` worker task so the candidates go through the
        same validation + persistence chain as rule-extracted text (gaining
        ingest cleaning, conflict resolution and identical-SPO dedup for free).

        Args:
            params: ``user_id``, ``candidates``, optional ``session_id`` /
                ``turn_id`` / ``scope_context`` / ``wait_ms``. When ``wait_ms`` is
                set the reply carries the write outcome instead of only the
                enqueue receipt.
        """
        user_id = params.get("user_id")
        candidates = params.get("candidates")
        if not user_id or not isinstance(candidates, list) or not candidates:
            raise _RpcError("persist_candidates requires user_id and non-empty candidates")
        if not self._started or self.mem is None or self.mem.db is None:
            raise _RpcError("not started; call start first")
        session_id = params.get("session_id", "s_default")
        turn_id = int(params.get("turn_id", 0))
        wait_ms = params.get("wait_ms")
        candidate_id = str(uuid.uuid4())
        created_at = now_ms()
        payload = {
            "candidate_id": candidate_id,
            "user_id": user_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "candidates": candidates,
        }
        if params.get("scope_context"):
            payload["scope_context"] = params["scope_context"]
        with self.mem.db:
            self.mem.db.execute(
                "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
                "turn_id, raw_text, status, created_at) "
                "VALUES (?, ?, ?, ?, NULL, 'pending', ?)",
                (candidate_id, user_id, session_id, turn_id, created_at),
            )
            self.mem.db.execute(
                "INSERT INTO task_queue(task_id, task_type, payload, status, "
                "priority, retry_count, max_retries, created_at) "
                "VALUES (?, 'persist_pre', ?, 'pending', 5, 0, ?, ?)",
                (
                    str(uuid.uuid4()),
                    json.dumps(payload),
                    self.mem.config.max_retries,
                    created_at,
                ),
            )
        receipt = await self.mem._write_receipt(candidate_id, None, wait_ms)
        receipt["queued"] = len(candidates)
        receipt.pop("trace_id", None)
        return receipt

    async def shutdown(self) -> None:
        """Stop the worker / close the DB so data is flushed before exit."""
        if self.mem is not None and self._started:
            try:
                await self.mem.stop()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                logger.exception("shutdown error")
        self._started = False
        self.mem = None

    # -- io helpers ----------------------------------------------------------

    def _write(self, payload: dict) -> None:
        """Write one NDJSON response line to stdout and flush it."""
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _emit(self, evt: Dict[str, Any]) -> None:
        """Write one NDJSON background-event line to stderr (tag-filtered)."""
        line = json.dumps(evt, ensure_ascii=False)
        sys.stderr.write("EVT " + line + "\n")
        sys.stderr.flush()

    async def _emit_background_events(self) -> None:
        """Poll candidate state transitions and surface them as events.

        The library worker has no callback hook, so the RPC layer samples the
        DB for candidates that reached a terminal state and emits a best-effort
        ``task_done`` / ``task_dead`` line. This is monitoring only — it never
        drives correctness.
        """
        # A bounded LRU of candidate ids already emitted. This is monitoring
        # only: a terminal candidate never changes state, so the set only guards
        # against re-emitting the same id on every poll. It is sized by the
        # number of candidates a busy session can realistically produce; once it
        # overflows, the oldest ids are forgotten and — if still terminal — may
        # be re-emitted once, which is harmless for best-effort monitoring (and
        # far better than growing without bound for the life of the process).
        seen: "collections.OrderedDict[str, None]" = collections.OrderedDict()
        seen_limit = 4096
        sleep = asyncio.sleep
        while True:
            await sleep(0.5)
            if not self._started or self.mem is None or self.mem.db is None:
                continue
            rows = self.mem.db.execute(
                "SELECT candidate_id, status FROM fact_candidates "
                "WHERE status IN ('applied', 'skipped', 'dead', 'error')"
            ).fetchall()
            for r in rows:
                cid = r["candidate_id"]
                if cid in seen:
                    # Refresh recency so a still-cold id is not the first evicted.
                    seen.move_to_end(cid)
                    continue
                seen[cid] = None
                while len(seen) > seen_limit:
                    seen.popitem(last=False)
                self._emit(
                    {
                        "evt": "task_dead" if r["status"] in ("dead", "error") else "task_done",
                        "candidate_id": cid,
                    }
                )


async def main() -> None:
    """Entry point for ``python -m atom_memory.rpc``."""
    handler = _StdErrLoggingHandler()
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    server = RpcServer()
    await server.run_forever()


class _StdErrLoggingHandler(logging.Handler):
    """Route library logs to stderr with a tag so the dsh host can filter."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            sys.stderr.write(
                "LOG " + self.format(record) + "\n"
            )
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())

