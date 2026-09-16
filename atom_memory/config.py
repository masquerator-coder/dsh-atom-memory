"""Configuration dataclass for :mod:`atom_memory`.

The ``MemConfig`` dataclass centralises every tunable that the library needs,
from the SQLite file location to the embedding model name and the optional
LLM-based extractor callable.

The knobs are grouped by the *policy* they belong to (retrieval, ingest,
retention, conflict resolution) rather than by the module that reads them,
because each group is one decision a deployer may want to make differently —
and because a knob whose policy is not written down is a knob nobody can tune
safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class MemConfig:
    """Runtime configuration for an :class:`~atom_memory.api.AtomMem` instance.

    Attributes:
        db_path: Filesystem location of the SQLite database file. A leading
            ``~`` is expanded to the current user's home directory. Parent
            directories are created automatically when the database is opened.
        embedding_model: The FastEmbed model name used to produce embeddings.
            Only the local ``BAAI/bge-small-zh-v1.5`` (512-dim) is supported by
            the schema; selecting another model requires a matching
            ``embedding_dim``.
        embedding_dim: Dimensionality of the embedding vectors written to the
            ``facts_vec`` virtual table. Must match the declared
            ``float[...]`` width and the chosen model.
        default_token_budget: Default token budget used by :meth:`recall`
            when the caller does not supply an explicit budget.
        summary_token_limit: Default token cap for the generated
            summary markdown.
        user_md_token_limit: Default token cap for the generated user-profile
            markdown.
        candidate_retention_days: How long a *finished* fact candidate row is
            kept before the maintenance pass may collect it. Unfinished
            (``pending``/``running``) rows and rows newer than this are never
            touched — a candidate is the provenance of the fact it produced.
        task_retention_days: How long a finished (``done``/``dead``) queue row is
            kept. Kept longer than candidates by default: a ``dead`` row is the
            record of a failure that may still need investigating.
        event_retention_days: How long rows in ``events`` (the audit log) are
            kept. Deliberately much longer than the bookkeeping tables, because
            the audit log is what makes a supersede or a rejection explainable
            after the fact.
        max_active_facts: Soft cap on a user's active facts. ``0`` (the default)
            means unlimited. When the cap is exceeded the maintenance pass moves
            the least valuable *unprotected* facts to the ``archived`` status —
            never deletes them — so the working set stays bounded while nothing
            is lost (see :meth:`AtomMem.unarchive`).
        archive_protect_days: Facts created within this many days are never
            archived, whatever their score: a fresh fact has not had time to be
            used, so archiving it would be a decision made without evidence.
        maintenance_interval_sec: How often the idle worker runs the maintenance
            pass (retention sweep, index repair, capacity enforcement). ``0``
            disables the periodic pass; the pass still runs once at start.
        max_retries: Maximum number of times the worker retries a failed task.
        worker_poll_interval_sec: Interval (seconds) at which the worker polls
            the task queue.
        llm_extractor: Optional callable used by the extractor to obtain fact
            candidates. It runs **first**; when it returns one or more usable
            candidates they are authoritative, and rule-based extraction only
            runs as a fallback (LLM threw or returned nothing). If ``None``,
            only rule-based extraction runs.
        privacy_filter: Default privacy tag applied to facts that do not
            declare one.
        max_field_chars: Ingest cap for one SPO field (subject / predicate /
            object). Longer values are truncated at ingest, so no single field
            can dominate a rendered line or a token budget.
        max_content_chars: Ingest cap for a knowledge ``content`` body.
        rrf_k: Reciprocal Rank Fusion constant. Larger values flatten the
            difference between neighbouring ranks.
        w_rrf: Weight of the fused relevance term in the re-rank.
        w_importance: Weight of the (absolute) effective-importance term.
        w_recency: Weight of the (absolute) recency term.
        w_trust: Weight of the trust term (confidence ⊕ source credibility).
        min_relevance: Relevance floor. A candidate whose fused relevance is
            below this is not returned at all, so "memory has nothing relevant"
            is expressible instead of always answering with the least bad row.
        max_vector_distance: Maximum cosine *distance* accepted from the KNN
            index. ``None`` disables the gate. This is the only filter that can
            tell "semantically close" from "merely in the top-k", because a
            rank-based score cannot.
        conflict_confidence_margin: How much stronger an already-stored claim's
            evidence must be before it is allowed to *outvote* a newly asserted
            value under a single-valued predicate, rather than being superseded
            by it. ``0.0`` means the newer assertion always wins ties.
        write_ack_timeout_ms: How long :meth:`AtomMem.add` waits for its own
            candidate to reach a terminal state before returning the enqueue
            receipt. ``0`` (the default) never waits — the library stays purely
            asynchronous; a caller that wants the outcome asks for a wait.
    """

    db_path: str = "~/.atom_memory/memory.db"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    default_token_budget: int = 2000
    summary_token_limit: int = 1500
    user_md_token_limit: int = 800

    # -- retention / lifecycle ------------------------------------------------
    candidate_retention_days: int = 7
    task_retention_days: int = 30
    event_retention_days: int = 180
    max_active_facts: int = 0
    archive_protect_days: int = 14
    maintenance_interval_sec: float = 900.0

    # -- worker ---------------------------------------------------------------
    max_retries: int = 3
    worker_poll_interval_sec: float = 0.5
    llm_extractor: Optional[Callable[..., list]] = field(default=None)
    privacy_filter: str = "private"

    # -- ingest ---------------------------------------------------------------
    max_field_chars: int = 2000
    max_content_chars: int = 20_000

    # -- retrieval ------------------------------------------------------------
    rrf_k: int = 60
    w_rrf: float = 0.4
    w_importance: float = 0.2
    w_recency: float = 0.2
    w_trust: float = 0.2
    min_relevance: float = 0.0
    max_vector_distance: Optional[float] = None

    # -- conflict resolution --------------------------------------------------
    conflict_confidence_margin: float = 0.05

    # -- write acknowledgement ------------------------------------------------
    write_ack_timeout_ms: int = 0

    def resolved_db_path(self) -> str:
        """Return ``db_path`` with ``~`` expanded to the home directory.

        Returns:
            An absolute path string suitable for :func:`os.path.expanduser`
            and friends.
        """
        return str(Path(self.db_path).expanduser())

    def weights_sum(self) -> float:
        """Return the sum of the four re-rank weights.

        Exposed so a deployer can check that a retuned weight set still
        normalises to 1.0 (the re-rank's output is only comparable across
        queries if the weights sum to a fixed total).

        Returns:
            ``w_rrf + w_importance + w_recency + w_trust``.
        """
        return self.w_rrf + self.w_importance + self.w_recency + self.w_trust
