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
        multi_valued_predicates: Extra predicates to treat as multi-valued,
            on top of the built-in set (see the field's own comment).
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
    # Hard cap on the number of rows in `user_profile`. The profile is rendered
    # into the session system prompt (through `user_md`), so every row is a cost
    # paid on every request of every session — an unbounded table is an
    # unbounded recurring prompt cost. Writes that would exceed the cap are
    # refused with the current count and the limit, rather than silently
    # dropping the oldest entry. 0 disables the cap.
    max_profile_rows: int = 50

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

    # -- content identity (see fingerprint.py) --------------------------------
    # Cosmetic reformatting of a stored knowledge body (whitespace, a re-typed
    # title) is caught by the fingerprint alone. This gate additionally merges a
    # body that was *reworded* but is semantically the same memory, by comparing
    # the candidate's embedding against existing facts of the same owner,
    # predicate and type. Keep it tight: a false merge deletes a distinct memory
    # from the working set, while a missed merge only costs a row. 0 disables it.
    dedup_max_distance: float = 0.10
    # Knowledge bodies shorter than this are identified by their fingerprint
    # only: a two-word object is a phrase, and two facts sharing a short phrase
    # are usually two facts.
    dedup_min_body_chars: int = 200

    # -- retrieval ------------------------------------------------------------
    rrf_k: int = 60
    w_rrf: float = 0.4
    w_importance: float = 0.2
    w_recency: float = 0.2
    w_trust: float = 0.2
    min_relevance: float = 0.0
    max_vector_distance: Optional[float] = None
    # Per-fact ceiling for one recall result, in estimated tokens. The recall
    # budget is otherwise a soft bound: the first fact is always kept (so a tiny
    # budget cannot return nothing), which let a single long SOP overshoot
    # `token_budget` by ~60x in measurement. A fact over this ceiling is
    # returned with a truncated body and `truncated: true`, and its full text
    # stays reachable through `get_fact`. 0 disables the ceiling.
    max_fact_tokens: int = 600

    # -- conflict resolution --------------------------------------------------
    conflict_confidence_margin: float = 0.05
    # Predicates to treat as *multi-valued* on top of the built-in set
    # (`validator.MULTI_VALUED_PREDICATES` and its suffix rule): a different
    # object under one of these is an independent claim, not a contradiction.
    #
    # This exists because predicates are open vocabulary — the extractor writes
    # them, and a claim that is one-to-many however it is worded ("拥有项目",
    # "课程大纲编写事项") cannot be enumerated in advance. Without an entry a
    # second value silently retires the first (`newer_assertion`) or, inside one
    # batch, is dropped (`batch_duplicate`); declaring the predicate here is the
    # deployment's fix, with no code change.
    multi_valued_predicates: tuple = ()

    # -- scope awareness (see docs/scopes.md) ---------------------------------
    # Scope is a storage *dimension*, not a semantic property: a fact belongs to
    # a node in a hierarchy (org / client / project / phase / document ...) and
    # recall expands the node's ancestors and descendants instead of searching
    # one global pool. Switched on by default because the pollution it prevents
    # is silent (a project's rule applied to another project) while the cost of
    # it being wrong is visible (fewer results, listed in `degraded`).
    scope_aware: bool = True
    # Ranking weights for the three scope terms. They are *added* to the base
    # four terms only when the query carries a scope context, so a caller that
    # sends none gets byte-identical ranking to the pre-scope library.
    w_scope: float = 0.20
    w_condition: float = 0.15
    w_phase: float = 0.05
    # Confidence thresholds, from the design's resolution table:
    #   >= scope_bind_threshold      bind without asking
    #   >= scope_pending_threshold   bind, but mark the scope unconfirmed
    #   >= scope_degrade_threshold   no scope: global + conditions + queue entry
    #   below                        global, no conditions, no queue entry
    scope_bind_threshold: float = 0.9
    scope_pending_threshold: float = 0.6
    scope_degrade_threshold: float = 0.3
    # Evidence a *new* scope needs. This is the design's 高 (high) band boundary:
    # a git remote, a durable document id or an explicit user tag can bring a
    # scope into existence, while a bare folder path, a package name or a
    # content anchor cannot — creating one from weak evidence is how a store
    # fills up with near-duplicate scopes that each hold half the facts.
    scope_new_threshold: float = 0.8
    # Evidence a new *document* (or email thread) needs, which is deliberately
    # higher than the general threshold. This is a recall fix, not a hygiene one:
    # recall's candidate set is "own scope + ancestors + phases + root", so a fact
    # bound to a document is invisible from every *sibling* document — while a
    # fact bound to a project is visible from all of them. A weak signal that
    # creates a document (folder_path 0.50, doc_title 0.50) therefore silently
    # buries the fact it was attached to: teaching a chapter in one file and
    # asking about it in the next file finds nothing. Only a durable document id
    # (doc_id / folder_id 0.90, email_thread 0.90) may create one; a share_link
    # (0.75), a title or a path may still *bind to* an existing document.
    scope_new_threshold_document: float = 0.85
    # A sub-threshold candidate is queued instead of created, and promoted once
    # it has been seen this many times with consistent evidence.
    scope_promote_after: int = 3
    # How many distinct scopes must independently hold the same claim before it
    # is promoted to a global rule (see ScopeStore.promote_abstractions).
    scope_abstraction_min_scopes: int = 3
    # Whether recall also considers facts bound to *other phases* of the same
    # project (the design's "默认召回所有阶段的 active 事实"). Off restricts
    # recall to the current phase, which is rarely what a user wants.
    scope_all_phases: bool = True

    # -- topic dimension (see docs/Domain 维度落地方案) -------------------------
    # A second, orthogonal axis: scope says *in which context* a fact holds,
    # domain says *about what*. Multi-label and registered rather than
    # free-form, because a tag vocabulary that grows on every write stops being
    # a vocabulary.
    #
    # `off` is the default and means "label everything, filter nothing": phase
    # one of the rollout writes fact_domain and leaves every read path exactly as
    # it is, so the labels can be inspected (and corrected) before they are
    # allowed to influence what is returned. `should` ranks topic matches,
    # `must` filters on them.
    domain_recall: str = "off"
    # Confidence a query's inferred topic needs before `must` may be used on it.
    # Hard-filtering a query whose topic was guessed is how a cross-topic
    # question ("write me a Python script for this lesson") loses the answer.
    domain_must_threshold: float = 0.8
    # Ranking weights for the topic terms, *added* to the base four (and to the
    # scope terms) rather than re-normalising them, so a stored weight set keeps
    # meaning the same thing in every mode.
    domain_should_weight: float = 0.15
    domain_primary_weight: float = 1.0
    domain_secondary_weight: float = 0.7
    # At most this many labels on one fact, and this many proposals honoured per
    # write. Both exist to stop one generic sentence accumulating every topic.
    domain_max_per_fact: int = 5
    domain_max_per_hint: int = 3
    # Tagging mode: `off` labels nothing, `auto` labels silently (the labelled
    # source is reported in the write receipt), `confirm_new` additionally
    # surfaces unregistered proposals in the receipt.
    domain_tagging_mode: str = "auto"
    # `((scope_path_prefix, domain_name), ...)`: the statement that "everything
    # under this project is teaching". This is the rule that keeps `general`
    # from becoming the default bucket.
    scope_domain_map: tuple = ()
    # `((keyword, domain_name), ...)`: the fallback when the session says nothing
    # and the extractor proposed nothing.
    domain_keywords: tuple = ()

    # -- write acknowledgement ------------------------------------------------
    write_ack_timeout_ms: int = 0

    # -- worker leases --------------------------------------------------------
    # A claimed task holds a lease for this long. Reclaim only touches tasks
    # whose lease has expired, so a second consumer on the same database cannot
    # steal work from a live worker. Delivery is at-least-once: a task that
    # legitimately runs longer than its lease may be re-run.
    task_lease_sec: float = 600.0

    # -- reuse-and-decay curve (see docs/reinforcement.md) --------------------
    # Defaults are the shipped constants. They are exposed because "how much can
    # reuse add" and "how fast does strength fade" are policy, not physics, and
    # the reinforcement log makes a retuned curve retro-applicable.
    reinforce_a_max: float = 0.5
    reinforce_n_half: float = 3.0
    reinforce_half_life_days: float = 75.0
    reinforce_cooldown_sec: float = 600.0

    def resolved_db_path(self) -> str:
        """Return ``db_path`` with ``~`` expanded to the home directory.

        Returns:
            An absolute path string suitable for :func:`os.path.expanduser`
            and friends.
        """
        return str(Path(self.db_path).expanduser())

    def weights_sum(self) -> float:
        """Return the sum of the four base re-rank weights.

        Exposed so a deployer can check that a retuned weight set still
        normalises to 1.0 (the re-rank's output is only comparable across
        queries if the weights sum to a fixed total).

        The three scope weights (``w_scope`` / ``w_condition`` / ``w_phase``) are
        deliberately *not* included: they are added only for a query that carries
        a scope context, so a scope-aware store and a scope-blind one still agree
        on the base terms and the total stays comparable in both modes.

        Returns:
            ``w_rrf + w_importance + w_recency + w_trust``.
        """
        return self.w_rrf + self.w_importance + self.w_recency + self.w_trust
