"""
validate.py -- input validation and output quality scoring for markitdown-batch.

Two independent halves:

  Pre-conversion (spec 13-16)
      Is this file actually what it claims to be, is it readable, intact and
      unencrypted?  Catching this here turns silent garbage output into a
      clear, categorised failure.

  Post-conversion (spec 23-30)
      Does the produced markdown look like real text?  Seven heuristics feed a
      0-100 score and a good / suspect / poor grade, plus a plain-English
      diagnosis and a suggested fix.

These heuristics reliably catch MECHANICALLY broken output -- empty
extractions, gibberish, encoding damage, runaway repetition.  They cannot
judge whether a cleanly-extracted document is semantically complete.  Treat a
"poor" grade as "look at this one", not as proof of corruption.
"""

from __future__ import annotations

import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Magic byte signatures (spec 13)
# --------------------------------------------------------------------------

MAGIC_SIGNATURES: List[Tuple[bytes, str]] = [
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),          # empty archive
    (b"PK\x07\x08", "zip"),          # spanned archive
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"RIFF", "riff"),               # wav / webp / avi
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
    (b"{\\rtf", "rtf"),
    (b"\x1f\x8b", "gzip"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!", "rar"),
]

# Which detected magic families are acceptable for a given extension.
EXTENSION_EXPECTS: Dict[str, set] = {
    ".pdf": {"pdf"},
    ".docx": {"zip", "ole2"},   # ole2 => legacy .doc or an encrypted OOXML file
    ".pptx": {"zip", "ole2"},
    ".xlsx": {"zip", "ole2"},
    ".epub": {"zip"},
    ".zip": {"zip"},
    ".xls": {"ole2", "zip"},
    ".doc": {"ole2"},
    ".ppt": {"ole2"},
    ".png": {"png"},
    ".jpg": {"jpeg"},
    ".jpeg": {"jpeg"},
    ".gif": {"gif"},
    ".bmp": {"bmp"},
    ".tif": {"tiff"},
    ".tiff": {"tiff"},
    ".wav": {"riff"},
    ".mp3": {"mp3", "riff"},
    ".rtf": {"rtf"},
}

# Extensions whose content is plain text -- no magic bytes to check.
TEXTUAL_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".msg"}

# Zip-container formats whose integrity we can test directly.
ZIP_CONTAINER_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".epub", ".zip"}


# --------------------------------------------------------------------------
# Pre-conversion validation
# --------------------------------------------------------------------------


@dataclass
class ValidationResult:
    ok: bool
    category: str = "valid"      # valid | empty | corrupt | encrypted | mismatch | unreadable
    reason: str = ""
    fix: str = ""
    detected_type: Optional[str] = None
    warning: str = ""            # non-fatal note, conversion still attempted


def sniff_magic(path: Path, header: Optional[bytes] = None) -> Optional[str]:
    """Return a coarse format family from the file's leading bytes."""
    if header is None:
        try:
            with open(path, "rb") as fh:
                header = fh.read(16)
        except OSError:
            return None
    for sig, name in MAGIC_SIGNATURES:
        if header.startswith(sig):
            return name
    # Text-ish HTML detection needs a case-insensitive look a little deeper.
    probe = header[:16].lstrip().lower()
    if probe.startswith(b"<!doctype") or probe.startswith(b"<html"):
        return "html"
    return None


def _pdf_is_encrypted(path: Path) -> bool:
    """Look for an /Encrypt entry. Reads in chunks so large PDFs stay cheap."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(min(size, 4096))
            tail_len = min(size, 8192)
            fh.seek(max(0, size - tail_len))
            tail = fh.read(tail_len)
        return b"/Encrypt" in head or b"/Encrypt" in tail
    except OSError:
        return False


def _ooxml_is_encrypted(path: Path) -> bool:
    """An encrypted OOXML file is an OLE2 container, not a zip."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(8)
        if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return True
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                return any("EncryptedPackage" in n for n in zf.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    return False


def validate_input_file(path: Path) -> ValidationResult:
    """Run every pre-conversion check (spec 13-16) against one file."""
    ext = path.suffix.lower()

    # --- readable at all? (spec 41-42 feed off this too) -------------------
    if not os.access(path, os.R_OK):
        try:
            st = path.stat()
            mode = oct(st.st_mode & 0o777)[2:]
            owner = _owner_string(st)
            detail = f"owner {owner}, mode {mode}"
        except OSError:
            detail = "stat() also denied"
        return ValidationResult(
            False,
            "unreadable",
            f"permission denied ({detail})",
            f"sudo chown $USER '{path}'  --or re-run allowing elevation",
        )

    # --- zero byte / truncated (spec 14) ----------------------------------
    try:
        size = path.stat().st_size
    except OSError as exc:
        return ValidationResult(False, "unreadable", f"stat failed: {exc}", "")

    if size == 0:
        return ValidationResult(
            False, "empty", "file is 0 bytes", "check the source -- nothing to convert"
        )

    detected = sniff_magic(path)

    # --- extension vs content (spec 13) -----------------------------------
    expected = EXTENSION_EXPECTS.get(ext)
    if expected and detected and detected not in expected:
        return ValidationResult(
            False,
            "mismatch",
            f"extension is {ext} but content looks like {detected}",
            f"rename to the real format (.{detected}) and re-run",
            detected,
        )
    if expected and detected is None and size >= 16:
        # Unknown header where a binary signature was expected.
        return ValidationResult(
            False,
            "mismatch",
            f"extension is {ext} but the file header matches no known {ext} signature",
            "file may be truncated or a renamed text/HTML file",
            None,
        )

    # --- encryption (spec 16) ---------------------------------------------
    if ext == ".pdf" and _pdf_is_encrypted(path):
        return ValidationResult(
            False,
            "encrypted",
            "PDF is encrypted / password-protected",
            "decrypt first, e.g. qpdf --decrypt --password=PW in.pdf out.pdf",
            detected,
        )
    if ext in {".docx", ".pptx", ".xlsx"} and _ooxml_is_encrypted(path):
        return ValidationResult(
            False,
            "encrypted",
            "Office file is encrypted or is a legacy OLE2 document",
            "open in Office and save without a password, or convert with LibreOffice",
            detected,
        )

    # --- archive integrity (spec 15) --------------------------------------
    if ext in ZIP_CONTAINER_EXTENSIONS:
        if not zipfile.is_zipfile(path):
            return ValidationResult(
                False,
                "corrupt",
                f"{ext} container is not a readable zip archive",
                "the file is truncated or damaged -- re-download the original",
                detected,
            )
        try:
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
            if bad is not None:
                return ValidationResult(
                    False,
                    "corrupt",
                    f"archive member failed CRC check: {bad}",
                    "the file is damaged -- re-download the original",
                    detected,
                )
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            return ValidationResult(
                False, "corrupt", f"archive could not be read: {exc}", "re-download the original", detected
            )

    warning = ""
    if ext in TEXTUAL_EXTENSIONS and detected in {"pdf", "ole2", "png", "jpeg", "gif"}:
        warning = f"content looks like {detected} despite the {ext} extension"

    return ValidationResult(True, "valid", "", "", detected, warning)


def _owner_string(st: os.stat_result) -> str:
    """'user:group' where resolvable, numeric ids otherwise."""
    try:
        import grp
        import pwd

        user = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        return f"{user}:{group}"
    except (ImportError, KeyError):
        return f"{st.st_uid}:{st.st_gid}"


# --------------------------------------------------------------------------
# Wordlist for the dictionary heuristic (spec 27)
# --------------------------------------------------------------------------

SYSTEM_WORDLISTS = (
    "/usr/share/dict/words",
    "/usr/share/dict/american-english",
    "/usr/share/dict/british-english",
    "/usr/dict/words",
)

# Fallback when no system wordlist is installed (Kali has none by default).
COMMON_WORDS = frozenset(
    """
a able about above accept access account across act action add additional address after again against age
agree all allow almost also although always among amount an analysis and animal another answer any anyone
appear apply approach approve april are area argue arm around arrive art article as ask associate assume at
attack attention august author available avoid away back bad bank base be because become been before begin
behind believe benefit best better between beyond big bill billion bit black board body book both box boy
break bring brother budget build business but buy by call camera campaign can cancel capital car card care
career carry case catch cause cell center central century certain certainly chair challenge chance change
character charge check child choice choose church citizen city civil claim class clear clearly close coach
cold collection college color come commercial common community company compare computer concern condition
conference congress consider consumer contain continue control cost could country couple course court cover
create crime cultural culture cup current customer cut dark data date daughter day dead deal death debate
decade decide decision deep defense degree democrat describe design despite detail determine develop
development die difference different difficult dinner direction director discover discuss discussion
disease do doctor document dog door down draw dream drive drop drug during each early east easy eat economic
economy edge education effect effort eight either election else employee end energy enough enter entire
environment especially establish even evening event ever every everybody everyone everything evidence exactly
example executive exist expect experience expert explain eye face fact factor fail fall family far fast
father fear federal feel feeling few field fight figure file fill film final finally financial find fine
finger finish fire firm first fish five floor fly focus follow food foot for force foreign forget form
former forward four free friend from front full fund future game garden gas general generation get girl give
glass go goal good government great green ground group grow growth guess gun guy hair half hand hang happen
happy hard have he head health hear heart heat heavy help her here herself high him himself his history hit
hold home hope hospital hot hotel hour house how however huge human hundred husband i idea identify if image
imagine impact important improve in include including increase indeed indicate individual industry
information inside instead institution interest international interview into investment involve is issue it
item its itself job join just keep key kid kill kind kitchen know knowledge land language large last late
later laugh law lawyer lay lead leader learn least leave left leg legal less let letter level lie life light
like likely line list listen little live local long look lose loss lot love low machine magazine main
maintain major majority make man manage management manager many market marriage material matter may maybe me
mean measure media medical meet meeting member memory mention message method middle might military million
mind minute miss mission model modern moment money month more morning most mother move movement movie much
music must my myself name nation national natural nature near nearly necessary need network never new news
newspaper next nice night no none nor north not note nothing notice now number occur of off offer office
officer official often oh oil ok old on once one only onto open operation opportunity option or order
organization other others our out outside over own owner page pain painting paper parent part participant
particular particularly partner party pass past patient pattern pay peace people per perform performance
perhaps period person personal phone physical pick picture piece place plan plant play player point police
policy political politics poor popular population position positive possible power practice prepare present
president pressure pretty prevent price private probably problem process produce product production
professional professor program project property protect prove provide public pull purpose push put quality
question quickly quite race radio raise range rate rather reach read ready real reality realize really reason
receive recent recently recognize record red reduce reflect region relate relationship religious remain
remember remove report represent republican require research resource respond response responsibility rest
result return reveal rich right rise risk road rock role room rule run safe same save say scene school
science scientist score sea season seat second section security see seek seem sell send senior sense series
serious serve service set seven several sex sexual shake share she shoot short shot should shoulder show side
sign significant similar simple simply since sing single sister sit site situation six size skill skin small
smile so social society soldier some somebody someone something sometimes son song soon sort sound source
south southern space speak special specific speech spend sport spring staff stage stand standard star start
state statement station stay step still stock stop store story strategy street strong structure student
study stuff style subject success successful such suddenly suffer suggest summer support sure surface system
table take talk task tax teach teacher team technology television tell ten tend term test text than thank
that the their them themselves then theory there these they thing think third this those though thought
thousand threat three through throughout throw thus time to today together tonight too top total tough
toward town trade traditional training travel treat treatment tree trial trip trouble true truth try turn tv
two type under understand unit until up upon us use usually value various very victim view violence visit
voice vote wait walk wall want war watch water way we weapon wear week weight well west western what whatever
when where whether which while white who whole whom whose why wide wife will win wind window wish with within
without woman wonder word work worker world worry would write writer wrong yard yeah year yes yet you young
your yourself
""".split()
)

_wordlist_cache: Optional[frozenset] = None
_wordlist_source = "builtin"

VOWELS = set("aeiouy")
CONSONANT_RUN_RE = re.compile(r"[bcdfghjklmnpqrstvwxz]{5,}")
REPEAT_RUN_RE = re.compile(r"(.)\1{3,}")


def load_wordlist() -> Tuple[frozenset, str]:
    """Prefer a system wordlist; fall back to the built-in common-word set."""
    global _wordlist_cache, _wordlist_source
    if _wordlist_cache is not None:
        return _wordlist_cache, _wordlist_source
    for candidate in SYSTEM_WORDLISTS:
        p = Path(candidate)
        if p.is_file():
            try:
                words = {
                    line.strip().lower()
                    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if line.strip()
                }
                if len(words) > 1000:
                    _wordlist_cache = frozenset(words) | COMMON_WORDS
                    _wordlist_source = candidate
                    return _wordlist_cache, _wordlist_source
            except OSError:
                continue
    _wordlist_cache = COMMON_WORDS
    _wordlist_source = "builtin (install 'wamerican' for a fuller dictionary)"
    return _wordlist_cache, _wordlist_source


def _is_plausible_word(token: str) -> bool:
    """Shape-based fallback: does this look like it could be a word?"""
    if len(token) == 1:
        return token in {"a", "i"}
    if len(token) > 24:
        return False
    if not any(c in VOWELS for c in token):
        return False
    if CONSONANT_RUN_RE.search(token):
        return False
    if REPEAT_RUN_RE.search(token):
        return False
    return True


# --------------------------------------------------------------------------
# Quality scoring
# --------------------------------------------------------------------------

MOJIBAKE_RE = re.compile(
    r"â€™|â€œ|â€\x9d|â€“|â€”|â€˜|Ã©|Ã¨|Ã¡|Ã­|Ã³|Ãº|Ã±|Ã¼|Ã¶|Ã¤|Â«|Â»|Â°|Â£|Â©|Ã\u0082|\(cid:\d+\)|�"
)
WORD_RE = re.compile(r"[A-Za-z']{1,}")
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

# Bytes of source per output character that a healthy conversion should beat.
# Image-heavy and slide formats legitimately produce far less text per byte.
FORMAT_MIN_RATIO: Dict[str, float] = {
    ".pdf": 0.0015,
    ".docx": 0.0020,
    ".pptx": 0.0004,
    ".xlsx": 0.0010,
    ".xls": 0.0010,
    ".epub": 0.0020,
    ".html": 0.0100,
    ".htm": 0.0100,
    ".csv": 0.2000,
    ".tsv": 0.2000,
    ".json": 0.1000,
    ".xml": 0.0500,
    ".txt": 0.3000,
}
DEFAULT_MIN_RATIO = 0.0005

MIN_MEANINGFUL_CHARS = 50

GRADE_GOOD = 75
GRADE_SUSPECT = 45

SIGNAL_WEIGHTS = {
    "extraction_yield": 25,
    "dictionary": 20,
    "encoding": 15,
    "character_mix": 15,
    "word_shape": 10,
    "repetition": 10,
    "whitespace": 5,
}


@dataclass
class Signal:
    name: str
    score: float          # 0.0 (bad) .. 1.0 (good)
    weight: int
    detail: str = ""


@dataclass
class QualityReport:
    score: int
    grade: str                      # good | suspect | poor
    signals: List[Signal] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    diagnosis: str = ""
    fix: str = ""

    @property
    def is_poor(self) -> bool:
        return self.grade == "poor"


def _ramp(value: float, bad: float, good: float) -> float:
    """Linear 0..1 ramp; handles both ascending and descending ranges."""
    if good == bad:
        return 1.0
    t = (value - bad) / (good - bad)
    return max(0.0, min(1.0, t))


def score_markdown(text: str, source_path: Path, source_size: int) -> QualityReport:
    """Spec 23-30. Produce a 0-100 quality score for one converted document."""
    body = FRONTMATTER_RE.sub("", text)
    total_chars = len(body)
    stripped = body.strip()
    meaningful = len(re.sub(r"\s", "", stripped))
    ext = source_path.suffix.lower()

    signals: List[Signal] = []
    warnings: List[str] = []

    # --- 24: extraction yield --------------------------------------------
    min_ratio = FORMAT_MIN_RATIO.get(ext, DEFAULT_MIN_RATIO)
    if meaningful < MIN_MEANINGFUL_CHARS:
        yield_score = 0.0
        yield_detail = f"only {meaningful} non-whitespace characters extracted"
        warnings.append("output is effectively empty")
    else:
        ratio = meaningful / max(1, source_size)
        yield_score = _ramp(ratio, min_ratio * 0.2, min_ratio)
        yield_detail = (
            f"{meaningful} chars from {source_size} bytes "
            f"(ratio {ratio:.5f}, expected >= {min_ratio:.5f})"
        )
        if yield_score < 0.5:
            warnings.append("very little text extracted relative to file size")
    signals.append(Signal("extraction_yield", yield_score, SIGNAL_WEIGHTS["extraction_yield"], yield_detail))

    # --- 25: encoding artifacts -------------------------------------------
    hits = MOJIBAKE_RE.findall(body)
    per_1k = (len(hits) / total_chars * 1000) if total_chars else 0.0
    enc_score = 1.0 if not hits else _ramp(per_1k, 3.0, 0.0)
    signals.append(
        Signal(
            "encoding",
            enc_score,
            SIGNAL_WEIGHTS["encoding"],
            f"{len(hits)} mojibake/replacement artifact(s) ({per_1k:.2f} per 1k chars)",
        )
    )
    if hits:
        warnings.append(f"{len(hits)} encoding artifact(s) detected")

    # --- 26: character mix -------------------------------------------------
    if total_chars:
        normal = sum(
            1
            for c in body
            if c.isalnum() or c.isspace() or c in ".,;:!?'\"()[]{}-_/\\@#$%&*+=<>|~`^’“”–—"
        )
        alnum_ratio = normal / total_chars
    else:
        alnum_ratio = 0.0
    mix_score = _ramp(alnum_ratio, 0.55, 0.88)
    signals.append(
        Signal("character_mix", mix_score, SIGNAL_WEIGHTS["character_mix"],
               f"{alnum_ratio:.1%} of characters are ordinary text characters")
    )

    # --- 27: dictionary hit rate -------------------------------------------
    words, source = load_wordlist()
    tokens = [t.lower().strip("'") for t in WORD_RE.findall(body)]
    tokens = [t for t in tokens if t]
    if len(tokens) < 20:
        dict_score = 0.0 if meaningful >= MIN_MEANINGFUL_CHARS else 0.5
        dict_detail = f"only {len(tokens)} word tokens -- too few to assess"
    else:
        sample = tokens[:5000]
        in_dict = sum(1 for t in sample if t in words)
        plausible = sum(1 for t in sample if t not in words and _is_plausible_word(t))
        # Dictionary membership counts fully; word-shaped unknowns count half,
        # which keeps proper nouns and jargon from being punished as gibberish.
        rate = (in_dict + 0.5 * plausible) / len(sample)
        dict_score = _ramp(rate, 0.35, 0.80)
        dict_detail = (
            f"{in_dict}/{len(sample)} in dictionary, {plausible} word-shaped unknowns "
            f"(effective {rate:.1%}, source: {source})"
        )
        if dict_score < 0.4:
            warnings.append("text does not resemble real words -- likely OCR gibberish")
    signals.append(Signal("dictionary", dict_score, SIGNAL_WEIGHTS["dictionary"], dict_detail))

    # --- 28: word shape ----------------------------------------------------
    if tokens:
        mean_len = sum(len(t) for t in tokens) / len(tokens)
        if mean_len < 4.0:
            shape_score = _ramp(mean_len, 1.8, 4.0)
            shape_note = "unusually short -- line breaking may have shattered the text"
        elif mean_len > 8.0:
            shape_score = _ramp(mean_len, 14.0, 8.0)
            shape_note = "unusually long -- spaces may have been lost"
        else:
            shape_score = 1.0
            shape_note = "normal"
        shape_detail = f"mean word length {mean_len:.1f} ({shape_note})"
    else:
        shape_score, shape_detail = 0.0, "no word tokens"
    signals.append(Signal("word_shape", shape_score, SIGNAL_WEIGHTS["word_shape"], shape_detail))

    # --- 29: repetition ----------------------------------------------------
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    if len(lines) >= 10:
        uniq_ratio = len(set(lines)) / len(lines)
        rep_score = _ramp(uniq_ratio, 0.30, 0.72)
        rep_detail = f"{len(set(lines))}/{len(lines)} lines unique ({uniq_ratio:.1%})"
        if rep_score < 0.5:
            warnings.append("heavy line repetition -- headers/footers or a stuck extractor")
    elif len(tokens) >= 50:
        # Too few lines to judge, but a document crammed onto one or two lines
        # can still be pathologically repetitive -- fall back to word level.
        ttr = len(set(tokens)) / len(tokens)
        rep_score = _ramp(ttr, 0.05, 0.20)
        rep_detail = f"{len(set(tokens))}/{len(tokens)} words unique ({ttr:.1%}, line-level N/A)"
        if rep_score < 0.5:
            warnings.append("the same handful of words repeat throughout")
    else:
        rep_score, rep_detail = 1.0, f"{len(lines)} line(s) -- too few to assess"
    signals.append(Signal("repetition", rep_score, SIGNAL_WEIGHTS["repetition"], rep_detail))

    # --- 30: whitespace ----------------------------------------------------
    if total_chars:
        ws_ratio = sum(1 for c in body if c.isspace()) / total_chars
        ws_score = _ramp(ws_ratio, 0.70, 0.40)
        ws_detail = f"{ws_ratio:.1%} of the document is whitespace"
    else:
        ws_score, ws_detail = 0.0, "empty document"
    signals.append(Signal("whitespace", ws_score, SIGNAL_WEIGHTS["whitespace"], ws_detail))

    # --- combine -----------------------------------------------------------
    total_weight = sum(s.weight for s in signals)
    raw = sum(s.score * s.weight for s in signals) / total_weight
    score = int(round(raw * 100))

    # Caps. A weighted average alone lets a document that is not made of words
    # still score well on the six mechanical signals, so the decisive failures
    # are allowed to veto the total outright.
    if meaningful < MIN_MEANINGFUL_CHARS:
        score = min(score, 10)

    if len(tokens) >= 50:
        if dict_score < 0.20:
            # Essentially nothing word-shaped: OCR soup or binary leakage.
            score = min(score, 30)
        elif dict_score < 0.45:
            score = min(score, 55)

    if enc_score < 0.30:
        score = min(score, 60)

    grade = "good" if score >= GRADE_GOOD else "suspect" if score >= GRADE_SUSPECT else "poor"

    # Only explain a problem when there is one. A healthy document always has
    # some weakest signal, and reporting it as a diagnosis is alarming noise.
    if grade == "good":
        diagnosis, fix = "", ""
    else:
        diagnosis, fix = _diagnose(signals, ext, meaningful, source_size)

    return QualityReport(score, grade, signals, warnings, diagnosis, fix)


def _diagnose(signals: List[Signal], ext: str, meaningful: int, source_size: int) -> Tuple[str, str]:
    """Turn the weakest signals into a plain-English cause and a suggested fix."""
    weakest = sorted(signals, key=lambda s: s.score)
    worst = weakest[0]
    if worst.score >= 0.50:
        return "", ""

    name = worst.name

    if name == "extraction_yield":
        if meaningful < MIN_MEANINGFUL_CHARS and ext == ".pdf":
            return (
                "near-zero text extracted from a PDF -- almost certainly a scanned "
                "document with no text layer",
                "pip install markitdown-ocr  (or pre-process: ocrmypdf in.pdf out.pdf)",
            )
        if meaningful < MIN_MEANINGFUL_CHARS:
            return (
                "conversion produced essentially no text",
                "open the source to confirm it actually contains text",
            )
        return (
            "far less text extracted than the file size suggests -- part of the "
            "document may not have been read",
            "if it is a PDF, try the OCR route: pip install markitdown-ocr",
        )

    if name == "dictionary":
        return (
            "extracted text does not resemble real words -- typical of bad OCR or a "
            "mis-detected format",
            "check the source renders correctly; for scans re-run OCR at higher quality",
        )

    if name == "encoding":
        return (
            "mojibake or replacement characters present -- the source was decoded "
            "with the wrong codec",
            "ensure ftfy is installed (pip install ftfy); cleanup repairs most of this",
        )

    if name == "character_mix":
        return (
            "unusually high proportion of symbol characters -- possible binary "
            "leakage or a broken font mapping",
            "verify the source file is not corrupt",
        )

    if name == "word_shape":
        return (
            "word lengths are abnormal -- spaces or line breaks were lost during "
            "extraction",
            "common with multi-column PDFs; try the OCR route or export the source to text",
        )

    if name == "repetition":
        return (
            "the same lines repeat throughout -- running headers/footers, a watermark, "
            "or an extractor stuck in a loop",
            "cleanup removes most boilerplate; re-run without --no-clean if you disabled it",
        )

    if name == "whitespace":
        return (
            "the document is mostly whitespace",
            "layout-heavy source; cleanup collapses blank runs",
        )

    return "", ""
