"""Public ``AtomMem`` API — the in-process memory front door.

Exposes the full write/read/mutation surface:

    lifecycle  — start() / stop()
    write      — add(user, session, text)
    read       — recall(), summary(), user_md()
    mutate     — replace(), forget(), reinforce()
    metrics    — stats()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Optional

from .backup import export_memory, import_memory, validate_backup
from .config import MemConfig
from .db import index_orphans, now_ms, open_db, record_event
from .embedder import Embedder
from .profile import (
    SOURCE_USER,
    ProfileLimitExceeded,
    delete_profile_row,
    known_profile_keys,
    list_profile_rows,
    profile_md,
    profile_row_count,
    suggestible_profile_entries,
    write_profile_rows,
)
from .reinforce import (
    KIND_USER_CONFIRMED,
    ReinforceCurve,
    adjust,
    effective_importance_at,
    record_reinforcement,
)
from .retriever import Retriever, estimate_tokens, segment_text, truncate_to_tokens
from .sanitize import clean_body, clean_body_meta, clean_field, clean_field_meta, truncation_records
from .summary import generate_summary
from .validator import is_multi_valued
from .worker import Worker

logger = logging.getLogger(__name__)

# Candidate statuses that mean "the unit of work is finished". Used by the
# write acknowledgement, which waits for one of these.
_TERMINAL_CANDIDATE_STATUS = ("applied", "skipped", "error")


def _decode_outcome(raw: Optional[str]) -> dict:
    """Decode a candidate's stored write outcome (never raises)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class AtomMem:
    """In-process long-term memory for DeepSeek Harness."""

    def __init__(self, config: MemConfig) -> None:
        """Initialise a memory instance.

        Args:
            config: Library configuration.
        """
        self.config = config
        self.db: Optional[sqlite3.Connection] = None
        self.embedder: Optional[Embedder] = None
        self.retriever: Optional[Retriever] = None
        self._worker: Optional[Worker] = None
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Open the database, load the embedder and start the worker.

        Idempotent: calling ``start`` more than once has no further effect.
        """
        if self._started:
            return
        self.db = open_db(self.config)
        self.embedder = Embedder(
            model_name=self.config.embedding_model,
            dim=self.config.embedding_dim,
        )
        # The configured reuse-and-decay curve, used by every strength read on
        # this object (the stored columns are state; the curve turns them into a
        # current score, so it must be the same curve the worker wrote under).
        self.curve = ReinforceCurve.from_config(self.config)
        self.retriever = Retriever(
            conn=self.db,
            embed_one=self.embedder.embed_one,
            top_k_default=10,
            config=self.config,
        )
        self._worker = Worker(
            conn=self.db,
            embed_func=self.embedder.embed_one,
            poll_interval_sec=self.config.worker_poll_interval_sec,
            max_retries=self.config.max_retries,
            llm_extractor=self.config.llm_extractor,
            privacy_filter=self.config.privacy_filter,
            config=self.config,
        )
        self._worker.start()
        self._started = True
        logger.info("AtomMem started (db=%s)", self.config.resolved_db_path())

    async def stop(self) -> None:
        """Stop the worker and close the database.

        Idempotent: calling ``stop`` after the memory is already stopped is a
        no-op.
        """
        if not self._started:
            return
        if self._worker is not None:
            await self._worker.stop()
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:  # pragma: no cover - best-effort close
                pass
            self.db = None
        self._started = False
        self._worker = None
        logger.info("AtomMem stopped")

    # -- write pipeline ----------------------------------------------------------

    async def add(
        self,
        user_id: str,
        session_id: str,
        text: str,
        turn_id: int = 0,
        wait_ms: Optional[int] = None,
    ) -> dict:
        """Enqueue a user utterance to be extracted into atomic facts.

        The utterance is recorded immediately as a pending candidate and an
        ``extract`` task is queued; the worker persists extracted facts
        asynchronously.

        The return value is the **enqueue receipt** unless a wait is asked for:
        ``wait_ms`` (or the configured
        :attr:`~atom_memory.config.MemConfig.write_ack_timeout_ms`) makes this
        method wait for its own candidate to finish and report what actually
        happened — written, superseded, refused-and-why. Without it a caller
        cannot tell "stored" from "refused", which is exactly how a rejected
        write used to look like a successful one.

        Args:
            user_id: The user who produced the utterance (isolation scope).
            session_id: The session the utterance belongs to.
            text: The raw utterance text (cleaned before it is stored or
                extracted: the provenance copy must not carry invisible
                characters either).
            turn_id: Optional zero-based turn number.
            wait_ms: How long to wait for the write outcome. ``None`` uses the
                configured default (``0`` = do not wait).

        Returns:
            A dict ``{"candidate_id", "status", "trace_id"}``, plus
            ``"outcome"`` when the status is terminal: ``{"written",
            "superseded", "rejected", "reinforced"}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        candidate_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        created_at = now_ms()
        cleaned = clean_body_meta(text, self.config.max_content_chars)
        text = cleaned.text
        truncated = truncation_records([cleaned.record("content")])

        with self.db:
            self.db.execute(
                "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
                "turn_id, raw_text, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (candidate_id, user_id, session_id, turn_id, text, created_at),
            )
            self.db.execute(
                "INSERT INTO task_queue(task_id, task_type, payload, status, "
                "priority, retry_count, max_retries, created_at) "
                "VALUES (?, 'extract', ?, 'pending', 5, 0, ?, ?)",
                (
                    str(uuid.uuid4()),
                    json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "user_id": user_id,
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "raw_text": text,
                        }
                    ),
                    self.config.max_retries,
                    created_at,
                ),
            )

        return await self._write_receipt(candidate_id, trace_id, wait_ms, truncated)

    async def _write_receipt(
        self,
        candidate_id: str,
        trace_id: str,
        wait_ms: Optional[int],
        truncated: Optional[list] = None,
    ) -> dict:
        """Return the enqueue receipt, waiting for the outcome when asked to.

        Args:
            candidate_id: The candidate this call produced.
            trace_id: Trace id to echo back.
            wait_ms: Wait budget; ``None`` uses the configured default.
            truncated: Fields this call had to shorten before enqueueing. Carried
                on the receipt whichever way the wait goes out, because the text
                was already shortened by the time this method runs: the loss is a
                fact about the write, not about what the store later decides.

        Returns:
            ``{"candidate_id", "status", "trace_id"}`` plus ``"outcome"``,
            ``"reject_kind"`` and ``"reject_reason"`` once the candidate has
            reached a terminal state.
        """
        receipt = {
            "candidate_id": candidate_id,
            "status": "pending",
            "trace_id": trace_id,
        }
        if truncated:
            receipt["outcome"] = {"truncated": list(truncated)}
        timeout_ms = (
            self.config.write_ack_timeout_ms if wait_ms is None else int(wait_ms)
        )
        if timeout_ms <= 0 or self.db is None:
            return receipt

        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            row = self.db.execute(
                "SELECT status, reject_kind, reject_reason, result_fact_ids "
                "FROM fact_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is not None and row["status"] in _TERMINAL_CANDIDATE_STATUS:
                receipt["status"] = row["status"]
                outcome = _decode_outcome(row["result_fact_ids"])
                if truncated:
                    outcome["truncated"] = list(outcome.get("truncated") or []) + list(truncated)
                receipt["outcome"] = outcome
                if row["reject_kind"]:
                    receipt["reject_kind"] = row["reject_kind"]
                if row["reject_reason"]:
                    receipt["reject_reason"] = row["reject_reason"]
                return receipt
            if time.monotonic() >= deadline:
                return receipt
            await asyncio.sleep(0.02)

    def candidate_outcome(self, candidate_id: str) -> Optional[dict]:
        """Return one candidate's terminal state and outcome, or ``None``.

        This is the pull-side of the write acknowledgement: a caller that did
        not wait can still find out what happened to the write it enqueued.

        Args:
            candidate_id: The candidate row to read.

        Returns:
            ``{"status", "outcome", "reject_kind", "reject_reason",
            "finished_at"}`` or ``None`` when the candidate is unknown or still
            pending.
        """
        if self.db is None:
            return None
        row = self.db.execute(
            "SELECT status, reject_kind, reject_reason, result_fact_ids, "
            "finished_at FROM fact_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None or row["status"] not in _TERMINAL_CANDIDATE_STATUS:
            return None
        return {
            "status": row["status"],
            "outcome": _decode_outcome(row["result_fact_ids"]),
            "reject_kind": row["reject_kind"],
            "reject_reason": row["reject_reason"],
            "finished_at": row["finished_at"],
        }

    def recent_outcomes(self, user_id: str, limit: int = 5) -> list:
        """Return the user's most recent finished writes, newest first.

        A memory system that refuses a write owes the writer an answer; this is
        where that answer lives when nobody was waiting at the time (a capture
        hook, a tool call that timed out).

        Args:
            user_id: The user whose writes to report.
            limit: Maximum number of entries.

        Returns:
            A list of ``{"candidate_id", "status", "reject_kind",
            "reject_reason", "outcome", "finished_at"}``.
        """
        if self.db is None:
            return []
        limit = max(1, min(int(limit), 50))
        rows = self.db.execute(
            "SELECT candidate_id, status, reject_kind, reject_reason, "
            "result_fact_ids, finished_at FROM fact_candidates "
            "WHERE user_id = ? AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [
            {
                "candidate_id": r["candidate_id"],
                "status": r["status"],
                "reject_kind": r["reject_kind"],
                "reject_reason": r["reject_reason"],
                "outcome": _decode_outcome(r["result_fact_ids"]),
                "finished_at": r["finished_at"],
            }
            for r in rows
        ]

    # -- recall pipeline ---------------------------------------------------------

    async def recall(
        self,
        user_id: str,
        query: str,
        token_budget: int = 2000,
        top_k: int = 10,
    ) -> dict:
        """Retrieve the user's most relevant active facts for a query.

        Runs FTS + vector retrieval, RRF fusion and re-ranking, then trims the
        result to ``token_budget``. A single fact larger than
        ``max_fact_tokens`` is returned with a shortened body and
        ``truncated: true``; :meth:`get_fact` returns its full text.

        Args:
            user_id: The user whose memory is searched.
            query: The natural-language query.
            token_budget: Upper bound on the estimated tokens returned.
            top_k: Maximum number of facts to consider before trimming.

        Returns:
            A dict with keys ``facts``, ``pending``, ``conflicts``,
            ``token_count`` and ``trace_id``.
        """
        if self.db is None or self.retriever is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        ranked = await self.retriever.search(user_id, query, top_k=top_k)
        facts: list = []
        used = 0
        ceiling = int(self.config.max_fact_tokens or 0)
        for fact in ranked:
            line = f"{fact['subject']}{fact['predicate']}{fact['object']}"
            body = fact.get("content") or ""
            # Budget the knowledge body too: it is delivered to the model (the
            # recall render prints it), so counting only the SPO line let a
            # handful of long SOPs blow far past the requested budget.
            t = estimate_tokens(line) + estimate_tokens(body)
            if used + t > token_budget and facts:
                break
            # A single fact can be larger than the whole budget, and the first
            # match is always kept so a tiny budget cannot return nothing. The
            # per-fact ceiling is what stops that from turning into an unbounded
            # overshoot (measured at ~60x before): the body is shortened to fit
            # and flagged, and the full text stays reachable through `get_fact`.
            truncated = False
            if ceiling > 0 and t > ceiling:
                body = truncate_to_tokens(
                    body, max(ceiling - estimate_tokens(line), 1)
                )
                t = estimate_tokens(line) + estimate_tokens(body)
                truncated = True
            facts.append(
                {
                    "fact_id": fact["fact_id"],
                    "subject": fact["subject"],
                    "predicate": fact["predicate"],
                    "object": fact["object"],
                    "type": fact.get("type", "semantic"),
                    "content": body or fact.get("content"),
                    "truncated": truncated,
                    "confidence": fact["confidence"],
                    "importance": fact["importance"],
                    "effective_importance": fact.get(
                        "effective_importance", fact["importance"]
                    ),
                    # The decayed reuse count behind that score — never the raw
                    # snapshot column, which was taken at last_used_at.
                    "strength": fact.get("strength", 0.0),
                    "reinforce_count": float(fact.get("reinforce_count") or 0.0),
                    "last_used_at": fact.get("last_used_at"),
                    "final_score": fact["final_score"],
                    "status": fact["status"],
                }
            )
            used += t

        return {
            "facts": facts,
            "conflicts": self._load_conflicts(user_id),
            "degraded": list(getattr(self.retriever, "last_degraded", []) or []),
            "token_count": used,
            "trace_id": str(uuid.uuid4()),
        }

    def _load_conflicts(self, user_id: str) -> list:
        """Report a user's *active* contradictory fact pairs, best-effort.

        A fact is in conflict when it and another active fact of the same user
        share a (subject, predicate) that is *single-valued* (see
        :func:`~atom_memory.validator.is_multi_valued`) yet hold different
        objects — exactly the condition the write-path validator rejects a new
        candidate for (:func:`~atom_memory.validator._check_conflict`). These
        normally arrive only through a legacy/imported row, since the write path
        blocks them; surfacing them here lets the model/user see that memory
        holds two values where it should hold one.

        Returns:
            A list of ``{"left": fact_id, "right": fact_id, "subject",
            "predicate", "object_left", "object_right"}`` — one entry per
            conflicting *pair*, each fact_id appearing on the left or the right
            (never both directions). Empty when memory holds no such pair.
        """
        if self.db is None:
            return []
        rows = self.db.execute(
            "SELECT fact_id, subject, predicate, object, type FROM facts "
            "WHERE user_id = ? AND status = 'active' ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        # Group active facts by their single-valued (subject, predicate) key.
        groups: dict = {}
        for r in rows:
            memory_type = r["type"] or "semantic"
            if is_multi_valued(str(r["predicate"]), memory_type):
                continue
            groups.setdefault((r["subject"], r["predicate"]), []).append(r)

        conflicts: list = []
        for (subject, predicate), members in groups.items():
            # Distinct objects within one single-valued key -> contradiction.
            distinct: dict = {}
            for r in members:
                obj = r["object"]
                distinct.setdefault(obj, r["fact_id"])
            if len(distinct) < 2:
                continue
            ordered_ids = list(distinct.values())
            for i in range(len(ordered_ids)):
                for j in range(i + 1, len(ordered_ids)):
                    conflicts.append(
                        {
                            "left": ordered_ids[i],
                            "right": ordered_ids[j],
                            "subject": subject,
                            "predicate": predicate,
                            "object_left": list(distinct.keys())[i],
                            "object_right": list(distinct.keys())[j],
                        }
                    )
        return conflicts

    def get_fact(self, user_id: str, fact_id: str) -> dict:
        """Return one fact in full, including any knowledge body.

        ``recall`` shortens a body that exceeds its per-fact ceiling and flags it
        as ``truncated``, so there has to be a way to read the rest. This is that
        way: a plain lookup by id, with no retrieval and no scoring, which is what
        makes shortening a recall result safe rather than lossy.

        Args:
            user_id: Owner of the fact (isolation scope).
            fact_id: The fact to read.

        Returns:
            The fact's stored fields, with its body in full.

        Raises:
            ValueError: When no fact with that id belongs to the user.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        row = self.db.execute(
            "SELECT fact_id, subject, predicate, object, type, content, status, "
            "importance, confidence, created_at, reinforce_count, last_used_at, "
            "superseded_by FROM facts WHERE user_id = ? AND fact_id = ?",
            (user_id, fact_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"no fact {fact_id} for user {user_id}")
        return {
            "fact_id": row["fact_id"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object"],
            "type": row["type"] or "semantic",
            "content": row["content"],
            "status": row["status"],
            "importance": row["importance"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
            "reinforce_count": float(row["reinforce_count"] or 0.0),
            "last_used_at": row["last_used_at"],
            "superseded_by": row["superseded_by"],
        }

    async def summary(
        self,
        user_id: str,
        max_tokens: int = 1500,
        detail: bool = True,
    ) -> str:
        """Render the user's ``summary`` derived view.

        Args:
            user_id: The user whose memory is rendered.
            max_tokens: Estimated token cap for the rendered text, footer
                included.
            detail: ``True`` (the default) lists every active fact with its
                ``fact_id``. ``False`` renders the compact, type-grouped digest
                the dsh host freezes into the session system prompt — no
                ``fact_id`` (the UUIDs cost more tokens than they carry
                information for the model) and priority-ordered rather than
                recency-ordered.

        Returns:
            A markdown string.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        return generate_summary(self.db, user_id, max_tokens, detail)

    async def user_md(self, user_id: str, max_tokens: int = 800) -> str:
        """Render the user's profile as markdown.

        A plain render of the profile table — it is *not* re-derived from the
        facts first (it used to be, which is what silently undid the panel's
        deletions). Entries reach the table only through the user.

        Args:
            user_id: The user whose profile is rendered.
            max_tokens: Estimated token cap for the body.

        Returns:
            A markdown string describing the user's profile.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        return profile_md(self.db, user_id, max_tokens)

    # -- mutation surface -----------------------------------------------------------

    async def replace(
        self,
        user_id: str,
        fact_id: str,
        new_text: str,
        wait_ms: Optional[int] = None,
    ) -> dict:
        """Replace an active fact with a new statement.

        The old fact is soft-superseded (``status='superseded'`` with
        ``superseded_by`` pointing at the newest replacement fact) by the
        worker asynchronously. Because the caller *names* the target, the
        replacement supersedes it whatever the evidence comparison would say —
        that is the difference between ``replace`` and re-asserting a value
        through :meth:`add`.

        Args:
            user_id: The user who owns the fact.
            fact_id: The active fact to replace.
            new_text: The replacement utterance to extract into facts.
            wait_ms: How long to wait for the write outcome (see :meth:`add`).

        Returns:
            A dict ``{"candidate_id", "status", "trace_id"}``, plus
            ``"outcome"`` once the write finished.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        old = self.db.execute(
            "SELECT session_id FROM facts WHERE user_id = ? AND fact_id = ? "
            "AND status = 'active'",
            (user_id, fact_id),
        ).fetchone()
        if old is None:
            raise ValueError(
                f"no active fact {fact_id} for user {user_id} to replace"
            )

        candidate_id = str(uuid.uuid4())
        created_at = now_ms()
        cleaned = clean_body_meta(new_text, self.config.max_content_chars)
        text = cleaned.text
        truncated = truncation_records([cleaned.record("content")])
        with self.db:
            self.db.execute(
                "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
                "turn_id, raw_text, status, created_at) "
                "VALUES (?, ?, ?, 0, ?, 'pending', ?)",
                (candidate_id, user_id, old["session_id"], text, created_at),
            )
            self.db.execute(
                "INSERT INTO task_queue(task_id, task_type, payload, status, "
                "priority, retry_count, max_retries, created_at) "
                "VALUES (?, 'replace', ?, 'pending', 5, 0, ?, ?)",
                (
                    str(uuid.uuid4()),
                    json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "user_id": user_id,
                            "old_fact_id": fact_id,
                            "new_text": text,
                            "session_id": old["session_id"],
                            "turn_id": 0,
                        }
                    ),
                    self.config.max_retries,
                    created_at,
                ),
            )
        return await self._write_receipt(
            candidate_id, str(uuid.uuid4()), wait_ms, truncated
        )

    async def forget(
        self,
        user_id: str,
        fact_id: Optional[str] = None,
        session_id: Optional[str] = None,
        purge: bool = False,
        wait_ms: Optional[int] = None,
    ) -> dict:
        """Forget a fact, or every active fact of a session.

        Forgetting is a **soft retract** by default: the row stays, ``status``
        becomes ``retracted``, and every read path stops seeing it, which keeps
        the provenance of what was removed. ``purge=True`` is the explicit
        exception — it deletes the facts, their index entries and their
        reinforcement log, for a user who wants the content gone rather than
        merely unused.

        Args:
            user_id: The user who owns the memory.
            fact_id: The specific fact to retract.
            session_id: Retract all active facts of this session instead.
            purge: Delete the matched facts physically instead of retracting.
            wait_ms: How long to wait for the write outcome (see :meth:`add`).

        Returns:
            A dict ``{"candidate_id", "status", "trace_id"}``, plus
            ``"outcome"`` once the task finished (``retracted`` / ``purged``
            list the affected ids, or ``rejected`` says nothing matched).
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        if not fact_id and not session_id:
            raise ValueError("forget requires either fact_id or session_id")

        candidate_id = str(uuid.uuid4())
        created_at = now_ms()
        with self.db:
            self.db.execute(
                "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
                "turn_id, raw_text, status, created_at) "
                "VALUES (?, ?, ?, 0, NULL, 'pending', ?)",
                (candidate_id, user_id, session_id or "", created_at),
            )
            self.db.execute(
                "INSERT INTO task_queue(task_id, task_type, payload, status, "
                "priority, retry_count, max_retries, created_at) "
                "VALUES (?, 'forget', ?, 'pending', 5, 0, ?, ?)",
                (
                    str(uuid.uuid4()),
                    json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "user_id": user_id,
                            "fact_id": fact_id,
                            "session_id": session_id,
                            "purge": bool(purge),
                        }
                    ),
                    self.config.max_retries,
                    created_at,
                ),
            )
        return await self._write_receipt(
            candidate_id, str(uuid.uuid4()), wait_ms
        )

    # --- UI-facing edit / backup / restore surface ---------------------------

    def list_facts(
        self,
        user_id: str,
        include_retracted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Paginate a user's facts in descending recency for the settings UI.

        Args:
            user_id: The user whose facts are listed.
            include_retracted: Whether to include soft-deleted rows.
            limit: Maximum rows returned.
            offset: Row offset for paging.

        Returns:
            ``{"facts", "total", "offset", "limit"}`` where each fact carries
            ``fact_id`` / ``subject`` / ``predicate`` / ``object`` / ``type`` /
            ``content`` / ``confidence`` / ``importance`` /
            ``effective_importance`` / ``reinforce_count`` / ``last_used_at`` /
            ``status`` / ``created_at``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        status_clause = "AND status = 'active'" if not include_retracted else ""
        # Decay every fact's reinforcement snapshot to one shared instant, so the
        # page is internally consistent and matches what retrieval would rank.
        at = now_ms()
        total = self.db.execute(
            f"SELECT COUNT(*) AS n FROM facts WHERE user_id = ? {status_clause}",
            (user_id,),
        ).fetchone()["n"]
        rows = self.db.execute(
            f"SELECT fact_id, subject, predicate, object, type, content, "
            f"confidence, importance, status, created_at, reinforce_count, "
            f"last_used_at FROM facts "
            f"WHERE user_id = ? {status_clause} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
        facts = [
            {
                "fact_id": r["fact_id"],
                "subject": r["subject"],
                "predicate": r["predicate"],
                "object": r["object"],
                "type": r["type"] or "semantic",
                "content": r["content"],
                "confidence": r["confidence"],
                "importance": r["importance"],
                "effective_importance": round(
                    effective_importance_at(
                        float(r["importance"] or 0.0),
                        float(r["reinforce_count"] or 0.0),
                        r["last_used_at"],
                        at,
                        curve=self.curve,
                    ),
                    6,
                ),
                "strength": round(
                    adjust(
                        float(r["reinforce_count"] or 0.0),
                        r["last_used_at"],
                        at,
                        curve=self.curve,
                    ),
                    6,
                ),
                "reinforce_count": float(r["reinforce_count"] or 0.0),
                "last_used_at": r["last_used_at"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        return {"facts": facts, "total": total, "offset": offset, "limit": limit}

    async def edit_fact(
        self,
        user_id: str,
        fact_id: str,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        object: Optional[str] = None,
        content: Optional[str] = None,
        type: Optional[str] = None,
    ) -> dict:
        """Directly update an active fact (user-invoked UI edit).

        Fields given as ``None`` are left unchanged. Editing re-embeds the
        fact's searchable text so retrieval and FTS stay consistent. Returns the
        updated fact.

        Args:
            user_id: Owner of the fact.
            fact_id: The active fact to edit.
            subject / predicate / object: SPO triple fields to change.
            content: Knowledge body to set (use a sentinel to clear).
            type: Memory type to set.

        Returns:
            The updated fact dict, or raises ``ValueError`` if the fact is not
            an active row owned by ``user_id``.
        """
        if self.db is None or self.embedder is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        row = self.db.execute(
            "SELECT fact_id FROM facts WHERE user_id = ? AND fact_id = ? "
            "AND status = 'active'",
            (user_id, fact_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"no active fact {fact_id} for user {user_id}")

        # Apply ordered updates directly to the row. Every text field is cleaned
        # exactly as an extracted candidate is: the panel is another write path,
        # so it must not be able to put an invisible character or an unbounded
        # value into the store that ingest would have stripped.
        updates: dict = {}
        truncated: list = []
        if subject is not None:
            cleaned = clean_field_meta(subject, self.config.max_field_chars)
            updates["subject"] = cleaned.text
            truncated.append(cleaned.record("subject"))
        if predicate is not None:
            cleaned = clean_field_meta(predicate, self.config.max_field_chars)
            updates["predicate"] = cleaned.text
            truncated.append(cleaned.record("predicate"))
        if object is not None:
            cleaned = clean_field_meta(object, self.config.max_field_chars)
            updates["object"] = cleaned.text
            truncated.append(cleaned.record("object"))
        if content is not None:
            body = clean_body_meta(content, self.config.max_content_chars)
            truncated.append(body.record("content"))
            updates["content"] = body.text or None
        if type is not None:
            updates["type"] = clean_field(type, 64) or "semantic"

        if updates:
            assignments = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [user_id, fact_id]
            with self.db:
                self.db.execute(
                    f"UPDATE facts SET {assignments} "
                    f"WHERE user_id = ? AND fact_id = ? AND status = 'active'",
                    values,
                )
            await self._resync_fact_vectors(user_id, fact_id)

        # Deliberately does *not* reinforce. An edit can be a reword, a type
        # fix, or a wholesale correction — none of which is evidence that the
        # fact was reused, and one of which ("fix a wrong memory") is evidence
        # against it. Treating every UI write as a confirmation let the settings
        # panel mint the strongest signal in the model (gain 1.0) for free.
        # Callers that genuinely mean "the user confirmed this" call
        # `reinforce(...)` themselves, which is explicit and auditable.
        result = self._fetch_fact(user_id, fact_id)
        shortened = truncation_records(truncated)
        if shortened:
            # The panel is a write path: if it shortened what was typed, it says
            # so instead of showing a value that is not what the user entered.
            result["truncated"] = shortened
        return result

    def reinforce(
        self,
        user_id: str,
        fact_id: str,
        kind: str = KIND_USER_CONFIRMED,
        session_id: str = "s_ui",
    ) -> dict:
        """Record a reuse event for a fact and return its new strength.

        This is the explicit half of the reinforcement loop; the implicit half
        fires automatically when the extractor observes the user re-stating a
        claim that is already stored (see :mod:`~atom_memory.reinforce`).

        Only genuine reuse should be reported here. In particular, do **not**
        call this for a mere retrieval hit: recall feeding back into the score
        is the self-reinforcing loop the design deliberately excludes.

        Args:
            user_id: Owner of the fact.
            fact_id: The active fact to strengthen.
            kind: Evidence kind — one of ``user_confirmed`` (default),
                ``user_restated``, ``applied``, ``retrieved_only``.
            session_id: Session the evidence came from; it scopes the
                idempotency guard, so one fact strengthens at most once per
                session per kind.

        Returns:
            A dict with ``fact_id``, ``kind``, ``reinforce_count`` (the stored
            snapshot), ``strength`` (the same snapshot decayed to now — what the
            ranker actually uses), ``importance`` (base),
            ``effective_importance`` (an alias of ``strength``), ``last_used_at``,
            ``gain`` and ``applied`` (``False`` for a suppressed duplicate).

        Raises:
            RuntimeError: If the memory is not started.
            ValueError: If the fact is not active or not owned by ``user_id``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        base_row = self.db.execute(
            "SELECT importance FROM facts "
            "WHERE user_id = ? AND fact_id = ? AND status = 'active'",
            (user_id, fact_id),
        ).fetchone()
        if base_row is None:
            raise ValueError(f"no active fact {fact_id} for user {user_id}")
        base = float(base_row["importance"] or 0.0)

        result = record_reinforcement(
            self.db, fact_id, user_id, session_id, kind, curve=self.curve
        )
        if result is None:
            # The kind is unregistered, or this session already contributed this
            # kind of evidence. Report the unchanged state.
            row = self.db.execute(
                "SELECT reinforce_count, last_used_at FROM facts "
                "WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            snapshot = float(row["reinforce_count"] or 0.0)
            strength = effective_importance_at(
                base, snapshot, row["last_used_at"], curve=self.curve
            )
            return {
                "fact_id": fact_id,
                "kind": kind,
                "reinforce_count": snapshot,
                "strength": round(strength, 6),
                "importance": base,
                "effective_importance": round(strength, 6),
                "last_used_at": row["last_used_at"],
                "gain": 0.0,
                "applied": False,
            }
        # ``result`` was computed at the event instant, which for the default
        # (now) path is this instant, so ``strong_after`` is already current.
        return {
            "fact_id": fact_id,
            "kind": kind,
            "reinforce_count": result.n,
            "strength": result.strong_after,
            "importance": base,
            "effective_importance": result.strong_after,
            "last_used_at": result.last_used_at,
            "gain": result.gain,
            "applied": result.applied,
        }

    def list_profile(self, user_id: str) -> dict:
        """List a user's profile rows for the settings UI.

        A plain read. The profile is an independent table now, not a projection
        over the facts, so reading it must not rebuild it — reading used to be a
        write, which is exactly what made the panel's deletes come back.

        Args:
            user_id: Owner of the profile.

        Returns:
            ``{"profile": [...], "count": n, "limit": n}``. Each row carries
            ``section`` / ``key`` / ``value`` / ``source`` / ``privacy``, where
            ``source`` is ``user`` (typed by the user) or ``generated``
            (accepted from a suggestion).
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        rows = list_profile_rows(self.db, user_id)
        return {
            "profile": [
                {
                    "section": r["section"],
                    "key": r["key"],
                    "value": r["value"],
                    "source": r["source"],
                    "privacy": r["privacy"],
                }
                for r in rows
            ],
            "count": len(rows),
            "limit": int(self.config.max_profile_rows or 0),
        }

    def profile_candidates(self, user_id: str) -> dict:
        """Return the profile entries the active facts imply, as raw material.

        Nothing is written: these are the candidate entries the dsh-side LLM
        synthesis turns into suggestions for the user to approve. Entries whose
        ``(section, key)`` is already in the profile are excluded here, so a
        generation run cannot offer back what the user already has.

        Args:
            user_id: Owner of the profile.

        Returns:
            ``{"candidates", "existing_keys", "existing", "limit", "remaining"}``.
            ``existing_keys`` carries the profile's own ``[section, key]`` pairs
            so the caller can filter suggestions without a second round trip
            (and without racing a concurrent edit between the two reads).
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        known = known_profile_keys(self.db, user_id)
        candidates = [
            c
            for c in suggestible_profile_entries(self.db, user_id)
            if (c["section"], c["key"]) not in known
        ]
        limit = int(self.config.max_profile_rows or 0)
        used = profile_row_count(self.db, user_id)
        return {
            "candidates": candidates,
            "existing_keys": sorted([section, key] for section, key in known),
            "existing": used,
            "limit": limit,
            # How many more rows the table can take; 0 means "full" (or
            # uncapped, which `limit == 0` distinguishes).
            "remaining": max(0, limit - used) if limit > 0 else 0,
        }

    def write_profile(self, user_id: str, rows: List[dict]) -> dict:
        """Apply a batch of profile edits (upserts + deletions) in one pass.

        This is the settings panel's "save all". The row cap is enforced on the
        whole batch before anything is written, so a rejected batch changes
        nothing.

        Args:
            user_id: Owner of the profile.
            rows: Each ``{"section", "key", "value", "deleted"?}``.

        Returns:
            ``{"ok": True, "written": n, "deleted": n, "count": n, "limit": n}``.

        Raises:
            ValueError: A row is missing its section or key.
            ProfileLimitExceeded: The batch would exceed ``max_profile_rows``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        result = write_profile_rows(
            self.db,
            user_id,
            rows,
            limit=int(self.config.max_profile_rows or 0),
        )
        return {
            "ok": True,
            "written": result["written"],
            "deleted": result["deleted"],
            "count": profile_row_count(self.db, user_id),
            "limit": int(self.config.max_profile_rows or 0),
        }

    def upsert_profile(
        self,
        user_id: str,
        section: str,
        key: str,
        value: str,
    ) -> dict:
        """Add or update one user-profile row (a user edit from the panel).

        Refused when it would add a row past ``max_profile_rows``; updating an
        existing row is always allowed, so a full profile can still be edited.

        Args:
            user_id: Owner of the profile.
            section: Profile section (a label like ``职业`` or ``偏好``).
            key: Key within the section (use ``"value"`` for simple rows).
            value: The stored value.

        Returns:
            ``{"ok": True, "source": "user", "count": n, "limit": n}``.

        Raises:
            ValueError: Section or key is blank.
            ProfileLimitExceeded: The write would exceed the row cap.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        section_clean = clean_field_meta(section, 200)
        key_clean = clean_field_meta(key, 200)
        value_clean = clean_body_meta(value, self.config.max_content_chars)
        section = section_clean.text
        key = key_clean.text
        value = value_clean.text
        if not section or not key:
            raise ValueError("profile section and key are required")

        # Enforce the cap through the same path the batch editor uses, so the
        # two write surfaces can never disagree about what "full" means.
        result = write_profile_rows(
            self.db,
            user_id,
            [{"section": section, "key": key, "value": value}],
            limit=int(self.config.max_profile_rows or 0),
        )
        out = {
            "ok": True,
            "source": SOURCE_USER,
            "written": result["written"],
            "count": profile_row_count(self.db, user_id),
            "limit": int(self.config.max_profile_rows or 0),
        }
        shortened = truncation_records(
            (section_clean.record("section"), key_clean.record("key"), value_clean.record("value"))
        )
        if shortened:
            out["truncated"] = shortened
        return out

    def delete_profile(self, user_id: str, section: str, key: str) -> dict:
        """Delete one user-profile row.

        A permanent delete: nothing re-derives the profile, so the row does not
        come back (see :meth:`list_profile`). The facts that may have suggested
        the row are untouched.

        Args:
            user_id: Owner of the profile.
            section: Profile section.
            key: Key within the section.

        Returns:
            ``{"ok": True, "deleted": n, "count": n, "limit": n}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        removed = delete_profile_row(self.db, user_id, section, key)
        return {
            "ok": True,
            "deleted": 1 if removed else 0,
            "count": profile_row_count(self.db, user_id),
            "limit": int(self.config.max_profile_rows or 0),
        }

    def backup(self, user_id: str) -> dict:
        """Export the user's memory as a portable JSON snapshot.

        Args:
            user_id: Owner of the memory.

        Returns:
            The serializable backup dict (see :mod:`atom_memory.backup`).
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        return export_memory(self.db, user_id)

    async def restore(self, user_id: str, payload: dict) -> dict:
        """Import a backup snapshot, replacing the user's current memory.

        Uses replace semantics (see :mod:`atom_memory.backup`): the user's live
        facts / profile are soft-cleared then the snapshot is written back.

        Args:
            user_id: The user whose memory is replaced.
            payload: A validated backup dict.

        Returns:
            ``{"facts_written", "profile_written"}``.
        """
        if self.db is None or self.embedder is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        payload = validate_backup(payload)
        # import_memory is async and keeps DB writes on this (loop) thread,
        # offloading only the embedding to worker threads.
        result = await import_memory(
            self.db,
            user_id,
            payload,
            self.embedder.embed_one,
        )
        return result

    # --- edit helpers ---------------------------------------------------------

    def _fetch_fact(self, user_id: str, fact_id: str) -> dict:
        row = self.db.execute(
            "SELECT fact_id, subject, predicate, object, type, content, "
            "confidence, importance, status, created_at, reinforce_count, "
            "last_used_at FROM facts "
            "WHERE user_id = ? AND fact_id = ?",
            (user_id, fact_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"no fact {fact_id} for user {user_id}")
        return {
            "fact_id": row["fact_id"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object"],
            "type": row["type"] or "semantic",
            "content": row["content"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "effective_importance": round(
                effective_importance_at(
                    float(row["importance"] or 0.0),
                    float(row["reinforce_count"] or 0.0),
                    row["last_used_at"],
                    curve=self.curve,
                ),
                6,
            ),
            "strength": round(
                adjust(
                    float(row["reinforce_count"] or 0.0),
                    row["last_used_at"],
                    curve=self.curve,
                ),
                6,
            ),
            "reinforce_count": float(row["reinforce_count"] or 0.0),
            "last_used_at": row["last_used_at"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    async def _resync_fact_vectors(self, user_id: str, fact_id: str) -> None:
        """Re-derive FTS + vector entries for one fact after an edit.

        The embedding is CPU-bound model inference, so it runs in a worker
        thread exactly like the library's own write path.
        """
        row = self.db.execute(
            "SELECT subject, predicate, object, content FROM facts "
            "WHERE user_id = ? AND fact_id = ?",
            (user_id, fact_id),
        ).fetchone()
        if row is None:
            return
        searchable = (
            f"{row['subject']} {row['predicate']} {row['object']} "
            f"{(row['content'] or '')}"
        ).strip()
        embed_text = searchable or " "
        blob = await asyncio.to_thread(self.embedder.embed_one, embed_text)
        with self.db:
            self.db.execute("DELETE FROM facts_fts WHERE fact_id = ?", (fact_id,))
            self.db.execute(
                "DELETE FROM facts_vec WHERE fact_id = ?", (fact_id,)
            )
            self.db.execute(
                "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
                (fact_id, " ".join(segment_text(searchable))),
            )
            self.db.execute(
                "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                (fact_id, blob),
            )

    def stats(self, user_id: str) -> dict:
        """Return aggregate counters for a user.

        Args:
            user_id: The user whose counts are reported.

        Returns:
            A dict with ``facts`` (active) and ``pending`` (unprocessed
            candidates).
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        facts = self.db.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
        pending = self.db.execute(
            "SELECT COUNT(*) AS n FROM fact_candidates "
            "WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()
        archived = self.db.execute(
            "SELECT COUNT(*) AS n FROM facts "
            "WHERE user_id = ? AND status = 'archived'",
            (user_id,),
        ).fetchone()

        return {
            "facts": facts["n"],
            "pending": pending["n"],
            "archived": archived["n"],
            "recent": self.recent_outcomes(user_id, limit=5),
        }

    # --- lifecycle surface ----------------------------------------------------

    def unarchive(self, user_id: str, fact_id: str) -> dict:
        """Return an archived fact to the active set.

        The archive tier exists so capacity can be enforced without deleting
        anything; this is the way back, and the reason archiving is safe to do
        automatically.

        Args:
            user_id: Owner of the fact.
            fact_id: The archived fact to restore.

        Returns:
            ``{"ok": True, "fact_id": ..., "restored": n}``.

        Raises:
            ValueError: When the fact is not an archived fact of ``user_id``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        cursor = self.db.execute(
            "UPDATE facts SET status = 'active', archived_at = NULL "
            "WHERE user_id = ? AND fact_id = ? AND status = 'archived'",
            (user_id, fact_id),
        )
        self.db.commit()
        if cursor.rowcount == 0:
            raise ValueError(
                f"no archived fact {fact_id} for user {user_id}"
            )
        record_event(
            self.db, "fact_unarchived", {"fact_id": fact_id}, user_id=user_id
        )
        return {"ok": True, "fact_id": fact_id, "restored": cursor.rowcount}

    def purge(self, user_id: str, fact_ids: Optional[list] = None) -> dict:
        """Physically delete facts and their derived rows.

        The erasure path. Unlike :meth:`forget` — which retracts and keeps the
        provenance — a purge removes the fact row, its FTS entry, its vector and
        (through the foreign key) its reinforcement log, so the content is gone
        from the database rather than merely unused. Pass ``fact_ids=None`` to
        purge every fact of the user, which is the point of an erasure request.

        Args:
            user_id: Owner of the facts.
            fact_ids: Explicit ids to purge, or ``None`` for all of the user's
                facts (active, superseded, retracted **and** archived).

        Returns:
            ``{"ok": True, "purged_facts": n, "purged_ids": [...]}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        if fact_ids is None:
            rows = self.db.execute(
                "SELECT fact_id FROM facts WHERE user_id = ?", (user_id,)
            ).fetchall()
            ids = [r["fact_id"] for r in rows]
        else:
            ids = [
                r["fact_id"]
                for r in self.db.execute(
                    "SELECT fact_id FROM facts WHERE user_id = ? AND fact_id IN "
                    "(" + ",".join("?" for _ in fact_ids) + ")",
                    (user_id, *fact_ids),
                ).fetchall()
            ] if fact_ids else []
        removed = self._worker.purge_facts(ids) if self._worker else 0
        record_event(
            self.db,
            "facts_purged",
            {"count": len(ids), "explicit": fact_ids is not None},
            user_id=user_id,
        )
        return {"ok": True, "purged_facts": removed, "purged_ids": ids}

    def vacuum(self) -> dict:
        """Rebuild the database file, reclaiming space left by purges.

        SQLite does not return freed pages to the filesystem on its own, so a
        store that has had content purged keeps its size until this runs.

        Returns:
            ``{"ok": True, "bytes_before": n, "bytes_after": n}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        path = self.config.resolved_db_path()
        before = _file_size(path)
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.commit()
        return {
            "ok": True,
            "bytes_before": before,
            "bytes_after": _file_size(path),
        }

    async def maintenance(self, user_id: Optional[str] = None) -> dict:
        """Run the retention, index-repair and capacity passes now.

        Exposed so a caller (or a health check) can ask for the housekeeping
        instead of waiting for the idle worker to decide it is due.

        Args:
            user_id: Restrict the capacity pass to one user.

        Returns:
            The maintenance summary (see :meth:`worker.Worker.maintenance`).
        """
        if self._worker is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        return await self._worker.maintenance(user_id)

    def index_health(self) -> dict:
        """Report whether ``facts`` and its two indexes agree.

        Returns:
            ``{"ok": bool, "orphans": {...}, "counts": {...}}`` — ``ok`` is
            ``False`` as soon as any fact is missing an index entry, so a caller
            can tell "nothing matched" from "the index is broken".
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        orphans = index_orphans(self.db)
        counts = {
            name: self.db.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()["n"]
            for name, table in (
                ("active_facts", "facts WHERE status = 'active'"),
                ("archived_facts", "facts WHERE status = 'archived'"),
                ("fts_rows", "facts_fts"),
                ("vector_rows", "facts_vec"),
            )
        }
        broken = any(orphans[key] for key in ("missing_vector", "missing_fts"))
        return {"ok": not broken, "orphans": orphans, "counts": counts}


def _file_size(path: str) -> int:
    """Return a file's size in bytes, or 0 when it does not exist."""
    try:
        return os.path.getsize(path)
    except OSError:  # pragma: no cover - the file always exists while open
        return 0
