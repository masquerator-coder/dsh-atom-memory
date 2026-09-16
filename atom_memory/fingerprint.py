"""Content identity: what counts as *the same memory* seen twice.

Deduplication in this store runs at two levels, and they answer different
questions:

* **Utterance level** — "have I already processed this exact message?" The
  capture path can legitimately see one message more than once (the per-message
  hook fires, the nudge sweep re-scans, the model calls ``memory_add`` on the
  same text), and processing it twice would store the same claim twice. The
  candidate's ``idempotency_key`` carries this answer into the store's existing
  suppression machinery.
* **Claim level** — "is this the same fact I already hold?" Restating a stored
  claim is the cleanest reuse evidence the library can observe, so an exact
  repeat should *reinforce* the existing fact rather than add a row next to it.

Both are answered by a fingerprint of the **canonical content**, not by the raw
bytes: whitespace and case are normalised, the type and owner are part of the
identity, and a knowledge fact is identified by its body rather than by the
title derived from it. The fingerprint is a plain SHA-1 hex string: it is an
identity, not a security boundary, and it has to stay stable across processes
and versions (a hash of a *normalised* string, never of a Python object).

Deliberately **not** part of the identity: ``created_at``, ``importance``,
``confidence``, ``session_id``, and the reinforcement state. Those describe the
observation, not the claim — folding them in would make every restatement a new
memory, which is the behaviour this module exists to prevent.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Optional

#: Knowledge types are identified by their body when one is present: the body IS
#: the memory, and two rows whose bodies are identical are the same knowledge no
#: matter how their titles were rendered.
BODY_IDENTIFIED_TYPES = frozenset({"lesson", "sop", "few_shot"})


def canonical(value: Optional[str]) -> str:
    """Normalise one component of a content identity.

    Lowercases, collapses every whitespace run to a single space and trims. CJK
    text is unaffected by the case step; for Latin text it is what stops
    "Coffee" and "coffee" from being two memories.

    Args:
        value: A raw component (``None`` becomes an empty string).

    Returns:
        The canonical form of the component.
    """
    text = unicodedata.normalize("NFC", str(value or ""))
    return " ".join(text.split()).casefold()


def content_fingerprint(
    *,
    user_id: str,
    type: str,
    subject: str,
    predicate: str,
    object: Optional[str] = None,
    content: Optional[str] = None,
    negated: bool = False,
) -> str:
    """Fingerprint the *claim* a fact carries.

    The owner is part of the fingerprint on purpose: the store's candidate table
    has a globally UNIQUE ``idempotency_key``, so an owner-blind key would make
    two users saying the same thing collide.

    ``negated`` is part of the identity too, and it has to be: "likes coffee"
    and "does not like coffee" share subject, predicate and object while being
    opposite claims. Without the flag the deduplication pass would silently
    swallow a correction as a repeat — the exact class of loss this module
    exists to prevent.

    Args:
        user_id: Owner of the memory.
        type: Fact type (``semantic`` / ``episodic`` / ...).
        subject: Claim subject.
        predicate: Claim predicate.
        object: Claim object, when the fact has one.
        content: Long-form body, when the fact has one.
        negated: Whether the claim is negated (``qualifiers``-derived).

    Returns:
        A 32-character hex digest identifying the claim's content.
    """
    fact_type = canonical(type)
    body = canonical(content)
    polarity = "neg" if negated else "pos"
    if fact_type in BODY_IDENTIFIED_TYPES and body:
        # A knowledge fact is its body, and *only* its body. The object field for
        # these types is a derived label — the summary renderer titles them from
        # the body's first line — so two captures of the same procedure often
        # carry different titles for identical content. Treating the title as
        # identity would make those two memories, which is exactly the duplicate
        # this module exists to prevent; the body is what the user wrote and what
        # recall returns, so it decides.
        payload = "\x1f".join(
            (canonical(user_id), fact_type, canonical(subject), canonical(predicate),
             body, polarity)
        )
    else:
        payload = "\x1f".join(
            (canonical(user_id), fact_type, canonical(subject), canonical(predicate),
             canonical(object), polarity)
        )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:32]
