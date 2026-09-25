"""
clean.py -- Markdown cleanup rules for markitdown-batch.

Every rule is non-destructive in the sense that it reports exactly what it
removed and how much, so nothing disappears silently. The orchestrator writes
those reports into the run log.

Rules implemented (spec items 31-39):
  31  encoding repair (ftfy + NFKC + zero-width + NBSP)
  32  repeated header/footer removal
  33  standalone page-number line removal
  34  whitespace normalisation
  35  rejoin hyphenated line breaks
  36  strip (cid:NNN) artifacts and control characters
  37  de-duplicate consecutive identical lines
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import List

try:
    import ftfy

    HAVE_FTFY = True
except ImportError:  # pragma: no cover - reported by the environment check
    HAVE_FTFY = False


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

ZERO_WIDTH_CHARS = "​‌‍⁠﻿­"
NBSP_CHARS = "   "

CID_RE = re.compile(r"\(cid:\d+\)")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
HYPHEN_BREAK_RE = re.compile(r"([A-Za-z])-[ \t]*\n[ \t]*([a-z])")
MULTI_BLANK_RE = re.compile(r"\n{3,}")
TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
HEADING_RE = re.compile(r"^#{1,6}\s+\S")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

# "3", "- 4 -", "Page 7", "Page 7 of 12", "[12]"
PAGE_NUM_RE = re.compile(
    r"^\s*[\[\(\-–—]*\s*(?:page\s+)?\d{1,4}\s*(?:of\s+\d{1,4})?\s*[\]\)\-–—]*\s*$",
    re.IGNORECASE,
)

# Lines that carry markdown structure and must never be treated as
# repeated boilerplate.
STRUCTURAL_PREFIXES = ("#", "-", "*", "+", ">", "|", "```", "~~~", "<!--")

# Tunables for the repeated-line detector.
REPEAT_MIN_OCCURRENCES = 4
REPEAT_MAX_LINE_LEN = 100
REPEAT_MIN_DOC_LINES = 20


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass
class CleanAction:
    """One cleanup rule's effect on the document."""

    rule: str
    occurrences: int
    chars_removed: int
    detail: str = ""

    def __str__(self) -> str:
        base = f"{self.rule}: {self.occurrences} occurrence(s), {self.chars_removed} char(s) removed"
        return f"{base} -- {self.detail}" if self.detail else base


@dataclass
class CleanResult:
    text: str
    actions: List[CleanAction] = field(default_factory=list)
    chars_before: int = 0
    chars_after: int = 0

    @property
    def total_removed(self) -> int:
        return max(0, self.chars_before - self.chars_after)

    @property
    def percent_removed(self) -> float:
        if not self.chars_before:
            return 0.0
        return 100.0 * self.total_removed / self.chars_before


# --------------------------------------------------------------------------
# Individual rules
# --------------------------------------------------------------------------


def repair_encoding(text: str) -> tuple[str, List[CleanAction]]:
    """Spec 31 -- undo mojibake, normalise Unicode, drop zero-width chars."""
    actions: List[CleanAction] = []
    before = text

    if HAVE_FTFY:
        fixed = ftfy.fix_text(text)
        if fixed != before:
            actions.append(
                CleanAction(
                    "encoding-repair(ftfy)",
                    1,
                    max(0, len(before) - len(fixed)),
                    "mojibake / mis-decoded text repaired",
                )
            )
        text = fixed

    normalised = unicodedata.normalize("NFKC", text)
    if normalised != text:
        actions.append(
            CleanAction(
                "unicode-nfkc",
                1,
                max(0, len(text) - len(normalised)),
                "compatibility characters folded",
            )
        )
    text = normalised

    zw_count = sum(text.count(c) for c in ZERO_WIDTH_CHARS)
    if zw_count:
        for c in ZERO_WIDTH_CHARS:
            text = text.replace(c, "")
        actions.append(CleanAction("zero-width-strip", zw_count, zw_count))

    nbsp_count = sum(text.count(c) for c in NBSP_CHARS)
    if nbsp_count:
        for c in NBSP_CHARS:
            text = text.replace(c, " ")
        actions.append(
            CleanAction("nbsp-normalise", nbsp_count, 0, "non-breaking spaces -> plain spaces")
        )

    # The replacement character means data was already lost upstream; we only
    # report it, we do not try to guess what it was.
    bad = text.count("�")
    if bad:
        actions.append(
            CleanAction(
                "replacement-char-detected",
                bad,
                0,
                "U+FFFD present -- source was decoded with the wrong codec",
            )
        )

    return text, actions


def strip_cid_and_control(text: str) -> tuple[str, List[CleanAction]]:
    """Spec 36 -- remove (cid:NNN) artifacts and stray control characters."""
    actions: List[CleanAction] = []

    cids = CID_RE.findall(text)
    if cids:
        removed = sum(len(c) for c in cids)
        text = CID_RE.sub("", text)
        actions.append(
            CleanAction(
                "cid-artifact-strip",
                len(cids),
                removed,
                "PDF font-encoding artifacts",
            )
        )

    ctrls = CONTROL_RE.findall(text)
    if ctrls:
        text = CONTROL_RE.sub("", text)
        actions.append(CleanAction("control-char-strip", len(ctrls), len(ctrls)))

    return text, actions


def rejoin_hyphenated(text: str) -> tuple[str, List[CleanAction]]:
    """Spec 35 -- 'inter-\\nnational' -> 'international'."""
    matches = HYPHEN_BREAK_RE.findall(text)
    if not matches:
        return text, []
    new_text = HYPHEN_BREAK_RE.sub(r"\1\2", text)
    return new_text, [
        CleanAction(
            "rejoin-hyphenated",
            len(matches),
            max(0, len(text) - len(new_text)),
            "words split across line breaks rejoined",
        )
    ]


def _is_structural(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(STRUCTURAL_PREFIXES)


def remove_repeated_lines(text: str) -> tuple[str, List[CleanAction]]:
    """Spec 32 -- drop headers/footers/watermarks repeating through the doc.

    Deliberately conservative: only short, non-structural, non-code lines that
    appear at least REPEAT_MIN_OCCURRENCES times in a document of reasonable
    length are removed, and each removed line is named in the action detail.
    """
    lines = text.split("\n")
    if len(lines) < REPEAT_MIN_DOC_LINES:
        return text, []

    # Identify fenced code regions so their contents are never touched.
    in_fence = False
    fenced = [False] * len(lines)
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            fenced[i] = True
            continue
        fenced[i] = in_fence

    counts: Counter[str] = Counter()
    for i, line in enumerate(lines):
        if fenced[i]:
            continue
        s = line.strip()
        if not s or len(s) > REPEAT_MAX_LINE_LEN or _is_structural(s):
            continue
        counts[s] += 1

    boilerplate = {
        s: n for s, n in counts.items() if n >= REPEAT_MIN_OCCURRENCES
    }
    if not boilerplate:
        return text, []

    kept: List[str] = []
    removed_chars = 0
    removed_total = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not fenced[i] and s in boilerplate:
            removed_chars += len(line) + 1
            removed_total += 1
            continue
        kept.append(line)

    sample = ", ".join(
        f'"{s[:40]}" x{n}' for s, n in sorted(boilerplate.items(), key=lambda kv: -kv[1])[:3]
    )
    return "\n".join(kept), [
        CleanAction(
            "repeated-line-removal",
            removed_total,
            removed_chars,
            f"{len(boilerplate)} distinct boilerplate line(s): {sample}",
        )
    ]


def remove_page_numbers(text: str) -> tuple[str, List[CleanAction]]:
    """Spec 33 -- delete standalone page-number lines."""
    lines = text.split("\n")
    kept: List[str] = []
    removed = 0
    removed_chars = 0
    for line in lines:
        if line.strip() and PAGE_NUM_RE.match(line):
            removed += 1
            removed_chars += len(line) + 1
            continue
        kept.append(line)
    if not removed:
        return text, []
    return "\n".join(kept), [
        CleanAction("page-number-removal", removed, removed_chars)
    ]


def dedupe_consecutive(text: str) -> tuple[str, List[CleanAction]]:
    """Spec 37 -- collapse runs of identical adjacent lines into one."""
    lines = text.split("\n")
    kept: List[str] = []
    removed = 0
    removed_chars = 0
    prev = None
    for line in lines:
        if line.strip() and line == prev:
            removed += 1
            removed_chars += len(line) + 1
            continue
        kept.append(line)
        prev = line
    if not removed:
        return text, []
    return "\n".join(kept), [
        CleanAction("consecutive-duplicate-removal", removed, removed_chars)
    ]


def normalize_whitespace(text: str) -> tuple[str, List[CleanAction]]:
    """Spec 34 -- trailing space, blank-line runs, spacing around headings."""
    actions: List[CleanAction] = []

    trailing = TRAILING_WS_RE.findall(text)
    if trailing:
        removed = sum(len(t) for t in trailing)
        text = TRAILING_WS_RE.sub("", text)
        actions.append(CleanAction("trailing-whitespace", len(trailing), removed))

    runs = MULTI_BLANK_RE.findall(text)
    if runs:
        before_len = len(text)
        text = MULTI_BLANK_RE.sub("\n\n", text)
        actions.append(
            CleanAction(
                "blank-line-collapse",
                len(runs),
                max(0, before_len - len(text)),
                "3+ consecutive blank lines collapsed to 1",
            )
        )

    # Guarantee a blank line before and after every heading so the markdown
    # actually renders as headings rather than paragraph text.
    lines = text.split("\n")
    out: List[str] = []
    fixed = 0
    for i, line in enumerate(lines):
        if HEADING_RE.match(line):
            if out and out[-1].strip():
                out.append("")
                fixed += 1
            out.append(line)
            if i + 1 < len(lines) and lines[i + 1].strip():
                out.append("")
                fixed += 1
            continue
        out.append(line)
    if fixed:
        text = "\n".join(out)
        actions.append(
            CleanAction("heading-spacing", fixed, 0, "blank lines inserted around headings")
        )

    text = text.strip("\n") + "\n"
    return text, actions


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

# Order matters: repair encoding first so later pattern matching sees correct
# characters; normalise whitespace last so earlier deletions do not leave
# ragged blank-line runs behind.
_PIPELINE = (
    repair_encoding,
    strip_cid_and_control,
    rejoin_hyphenated,
    remove_repeated_lines,
    remove_page_numbers,
    dedupe_consecutive,
    normalize_whitespace,
)


def clean_markdown(text: str) -> CleanResult:
    """Run every cleanup rule in order and report what each one did."""
    result = CleanResult(text=text, chars_before=len(text))
    for rule in _PIPELINE:
        text, actions = rule(text)
        result.actions.extend(actions)
    result.text = text
    result.chars_after = len(text)
    return result
