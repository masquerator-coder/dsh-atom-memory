"""Conflict resolution: what a *new* claim does to an *existing* one.

A memory store that refuses a contradicting claim silently loses the user's
newest statement, and one that always overwrites loses the evidence that the
value ever changed. Neither is a policy — they are the two ways of not having
one. This module is the policy, kept pure so it can be tested exhaustively and
so the write path only has to carry out a decision.

The rules, in the order they are applied:

1. **An independent claim is not a conflict.** Multi-valued predicates
   (preferences, interests), episodic events, and knowledge items (SOP, lesson,
   decision rule, few-shot) may hold many objects under one predicate; a
   different object is a new item. Handled upstream by
   :func:`~atom_memory.validator.is_multi_valued`.

2. **A restatement is not a conflict.** Identical object and identical negation
   is the same claim; it is reported as ``idempotent`` and reinforces the stored
   fact rather than competing with it. Also handled upstream.

3. **Under a single-valued predicate, the newer assertion wins** — that is what
   makes the store answerable to the user, who is the only authority on a value
   that has one slot. If a user says "my usual colour is blue" and later "my
   usual colour is green", the store must not keep answering blue.

4. **…unless the stored claim has clearly stronger evidence.** A contradiction
   from a weaker source (lower confidence) must not overwrite a strong stored
   claim: the store's job is to be *right*, not merely recent. "Clearly" is
   :attr:`~atom_memory.config.MemConfig.conflict_confidence_margin`, so a
   near-tie resolves in favour of the newer assertion while a decisive evidence
   gap does not.

Evidence weight is ``0.7 · confidence + 0.3 · importance``: confidence answers
"how sure are we this was stated", importance answers "how much does it matter".
Only the first is really an argument about *truth*; importance breaks ties, which
is why it carries the smaller weight.
"""

from __future__ import annotations

from dataclasses import dataclass

# Action names returned by :func:`resolve_conflict`.
ACTION_WRITE = "write"        # no conflicting active row: write the candidate
ACTION_SUPERSEDE = "supersede"  # write, and supersede the stored value(s)
ACTION_REJECT = "reject"      # do not write: the stored claim outranks it

# Reason codes recorded on the candidate row and in the audit log.
REASON_NEWER_ASSERTION = "newer_assertion"
REASON_STRONGER_EVIDENCE = "stronger_evidence_stored"
REASON_BATCH_DUPLICATE = "batch_duplicate"

_EVIDENCE_CONFIDENCE_WEIGHT = 0.7
_EVIDENCE_IMPORTANCE_WEIGHT = 0.3


@dataclass(frozen=True)
class ConflictResolution:
    """What to do with a candidate that collides with an active claim.

    Attributes:
        action: One of :data:`ACTION_WRITE` / :data:`ACTION_SUPERSEDE` /
            :data:`ACTION_REJECT`.
        reason: Machine-readable reason code, recorded for audit.
        supersede_ids: The stored fact ids the new fact replaces (empty unless
            ``action`` is :data:`ACTION_SUPERSEDE`).
        stored_object: The object currently stored under the key, when there is
            one.
        detail: Human-readable explanation, for logs and tool output.
    """

    action: str
    reason: str
    supersede_ids: tuple = ()
    stored_object: str = ""
    detail: str = ""


def evidence_weight(confidence: float, importance: float) -> float:
    """Return the evidence weight of a claim.

    Args:
        confidence: 0..1 confidence the claim was actually stated.
        importance: 0..1 how much the claim matters.

    Returns:
        A 0..1 weight; higher means the claim has more standing against a
        competing value.
    """
    conf = min(1.0, max(0.0, float(confidence or 0.0)))
    imp = min(1.0, max(0.0, float(importance or 0.0)))
    return _EVIDENCE_CONFIDENCE_WEIGHT * conf + _EVIDENCE_IMPORTANCE_WEIGHT * imp


def strongest_stored(rows) -> float:
    """Return the *highest* evidence weight among the stored rows being replaced.

    The comparison uses the strongest stored claim, because superseding a key
    means replacing **every** active value under it: if any one of them outranks
    the candidate, the candidate has not earned the right to retire the rest. A
    weakest-claim comparison would let a weak sibling talk the candidate past a
    well-evidenced value, which is exactly backwards.

    Args:
        rows: Iterable of mappings carrying ``confidence`` and ``importance``.

    Returns:
        The maximum evidence weight, or ``0.0`` for an empty iterable.
    """
    weights = [
        evidence_weight(r["confidence"], r["importance"]) for r in rows
    ]
    return max(weights) if weights else 0.0


def resolve_conflict(
    candidate_confidence: float,
    candidate_importance: float,
    stored_rows,
    *,
    confidence_margin: float = 0.05,
) -> ConflictResolution:
    """Decide what a contradicting candidate does to the stored value(s).

    Args:
        candidate_confidence: The new claim's confidence.
        candidate_importance: The new claim's importance.
        stored_rows: The active rows the candidate contradicts (already filtered
            to the same user + subject + single-valued predicate).
        confidence_margin: How much stronger the stored evidence must be before
            it outvotes the new claim.

    Returns:
        A :class:`ConflictResolution`. ``stored_rows`` empty means
        :data:`ACTION_WRITE`; otherwise the newer assertion either supersedes
        the stored value(s) or is rejected for weaker evidence.
    """
    rows = list(stored_rows)
    if not rows:
        return ConflictResolution(action=ACTION_WRITE, reason="no_conflict")

    stored_object = str(rows[0]["object"] if rows else "")
    cand_weight = evidence_weight(candidate_confidence, candidate_importance)
    strongest = strongest_stored(rows)
    ids = tuple(str(r["fact_id"]) for r in rows)

    if cand_weight + confidence_margin < strongest:
        return ConflictResolution(
            action=ACTION_REJECT,
            reason=REASON_STRONGER_EVIDENCE,
            supersede_ids=ids,
            stored_object=stored_object,
            detail=(
                f"stored claim outranks the new one "
                f"(evidence {strongest:.3f} vs {cand_weight:.3f}, "
                f"margin {confidence_margin:.3f})"
            ),
        )

    return ConflictResolution(
        action=ACTION_SUPERSEDE,
        reason=REASON_NEWER_ASSERTION,
        supersede_ids=ids,
        stored_object=stored_object,
        detail=(
            f"newer assertion replaces {len(ids)} stored value(s) "
            f"(evidence {cand_weight:.3f} vs {strongest:.3f})"
        ),
    )
