"""Asyncio worker that drains the ``task_queue``.

The worker owns the write-side of the pipeline. It polls ``task_queue``,
dispatches each task by ``task_type``, retries failures with exponential
backoff and marks permanently-failing tasks as ``dead`` (recording an
``events`` row and emitting an error log).

Stage 2 implements the ``extract`` handler: extract candidates from the raw
utterance, run the validation chain, then persist accepted facts into the
``facts``, ``facts_fts`` and ``facts_vec`` tables.

Two policies live here rather than in the API layer, because both are decisions
about *what the store should contain*:

- **Conflict resolution.** The validator classifies a candidate against the
  stored value; this module carries out the verdict: under a single-valued
  predicate a newer assertion supersedes the stored value(s) — losing the
  newest statement silently is not an option — unless the stored claim has
  decisively stronger evidence (:mod:`atom_memory.conflict`). Every supersede
  and every rejection is recorded in ``events`` and on the candidate row, so a
  write that did *not* land is never reported as if it had.
- **Retention and self-repair.** On idle the worker runs a maintenance pass:
  it collects finished bookkeeping rows past their retention, archives the
  least valuable *unprotected* facts when a capacity cap is configured, and
  re-derives any fact whose FTS or vector entry went missing. Facts themselves
  are never deleted by policy — the archive tier is how the working set stays
  bounded while nothing is lost.

The worker also owns the implicit half of the reuse-reinforcement loop: when a
candidate is rejected as ``idempotent`` the user has re-stated a claim already
stored, which is recorded as a reinforcement event (see
:mod:`~atom_memory.reinforce`). Retrieval hits are deliberately not a signal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Callable, List, Optional

from .conflict import (
    ACTION_REJECT,
    REASON_BATCH_DUPLICATE,
    resolve_conflict,
)
from .config import MemConfig
from .context import GLOBAL_SCOPE_ID, MAX_CONDITIONS, normalize_conditions
from .db import index_orphans, now_ms, record_event
from .domain import DomainAssignment, DomainStore
from .fingerprint import BODY_IDENTIFIED_TYPES, content_fingerprint
from .models import (
    FactCandidate,
    NEUTRAL_SCORE,
    default_importance,
)
from .reinforce import (
    KIND_USER_RESTATED,
    ReinforceCurve,
    adjust,
    effective_importance,
    record_reinforcement,
)
from .retriever import segment_text
from .scope import (
    STATUS_GLOBAL,
    ScopeResolution,
    ScopeStore,
    resolution_for,
)
from .validator import (
    cross_scope_neighbours,
    has_negation,
    is_multi_valued,
    validate,
)

logger = logging.getLogger(__name__)

# Confidence stamped on a candidate whose extractor supplied none. Unlike
# ``importance`` this is not type-dependent: it answers "how sure are we this
# was stated", which is uniform when the source is a direct user message.
DEFAULT_CONFIDENCE = 0.7

# Candidate lifecycle statuses.
CAND_STATUS_APPLIED = "applied"
CAND_STATUS_SKIPPED = "skipped"
CAND_STATUS_ERROR = "error"

# Task statuses.
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_DEAD = "dead"

# Fact statuses that a capacity pass may move a row *into*. Everything else
# (``active``) is what the read paths see.
FACT_STATUS_ARCHIVED = "archived"


def _candidate_from_rpc_dict(
    d: dict,
    user_id: str,
    session_id: str,
    turn_id: int,
) -> FactCandidate:
    """Build a :class:`FactCandidate` from an RPC candidate dict.

    Used by the ``persist_pre`` worker task to accept candidates that the dsh
    host already extracted (LLM-first) so they can be validated and persisted
    through the same chain as rule-extracted candidates.

    Args:
        d: A dict with any of subject / predicate / object / type / content /
            qualifiers / confidence / importance / privacy keys.
        user_id: Owner to stamp when the dict omits it.
        session_id: Session to stamp when the dict omits it.
        turn_id: Turn to stamp when the dict omits it.

    Returns:
        A populated candidate with a generated id. When the dict omits
        ``importance`` it defaults to the type's rank
        (:func:`~atom_memory.models.default_importance`), and ``confidence``
        defaults to :data:`DEFAULT_CONFIDENCE`.
    """
    import json as _json

    quals = d.get("qualifiers")
    if isinstance(quals, (dict, list)):
        quals = _json.dumps(quals, ensure_ascii=False)
    memory_type = d.get("type") or "semantic"
    # Priority falls back to the *type's* default rank rather than a flat 0.5:
    # a uniform default makes every fact tie, which collapses ordering in the
    # derived views to plain recency and hides what actually matters.
    importance = d.get("importance")
    if importance is None:
        importance = default_importance(memory_type)
    confidence = d.get("confidence")
    if confidence is None:
        confidence = DEFAULT_CONFIDENCE
    conditions = d.get("conditions")
    if isinstance(conditions, dict):
        # The host may send `{"language": "typescript"}` as readily as the
        # list form the extraction prompt asks for; both mean the same thing.
        conditions = [{"key": k, "value": v} for k, v in conditions.items()]
    hints = d.get("domain_hints")
    if isinstance(hints, str):
        hints = [hints]
    elif isinstance(hints, (list, tuple)):
        hints = [str(h) for h in hints if str(h or "").strip()]
    else:
        hints = None
    return FactCandidate(
        candidate_id=str(uuid.uuid4()),
        user_id=d.get("user_id") or user_id,
        session_id=d.get("session_id") or session_id,
        turn_id=int(d.get("turn_id", turn_id) or turn_id),
        subject=d.get("subject"),
        predicate=d.get("predicate"),
        object=d.get("object"),
        qualifiers=quals,
        confidence=confidence,
        importance=importance,
        privacy=d.get("privacy", "private"),
        raw_text=d.get("raw_text"),
        idempotency_key=d.get("idempotency_key"),
        type=memory_type,
        content=d.get("content"),
        conditions=conditions if isinstance(conditions, list) else None,
        scope_hint=d.get("scope_hint"),
        domain_hints=hints or None,
        primary_domain=d.get("primary_domain") or None,
    )


def empty_outcome() -> dict:
    """Return a fresh write-outcome accumulator.

    The shape is what a caller sees when it asks for a synchronous outcome, and
    what gets stored on the candidate row: what was written, what was replaced,
    what was refused and why, and what had to be shortened.

    Returns:
        ``{"written", "superseded", "rejected", "reinforced", "truncated",
        "cross_scope"}`` — all lists. ``cross_scope`` carries the relations
        recorded against same-key facts in *other* scopes; it is a list (not a
        scope dict) so the shape stays uniform, and the resolved scope itself is
        added by the write gate as a ``scope`` key.
    """
    return {
        "written": [],
        "superseded": [],
        "rejected": [],
        "reinforced": [],
        "truncated": [],
        "cross_scope": [],
    }


@dataclass(frozen=True)
class PersistResult:
    """What :meth:`Worker._persist_fact` did with one candidate.

    ``fact_id`` is ``None`` when the candidate turned out to be a memory the
    store already holds: in that case ``existing_fact_id`` names the row that was
    reinforced instead, and ``deduped_on`` says which test recognised it
    (``fingerprint`` for identical content, ``embedding`` for a reworded body).
    A caller that ignored the distinction would report "written" for a write
    that never happened.
    """

    fact_id: Optional[str]
    """The new fact id, or ``None`` when the claim was already stored."""
    deduped_on: Optional[str] = None
    """``fingerprint`` / ``embedding`` when deduplicated, else ``None``."""
    existing_fact_id: Optional[str] = None
    """The active fact the candidate was folded into, when deduplicated."""

    @property
    def written(self) -> bool:
        """Whether a new fact row was created."""
        return self.fact_id is not None


class Worker:
    """Polling worker for the memory task queue."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        embed_func: Callable[[str], bytes],
        poll_interval_sec: float = 0.5,
        max_retries: int = 3,
        llm_extractor: Optional[Callable[..., list]] = None,
        privacy_filter: str = "private",
        config: Optional[MemConfig] = None,
    ) -> None:
        """Initialise the worker.

        Args:
            conn: The SQLite connection (single writer).
            embed_func: Callable mapping a text string to a serialized
                embedding BLOB. Must be safe to call from worker threads.
            poll_interval_sec: Seconds between queue polls.
            max_retries: Max retries before a task is marked dead.
            llm_extractor: Optional LLM extractor callable.
            privacy_filter: Default privacy tag applied during validation.
            config: Configuration carrying the ingest caps, the conflict margin
                and the retention policy. ``None`` uses library defaults, which
                is what direct (non-``AtomMem``) construction wants.
        """
        self.conn = conn
        self.embed_func = embed_func
        self.poll_interval_sec = poll_interval_sec
        self.max_retries = max_retries
        self.config = config or MemConfig()
        self.privacy_filter = privacy_filter
        self._task: Optional[asyncio.Task] = None
        self._last_maintenance: Optional[int] = None
        # Identity of this consumer. A claim records who holds it, so a second
        # consumer over the same file can tell "abandoned" from "someone else is
        # working on it" instead of guessing from timestamps alone.
        self.worker_id = f"{socket.gethostname()}#{os.getpid()}#{uuid.uuid4().hex[:8]}"
        # The reuse-and-decay curve this store was configured with. Every
        # reinforcement and every strength read goes through it, so retuning
        # `reinforce_*` changes behaviour rather than just the constants.
        self.curve = ReinforceCurve.from_config(self.config)
        # The reclaim-on-start boundary. A `running` row older than this worker
        # was orphaned by a previous process (that is the crash-recovery case);
        # a row claimed *after* this worker was constructed belongs to a live
        # consumer and must be left alone. With a lease the boundary only has to
        # cover rows written before the lease column existed (NULL lease).
        self._started_before_ms = now_ms()
        self.last_maintenance_result: dict = {}

        from .extractor import Extractor

        self.extractor = Extractor(llm_extractor=llm_extractor)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Begin draining the queue in a background asyncio task."""
        if self._task is None or self._task.done():
            # Reclaim any task a previous worker instance left in `running` —
            # from a hard crash, or from a graceful `stop()` that cancelled the
            # drain mid-task. A freshly-started worker owns nothing in flight,
            # so those rows are orphaned; if left `running` they are never
            # selected again (the drain only claims `pending`) and the unit of
            # work is lost forever. Resetting them to `pending` re-enqueues them
            # under the same at-least-once retry semantics the worker already
            # has for failed tasks.
            #
            # Only rows that have been `running` since before this process
            # started are reclaimed: a task claimed by *another* live consumer
            # (two AtomMem instances over one database file) carries a
            # `started_at` inside this worker's own lifetime only if this worker
            # claims it, so the guard keeps the crash-recovery behaviour while
            # making the reclaim no longer steal a peer's in-flight unit of
            # work.
            self.conn.execute(
                "UPDATE task_queue SET status = ?, claimed_by = NULL, "
                "lease_expires_at = NULL "
                "WHERE status = ? AND (lease_expires_at IS NOT NULL AND lease_expires_at < ? "
                "OR lease_expires_at IS NULL AND (started_at IS NULL OR started_at < ?))",
                (TASK_PENDING, TASK_RUNNING, now_ms(), self._started_before_ms),
            )
            self.conn.commit()
            self._task = asyncio.create_task(self._run(), name="dsh-worker")

    async def stop(self) -> None:
        """Stop the worker and await graceful shutdown."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    # -- main loop -----------------------------------------------------------

    async def _run(self) -> None:
        while True:
            # Declared here so the ``CancelledError`` handler below can always
            # test it safely: if the cancel lands while ``_claim_next_task`` is
            # executing (before any row is bound), ``task_row`` stays ``None``
            # and there is nothing to requeue.
            task_row: Optional[sqlite3.Row] = None
            try:
                task_row = self._claim_next_task()
                if task_row is None:
                    # Idle: this is the only place the maintenance pass runs, so
                    # it can never delay a queued write.
                    if self._maintenance_due():
                        await self.maintenance()
                    await asyncio.sleep(self.poll_interval_sec)
                    continue
                await self._handle_task(task_row)
            except asyncio.CancelledError:
                # The worker was stopped / hard-cancelled mid-task (e.g.
                # `stop()` or shutdown). ``CancelledError`` is a
                # ``BaseException``, so ``_handle_task``'s ``except
                # Exception`` (which would have requeued a *failed* task)
                # never sees it and the claimed row would otherwise stay
                # ``running`` forever. The drain only claims ``pending``
                # rows, so return the in-flight unit of work to ``pending``
                # before propagating — the next start (or a restart of the
                # process) will pick it back up instead of losing it.
                if task_row is not None:
                    self._requeue_inflight(task_row["task_id"])
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Worker loop error: %s", exc)
                await asyncio.sleep(self.poll_interval_sec)

    # -- queue -----------------------------------------------------------------

    def _claim_next_task(self) -> Optional[sqlite3.Row]:
        """Atomically claim the highest-priority pending task.

        The claim is a compare-and-swap: the row is selected, then moved to
        ``running`` **only if it is still ``pending``**, and the update's row
        count decides whether this worker got it. A bare
        ``UPDATE ... WHERE task_id = ?`` would let two consumers over one
        database file both claim the same row (both select it before either
        updates) and execute the same unit of work twice.

        Returns:
            The claimed task row, or ``None`` if the queue is empty (or the row
            was taken by another consumer between the select and the update).
        """
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT task_id, task_type, payload, retry_count, max_retries "
            "FROM task_queue "
            "WHERE status = ? "
            "ORDER BY priority, created_at "
            "LIMIT 1",
            (TASK_PENDING,),
        ).fetchone()
        if row is None:
            return None
        cursor = self.conn.execute(
            "UPDATE task_queue SET status = ?, started_at = ?, claimed_by = ?, "
            "lease_expires_at = ? "
            "WHERE task_id = ? AND status = ?",
            (
                TASK_RUNNING,
                now_ms(),
                self.worker_id,
                now_ms() + int(self.config.task_lease_sec * 1000),
                row["task_id"],
                TASK_PENDING,
            ),
        )
        self.conn.commit()
        if cursor.rowcount != 1:
            logger.debug(
                "Task %s was claimed by another consumer; skipping",
                row["task_id"],
            )
            return None
        return row

    def _requeue_inflight(self, task_id: str) -> None:
        """Return an in-flight (``running``) task to ``pending`` on interruption.

        Called from the cancellation path so a ``stop()`` / shutdown / hard
        crash mid-task does not permanently strand the row in ``running`` (the
        drain only claims ``pending``). The ``status = 'running'`` guard means a
        task that was already completed/failed — and therefore no longer
        ``running`` — is never touched. This complements the reclaim-on-``start``
        in :meth:`start`, which handles rows orphaned by a hard crash that never
        ran this path.
        """
        if self.conn is None:
            return
        try:
            self.conn.execute(
                "UPDATE task_queue SET status = ?, claimed_by = NULL, "
                "lease_expires_at = NULL WHERE task_id = ? AND status = ?",
                (TASK_PENDING, task_id, TASK_RUNNING),
            )
            self.conn.commit()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to requeue in-flight task %s", task_id)

    async def _handle_task(self, row: sqlite3.Row) -> None:
        task_id = row["task_id"]
        task_type = row["task_type"]
        payload = json.loads(row["payload"]) if row["payload"] else {}

        try:
            if task_type == "extract":
                await self._process_extract(payload)
            elif task_type == "replace":
                await self._process_replace(payload)
            elif task_type == "forget":
                await self._process_forget(payload)
            elif task_type == "persist_pre":
                await self._process_persist_pre(payload)
            else:
                raise ValueError(f"unknown task_type: {task_type}")

            self.conn.execute(
                "UPDATE task_queue SET status = ?, completed_at = ?, "
                "claimed_by = NULL, lease_expires_at = NULL "
                "WHERE task_id = ?",
                (TASK_DONE, now_ms(), task_id),
            )
            self.conn.commit()
        except Exception as exc:
            # Roll *back* first. A handler that failed halfway through its
            # inserts leaves an open implicit transaction; committing the
            # bookkeeping below without rolling back would persist the partial
            # write — a fact row that has an FTS entry but no vector, which no
            # index repair would ever be told about because the task reports
            # `dead` for an unrelated reason.
            self._safe_rollback()
            candidate_id = payload.get("candidate_id")
            if candidate_id:
                self._finish_candidate(
                    candidate_id,
                    CAND_STATUS_ERROR,
                    reject_kind="task_error",
                    reject_reason=str(exc)[:500],
                )
            await self._record_failure(task_id, row["retry_count"], exc)

    def _safe_rollback(self) -> None:
        """Roll back any open implicit transaction, best-effort."""
        try:
            self.conn.rollback()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to roll back after a task failure")

    async def _record_failure(
        self, task_id: str, retry_count: int, exc: Exception
    ) -> None:
        """Apply the retry / dead policy after a failed task.

        A task is allowed ``max_retries`` attempts in total; after that many
        failures it is marked ``dead``. Retries in between are spaced by an
        exponential backoff (1s, 2s, 4s, ... capped at 60s).
        """
        retry_count = int(retry_count) + 1
        if retry_count >= self.max_retries:
            self.conn.execute(
                "UPDATE task_queue SET status = ?, retry_count = ?, error = ?, "
                "completed_at = ?, claimed_by = NULL, lease_expires_at = NULL "
                "WHERE task_id = ?",
                (TASK_DEAD, retry_count, str(exc)[:2000], now_ms(), task_id),
            )
            self.conn.commit()
            self._log_dead(task_id, exc)
            return

        # Requeue for the next poll after the backoff delay. The claim is
        # released with it: the row is going back to `pending`, so keeping
        # `claimed_by` would attribute it to a worker that has stopped working
        # on it.
        delay = min(2 ** (retry_count - 1), 60)
        self.conn.execute(
            "UPDATE task_queue SET status = ?, retry_count = ?, error = ?, "
            "claimed_by = NULL, lease_expires_at = NULL "
            "WHERE task_id = ?",
            (TASK_PENDING, retry_count, str(exc)[:2000], task_id),
        )
        self.conn.commit()
        logger.warning(
            "Task %s failed (%d/%d): %s; retrying in %ss",
            task_id, retry_count, self.max_retries, exc, delay,
        )
        # Sleep the backoff *outside* the claim loop by yielding to the loop.
        await asyncio.sleep(delay)

    def _log_dead(self, task_id: str, exc: Exception) -> None:
        """Mark a dead task, log an alarm and record an event."""
        logger.error("Task %s permanently failed (dead): %s", task_id, exc)
        record_event(
            self.conn,
            "task_dead",
            {"task_id": task_id, "error": str(exc)[:2000]},
        )

    # -- extract handler ---------------------------------------------------------

    async def _process_extract(self, payload: dict) -> None:
        """Run extraction + validation + persistence for an extract task.

        Args:
            payload: The task payload (candidate_id, user_id, session_id,
                turn_id, raw_text, optional scope_context).
        """
        candidate_id = payload["candidate_id"]
        user_id = payload["user_id"]
        session_id = payload["session_id"]
        turn_id = int(payload.get("turn_id", 0))
        text = payload.get("raw_text", "")
        scope_context = payload.get("scope_context")

        candidates = self.extractor.extract(text, user_id, session_id, turn_id)

        if not candidates:
            self._finish_candidate(candidate_id, CAND_STATUS_SKIPPED)
            return

        outcome = await self._apply_candidates(
            candidates, user_id, session_id, scope_context=scope_context
        )
        self._finish_candidate(
            candidate_id, CAND_STATUS_APPLIED, outcome=outcome
        )

    def _reinforce(
        self, fact_id: str, user_id: str, session_id: str, kind: str
    ) -> bool:
        """Record one reinforcement event, never letting it break the task.

        Reinforcement is a ranking refinement: a failure to record it must not
        fail the extract/persist task that produced it.

        Args:
            fact_id: The fact being strengthened.
            user_id: Owner of the fact.
            session_id: Session the evidence came from.
            kind: One of the ``KIND_*`` constants (see
                :mod:`~atom_memory.reinforce`).

        Returns:
            ``True`` when the aggregate actually changed.
        """
        try:
            return record_reinforcement(
                self.conn, fact_id, user_id, session_id, kind, curve=self.curve
            ) is not None
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to reinforce fact %s (%s)", fact_id, kind)
            return False

    async def _apply_candidates(
        self,
        candidates: List[FactCandidate],
        user_id: str,
        session_id: str,
        force_supersede: Optional[List[str]] = None,
        scope_context: Optional[dict] = None,
        force_scope_ids: Optional[List[int]] = None,
    ) -> dict:
        """Validate and persist a batch of candidates under one outcome.

        This is the single write gate for extracted candidates, whichever
        extractor produced them (rules, the LLM, or a ``replace`` request), so
        every path gets the same conflict policy, the same ingest cleaning and
        the same auditable outcome.

        A batch is first reduced to **one winner per single-valued key**: if an
        extractor emits two competing values for the same key from one utterance
        the store cannot honour both, and resolving the ambiguity by keeping
        whichever happened to be written first would make the result depend on
        the extraction order. The winner is the candidate with the stronger
        evidence (ties go to the first, and the losers are reported as
        ``batch_duplicate`` rather than silently absorbed).

        Scope is resolved **once per batch**, before any candidate is validated:
        one utterance comes from one place, and a per-candidate resolution would
        let two facts from the same sentence land in different projects.

        Args:
            candidates: Candidates to persist.
            user_id: Owner of the facts.
            session_id: Session the write belongs to (reinforcement scope).
            force_supersede: Fact ids the caller has explicitly asked to
                replace (``replace`` requests). Those are superseded by the new
                fact even when the evidence comparison would have kept them —
                the caller named them.
            scope_context: The session context payload (see
                :func:`~atom_memory.scope.resolution_for`), or ``None`` for a
                scope-blind write.
            force_scope_ids: Scopes the new facts must bind to, whatever the
                context says. Used by ``replace`` so a replacement stays in the
                scope of the fact it replaces rather than jumping to wherever the
                caller happens to be now.

        Returns:
            The accumulated :func:`empty_outcome` mapping, plus a ``scope`` key
            describing where the batch was filed.
        """
        forced = {str(fid) for fid in (force_supersede or [])}
        outcome = empty_outcome()
        winners = self._dedupe_batch(candidates, outcome)
        new_ids: List[str] = []

        store = self.scope_store()
        resolution = self._resolve_write_scope(
            user_id, session_id, scope_context, force_scope_ids
        )
        visible_ids = self._write_scope_ids(resolution)
        outcome["scope"] = resolution.to_dict(store)
        domains = self._resolve_write_domains(user_id, winners, resolution)
        outcome["domains"] = [assignment.to_dict() for assignment in domains]

        for candidate, assignment in zip(winners, domains):
            result = validate(
                candidate,
                self.conn,
                privacy_filter=self.privacy_filter,
                max_field_chars=self.config.max_field_chars,
                max_content_chars=self.config.max_content_chars,
                scope_ids=visible_ids,
                multi_valued_predicates=self.config.multi_valued_predicates,
            )
            if not result.ok:
                if result.kind == "idempotent" and result.suppressed:
                    # The user re-stated a claim we already hold: the cleanest
                    # reuse evidence there is, and it costs nothing to observe.
                    changed = self._reinforce(
                        result.suppressed, user_id, session_id, KIND_USER_RESTATED
                    )
                    self._attach_domains(result.suppressed, assignment)
                    outcome["reinforced"].append(
                        {
                            "fact_id": result.suppressed,
                            "applied": bool(changed),
                            "object": candidate.object,
                            "predicate": candidate.predicate,
                            # Which test recognised the repeat: the SPO-exact pass
                            # here, or content identity / embedding further down.
                            # The receipt names it so a human can tell an exact
                            # restatement from a semantic merge.
                            "on": "idempotent",
                        }
                    )
                    continue
                if result.kind == "conflict" and result.conflict_rows:
                    await self._resolve_and_write(
                        candidate, result, outcome, forced, resolution, visible_ids,
                        assignment,
                    )
                    continue
                self._reject(
                    outcome,
                    candidate,
                    kind=result.kind,
                    reason=result.reason,
                )
                continue

            persisted = await self._persist_fact(
                candidate,
                trace_id=None,
                scope_ids=visible_ids,
                bind_scope_id=resolution.scope_id,
                conditions=self._conditions_for(candidate, resolution),
                domains=assignment,
            )
            if not persisted.written:
                # The claim (or the body) is already stored: this is a repeat, and
                # the only correct effect is the reinforcement `_fold_into` just
                # recorded. Reporting it as "written" would claim a row that does
                # not exist.
                outcome["reinforced"].append(
                    {
                        "fact_id": persisted.existing_fact_id,
                        "applied": True,
                        "object": candidate.object,
                        "predicate": candidate.predicate,
                        "on": persisted.deduped_on,
                    }
                )
                continue
            if result.truncated_fields:
                outcome["truncated"].extend(result.truncated_fields)
            new_ids.append(persisted.fact_id)
            outcome["written"].append(persisted.fact_id)
            self._link_cross_scope(
                candidate, persisted.fact_id, resolution.scope_id, visible_ids, outcome
            )

        # A `replace` whose new text no longer collides with anything still has
        # to retire the fact the caller named.
        already = {
            str(fid)
            for entry in outcome["superseded"]
            for fid in (entry.get("old_fact_ids") or [])
            or ([entry["old_fact_id"]] if entry.get("old_fact_id") else [])
        }
        for old_id in forced:
            if old_id in already:
                continue
            fresh = self._active_fact(old_id)
            if fresh is not None and new_ids:
                self._supersede(old_id, new_ids[0])
                outcome["superseded"].append(
                    {
                        "old_fact_id": old_id,
                        "old_fact_ids": [old_id],
                        "new_fact_id": new_ids[0],
                        "predicate": fresh["predicate"],
                        "old_object": fresh["object"],
                        "new_object": None,
                        "reason": "caller_replace",
                    }
                )
        return outcome

    def _dedupe_batch(
        self, candidates: List[FactCandidate], outcome: dict
    ) -> List[FactCandidate]:
        """Reduce a batch to one candidate per single-valued key.

        Args:
            candidates: The batch, in extraction order.
            outcome: The outcome accumulator the losers are reported into.

        Returns:
            The surviving candidates, in their original relative order.
        """
        best: dict = {}
        for index, candidate in enumerate(candidates):
            if is_multi_valued(
                candidate.predicate or "",
                getattr(candidate, "type", "semantic"),
                self.config.multi_valued_predicates,
            ):
                best[("multi", index)] = candidate
                continue
            key = (candidate.subject, candidate.predicate)
            weight = _evidence(candidate)
            current = best.get(key)
            if current is None or weight > _evidence(current):
                if current is not None:
                    self._reject_dropped_batch_member(current, candidate)
                    self._reject(
                        outcome,
                        current,
                        kind=REASON_BATCH_DUPLICATE,
                        reason=(
                            f"another candidate in the same batch claims "
                            f"{candidate.predicate!r} with stronger evidence"
                        ),
                    )
                best[key] = candidate
            else:
                self._reject_dropped_batch_member(current, candidate)
                self._reject(
                    outcome,
                    candidate,
                    kind=REASON_BATCH_DUPLICATE,
                    reason=(
                        f"{candidate.predicate!r} is single-valued and already "
                        f"claimed in this batch"
                    ),
                )
        return list(best.values())

    def _reject_dropped_batch_member(
        self, candidate: FactCandidate, kept: FactCandidate
    ) -> None:
        """Audit-log a candidate dropped by :meth:`_dedupe_batch`.

        A batch drop never reaches the store, so without this the only trace is
        the write receipt and the outcome JSON on the task row — a memory that
        was *never written* leaves no queryable record, which is exactly how a
        dropped to-do can go missing without anyone being able to say what it
        was. The event is written best-effort, like every other audit event.

        Args:
            candidate: The dropped candidate.
            kept: The candidate that won the key and was kept.
        """
        record_event(
            self.conn,
            "fact_rejected",
            {
                "candidate_id": candidate.candidate_id,
                "subject": candidate.subject,
                "predicate": candidate.predicate,
                "object": candidate.object,
                "kept_candidate_id": kept.candidate_id,
                "kept_object": kept.object,
                "reason": REASON_BATCH_DUPLICATE,
                "detail": (
                    f"{candidate.predicate!r} was read as single-valued; the "
                    f"batch kept the stronger claim {kept.object!r}"
                ),
            },
            user_id=candidate.user_id,
        )

    async def _resolve_and_write(
        self,
        candidate: FactCandidate,
        result,
        outcome: dict,
        forced: set,
        scope_resolution: ScopeResolution,
        visible_ids: List[int],
        domains: Optional[DomainAssignment] = None,
    ) -> None:
        """Apply the conflict policy to one contradicting candidate.

        Args:
            candidate: The candidate the validator found conflicting.
            result: The validator's result, carrying ``conflict_rows``.
            outcome: The outcome accumulator.
            forced: Fact ids the caller explicitly asked to replace.
            scope_resolution: The scope the batch is being written into.
            visible_ids: The scopes visible from it (dedup/conflict window).
            domains: The topics the batch resolved for this candidate.
        """
        rows = list(result.conflict_rows or [])
        resolution = resolve_conflict(
            candidate.confidence,
            candidate.importance,
            rows,
            confidence_margin=self.config.conflict_confidence_margin,
        )
        forced_rows = [r for r in rows if str(r["fact_id"]) in forced]
        if resolution.action == ACTION_REJECT and not forced_rows:
            self._reject(
                outcome,
                candidate,
                kind="conflict",
                reason=resolution.reason,
                detail=resolution.detail,
                conflict_with=result.conflict_with,
                stored_object=resolution.stored_object,
            )
            record_event(
                self.conn,
                "fact_rejected",
                {
                    "candidate_id": candidate.candidate_id,
                    "subject": candidate.subject,
                    "predicate": candidate.predicate,
                    "object": candidate.object,
                    "stored_object": resolution.stored_object,
                    "reason": resolution.reason,
                    "detail": resolution.detail,
                },
                user_id=candidate.user_id,
            )
            return

        # Either the newer assertion wins, or the caller named the target of a
        # `replace` — in both cases the stored value(s) retire behind the new
        # fact, and the whole key is retired together rather than one row of it.
        supersede_ids = [
            str(r["fact_id"]) for r in rows if str(r["fact_id"]) in forced
        ] or list(resolution.supersede_ids)
        persisted = await self._persist_fact(
            candidate,
            trace_id=None,
            scope_ids=visible_ids,
            bind_scope_id=scope_resolution.scope_id,
            conditions=self._conditions_for(candidate, scope_resolution),
            domains=domains,
        )
        if not persisted.written:
            # The new value turned out to be the one already stored (a reworded
            # repeat of an active claim). Retiring anything now would remove the
            # very value that matched, so the reinforcement is the whole effect.
            self._attach_domains(persisted.existing_fact_id, domains)
            outcome["reinforced"].append(
                {
                    "fact_id": persisted.existing_fact_id,
                    "applied": True,
                    "object": candidate.object,
                    "predicate": candidate.predicate,
                    "on": persisted.deduped_on,
                }
            )
            return
        new_id = persisted.fact_id
        for old_id in supersede_ids:
            self._supersede(old_id, new_id)
        outcome["written"].append(new_id)
        self._link_cross_scope(
            candidate, new_id, scope_resolution.scope_id, visible_ids, outcome
        )
        outcome["superseded"].append(
            {
                "old_fact_id": supersede_ids[0] if supersede_ids else None,
                "old_fact_ids": supersede_ids,
                "new_fact_id": new_id,
                "predicate": candidate.predicate,
                "old_object": resolution.stored_object,
                "new_object": candidate.object,
                "reason": (
                    "caller_replace" if forced_rows else resolution.reason
                ),
            }
        )
        record_event(
            self.conn,
            "fact_superseded",
            {
                "subject": candidate.subject,
                "predicate": candidate.predicate,
                "old_object": resolution.stored_object,
                "new_object": candidate.object,
                "old_fact_ids": supersede_ids,
                "new_fact_id": new_id,
                "reason": "caller_replace" if forced_rows else resolution.reason,
            },
            user_id=candidate.user_id,
        )

    def _reject(
        self,
        outcome: dict,
        candidate: FactCandidate,
        *,
        kind: str,
        reason: str,
        detail: str = "",
        conflict_with: Optional[str] = None,
        stored_object: str = "",
    ) -> None:
        """Record a candidate that was *not* written, with its reason.

        A refusal is part of the outcome, not a silent `continue`: the caller
        (and the model, through the tool's render) has to be able to tell "this
        was stored" from "this was refused because a stronger claim is already
        stored".
        """
        outcome["rejected"].append(
            {
                "candidate_id": candidate.candidate_id,
                "kind": kind,
                "reason": reason,
                "detail": detail,
                "subject": candidate.subject,
                "predicate": candidate.predicate,
                "object": candidate.object,
                "stored_object": stored_object,
                "conflict_with": conflict_with,
            }
        )
        logger.debug(
            "Candidate %s rejected (%s): %s",
            candidate.candidate_id, kind, reason or detail,
        )

    def _active_fact(self, fact_id: str) -> Optional[sqlite3.Row]:
        """Return one active fact row by id, or ``None``."""
        return self.conn.execute(
            "SELECT fact_id, subject, predicate, object FROM facts "
            "WHERE fact_id = ? AND status = 'active'",
            (fact_id,),
        ).fetchone()

    async def _process_persist_pre(self, payload: dict) -> None:
        """Persist pre-extracted candidates (e.g. from the dsh-side LLM extractor).

        The dsh host runs LLM-first extraction in its own process (where
        ``ctx.llm`` lives) and ships the resulting typed candidates here via the
        ``persist_pre`` task. Each candidate dict is turned into a
        :class:`FactCandidate` and pushed through the *same* validation +
        persistence chain as rule extraction, so ingest cleaning, conflict
        resolution and idempotency (identical-SPO suppression) all apply.

        Args:
            payload: keys ``candidate_id``, ``user_id``, ``session_id``,
                ``turn_id``, ``candidates`` (list of dicts with subject /
                predicate / object / type / content / qualifiers / conditions /
                scope_hint ...), optional ``scope_context``.
        """
        candidate_id = payload.get("candidate_id") or str(uuid.uuid4())
        user_id = payload["user_id"]
        session_id = payload.get("session_id", "s_default")
        turn_id = int(payload.get("turn_id", 0))
        candidates = payload.get("candidates") or []
        scope_context = payload.get("scope_context")

        if not candidates:
            self._finish_candidate(candidate_id, CAND_STATUS_SKIPPED)
            return

        parsed = [
            _candidate_from_rpc_dict(d, user_id, session_id, turn_id)
            for d in candidates
        ]
        outcome = await self._apply_candidates(
            parsed, user_id, session_id, scope_context=scope_context
        )
        self._finish_candidate(candidate_id, CAND_STATUS_APPLIED, outcome=outcome)

    def _finish_candidate(
        self,
        candidate_id: str,
        status: str,
        *,
        outcome: Optional[dict] = None,
        reject_kind: Optional[str] = None,
        reject_reason: Optional[str] = None,
    ) -> None:
        """Record a candidate's terminal state and what it produced.

        The status alone is not an outcome: ``applied`` used to be written even
        when every extracted candidate had been refused, which made a rejected
        write indistinguishable from a stored one. The reason and the produced
        ids are stored alongside it.

        Args:
            candidate_id: The candidate row to finish.
            status: Terminal status (``applied`` / ``skipped`` / ``error``).
            outcome: The write outcome, when the task produced one.
            reject_kind: Explicit refusal category (used by the error path).
            reject_reason: Human-readable refusal reason.
        """
        outcome = outcome or {}
        rejected = list(outcome.get("rejected") or [])
        if reject_kind is None and rejected:
            # The first refusal is the one the caller needs to see; the full
            # list is in the event log.
            reject_kind = str(rejected[0].get("kind") or "")
            reject_reason = str(
                rejected[0].get("detail") or rejected[0].get("reason") or ""
            )
        self.conn.execute(
            "UPDATE fact_candidates SET status = ?, finished_at = ?, "
            "reject_kind = ?, reject_reason = ?, result_fact_ids = ? "
            "WHERE candidate_id = ?",
            (
                status,
                now_ms(),
                reject_kind,
                (reject_reason or None),
                json.dumps(outcome, ensure_ascii=False, default=str),
                candidate_id,
            ),
        )
        self.conn.commit()

    async def _persist_fact(
        self,
        candidate: FactCandidate,
        trace_id: Optional[str],
        scope_ids: Optional[List[int]] = None,
        bind_scope_id: Optional[int] = None,
        conditions: Tuple[Tuple[str, str], ...] = (),
        domains: Optional[DomainAssignment] = None,
    ) -> PersistResult:
        """Persist a validated candidate, or fold it into a memory already held.

        Two questions are answered before anything is written, and both matter
        more than they look:

        1. **Is this claim already stored?** The content fingerprint (see
           :mod:`~atom_memory.fingerprint`) identifies a claim by its normalised
           content, polarity included. A match means the user just restated
           something the store holds — the cleanest reuse evidence the library
           can observe — so the existing row is *reinforced* instead of being
           duplicated. Before this, only exact SPO repeats of single-valued
           predicates were caught; knowledge bodies and multi-valued facts could
           be re-stored indefinitely, which is where the noise came from.
        2. **Is this body a reworded copy?** For a long body, the candidate's
           own embedding is compared against active facts sharing owner, type,
           subject and predicate. The gate is deliberately tight and narrow: a
           false merge removes a distinct memory from the working set, while a
           missed merge only costs a row.

        Both tests are **restricted to the write's own scope window**
        (``scope_ids``). That is the design's rule, and it is what makes a claim
        repeated in a second project a *second fact* (linked through
        ``fact_origin``) rather than a silent merge: an independent restatement is
        the evidence :meth:`~atom_memory.scope.ScopeStore.abstraction_candidates`
        promotes into a global rule, and folding it away destroys exactly that.

        The fact row, its FTS entry, its vector, its scope binding, its topic
        labels and its conditions go in **one transaction**, and the embedding is
        computed before it opens. They are one logical fact: an interruption
        between them used to leave a row that full-text search could find and
        semantic search could not, which no caller could distinguish from "the
        fact is fine".

        Args:
            candidate: The validated candidate.
            trace_id: Optional trace id recorded with the fact.
            scope_ids: The scopes visible from the write's scope; ``None``
                disables the scope filter (scope-blind writes).
            bind_scope_id: The scope the new fact is filed under. ``None`` leaves
                the fact unbound, which every read path reads as global.
            conditions: Normalised conditions to store with the fact.
            domains: The topics the batch decided this fact is about. ``None``
                (or an empty assignment) stores no labels, which is what
                ``domain_tagging_mode = "off"`` does.

        Returns:
            What happened: the new fact id, or the fact this candidate was
            deduplicated onto and which test recognised it.
        """
        fingerprint = self._fingerprint(candidate)

        scope_sql = ""
        scope_args: list = []
        if scope_ids is not None:
            placeholders = ",".join("?" for _ in scope_ids)
            unbound = ""
            if GLOBAL_SCOPE_ID in scope_ids:
                unbound = (
                    " OR NOT EXISTS (SELECT 1 FROM fact_scope fsu "
                    "WHERE fsu.fact_id = facts.fact_id)"
                )
            scope_sql = (
                f"AND (EXISTS (SELECT 1 FROM fact_scope fs "
                f"WHERE fs.fact_id = facts.fact_id "
                f"AND fs.scope_id IN ({placeholders})){unbound}) "
            )
            scope_args = list(scope_ids)

        existing = self.conn.execute(
            "SELECT fact_id FROM facts WHERE user_id = ? AND content_fingerprint = ? "
            "AND status = 'active' " + scope_sql + "ORDER BY created_at ASC LIMIT 1",
            [candidate.user_id, fingerprint, *scope_args],
        ).fetchone()
        if existing is not None:
            fact_id = str(existing["fact_id"])
            # A restatement is also a second observation of the topic, so the
            # labels merge (by max confidence — see DomainStore.attach) rather
            # than being discarded with the duplicate row.
            self._attach_domains(fact_id, domains)
            return self._fold_into(fact_id, candidate, "fingerprint")

        fact_id = str(uuid.uuid4())
        text = f"{candidate.subject} {candidate.predicate} {candidate.object}"
        content = candidate.content or ""
        searchable = (text + " " + content).strip()
        created_at = now_ms()

        # Embedding is CPU-bound; run in a worker thread so the loop stays
        # responsive during model inference.
        blob = await asyncio.to_thread(self.embed_func, searchable)

        near = self._near_duplicate(candidate, blob, scope_ids)
        if near is not None:
            self._attach_domains(near, domains)
            return self._fold_into(near, candidate, "embedding")

        with self.conn:
            self.conn.execute(
                "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
                "object, qualifiers, confidence, importance, privacy, source_type, "
                "status, superseded_by, observed_at, created_at, trace_id, version, type, "
                "content, content_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fact_id,
                    candidate.user_id,
                    candidate.session_id,
                    candidate.subject,
                    candidate.predicate,
                    candidate.object,
                    candidate.qualifiers,
                    candidate.confidence,
                    candidate.importance,
                    candidate.privacy,
                    "user_explicit",
                    "active",
                    None,
                    created_at,
                    created_at,
                    trace_id,
                    1,
                    getattr(candidate, "type", "semantic") or "semantic",
                    content or None,
                    fingerprint,
                ),
            )
            # FTS index uses jieba-segmented text so Chinese queries can match
            # individual words (unicode61 treats a CJK span as a single token).
            # The body content is indexed too so knowledge facts are findable by
            # their full text, not only the SPO title.
            self.conn.execute(
                "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
                (fact_id, " ".join(segment_text(searchable))),
            )
            self.conn.execute(
                "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                (fact_id, blob),
            )
            # The scope binding and the conditions belong to the same
            # transaction as the row: a fact that exists but is unbound would be
            # read as a global fact (the compatibility rule), which is the one
            # wrong answer that looks normal.
            if bind_scope_id is not None:
                self.conn.execute(
                    "INSERT INTO fact_scope(fact_id, scope_id, priority) "
                    "VALUES (?, ?, 0) ON CONFLICT(fact_id, scope_id) DO NOTHING",
                    (fact_id, int(bind_scope_id)),
                )
            for key, value in conditions:
                self.conn.execute(
                    "INSERT INTO fact_condition(fact_id, key, value) VALUES (?, ?, ?)",
                    (fact_id, key, value),
                )
            # Topic labels share the transaction for the same reason the scope
            # binding does: a fact whose topic was decided but not stored would be
            # read as unlabelled (the compatibility rule) and silently escape
            # every topic filter once one exists.
            if domains is not None and domains.labels:
                for label in domains.labels:
                    self.conn.execute(
                        "INSERT INTO fact_domain(fact_id, domain_id, confidence, "
                        "is_primary, source, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(fact_id, domain_id) DO UPDATE SET "
                        "confidence = MAX(confidence, excluded.confidence), "
                        "is_primary = MAX(is_primary, excluded.is_primary), "
                        "source = excluded.source",
                        (
                            fact_id,
                            int(label.domain_id),
                            float(label.confidence),
                            1 if label.is_primary else 0,
                            str(label.source),
                            created_at,
                        ),
                    )
        return PersistResult(fact_id)

    def _fingerprint(self, candidate: FactCandidate) -> str:
        """Content identity of a candidate (polarity included).

        Args:
            candidate: The candidate to identify.

        Returns:
            A 32-character hex digest.
        """
        return content_fingerprint(
            user_id=candidate.user_id,
            type=getattr(candidate, "type", "semantic") or "semantic",
            subject=candidate.subject,
            predicate=candidate.predicate,
            object=candidate.object,
            content=candidate.content,
            negated=has_negation(candidate.qualifiers),
        )

    def _fold_into(self, fact_id: str, candidate: FactCandidate, on: str) -> PersistResult:
        """Reinforce an existing fact instead of writing a duplicate.

        Args:
            fact_id: The active fact that already holds this memory.
            candidate: The candidate that turned out to repeat it.
            on: Which test recognised the repeat (``fingerprint`` /
                ``embedding``) — recorded in the audit event so a later reader
                can tell an exact repeat from a semantic one.

        Returns:
            A deduplicated :class:`PersistResult`.
        """
        self._reinforce(
            fact_id, candidate.user_id, candidate.session_id, KIND_USER_RESTATED
        )
        record_event(
            self.conn,
            "fact_deduplicated",
            {
                "fact_id": fact_id,
                "on": on,
                "subject": candidate.subject,
                "predicate": candidate.predicate,
                "object": candidate.object,
            },
            user_id=candidate.user_id,
        )
        return PersistResult(None, on, fact_id)

    def _near_duplicate(
        self,
        candidate: FactCandidate,
        blob: bytes,
        scope_ids: Optional[List[int]] = None,
    ) -> Optional[str]:
        """Find an active fact whose body is the same memory, reworded.

        Narrow on purpose, because the failure modes are asymmetric: merging two
        genuinely different procedures loses one of them from every recall, while
        a missed merge costs one redundant row. The comparison therefore requires
        the same owner, type, subject **and** predicate, a distance inside a tight
        gate, and — when the write is scoped — membership of the same scope
        window, and it only runs for bodies long enough to be a document.

        Args:
            candidate: The candidate being written.
            blob: Its serialised embedding (already computed for the write).
            scope_ids: The write's scope window, or ``None`` to compare across
                every scope.

        Returns:
            The fact id to fold into, or ``None``.
        """
        threshold = float(self.config.dedup_max_distance or 0.0)
        if threshold <= 0:
            return None
        body = (candidate.content or "").strip()
        if len(body) < int(self.config.dedup_min_body_chars or 0):
            return None
        fact_type = getattr(candidate, "type", "semantic") or "semantic"
        try:
            neighbours = self.conn.execute(
                "SELECT fact_id, distance FROM facts_vec WHERE embedding MATCH ? AND k = 5",
                (blob,),
            ).fetchall()
        except sqlite3.Error:
            logger.exception("Near-duplicate probe failed; storing as a new fact")
            return None
        window = set(int(s) for s in scope_ids) if scope_ids is not None else None
        for neighbour in neighbours:
            distance = neighbour["distance"]
            if distance is None or float(distance) > threshold:
                continue
            fact = self.conn.execute(
                "SELECT fact_id, status, type, subject, predicate FROM facts "
                "WHERE fact_id = ?",
                (neighbour["fact_id"],),
            ).fetchone()
            if fact is None or fact["status"] != "active":
                continue
            if (fact["type"] or "semantic") != fact_type:
                continue
            if (fact["subject"] or "") != (candidate.subject or ""):
                continue
            if (fact["predicate"] or "") != (candidate.predicate or ""):
                continue
            if window is not None and not self._fact_in_window(
                str(fact["fact_id"]), window
            ):
                continue
            return str(fact["fact_id"])
        return None

    def _fact_in_window(self, fact_id: str, window: set) -> bool:
        """Whether a fact is visible from a scope window.

        An unbound fact counts as global, so it is inside every window that
        contains the root — which every resolution does.
        """
        rows = self.conn.execute(
            "SELECT scope_id FROM fact_scope WHERE fact_id = ?", (fact_id,)
        ).fetchall()
        if not rows:
            return GLOBAL_SCOPE_ID in window
        return any(int(r["scope_id"]) in window for r in rows)

    # -- scope plumbing ------------------------------------------------------

    def scope_store(self) -> ScopeStore:
        """Return the scope store bound to this worker's connection.

        Rebuilt per call rather than cached: the store holds a label cache, and a
        long-lived worker that cached it would keep serving labels for scopes
        renamed or merged in another process.
        """
        return ScopeStore(self.conn, self.config)

    # -- topic plumbing ------------------------------------------------------

    def domain_store(self) -> DomainStore:
        """Return the topic store bound to this worker's connection.

        Rebuilt per call for the same reason :meth:`scope_store` is: the store
        caches names, and a worker outliving a rename would keep labelling facts
        with a name that no longer exists.
        """
        return DomainStore(self.conn, self.config)

    def _resolve_write_domains(
        self,
        user_id: str,
        candidates: List[FactCandidate],
        resolution: ScopeResolution,
    ) -> List[DomainAssignment]:
        """Decide the topics of a batch, one assignment per candidate.

        The session's topic set is derived **once per batch** (like the scope
        resolution, and for the same reason: one utterance comes from one place),
        then each candidate is labelled against it. Tagging mode ``off`` returns
        empty assignments for everyone, which is what keeps phase one of the
        rollout free of any behaviour change.

        Args:
            user_id: Owner of the words the labels come from.
            candidates: The batch, in order.
            resolution: The scope the batch is being written into.

        Returns:
            An assignment per candidate, positionally aligned.
        """
        mode = str(getattr(self.config, "domain_tagging_mode", "auto") or "auto")
        if mode == "off" or not candidates:
            return [DomainAssignment() for _ in candidates]
        store = self.domain_store()
        scope_store = self.scope_store()
        scope_id = int(resolution.scope_id)
        path = scope_store.path_of(scope_id)
        session = store.session_domains(user_id, scope_ids=[scope_id], scope_paths=[path])
        out: List[DomainAssignment] = []
        for candidate in candidates:
            hints = self._ordered_hints(candidate)
            out.append(
                store.assign(
                    user_id,
                    hints=hints,
                    text=" ".join(
                        part
                        for part in (
                            candidate.subject or "",
                            candidate.predicate or "",
                            candidate.object or "",
                            candidate.content or "",
                        )
                        if part
                    ),
                    session=session,
                    scope_id=scope_id,
                )
            )
        return out

    def _ordered_hints(self, candidate: FactCandidate) -> List[str]:
        """Return a candidate's topic proposals, primary first.

        The order matters twice over: the first entry becomes the fact's primary
        label, and the cap drops from the tail. So the extractor's own
        ``primary_domain`` is moved to the front **before** anything is capped —
        the version of this that capped first threw away a primary the model had
        deliberately placed last, which is the one ordering the cap must not be
        allowed to break.

        Args:
            candidate: The candidate being labelled.

        Returns:
            At most ``domain_max_per_hint`` proposals, primary first.
        """
        raw = [str(hint) for hint in (candidate.domain_hints or ()) if str(hint or "").strip()]
        primary = str(candidate.primary_domain or "").strip()
        ordered: List[str] = []
        if primary:
            ordered.append(primary)
        for hint in raw:
            if hint != primary:
                ordered.append(hint)
        cap = max(1, int(getattr(self.config, "domain_max_per_hint", 3) or 3))
        return ordered[:cap]

    def _attach_domains(
        self, fact_id: str, assignment: Optional[DomainAssignment]
    ) -> List:
        """Merge a batch's topic labels onto a fact that already exists.

        Args:
            fact_id: The fact being folded into.
            assignment: The labels the batch resolved to, or ``None``.

        Returns:
            The labels stored on the fact afterwards.
        """
        if not fact_id or assignment is None or not assignment.labels:
            return []
        try:
            return self.domain_store().attach(fact_id, assignment)
        except sqlite3.Error:  # pragma: no cover - defensive
            logger.exception("Failed to attach domain labels to %s", fact_id)
            return []

    def _resolve_write_scope(
        self,
        user_id: str,
        session_id: str,
        scope_context: Optional[dict],
        force_scope_ids: Optional[List[int]] = None,
    ) -> ScopeResolution:
        """Decide where a batch of writes belongs.

        Args:
            user_id: Owner of the write.
            session_id: Session the context came from.
            scope_context: The host's context payload, or ``None``.
            force_scope_ids: Scopes the write must use (a ``replace`` inherits
                the replaced fact's scopes). When given, no resolution runs at
                all: the caller has already decided, and re-resolving could file
                the replacement in a different project from the fact it replaces.

        Returns:
            The resolution to file the batch under. Always a real scope id: with
            scope awareness disabled, or with no usable context, that is the root,
            which is exactly the pre-scope behaviour.
        """
        store = self.scope_store()
        if force_scope_ids:
            target = int(force_scope_ids[0])
            return ScopeResolution(
                scope_id=target,
                confidence=1.0,
                status=STATUS_GLOBAL if target == GLOBAL_SCOPE_ID else "bound",
                detail="scope inherited from the replaced fact",
            )
        if not self.config.scope_aware:
            return ScopeResolution(
                scope_id=GLOBAL_SCOPE_ID,
                confidence=1.0,
                status=STATUS_GLOBAL,
                detail="scope awareness disabled",
            )
        _, resolution = resolution_for(
            self.conn,
            self.config,
            scope_context,
            user_id=user_id,
            session_id=session_id,
            create=True,
        )
        # A plain global resolution is the default for a caller that sends no
        # context, and an audit line per write saying "nothing to resolve" would
        # bury the resolutions that are actually interesting.
        if resolution.status != STATUS_GLOBAL:
            record_event(
                self.conn,
                "scope_resolved",
                {
                    "status": resolution.status,
                    "scope_id": resolution.scope_id,
                    "confidence": round(float(resolution.confidence), 4),
                    "matched": list(resolution.matched),
                    "candidates": [c.to_dict() for c in resolution.candidates],
                },
                user_id=user_id,
            )
        return resolution

    def _write_scope_ids(self, resolution: ScopeResolution) -> Optional[List[int]]:
        """Return the scopes a write's dedup/conflict window covers.

        **Exactly the write's own scope** — one id — and not its ancestors. That
        is the design's rule for a collision across scopes: "具体覆盖一般 → 保留双
        方，具体作用域优先级更高" (§5.4). If the window included ancestors, a
        project that states its own value under a single-valued key would
        *supersede* the global rule rather than coexist with it, and the general
        rule — the one every other project still needs — would be gone. Recall
        expands ancestors instead (§6.2), so the project's statement and the
        global rule are both returned, the specific one ranked higher.

        A fact with no binding counts as global (the compatibility rule), which is
        why the window is expressed as a scope id rather than as a set of facts.

        Returns ``None`` when scope awareness is off, which switches every
        scope-aware query back to its pre-scope form.
        """
        if not self.config.scope_aware:
            return None
        return [int(resolution.scope_id)]

    def _conditions_for(
        self, candidate: FactCandidate, resolution: ScopeResolution
    ) -> Tuple[Tuple[str, str], ...]:
        """Return the conditions to store with a fact.

        The context's conditions (``doc_type=proposal`` for a session that is
        writing a proposal) and the extractor's own conditions are merged: both
        describe when the claim holds, and dropping either would make the fact
        look unconditional. Capped and normalised by
        :func:`~atom_memory.context.normalize_conditions`.
        """
        merged: List[Tuple[str, str]] = list(resolution.conditions or ())
        merged.extend(normalize_conditions(candidate.conditions or ()))
        if not merged:
            return ()
        return tuple(sorted(set(merged)))[:MAX_CONDITIONS]

    def _link_cross_scope(
        self,
        candidate: FactCandidate,
        fact_id: str,
        scope_id: int,
        visible_ids: Optional[List[int]],
        outcome: dict,
    ) -> None:
        """Record relations between a new fact and same-key facts elsewhere.

        Called for every fact that was actually written. The design forbids
        judging a contradiction across scopes — "a project overrides the global
        rule" is not evidence the rule is wrong — so the two statements are kept
        and the relation between them is recorded instead
        (:meth:`~atom_memory.scope.ScopeStore.relate_cross_scope`). The resulting
        links are what the abstraction pass later reads to promote a pattern that
        several projects arrived at independently.

        Best-effort: a missing relation costs an explanation, never a fact.
        """
        if visible_ids is None or not candidate.subject or not candidate.predicate:
            return
        try:
            neighbours = cross_scope_neighbours(candidate, self.conn, visible_ids)
        except sqlite3.Error:  # pragma: no cover - defensive
            logger.exception("Cross-scope neighbour lookup failed")
            return
        if not neighbours:
            return
        store = self.scope_store()
        candidate_neg = has_negation(candidate.qualifiers)
        cand_obj = (candidate.object or "").strip()
        relations: List[dict] = []
        for neighbour in neighbours:
            other_id = str(neighbour["fact_id"])
            if other_id == fact_id:
                continue
            same_object = (
                (neighbour["object"] or "").strip() == cand_obj
                and has_negation(neighbour["qualifiers"]) == candidate_neg
            )
            other_scopes = store.fact_scopes([other_id]).get(other_id) or ()
            if not other_scopes:
                continue
            relation = store.relate_cross_scope(
                fact_id,
                scope_id,
                other_id,
                int(other_scopes[0]),
                same_object,
            )
            relations.append(
                {"other_fact_id": other_id, "relation": relation,
                 "same_object": same_object}
            )
        if relations:
            outcome.setdefault("cross_scope", []).extend(relations)
            record_event(
                self.conn,
                "fact_cross_scope",
                {
                    "fact_id": fact_id,
                    "scope_id": scope_id,
                    "relations": relations,
                },
                user_id=candidate.user_id,
            )

    # -- mutation bookkeeping -----------------------------------------------------

    def _enqueue(self, task_type: str, user_id: str, payload: dict) -> str:
        """Insert a task into the queue and return its id."""
        task_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO task_queue(task_id, task_type, payload, status, "
            "priority, retry_count, max_retries, created_at) "
            "VALUES (?, ?, ?, 'pending', 5, 0, ?, ?)",
            (
                task_id,
                task_type,
                json.dumps(payload),
                self.max_retries,
                now_ms(),
            ),
        )
        self.conn.commit()
        return task_id

    def _supersede(self, old_fact_id: str, new_fact_id: str) -> None:
        """Soft-replace: mark ``old_fact_id`` superseded by ``new_fact_id``."""
        self.conn.execute(
            "UPDATE facts SET status = 'superseded', superseded_by = ? "
            "WHERE fact_id = ?",
            (new_fact_id, old_fact_id),
        )
        self.conn.commit()

    def _retract(self, fact_id: str) -> None:
        """Soft-delete: mark a fact ``retracted``."""
        self.conn.execute(
            "UPDATE facts SET status = 'retracted' WHERE fact_id = ?",
            (fact_id,),
        )
        self.conn.commit()

    def purge_facts(self, fact_ids: List[str]) -> int:
        """Physically delete facts and every derived row for them.

        Soft deletion is the default because it keeps provenance; a *purge* is
        the explicit exception, for a user who wants the content gone rather
        than merely unused. It removes the fact row (which cascades
        ``fact_reinforcements``), its FTS entry and its vector, so nothing is
        left behind to be re-derived by the maintenance pass.

        Args:
            fact_ids: Fact ids to delete.

        Returns:
            How many fact rows were removed.
        """
        if not fact_ids:
            return 0
        removed = 0
        with self.conn:
            for fact_id in fact_ids:
                self.conn.execute(
                    "DELETE FROM facts_fts WHERE fact_id = ?", (fact_id,)
                )
                self.conn.execute(
                    "DELETE FROM facts_vec WHERE fact_id = ?", (fact_id,)
                )
                cursor = self.conn.execute(
                    "DELETE FROM facts WHERE fact_id = ?", (fact_id,)
                )
                removed += cursor.rowcount
        return removed

    # -- replace handler -----------------------------------------------------------

    async def _process_replace(self, payload: dict) -> None:
        """Handle a replace task: persist the new fact(s), supersede the old.

        A ``replace`` is the one write whose target is *named* by the caller, so
        the conflict policy yields to that: the named fact is superseded by the
        new statement even when the evidence comparison would have kept it.

        Args:
            payload: keys candidate_id, user_id, old_fact_id, new_text,
                session_id, turn_id, optional scope_context.
        """
        candidate_id = payload["candidate_id"]
        user_id = payload["user_id"]
        old_fact_id = payload["old_fact_id"]
        text = payload.get("new_text", "")
        session_id = payload.get("session_id", "s_default")
        turn_id = int(payload.get("turn_id", 0))

        # Only replace an active fact owned by the user.
        old = self.conn.execute(
            "SELECT fact_id FROM facts WHERE user_id = ? AND fact_id = ? "
            "AND status = 'active'",
            (user_id, old_fact_id),
        ).fetchone()
        if old is None:
            self._finish_candidate(candidate_id, CAND_STATUS_SKIPPED)
            return

        # A replacement belongs where the fact it replaces belongs. Resolving
        # from the *current* context instead would silently move a fact from the
        # project it documents into whatever project the caller is in now.
        inherited = list(
            self.scope_store().fact_scopes([old_fact_id]).get(old_fact_id) or ()
        )

        candidates = self.extractor.extract(text, user_id, session_id, turn_id)
        if not candidates:
            self._finish_candidate(candidate_id, CAND_STATUS_SKIPPED)
            return
        outcome = await self._apply_candidates(
            candidates,
            user_id,
            session_id,
            force_supersede=[old_fact_id],
            scope_context=payload.get("scope_context"),
            force_scope_ids=inherited or None,
        )
        self._finish_candidate(candidate_id, CAND_STATUS_APPLIED, outcome=outcome)

    # -- forget handler ---------------------------------------------------------------

    async def _process_forget(self, payload: dict) -> None:
        """Handle a forget task: soft-delete (or purge) the target fact(s).

        Args:
            payload: keys candidate_id, user_id, fact_id (optional),
                session_id (optional), purge (optional bool).
        """
        candidate_id = payload["candidate_id"]
        user_id = payload["user_id"]
        fact_id = payload.get("fact_id")
        session_id = payload.get("session_id")
        purge = bool(payload.get("purge"))
        outcome = empty_outcome()

        targets: List[str] = []
        if fact_id:
            # Honour user isolation: retract any non-retracted fact of the
            # user (active or superseded both leave memory).
            targets = [
                r["fact_id"]
                for r in self.conn.execute(
                    "SELECT fact_id FROM facts WHERE user_id = ? AND fact_id = ? "
                    "AND status IN ('active', 'superseded')",
                    (user_id, fact_id),
                ).fetchall()
            ]
        elif session_id:
            targets = [
                r["fact_id"]
                for r in self.conn.execute(
                    "SELECT fact_id FROM facts WHERE user_id = ? AND session_id = ? "
                    "AND status IN ('active', 'superseded')",
                    (user_id, session_id),
                ).fetchall()
            ]

        if purge and targets:
            removed = self.purge_facts(targets)
            record_event(
                self.conn,
                "facts_purged",
                {"user_id": user_id, "fact_ids": targets, "removed": removed},
                user_id=user_id,
            )
            outcome["purged"] = targets
        elif targets:
            with self.conn:
                for target in targets:
                    self.conn.execute(
                        "UPDATE facts SET status = 'retracted' WHERE fact_id = ?",
                        (target,),
                    )
            outcome["retracted"] = targets
        else:
            outcome["rejected"].append(
                {
                    "kind": "not_found",
                    "reason": "no active or superseded fact matched the request",
                    "conflict_with": fact_id,
                }
            )

        self._finish_candidate(candidate_id, CAND_STATUS_APPLIED, outcome=outcome)

    # -- maintenance ---------------------------------------------------------------

    def _maintenance_due(self) -> bool:
        """Whether the idle maintenance pass should run now."""
        interval = float(self.config.maintenance_interval_sec or 0.0)
        if self._last_maintenance is None:
            return True  # once per start, so a restart self-repairs
        if interval <= 0:
            return False
        return now_ms() - self._last_maintenance >= interval * 1000.0

    async def maintenance(self, user_id: Optional[str] = None) -> dict:
        """Run the retention, repair, capacity and promotion passes.

        Everything here is *policy about what the store keeps*: bookkeeping
        rows past their retention are collected, facts whose index entries went
        missing are re-derived, the least valuable unprotected facts move to the
        archive tier when a capacity cap is configured, and a claim that several
        scopes arrived at independently is promoted to a global rule.

        Args:
            user_id: Restrict the passes to one user; ``None`` covers every user.

        Returns:
            A summary dict of what changed.
        """
        self._last_maintenance = now_ms()
        result: dict = {
            "pruned_candidates": 0,
            "pruned_tasks": 0,
            "pruned_events": 0,
            "repaired": {},
            "archived": [],
            "promoted": [],
        }
        try:
            result["pruned_candidates"] = self._prune_table(
                "fact_candidates",
                self.config.candidate_retention_days,
                statuses=("applied", "skipped", "error"),
                guard="finished_at IS NOT NULL",
            )
            result["pruned_tasks"] = self._prune_table(
                "task_queue",
                self.config.task_retention_days,
                statuses=(TASK_DONE, TASK_DEAD),
                guard="completed_at IS NOT NULL",
            )
            result["pruned_events"] = self._prune_table(
                "events",
                self.config.event_retention_days,
                statuses=None,
                guard=None,
            )
            result["repaired"] = await self.repair_index()
            result["archived"] = self.enforce_capacity(user_id)
            result["promoted"] = await self.promote_abstractions(user_id)
        except Exception:  # pragma: no cover - maintenance must not kill the loop
            logger.exception("Maintenance pass failed")
        self.last_maintenance_result = result
        return result

    async def promote_abstractions(self, user_id: Optional[str] = None) -> List[dict]:
        """Promote claims that several scopes hold independently into global rules.

        The design's cross-project reuse rule: a pattern three different projects
        arrived at is not a project detail, it is how the work is done — and
        leaving it inside those three projects means the fourth re-learns it. The
        concrete facts stay: they are the evidence behind the rule (linked through
        ``fact_origin``) and the place a project-specific nuance remains visible.

        Runs from the maintenance pass rather than from the write path because it
        is a *store-wide* judgement (it needs three independent scopes, which no
        single write can see) and because the promoted row needs an embedding like
        any other fact.

        Args:
            user_id: Restrict to one user; ``None`` covers every user.

        Returns:
            One entry per promoted rule: ``{"fact_id", "fingerprint",
            "source_fact_ids", "scope_ids"}``.
        """
        if not self.config.scope_aware:
            return []
        store = self.scope_store()
        owners = (
            [user_id]
            if user_id
            else [
                str(r["user_id"])
                for r in self.conn.execute(
                    "SELECT DISTINCT user_id FROM facts WHERE status = 'active'"
                ).fetchall()
            ]
        )
        promoted: List[dict] = []
        for owner in owners:
            for candidate in store.abstraction_candidates(owner):
                source = self.conn.execute(
                    "SELECT fact_id, user_id, session_id, subject, predicate, object, "
                    "qualifiers, confidence, importance, privacy, type, content, "
                    "content_fingerprint FROM facts WHERE fact_id = ? AND status = 'active'",
                    (candidate["representative"],),
                ).fetchone()
                if source is None:
                    continue
                row = dict(source)
                # The rule inherits the strongest evidence among its sources: a
                # rule derived from a strongly-held claim must not be diluted by
                # the promotion itself.
                strongest = self.conn.execute(
                    "SELECT MAX(importance) AS imp, MAX(confidence) AS conf FROM facts "
                    "WHERE fact_id IN (" + ",".join("?" for _ in candidate["fact_ids"])
                    + ")",
                    candidate["fact_ids"],
                ).fetchone()
                if strongest is not None:
                    row["importance"] = max(
                        float(row.get("importance") or 0.0),
                        float(strongest["imp"] or 0.0),
                    )
                    row["confidence"] = max(
                        float(row.get("confidence") or 0.0),
                        float(strongest["conf"] or 0.0),
                    )
                fact_id = await self._write_abstracted_fact(row)
                if fact_id is None:
                    continue
                for source_id in candidate["fact_ids"]:
                    store.link_origin(fact_id, source_id, "abstraction")
                record_event(
                    self.conn,
                    "scope_abstraction_promoted",
                    {
                        "fact_id": fact_id,
                        "source_fact_ids": candidate["fact_ids"],
                        "scope_ids": candidate["scope_ids"],
                    },
                    user_id=owner,
                )
                promoted.append(
                    {
                        "fact_id": fact_id,
                        "fingerprint": candidate["fingerprint"],
                        "source_fact_ids": candidate["fact_ids"],
                        "scope_ids": candidate["scope_ids"],
                    }
                )
        if promoted:
            logger.info("Promoted %d cross-scope claim(s) to global rules", len(promoted))
        return promoted

    async def _write_abstracted_fact(self, source: dict) -> Optional[str]:
        """Write a promoted global rule, indexes and all.

        The rule is stored as ``system_inferred_high`` rather than
        ``user_explicit``: nobody stated it *as a global rule* — three projects
        stating it is what makes it one, and the trust term in the re-rank should
        reflect that provenance.
        """
        fact_id = str(uuid.uuid4())
        text = f"{source['subject']} {source['predicate']} {source['object']}"
        content = source.get("content") or ""
        searchable = (text + " " + content).strip()
        created_at = now_ms()
        blob = await asyncio.to_thread(self.embed_func, searchable or " ")
        with self.conn:
            self.conn.execute(
                "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
                "object, qualifiers, confidence, importance, privacy, source_type, "
                "status, superseded_by, observed_at, created_at, trace_id, version, "
                "type, content, content_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'system_inferred_high', "
                "'active', NULL, ?, ?, NULL, 1, ?, ?, ?)",
                (
                    fact_id,
                    source["user_id"],
                    source.get("session_id") or "s_default",
                    source["subject"],
                    source["predicate"],
                    source["object"],
                    source.get("qualifiers"),
                    float(source.get("confidence") or 0.5),
                    float(source.get("importance") or 0.5),
                    str(source.get("privacy") or "private"),
                    created_at,
                    created_at,
                    source.get("type") or "semantic",
                    content or None,
                    source.get("content_fingerprint"),
                ),
            )
            self.conn.execute(
                "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
                (fact_id, " ".join(segment_text(searchable))),
            )
            self.conn.execute(
                "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                (fact_id, blob),
            )
            self.conn.execute(
                "INSERT INTO fact_scope(fact_id, scope_id, priority) VALUES (?, ?, 0) "
                "ON CONFLICT(fact_id, scope_id) DO NOTHING",
                (fact_id, GLOBAL_SCOPE_ID),
            )
        return fact_id

    def _prune_table(
        self,
        table: str,
        retention_days: int,
        *,
        statuses: Optional[tuple],
        guard: Optional[str],
    ) -> int:
        """Delete bookkeeping rows older than their retention.

        Args:
            table: Table name (from a fixed internal set, never user input).
            retention_days: Age beyond which a finished row may go. ``<=0``
                keeps rows forever.
            statuses: Terminal statuses eligible for collection; ``None`` means
                the table has no status column and age alone decides.
            guard: Extra predicate ensuring only *finished* rows are eligible.

        Returns:
            Number of rows deleted.
        """
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = now_ms() - int(retention_days) * 86_400_000
        clauses = ["created_at < ?"]
        args: list = [cutoff]
        if statuses:
            clauses.append(
                "status IN (" + ",".join("?" for _ in statuses) + ")"
            )
            args.extend(statuses)
        if guard:
            clauses.append(guard)
        cursor = self.conn.execute(
            f"DELETE FROM {table} WHERE " + " AND ".join(clauses), tuple(args)
        )
        self.conn.commit()
        return max(0, cursor.rowcount)

    async def repair_index(self) -> dict:
        """Re-derive index entries for facts that are missing them.

        Facts are meant to be written with their FTS and vector entries in one
        transaction; this is the safety net for everything that can still go
        wrong (an interrupted process, a hand-edited database, a row written
        before this invariant existed). Re-deriving is cheap and idempotent, and
        it is what turns "silently missing from semantic search" back into
        "searchable".

        Returns:
            ``{"vector": n, "fts": n, "dropped_vector": n, "dropped_fts": n}``.
        """
        orphans = index_orphans(self.conn)
        fixed = {"vector": 0, "fts": 0, "dropped_vector": 0, "dropped_fts": 0}

        for fact_id in dict.fromkeys(
            orphans["missing_vector"] + orphans["missing_fts"]
        ):
            row = self.conn.execute(
                "SELECT subject, predicate, object, content FROM facts "
                "WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                continue
            searchable = (
                f"{row['subject']} {row['predicate']} {row['object']} "
                f"{(row['content'] or '')}"
            ).strip()
            blob = await asyncio.to_thread(self.embed_func, searchable or " ")
            with self.conn:
                if fact_id in orphans["missing_fts"]:
                    self.conn.execute(
                        "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
                        (fact_id, " ".join(segment_text(searchable))),
                    )
                    fixed["fts"] += 1
                if fact_id in orphans["missing_vector"]:
                    self.conn.execute(
                        "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                        (fact_id, blob),
                    )
                    fixed["vector"] += 1

        with self.conn:
            for fact_id in orphans["orphan_vector"]:
                self.conn.execute(
                    "DELETE FROM facts_vec WHERE fact_id = ?", (fact_id,)
                )
                fixed["dropped_vector"] += 1
            for fact_id in orphans["orphan_fts"]:
                self.conn.execute(
                    "DELETE FROM facts_fts WHERE fact_id = ?", (fact_id,)
                )
                fixed["dropped_fts"] += 1

        if any(fixed.values()):
            record_event(self.conn, "index_repaired", fixed)
            logger.warning("Index repair: %s", fixed)
        return fixed

    def enforce_capacity(self, user_id: Optional[str] = None) -> List[dict]:
        """Archive the least valuable unprotected facts when over capacity.

        The cap is a cap on the *working set*, not on the store: an archived
        fact keeps its row, its content and its provenance, and can be brought
        back with ``unarchive``. Which facts go first is the whole policy, so it
        is spelled out:

        - **Protected** — never archived whatever their score: facts young
          enough that no reuse could have been observed yet
          (``archive_protect_days``), facts with any reinforcement evidence
          (someone has used them), durable knowledge types (decision rule,
          lesson, SOP — the material whose value is least time-dependent).
        - **Order** — everything else is archived oldest-and-least-important
          first, by its *effective* importance (base rank, plus the decayed
          reuse bonus), so a fact that was once heavily used but has not been
          touched for months is archived before a modest one that is live.

        Args:
            user_id: Restrict to one user; ``None`` applies to every user.

        Returns:
            One entry per archived fact, in archival order.
        """
        cap = int(self.config.max_active_facts or 0)
        if cap <= 0:
            return []

        scope = "AND f.user_id = ? " if user_id else ""
        args: list = [user_id] if user_id else []
        rows = self.conn.execute(
            "SELECT f.fact_id, f.user_id, f.predicate, f.type, f.importance, "
            "f.created_at, f.reinforce_count, f.last_used_at "
            "FROM facts f WHERE f.status = 'active' " + scope +
            "ORDER BY f.created_at DESC",
            tuple(args),
        ).fetchall()

        protect_ms = int(self.config.archive_protect_days) * 86_400_000
        cutoff = now_ms() - protect_ms
        durable = {"decision_rule", "lesson", "sop"}

        by_user: dict = {}
        for row in rows:
            by_user.setdefault(row["user_id"], []).append(row)

        archived: List[dict] = []
        for owner, owner_rows in by_user.items():
            overflow = len(owner_rows) - cap
            if overflow <= 0:
                continue
            candidates = []
            for row in owner_rows:
                if int(row["created_at"]) >= cutoff:
                    continue  # too young to judge
                if float(row["reinforce_count"] or 0.0) > 0.0:
                    continue  # there is evidence of reuse
                if (row["type"] or "semantic") in durable:
                    continue
                candidates.append(
                    (
                        _effective_rank(row, self.curve),
                        int(row["created_at"]),
                        row["fact_id"],
                        row["predicate"],
                        row["type"],
                    )
                )
            candidates.sort()
            for _rank, _created, fact_id, predicate, memory_type in candidates[
                :overflow
            ]:
                self.conn.execute(
                    "UPDATE facts SET status = ?, archived_at = ? WHERE fact_id = ?",
                    (FACT_STATUS_ARCHIVED, now_ms(), fact_id),
                )
                archived.append(
                    {
                        "fact_id": fact_id,
                        "user_id": owner,
                        "predicate": predicate,
                        "type": memory_type,
                    }
                )
        if archived:
            self.conn.commit()
            record_event(
                self.conn,
                "facts_archived",
                {"cap": cap, "archived": archived},
                user_id=user_id or "",
            )
            logger.warning(
                "Capacity pass archived %d fact(s) over cap %d", len(archived), cap
            )
        return archived


def _evidence(candidate: FactCandidate) -> float:
    """Return a candidate's evidence weight (confidence-led)."""
    from .conflict import evidence_weight

    return evidence_weight(candidate.confidence, candidate.importance)


def _effective_rank(
    row: sqlite3.Row, curve: Optional[ReinforceCurve] = None
) -> float:
    """Return a fact's current importance rank for the archive ordering.

    Stored importance is only a signal when the extractor supplied one; the
    neutral default means "unknown" and defers to the fact's type rank — the
    same rule the derived views use, so a fact is archived for the same reason
    it would be ranked low.

    Args:
        row: A ``facts`` row carrying ``importance``/``type`` and the
            reinforcement state columns.
        curve: The curve to evaluate strength under; defaults to the shipped
            constants.

    Returns:
        The fact's current effective importance, in ``[0, 1]``.
    """
    stated = float(row["importance"] or 0.0)
    base = (
        default_importance(row["type"] or "semantic")
        if abs(stated - NEUTRAL_SCORE) <= 1e-9
        else stated
    )
    count = adjust(float(row["reinforce_count"] or 0.0), row["last_used_at"], curve=curve)
    return effective_importance(base, count, curve)
