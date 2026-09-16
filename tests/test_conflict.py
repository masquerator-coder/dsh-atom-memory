"""Tests for the conflict-resolution policy (`atom_memory.conflict`).

The policy is pure, so it is tested exhaustively here rather than through the
worker: what a newer claim does to a stored one is *the* decision the memory
system has to be able to explain, and "the newer assertion wins unless the
stored claim is decisively stronger" is only meaningful if the comparison and
the margin behave as documented.
"""

from __future__ import annotations

import pytest

from atom_memory.conflict import (
    ACTION_REJECT,
    ACTION_SUPERSEDE,
    ACTION_WRITE,
    REASON_NEWER_ASSERTION,
    REASON_STRONGER_EVIDENCE,
    evidence_weight,
    resolve_conflict,
)


def _row(fact_id, obj, confidence=0.7, importance=0.6):
    return {
        "fact_id": fact_id,
        "object": obj,
        "confidence": confidence,
        "importance": importance,
        "created_at": 1,
    }


def test_evidence_weight_is_confidence_led():
    assert evidence_weight(1.0, 1.0) == pytest.approx(1.0)
    assert evidence_weight(0.0, 0.0) == 0.0
    # Confidence dominates: the same importance gap moves the weight less.
    assert evidence_weight(0.8, 0.5) > evidence_weight(0.5, 0.8)
    # Out-of-range input is clamped rather than trusted.
    assert evidence_weight(5.0, -1.0) == pytest.approx(0.7)


def test_no_stored_rows_means_plain_write():
    resolution = resolve_conflict(0.7, 0.6, [])
    assert resolution.action == ACTION_WRITE
    assert resolution.supersede_ids == ()


def test_newer_assertion_supersedes_when_evidence_is_comparable():
    resolution = resolve_conflict(0.7, 0.6, [_row("old", "蓝色", 0.7, 0.6)])
    assert resolution.action == ACTION_SUPERSEDE
    assert resolution.reason == REASON_NEWER_ASSERTION
    assert resolution.supersede_ids == ("old",)
    assert resolution.stored_object == "蓝色"


def test_newer_assertion_wins_ties():
    resolution = resolve_conflict(0.9, 0.9, [_row("old", "蓝色", 0.9, 0.9)])
    assert resolution.action == ACTION_SUPERSEDE


def test_decisively_stronger_stored_claim_outvotes_the_new_one():
    resolution = resolve_conflict(
        0.5, 0.5, [_row("strong", "蓝色", 1.0, 1.0)], confidence_margin=0.05
    )
    assert resolution.action == ACTION_REJECT
    assert resolution.reason == REASON_STRONGER_EVIDENCE
    assert resolution.supersede_ids == ("strong",)
    assert "outranks" in resolution.detail


def test_the_margin_is_the_decision_boundary():
    stored = [_row("old", "蓝色", 0.7, 0.7)]  # weight 0.7
    just_inside = resolve_conflict(0.66, 0.7, stored, confidence_margin=0.05)
    assert just_inside.action == ACTION_SUPERSEDE  # 0.672 + 0.05 > 0.7
    just_outside = resolve_conflict(0.6, 0.7, stored, confidence_margin=0.05)
    assert just_outside.action == ACTION_REJECT  # 0.63 + 0.05 < 0.7


def test_the_weakest_stored_claim_decides_when_replacing_a_whole_key():
    """Superseding a key replaces every value under it, so one strong stored
    value is enough to deny the candidate that right."""
    rows = [
        _row("weak", "红色", 0.2, 0.2),
        _row("strong", "蓝色", 1.0, 1.0),
    ]
    assert resolve_conflict(0.7, 0.6, rows, confidence_margin=0.0).action == ACTION_REJECT
    # With only the weak one present the same candidate supersedes it.
    assert resolve_conflict(0.7, 0.6, rows[:1]).action == ACTION_SUPERSEDE


def test_a_zero_margin_makes_any_evidence_advantage_decisive():
    rows = [_row("old", "蓝色", 0.75, 0.75)]
    assert resolve_conflict(0.74, 0.75, rows, confidence_margin=0.0).action == ACTION_REJECT
    assert resolve_conflict(0.75, 0.75, rows, confidence_margin=0.0).action == ACTION_SUPERSEDE
