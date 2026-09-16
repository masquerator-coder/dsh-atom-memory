"""Ingest-time sanitisation: the write-side half of the memory-injection guard.

Memory content is *data* that reaches the model inside the system prompt, so the
pipeline has to make sure stored text cannot carry anything a prompt cannot
defend against: invisible/format characters (bidi overrides, zero-width joiners,
BOMs) that hide instructions, control characters, embedded line structure in a
field that is meant to be one line, or an unbounded body.

This module is the **ingest** half. Because every write path funnels through
:func:`~atom_memory.validator.validate` for facts (and through the explicit calls
in ``api.edit_fact`` / ``api.upsert_profile`` / ``backup._write_fact`` for the
paths that bypass it), cleaning here means the store never *contains* a disguised
instruction — which is what makes the egress fence in the dsh host a
belt-and-braces second layer rather than the only one.

Design decisions, deliberately conservative:

- **No NFKC (or any) Unicode normalisation.** Normalising would silently rewrite
  legitimate content (full-width punctuation, ligatures, CJK compatibility
  forms); a memory system must store what the user meant. Only characters that
  are *invisible or structural* are removed.
- **Format characters are removed, except ZWNJ (U+200C) and ZWJ (U+200D).**
  Those two are the only format characters with a load-bearing typographic role
  (emoji sequences, Indic/Persian letter joining). They are also the only two
  that cannot carry a readable instruction into the model, because they carry no
  glyph at all beyond joining their neighbours — so keeping them costs no safety
  and avoids mangling emoji and Persian text.
- **Bidi controls are always removed.** U+202A..U+202E and U+2066..U+2069 are the
  classic "reorder how a line reads" spoofing vector, plus U+200E/U+200F and
  U+061C. A memory has no legitimate need for them.
- **Fields are single-line, bodies may be multi-line.** A subject/predicate/object
  is a headline: any embedded newline is whitespace to collapse. A ``content``
  body keeps its line structure (procedures are lists) but has its control
  characters and invisible characters stripped and its length capped.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Invisible / structural characters that must never survive into the store.
# Built from an explicit list rather than "all of category Cf" so ZWJ/ZWNJ can be
# exempted and so the intent is reviewable.
_BIDI_CONTROLS = "".join(
    chr(cp)
    for cp in (
        0x061C,                              # ARABIC LETTER MARK
        0x200E, 0x200F,                      # LRM / RLM
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # LRE/RLE/PDF/LRO/RLO
        0x2066, 0x2067, 0x2068, 0x2069,      # LRI/RLI/FSI/PDI
    )
)

_INVISIBLE = "".join(
    chr(cp)
    for cp in (
        0x00AD,                              # SOFT HYPHEN
        0x034F,                              # COMBINING GRAPHEME JOINER
        0x115F, 0x1160,                      # HANGUL CHOSEONG/JUNGSEONG FILLER
        0x17B4, 0x17B5,                      # KHMER VOWEL INHERENT (invisible)
        0x180B, 0x180C, 0x180D, 0x180E,      # MONGOLIAN FREE VARIATION SELECTORS
        0x2060, 0x2061, 0x2062, 0x2063, 0x2064,  # WORD JOINER / invisible ops
        0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F,  # deprecated format
        0xFE00, 0xFE01,                      # VARIATION SELECTOR-1/2 (rarely typed)
        0xFEFF,                              # ZERO WIDTH NO-BREAK SPACE / BOM
        0xFFA0,                              # HALFWIDTH HANGUL FILLER
        0xFFF9, 0xFFFA, 0xFFFB, 0xFFFC,      # interlinear / object replacement
        0xE0001,                             # LANGUAGE TAG
        0xE0020,                             # TAG SPACE (start of the tag block)
    )
) + "".join(chr(cp) for cp in range(0xE0020, 0xE0080))  # TAG characters (invisible ASCII smuggling)

# Zero-width joiners are kept (emoji / Indic / Persian), everything else in the
# Cf category that is not already listed is removed as a catch-all.
_FORMAT_KEEP = {0x200C, 0x200D}

_ZERO_WIDTH = "".join(chr(cp) for cp in (0x200B, 0x200C, 0x200D, 0x2060))

# Line / paragraph separators: they can inject structure into a rendered line.
_LINE_SEPARATORS = "\u2028\u2029\v\f\x1c\x1d\x1e\x85"

_WS_RUN = re.compile(r"[ \t\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+")
_NEWLINE_RUN = re.compile(r"\n{3,}")

# Defaults: a headline field is short, a knowledge body is allowed to be long but
# not unbounded. Both are overridable through :class:`~atom_memory.config.MemConfig`.
DEFAULT_MAX_FIELD_CHARS = 2_000
DEFAULT_MAX_CONTENT_CHARS = 20_000


def _is_stripped_format(ch: str) -> bool:
    """Whether a character is a format character we always remove."""
    cp = ord(ch)
    if cp in _FORMAT_KEEP:
        return False
    return unicodedata.category(ch) == "Cf"


def _is_stripped_control(ch: str) -> bool:
    """Whether a character is a control character we always remove."""
    return unicodedata.category(ch) == "Cc"


def strip_invisible(text: str) -> str:
    """Remove invisible, format and control characters from ``text``.

    Newlines survive (they are structural in a body) and tabs become spaces —
    a tab carries no glyph but *does* separate words, so deleting it would glue
    ``职业`` and ``是`` into ``职业是``. Every other control character, every
    bidi control, every zero-width character other than the two joiners, and
    every stray format character is dropped.

    Args:
        text: The raw text.

    Returns:
        The text with invisible characters removed.
    """
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append(ch)
            continue
        if ch == "\t":
            out.append(" ")
            continue
        if ch in _BIDI_CONTROLS or ch in _INVISIBLE or ch in _LINE_SEPARATORS:
            continue
        if _is_stripped_control(ch) or _is_stripped_format(ch):
            continue
        out.append(ch)
    return "".join(out)


def normalize_whitespace(text: str, *, multiline: bool) -> str:
    """Collapse whitespace, keeping or dropping line structure.

    Args:
        text: The text to normalise.
        multiline: When ``True`` line breaks are preserved (collapsed to at most
            one blank line); when ``False`` every line break becomes a space, so
            the result is a single line.

    Returns:
        The normalised text, stripped at both ends.
    """
    if multiline:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(_WS_RUN.sub(" ", line).strip() for line in text.split("\n"))
        text = _NEWLINE_RUN.sub("\n\n", text)
        return text.strip()
    text = _WS_RUN.sub(" ", text.replace("\r\n", " ").replace("\r", " ").replace("\n", " "))
    return text.strip()


def clean_field(value: Optional[str], limit: int = DEFAULT_MAX_FIELD_CHARS) -> str:
    """Normalise one SPO field: single line, no invisible characters, bounded.

    Args:
        value: The raw field value (``None`` becomes an empty string).
        limit: Maximum number of characters kept.

    Returns:
        The cleaned field, at most ``limit`` characters (an ellipsis counts
        inside the limit, so ``len(result) <= limit`` always holds).
    """
    text = normalize_whitespace(strip_invisible(str(value or "")), multiline=False)
    return _cap(text, limit)


def clean_body(value: Optional[str], limit: int = DEFAULT_MAX_CONTENT_CHARS) -> str:
    """Normalise a knowledge body: multi-line, no invisible characters, bounded.

    Args:
        value: The raw body (``None`` becomes an empty string).
        limit: Maximum number of characters kept.

    Returns:
        The cleaned body, at most ``limit`` characters.
    """
    text = normalize_whitespace(strip_invisible(str(value or "")), multiline=True)
    return _cap(text, limit)


def _cap(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` characters, ellipsis included."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return text[:1]
    return text[: limit - 1] + "…"
