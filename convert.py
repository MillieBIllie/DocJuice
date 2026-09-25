#!/usr/bin/env python3
"""
md-batch -- batch document to Markdown converter built on Microsoft MarkItDown.

Takes a zip archive, a folder, or a single file; finds every convertible
document (recursing through nested folders); converts each to Markdown; and
writes the results into a new folder that mirrors the input tree exactly.

Run with --help for the full flag list, or --version to check versions.

Project layout:
    convert.py   this file -- CLI, scanning, orchestration, reporting
    validate.py  input validation + output quality scoring
    clean.py     markdown cleanup rules
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import traceback
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_NAME = "md-batch"
SCRIPT_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Optional presentation dependency
# --------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table

    HAVE_RICH = True
except ImportError:  # the tool still runs without it, just plainer
    HAVE_RICH = False

# Local modules -- imported after rich so an ImportError here is clearly ours.
try:
    import clean as clean_mod
    import validate as validate_mod
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        f"FATAL: could not import companion modules ({exc}).\n"
        f"clean.py and validate.py must sit next to {Path(__file__).name}.\n"
    )
    sys.exit(2)


# --------------------------------------------------------------------------
# Format tables
# --------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {
    # documents
    ".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".epub", ".msg",
    # text-ish
    ".html", ".htm", ".csv", ".tsv", ".json", ".xml", ".txt", ".rtf",
    # images (EXIF + OCR)
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    # audio (EXIF + transcription)
    ".wav", ".mp3", ".m4a",
}

# Document formats that happen to BE zip containers. These must never be
# treated as archives to unpack -- markitdown converts them directly.
ZIP_DOCUMENT_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".epub", ".odt", ".ods", ".odp"}

JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".gitkeep"}
JUNK_DIR_PARTS = {"__MACOSX", ".git", ".svn", "__pycache__", ".ipynb_checkpoints"}

# extension -> (markitdown extra, pip name, import name used to detect it)
EXTRA_REQUIREMENTS: Dict[str, Tuple[str, str, str]] = {
    ".pdf": ("pdf", "markitdown[pdf]", "pdfminer"),
    ".docx": ("docx", "markitdown[docx]", "mammoth"),
    ".pptx": ("pptx", "markitdown[pptx]", "pptx"),
    ".xlsx": ("xlsx", "markitdown[xlsx]", "openpyxl"),
    ".xls": ("xls", "markitdown[xls]", "xlrd"),
    ".msg": ("outlook", "markitdown[outlook]", "olefile"),
}

# Rough per-file seconds, used only for the pre-flight time estimate.
TIME_ESTIMATE: Dict[str, float] = {
    ".pdf": 1.5, ".docx": 0.3, ".pptx": 0.6, ".xlsx": 0.4, ".xls": 0.4,
    ".epub": 0.5, ".png": 0.6, ".jpg": 0.6, ".jpeg": 0.6, ".tiff": 0.9,
    ".wav": 3.0, ".mp3": 3.0,
}
DEFAULT_TIME_ESTIMATE = 0.15

# Status vocabulary (spec 54)
STATUS_CONVERTED = "converted"
STATUS_SKIPPED = "skipped"
STATUS_UNCHANGED = "unchanged"
STATUS_DENIED = "permission-denied"
STATUS_ENCRYPTED = "encrypted"
STATUS_TIMEOUT = "timed-out"
STATUS_FAILED = "failed"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FATAL = 2


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------


@dataclass
class FileResult:
    source: Path
    rel_path: Path
    status: str
    output: Optional[Path] = None
    reason: str = ""
    fix: str = ""
    quality_score: Optional[int] = None
    quality_grade: str = ""
    diagnosis: str = ""
    duration: float = 0.0
    cleanup_actions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_CONVERTED, STATUS_UNCHANGED)


@dataclass
class ScanEntry:
    path: Path
    rel_path: Path
    size: int
    readable: bool
    denial: str = ""


@dataclass
class ScanResult:
    root: Path
    files: List[ScanEntry] = field(default_factory=list)
    unsupported: List[Path] = field(default_factory=list)
    junk_count: int = 0
    unreadable_dirs: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def readable(self) -> List[ScanEntry]:
        return [e for e in self.files if e.readable]

    @property
    def denied(self) -> List[ScanEntry]:
        return [e for e in self.files if not e.readable]

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.files)

    def by_extension(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for e in self.files:
            counts[e.path.suffix.lower()] = counts.get(e.path.suffix.lower(), 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def estimated_seconds(self) -> float:
        return sum(
            TIME_ESTIMATE.get(e.path.suffix.lower(), DEFAULT_TIME_ESTIMATE)
            for e in self.readable
        )


class ConversionTimeout(Exception):
    pass


class FatalError(Exception):
    """Unrecoverable problem -- exits with code 2."""


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


class RunLog:
    """Plain-text run log with full tracebacks (spec 61)."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self._fh = None
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(path, "a", encoding="utf-8")
            except OSError as exc:
                sys.stderr.write(f"warning: could not open log file {path}: {exc}\n")

    def write(self, level: str, message: str) -> None:
        if self._fh is None:
            return
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        self._fh.write(f"{stamp} [{level:<7}] {message}\n")
        self._fh.flush()

    def info(self, message: str) -> None:
        self.write("INFO", message)

    def warn(self, message: str) -> None:
        self.write("WARNING", message)

    def error(self, message: str, exc: Optional[BaseException] = None) -> None:
        self.write("ERROR", message)
        if exc is not None and self._fh is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self._fh.write(tb.rstrip() + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------
# Console output
# --------------------------------------------------------------------------


class UI:
    """Thin wrapper so the tool degrades gracefully without rich (spec 50-53)."""

    STYLES = {
        "ok": "\033[32m", "warn": "\033[33m", "err": "\033[31m",
        "dim": "\033[2m", "bold": "\033[1m", "info": "\033[36m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool = True, verbose: bool = False):
        self.verbose = verbose
        self.color = color and sys.stdout.isatty()
        self.rich = HAVE_RICH and self.color
        self.console = Console() if self.rich else None

    def _plain(self, text: str, style: Optional[str]) -> str:
        if not self.color or style not in self.STYLES:
            return text
        return f"{self.STYLES[style]}{text}{self.RESET}"

    def print(self, text: str = "", style: Optional[str] = None) -> None:
        if self.rich:
            rich_style = {
                "ok": "green", "warn": "yellow", "err": "red",
                "dim": "dim", "bold": "bold", "info": "cyan",
            }.get(style or "")
            self.console.print(text, style=rich_style, highlight=False)
        else:
            print(self._plain(text, style))

    def rule(self, title: str = "") -> None:
        if self.rich:
            self.console.rule(f"[bold]{title}" if title else "")
        else:
            width = min(shutil.get_terminal_size((80, 24)).columns, 100)
            if title:
                pad = max(0, width - len(title) - 4)
                print(self._plain(f"-- {title} " + "-" * pad, "dim"))
            else:
                print(self._plain("-" * width, "dim"))

    def confirm(self, question: str, assume_yes: bool = False) -> bool:
        """Ask the user. Answering from a pipe works; a closed stdin means no.

        We deliberately do NOT pre-check isatty(): that would reject piped
        answers (echo y | md-batch ...). A non-interactive stdin raises
        EOFError immediately rather than hanging, which is the behaviour a
        cron job needs.
        """
        if assume_yes:
            self.print(f"{question} [auto-yes]", "dim")
            return True
        try:
            answer = input(f"{question} [y/n] ").strip().lower()
        except EOFError:
            # input() has already echoed the question, so don't repeat it.
            self.print(" -- stdin closed, assuming NO (use --yes to proceed)", "warn")
            return False
        except (KeyboardInterrupt, OSError):
            print()
            return False
        return answer in ("y", "yes")

    @contextmanager
    def progress(self, total: int, description: str):
        """Yields advance(label) -- a rich bar, or a plain counter."""
        if self.rich and not self.verbose:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("*"),
                TimeElapsedColumn(),
                TextColumn("elapsed, ~"),
                TimeRemainingColumn(),
                TextColumn("left"),
                console=self.console,
                transient=False,
            ) as prog:
                task = prog.add_task(description, total=total)

                def advance(label: str = "") -> None:
                    prog.update(task, advance=1)

                yield advance
        else:
            counter = {"n": 0}

            def advance(label: str = "") -> None:
                counter["n"] += 1
                if not self.verbose:
                    pct = 100 * counter["n"] / max(1, total)
                    sys.stdout.write(f"\r  {counter['n']}/{total} ({pct:5.1f}%)")
                    sys.stdout.flush()

            yield advance
            if not self.verbose:
                sys.stdout.write("\n")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def human_bytes(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB"):
        if n < step or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} GB"


def human_time(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


def is_junk(path: Path) -> bool:
    if path.name in JUNK_NAMES or path.name.startswith("._"):
        return True
    return any(part in JUNK_DIR_PARTS for part in path.parts)


@contextmanager
def time_limit(seconds: float):
    """Spec 19. SIGALRM-based; a no-op where SIGALRM is unavailable."""
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def handler(signum, frame):
        raise ConversionTimeout(f"exceeded {seconds:.0f}s")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def yaml_escape(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------
# Environment check (spec 62-63, 65)
# --------------------------------------------------------------------------


def markitdown_version() -> str:
    try:
        from importlib.metadata import version

        return version("markitdown")
    except Exception:
        return "unknown"


@dataclass
class PythonEnv:
    """Where we are installing to, and whether that is allowed."""

    in_venv: bool
    externally_managed: bool
    prefix: str

    @property
    def can_install_freely(self) -> bool:
        # Inside a virtualenv pip is always safe. Outside one, a distro that
        # marks its Python externally managed (PEP 668 -- Kali, Debian, Ubuntu)
        # will refuse pip, and overriding that can break system packages.
        return self.in_venv or not self.externally_managed

    def describe(self) -> str:
        if self.in_venv:
            return f"virtual environment ({self.prefix})"
        if self.externally_managed:
            return f"system Python, externally managed ({self.prefix})"
        return f"system Python ({self.prefix})"


def detect_python_env() -> PythonEnv:
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    marker = Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED"
    return PythonEnv(in_venv, marker.exists(), sys.prefix)


def module_present(import_name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def pip_install(packages: List[str], env: PythonEnv, ui: UI, log: RunLog,
                allow_system: bool = False) -> bool:
    """Install packages into THIS interpreter. Streams pip's own output."""
    cmd = [sys.executable, "-m", "pip", "install"] + packages
    if not env.in_venv and env.externally_managed and allow_system:
        cmd.append("--break-system-packages")

    ui.print(f"  Running: {' '.join(cmd)}", "dim")
    log.info(f"pip install: {' '.join(cmd)}")
    try:
        # Not captured on purpose -- pip's progress is worth seeing, and an
        # install of markitdown[all] is not instant.
        proc = subprocess.run(cmd, timeout=1800)
    except subprocess.TimeoutExpired:
        ui.print("  Install timed out after 30 minutes.", "err")
        log.warn("pip install timed out")
        return False
    except OSError as exc:
        ui.print(f"  Could not run pip: {exc}", "err")
        log.error("pip could not be run", exc)
        return False

    if proc.returncode != 0:
        ui.print(f"  pip exited with {proc.returncode} -- install failed.", "err")
        log.warn(f"pip install failed with exit {proc.returncode}")
        return False

    # Make the freshly installed package visible to this running process.
    try:
        import importlib

        importlib.invalidate_caches()
    except Exception:  # pragma: no cover
        pass
    ui.print("  Install finished.", "ok")
    log.info("pip install succeeded")
    return True


def venv_setup_instructions() -> str:
    return (
        "  Recommended (a virtual environment keeps this off your system Python):\n"
        "      python3 -m venv ~/md-batch-env\n"
        "      source ~/md-batch-env/bin/activate\n"
        "      pip install 'markitdown[all]' rich ftfy\n"
        "      # then re-run this script from inside that environment\n"
        "\n"
        "  Or, to install onto the system Python anyway (not recommended --\n"
        "  it can conflict with apt-managed packages):\n"
        "      re-run with --allow-system-install"
    )


def version_banner() -> str:
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    tools = detect_extractors()
    env = detect_python_env()
    lines = [
        f"{SCRIPT_NAME} {SCRIPT_VERSION}",
        f"  markitdown : {markitdown_version() if module_present('markitdown') else 'NOT INSTALLED'}",
        f"  python     : {py} ({sys.executable})",
        f"  environment: {env.describe()}",
        f"  rich       : {'installed' if HAVE_RICH else 'MISSING (plain output)'}",
        f"  ftfy       : {'installed' if clean_mod.HAVE_FTFY else 'MISSING (no encoding repair)'}",
        "  unzip tools:",
    ]
    for i, tool in enumerate(tools):
        marker = "-> " if i == 0 else "   "
        lines.append(f"    {marker}{tool.key:<8} {tool.label}")
    lines.append("       ('->' is the one --extractor auto will use)")
    return "\n".join(lines)


def _import_markitdown() -> Tuple[bool, str]:
    """(importable, error message)."""
    try:
        import markitdown  # noqa: F401

        return True, ""
    except KeyboardInterrupt:
        raise
    except ImportError as exc:
        return False, str(exc)


def ensure_markitdown(args, ui: UI, log: RunLog) -> None:
    """Verify the conversion engine is present, and offer to install it.

    Everything this tool does is a wrapper around markitdown, so without it
    there is nothing to run. Rather than printing a command and quitting, this
    offers to install it into the interpreter that is actually running -- which
    is also the one that matters, and the usual cause of "but I did install it"
    (it went into a different Python).
    """
    if sys.version_info < (3, 10):
        raise FatalError(
            f"Python 3.10+ is required by markitdown, found "
            f"{sys.version_info.major}.{sys.version_info.minor}.\n"
            f"  Interpreter: {sys.executable}"
        )

    ok, err = _import_markitdown()
    if ok:
        log.info(f"markitdown {markitdown_version()} present")
        return

    env = detect_python_env()

    # Installed but broken is a different problem from not installed at all --
    # telling someone to install a package they already have wastes their time.
    if module_present("markitdown"):
        raise FatalError(
            f"markitdown is installed but failed to import: {err}\n"
            "  That usually means a partial or broken install.\n"
            "  Fix: pip install --force-reinstall 'markitdown[all]'\n"
            f"  Interpreter: {sys.executable}"
        )

    ui.print()
    ui.print("markitdown is not installed -- it is the engine this tool runs on.", "warn")
    ui.print(f"  Target interpreter: {sys.executable}", "dim")
    ui.print(f"  Environment       : {env.describe()}", "dim")

    if args.no_install:
        raise FatalError(
            "markitdown is missing and --no-install was given.\n"
            "  Fix: pip install 'markitdown[all]'"
        )

    if not env.can_install_freely and not args.allow_system_install:
        # PEP 668: the distro asked us not to pip into the system Python.
        raise FatalError(
            "markitdown is not installed, and this is an externally managed\n"
            "  system Python (Kali/Debian/Ubuntu protect it from pip).\n\n"
            + venv_setup_instructions()
        )

    if not ui.confirm("Install markitdown[all] now?", assume_yes=args.yes):
        raise FatalError(
            "markitdown is required and was not installed.\n"
            "  Fix: pip install 'markitdown[all]'"
        )

    if not pip_install(["markitdown[all]"], env, ui, log, args.allow_system_install):
        raise FatalError(
            "the install did not succeed -- see the pip output above.\n"
            "  Try manually: pip install 'markitdown[all]'"
        )

    ok, err = _import_markitdown()
    if not ok:
        raise FatalError(
            f"markitdown still cannot be imported after installing: {err}\n"
            "  Try a fresh terminal, or reinstall with:\n"
            "      pip install --force-reinstall 'markitdown[all]'"
        )
    ui.print(f"markitdown {markitdown_version()} is ready.", "ok")


def check_optional_deps(args, ui: UI, log: RunLog) -> None:
    """rich and ftfy are optional; offer them rather than nagging every run."""
    wanted: List[str] = []
    if not HAVE_RICH:
        wanted.append("rich")
    if not clean_mod.HAVE_FTFY:
        wanted.append("ftfy")
    if not wanted:
        return

    notes = {
        "rich": "progress bar and coloured output",
        "ftfy": "mojibake repair in the cleanup pass",
    }
    for pkg in wanted:
        style = "warn" if pkg == "ftfy" else "dim"
        ui.print(f"note: '{pkg}' not installed -- {notes[pkg]} unavailable.", style)

    env = detect_python_env()
    if args.no_install or not env.can_install_freely and not args.allow_system_install:
        ui.print(f"  Install with: pip install {' '.join(wanted)}", "dim")
        return
    if ui.confirm(f"Install {' and '.join(wanted)} now?", assume_yes=args.yes):
        if pip_install(wanted, env, ui, log, args.allow_system_install):
            ui.print("  Restart the script to use them for this run.", "dim")


def check_environment(args, ui: UI, log: RunLog) -> None:
    ensure_markitdown(args, ui, log)
    log.info(version_banner().replace("\n", " | "))
    check_optional_deps(args, ui, log)


def missing_extras_for(extensions: Iterable[str]) -> List[Tuple[str, str, List[str]]]:
    """Which markitdown extras are needed but absent (spec 63)."""
    grouped: Dict[str, List[str]] = {}
    for ext in extensions:
        req = EXTRA_REQUIREMENTS.get(ext)
        if not req:
            continue
        _extra, pip_name, import_name = req
        if not module_present(import_name):
            grouped.setdefault(pip_name, []).append(ext)
    return [(pip_name, f"pip install '{pip_name}'", exts) for pip_name, exts in grouped.items()]


def offer_extras_install(
    missing: List[Tuple[str, str, List[str]]], args, ui: UI, log: RunLog
) -> bool:
    """Offer to install format support the scan found we need. True if installed."""
    if not missing or args.no_install:
        return False
    env = detect_python_env()
    if not env.can_install_freely and not args.allow_system_install:
        ui.print(
            "  (cannot install here -- externally managed Python; "
            "use a venv or --allow-system-install)",
            "dim",
        )
        return False

    packages = [pip_name for pip_name, _fix, _exts in missing]
    affected = sum(len(exts) for _p, _f, exts in missing)
    if not ui.confirm(
        f"Install the missing format support now ({affected} file type(s))?",
        assume_yes=args.yes,
    ):
        return False
    if not pip_install(packages, env, ui, log, args.allow_system_install):
        ui.print("  Install failed -- those files will be reported as failures.", "warn")
        return False
    return True


def run_doctor(args, ui: UI, log: RunLog) -> int:
    """--doctor: diagnose the setup and offer to fix it, then exit."""
    ui.rule("Environment check")
    env = detect_python_env()
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    ui.print(f"Python        : {py}  ({sys.executable})")
    ui.print(f"Environment   : {env.describe()}")
    if sys.version_info < (3, 10):
        ui.print("  Python 3.10+ is required by markitdown.", "err")
    ui.print()

    core_ok = module_present("markitdown")
    ui.print(
        f"markitdown    : {markitdown_version() if core_ok else 'NOT INSTALLED'}",
        "ok" if core_ok else "err",
    )
    for pkg, present, why in (
        ("rich", HAVE_RICH, "progress bar and colour"),
        ("ftfy", clean_mod.HAVE_FTFY, "mojibake repair"),
    ):
        ui.print(f"{pkg:<14}: {'installed' if present else 'not installed'} ({why})",
                 "ok" if present else "warn")

    ui.print()
    ui.print("Format support:")
    missing: List[Tuple[str, str, List[str]]] = []
    for ext, (_extra, pip_name, import_name) in sorted(EXTRA_REQUIREMENTS.items()):
        present = module_present(import_name)
        ui.print(f"  {ext:<7} {'yes' if present else 'NO '}   ({import_name})",
                 "ok" if present else "warn")
        if not present:
            missing.append((pip_name, f"pip install '{pip_name}'", [ext]))

    ui.print()
    ui.print("Unzip tools:")
    for i, tool in enumerate(detect_extractors()):
        ui.print(f"  {'->' if i == 0 else '  '} {tool.key:<8} {tool.label}",
                 "ok" if i == 0 else "dim")

    words, source = validate_mod.load_wordlist()
    ui.print()
    ui.print(f"Dictionary    : {len(words)} words ({source})", "dim")

    todo: List[str] = []
    if not core_ok:
        todo.append("markitdown[all]")
    else:
        todo.extend(p for p, _f, _e in missing)
    if not HAVE_RICH:
        todo.append("rich")
    if not clean_mod.HAVE_FTFY:
        todo.append("ftfy")

    ui.print()
    if not todo:
        ui.print("Everything needed is installed.", "ok")
        return EXIT_OK

    ui.print(f"Missing: {', '.join(todo)}", "warn")
    if args.no_install:
        ui.print(f"  Install with: pip install {' '.join(repr(t) for t in todo)}", "dim")
        return EXIT_PARTIAL
    if not env.can_install_freely and not args.allow_system_install:
        ui.print()
        ui.print(venv_setup_instructions(), "dim")
        return EXIT_PARTIAL
    if ui.confirm("Install all of the above now?", assume_yes=args.yes):
        if pip_install(todo, env, ui, log, args.allow_system_install):
            ui.print("Done. Re-run --doctor to confirm.", "ok")
            return EXIT_OK
        return EXIT_PARTIAL
    return EXIT_PARTIAL


# --------------------------------------------------------------------------
# Scanning (spec 3, 5, 6, 40-44)
# --------------------------------------------------------------------------


def scan_tree(root: Path, log: RunLog) -> ScanResult:
    result = ScanResult(root=root)

    def on_walk_error(err: OSError) -> None:
        # Spec 40: without this handler os.walk silently skips whole subtrees.
        name = getattr(err, "filename", "?") or "?"
        reason = getattr(err, "strerror", str(err))
        result.unreadable_dirs.append((str(name), reason))
        log.warn(f"directory not listable: {name} ({reason})")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_walk_error):
        dirnames[:] = [d for d in dirnames if d not in JUNK_DIR_PARTS]
        base = Path(dirpath)
        for name in sorted(filenames):
            path = base / name
            if is_junk(path):
                result.junk_count += 1
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                result.unsupported.append(path)
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                size = 0
                log.warn(f"stat failed for {path}: {exc}")
            readable = os.access(path, os.R_OK)
            denial = ""
            if not readable:
                try:
                    st = path.stat()
                    denial = (
                        f"owner {validate_mod._owner_string(st)}, "
                        f"mode {oct(st.st_mode & 0o777)[2:]}"
                    )
                except OSError:
                    denial = "stat() denied"
                log.warn(f"unreadable file: {path} ({denial})")
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = Path(path.name)
            result.files.append(ScanEntry(path, rel, size, readable, denial))
    return result


# --------------------------------------------------------------------------
# Privilege escalation (spec 45-47)
# --------------------------------------------------------------------------


def sudo_available() -> bool:
    return shutil.which("sudo") is not None


def sudo_cached() -> bool:
    """True when sudo needs no password right now."""
    try:
        return (
            subprocess.run(
                ["sudo", "-n", "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def elevate_denied_files(
    denied: Sequence[ScanEntry],
    staging_root: Path,
    ui: UI,
    log: RunLog,
    assume_yes: bool,
    mode: str,
) -> Dict[Path, Path]:
    """Copy unreadable files to a staging area using sudo, then drop privileges.

    The password is never seen by this process: sudo prompts on the terminal
    itself. Only the narrow copy runs elevated -- markitdown's parsing always
    runs unprivileged.
    """
    if not denied or mode == "never":
        return {}

    if not sudo_available():
        ui.print("sudo is not installed -- leaving unreadable files as denied.", "warn")
        log.warn("elevation skipped: sudo not found")
        return {}

    cached = sudo_cached()
    if not cached and not sys.stdin.isatty():
        ui.print(
            "No terminal available for a sudo password prompt -- "
            "leaving unreadable files as denied.",
            "warn",
        )
        log.warn("elevation skipped: no TTY for sudo prompt")
        return {}

    if mode != "always":
        ui.print()
        ui.print(f"{len(denied)} file(s) could not be read (permission denied):", "warn")
        for entry in denied[:10]:
            ui.print(f"    {entry.rel_path}  ({entry.denial})", "dim")
        if len(denied) > 10:
            ui.print(f"    ... and {len(denied) - 10} more", "dim")
        prompt = "Attempt to access them with elevated privileges?"
        if not cached:
            prompt += " (sudo will prompt for your password directly)"
        if not ui.confirm(prompt, assume_yes=assume_yes):
            log.info("user declined privilege escalation")
            return {}

    staging_root.mkdir(parents=True, exist_ok=True)
    mapping: Dict[Path, Path] = {}
    for entry in denied:
        target = staging_root / entry.rel_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["sudo", "cp", "--preserve=timestamps", str(entry.path), str(target)],
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                log.warn(f"sudo cp failed for {entry.path}: {proc.stderr.strip()}")
                continue
            mapping[entry.path] = target
            log.info(f"elevated copy: {entry.path} -> {target}")
        except (OSError, subprocess.SubprocessError) as exc:
            log.error(f"elevated copy failed for {entry.path}", exc)

    if mapping:
        try:
            subprocess.run(
                ["sudo", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(staging_root)],
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("chown of staged files failed", exc)
        ui.print(f"Staged {len(mapping)} file(s) with elevated access.", "ok")
    else:
        ui.print("No files could be staged with elevated access.", "warn")
    return mapping


# --------------------------------------------------------------------------
# Conversion (spec 17-21)
# --------------------------------------------------------------------------


class Converter:
    def __init__(self, args, log: RunLog):
        from markitdown import MarkItDown

        self.md = MarkItDown(enable_plugins=args.plugins)
        self.args = args
        self.log = log

    def _raw_convert(self, path: Path) -> str:
        # convert_local is the narrowest API for local files (upstream security note).
        convert = getattr(self.md, "convert_local", None) or self.md.convert
        result = convert(str(path))
        text = getattr(result, "markdown", None)
        if text is None:
            text = getattr(result, "text_content", "") or ""
        return text

    def convert_one(self, entry: ScanEntry, read_path: Path, out_path: Path) -> FileResult:
        started = time.monotonic()
        res = FileResult(source=entry.path, rel_path=entry.rel_path, status=STATUS_FAILED)

        # --- spec 20: incremental skip ------------------------------------
        if not self.args.force and out_path.exists():
            try:
                if out_path.stat().st_mtime >= read_path.stat().st_mtime:
                    res.status = STATUS_UNCHANGED
                    res.output = out_path
                    res.reason = "output already newer than source"
                    res.duration = time.monotonic() - started
                    return res
            except OSError:
                pass

        # --- spec 13-16: validate before converting -----------------------
        v = validate_mod.validate_input_file(read_path)
        if not v.ok:
            res.status = {
                "unreadable": STATUS_DENIED,
                "encrypted": STATUS_ENCRYPTED,
            }.get(v.category, STATUS_FAILED)
            res.reason = v.reason
            res.fix = v.fix
            res.duration = time.monotonic() - started
            self.log.warn(f"{entry.rel_path}: {v.category} -- {v.reason}")
            return res
        if v.warning:
            res.warnings.append(v.warning)

        # --- convert -------------------------------------------------------
        try:
            with time_limit(self.args.timeout):
                text = self._raw_convert(read_path)
        except ConversionTimeout as exc:
            res.status = STATUS_TIMEOUT
            res.reason = f"timed out after {human_time(self.args.timeout)}"
            res.fix = "raise --timeout, or exclude this file"
            res.duration = time.monotonic() - started
            self.log.error(f"{entry.rel_path}: {res.reason}", exc)
            return res
        except PermissionError as exc:
            res.status = STATUS_DENIED
            res.reason = f"permission denied while reading: {exc}"
            res.fix = f"sudo chown $USER '{entry.path}'"
            res.duration = time.monotonic() - started
            self.log.error(f"{entry.rel_path}: permission denied", exc)
            return res
        except FileNotFoundError as exc:
            res.reason = "file disappeared during the run"
            res.duration = time.monotonic() - started
            self.log.error(f"{entry.rel_path}: vanished", exc)
            return res
        except MemoryError as exc:
            res.reason = "ran out of memory"
            res.fix = "convert this file on its own, or split it first"
            res.duration = time.monotonic() - started
            self.log.error(f"{entry.rel_path}: MemoryError", exc)
            return res
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            res.reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            res.fix = _suggest_fix_for_exception(exc, entry.path)
            res.duration = time.monotonic() - started
            self.log.error(f"{entry.rel_path}: conversion failed", exc)
            return res

        raw_text = text or ""

        # --- spec 31-39: cleanup -------------------------------------------
        if self.args.clean:
            cleaned = clean_mod.clean_markdown(raw_text)
            final_text = cleaned.text
            res.cleanup_actions = [str(a) for a in cleaned.actions]
            for action in cleaned.actions:
                self.log.info(f"{entry.rel_path}: cleanup {action}")
            if cleaned.percent_removed > 40:
                res.warnings.append(
                    f"cleanup removed {cleaned.percent_removed:.0f}% of the text -- "
                    f"inspect with --keep-raw"
                )
        else:
            final_text = raw_text

        # --- spec 23-30: quality scoring -----------------------------------
        quality = validate_mod.score_markdown(final_text, entry.path, max(1, entry.size))
        res.quality_score = quality.score
        res.quality_grade = quality.grade
        res.diagnosis = quality.diagnosis
        if quality.diagnosis and not res.fix:
            res.fix = quality.fix
        res.warnings.extend(quality.warnings)

        # --- write ----------------------------------------------------------
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                self._frontmatter(entry, quality) + final_text, encoding="utf-8"
            )
            if self.args.keep_raw and self.args.clean:
                raw_path = out_path.with_suffix(".raw.md")
                raw_path.write_text(raw_text, encoding="utf-8")
        except OSError as exc:
            res.reason = f"could not write output: {exc}"
            res.fix = "check permissions and free space on the output path"
            res.duration = time.monotonic() - started
            self.log.error(f"{entry.rel_path}: write failed", exc)
            return res

        res.status = STATUS_CONVERTED
        res.output = out_path
        res.duration = time.monotonic() - started
        return res

    def _frontmatter(self, entry: ScanEntry, quality) -> str:
        """Spec 21."""
        lines = [
            "---",
            f"source: {yaml_escape(entry.rel_path.as_posix())}",
            f"source_bytes: {entry.size}",
            f"converted: {_dt.datetime.now().isoformat(timespec='seconds')}",
            f"converter: {yaml_escape('markitdown ' + markitdown_version())}",
            f"tool: {yaml_escape(SCRIPT_NAME + ' ' + SCRIPT_VERSION)}",
            f"quality_score: {quality.score}",
            f"quality_grade: {quality.grade}",
        ]
        if quality.warnings:
            lines.append("warnings:")
            lines.extend(f"  - {yaml_escape(w)}" for w in quality.warnings)
        lines.append("---")
        lines.append("")
        return "\n".join(lines) + "\n"


def _suggest_fix_for_exception(exc: Exception, path: Path) -> str:
    """Spec 58 -- turn a raw exception into something actionable."""
    name = type(exc).__name__
    text = str(exc).lower()
    ext = path.suffix.lower()
    req = EXTRA_REQUIREMENTS.get(ext)

    if name in ("ModuleNotFoundError", "ImportError") or "no module named" in text:
        if req:
            return f"missing dependency -- pip install '{req[1]}'"
        return "missing dependency -- pip install 'markitdown[all]'"
    if "unsupportedformat" in name.lower() or "could not convert" in text:
        if req:
            return f"format support may be missing -- pip install '{req[1]}'"
        return "markitdown has no converter for this file"
    if "password" in text or "encrypt" in text:
        return "file appears encrypted -- decrypt it first"
    if "badzipfile" in name.lower() or "not a zip" in text:
        return "container is damaged -- re-download the original"
    if "codec" in text or "decode" in text:
        return "text decoding problem -- pip install ftfy and re-run"
    return ""


# --------------------------------------------------------------------------
# Zip handling (spec 1-2, 4, 7, 9)
# --------------------------------------------------------------------------


@dataclass
class ExtractorTool:
    """An available way to unpack an archive."""

    key: str            # 7z | unzip | bsdtar | python
    label: str          # human-readable, includes version when known
    executable: Optional[str] = None   # None for the built-in reader

    @property
    def is_external(self) -> bool:
        return self.executable is not None


# Preference order. System tools come first because Python's zipfile cannot
# read Deflate64 archives (common from Windows "Send to > Compressed folder"
# on large files) and a few other vendor variants, whereas 7z/unzip handle
# them. The built-in reader is always available as the final fallback.
_EXTERNAL_EXTRACTORS: List[Tuple[str, List[str], List[str]]] = [
    # key,     candidate executables,     version probe args
    ("7z", ["7z", "7za", "7zr"], ["--help"]),
    ("unzip", ["unzip"], ["-v"]),
    ("bsdtar", ["bsdtar"], ["--version"]),
]

_extractor_cache: Optional[List[ExtractorTool]] = None


def _probe_version(executable: str, args: List[str]) -> str:
    try:
        proc = subprocess.run(
            [executable] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line and any(ch.isdigit() for ch in line):
                return line[:60]
    except (OSError, subprocess.SubprocessError):
        pass
    return "version unknown"


def detect_extractors() -> List[ExtractorTool]:
    """Find the archive tools installed on this machine, best first."""
    global _extractor_cache
    if _extractor_cache is not None:
        return _extractor_cache

    found: List[ExtractorTool] = []
    for key, candidates, probe in _EXTERNAL_EXTRACTORS:
        for exe in candidates:
            path = shutil.which(exe)
            if path:
                found.append(ExtractorTool(key, f"{exe} ({_probe_version(path, probe)})", path))
                break
    found.append(ExtractorTool("python", f"python zipfile (stdlib {sys.version_info.major}.{sys.version_info.minor})"))
    _extractor_cache = found
    return found


def choose_extractor(preference: str = "auto") -> ExtractorTool:
    """Pick the extractor to use, honouring an explicit --extractor choice."""
    available = detect_extractors()
    if preference != "auto":
        for tool in available:
            if tool.key == preference:
                return tool
        raise FatalError(
            f"requested extractor '{preference}' is not installed on this system.\n"
            f"  Available: {', '.join(t.key for t in available)}\n"
            f"  Fix: install it (e.g. sudo apt install p7zip-full), or use --extractor auto"
        )
    return available[0]


def _count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())


def audit_extracted_tree(dest: Path, log: RunLog) -> int:
    """Remove symlinks that point outside the extraction directory.

    External tools recreate symlinks from an archive; a hostile archive can use
    one to redirect later writes onto a file elsewhere on disk. Python's
    zipfile does not recreate them, so this matters mainly for 7z/unzip/bsdtar.
    """
    removed = 0
    dest_resolved = dest.resolve()
    for path in dest.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            target = (path.parent / os.readlink(path)).resolve()
        except OSError:
            target = None
        if target is None or not str(target).startswith(str(dest_resolved)):
            try:
                path.unlink()
                removed += 1
                log.warn(f"removed symlink escaping the extraction folder: {path}")
            except OSError as exc:
                log.error(f"could not remove unsafe symlink {path}", exc)
    return removed


def _extract_with_python(zip_path: Path, dest: Path, log: RunLog) -> int:
    """Built-in reader. Refuses path-traversal entries member by member."""
    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        dest_resolved = dest.resolve()
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest_resolved)):
                log.warn(f"refused unsafe zip entry: {member.filename}")
                continue
            zf.extract(member, dest)
            extracted += 1
    return extracted


def _external_command(tool: ExtractorTool, zip_path: Path, dest: Path) -> List[str]:
    exe = tool.executable
    if tool.key == "7z":
        # -y assume yes, -bso0/-bsp0 silence, -p'' so an encrypted archive
        # fails immediately instead of blocking on a password prompt.
        return [exe, "x", "-y", "-bso0", "-bsp0", "-p", f"-o{dest}", "--", str(zip_path)]
    if tool.key == "unzip":
        # -qq quiet, -o overwrite, -P '' so encrypted archives fail rather than
        # hang waiting for input. Paths are absolute, so no leading-dash risk.
        return [exe, "-qq", "-o", "-P", "", str(zip_path), "-d", str(dest)]
    if tool.key == "bsdtar":
        return [exe, "-xf", str(zip_path), "-C", str(dest)]
    raise FatalError(f"no extraction command defined for '{tool.key}'")


def extract_archive(
    zip_path: Path, dest: Path, log: RunLog, ui: UI, tool: Optional[ExtractorTool] = None
) -> Tuple[int, ExtractorTool]:
    """Unpack an archive, preferring a system tool and falling back to Python.

    Returns (files extracted, tool actually used).
    """
    dest.mkdir(parents=True, exist_ok=True)
    tool = tool or choose_extractor()

    if tool.is_external:
        cmd = _external_command(tool, zip_path, dest)
        log.info(f"extracting with {tool.label}: {' '.join(cmd)}")
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
            )
            # unzip returns 1 for warnings (e.g. skipped entries) but still
            # extracts; anything above that is a genuine failure.
            acceptable = (0, 1) if tool.key == "unzip" else (0,)
            if proc.returncode in acceptable:
                if proc.returncode != 0:
                    log.warn(f"{tool.key} reported warnings: {proc.stderr.strip()[:300]}")
                removed = audit_extracted_tree(dest, log)
                if removed:
                    ui.print(
                        f"  removed {removed} unsafe symlink(s) from the extracted files",
                        "warn",
                    )
                return _count_files(dest), tool
            log.warn(
                f"{tool.key} exited with {proc.returncode}: {proc.stderr.strip()[:300]} "
                f"-- falling back to the built-in reader"
            )
            ui.print(
                f"  {tool.key} could not extract this archive "
                f"(exit {proc.returncode}); falling back to Python's reader.",
                "warn",
            )
        except subprocess.TimeoutExpired:
            log.warn(f"{tool.key} timed out -- falling back to the built-in reader")
            ui.print(f"  {tool.key} timed out; falling back to Python's reader.", "warn")
        except OSError as exc:
            log.error(f"{tool.key} could not be run -- falling back", exc)
            ui.print(f"  {tool.key} could not be run; falling back to Python's reader.", "warn")

    fallback = ExtractorTool("python", "python zipfile (stdlib)")
    log.info(f"extracting with {fallback.label}")
    return _extract_with_python(zip_path, dest, log), fallback


def safe_extract(zip_path: Path, dest: Path, log: RunLog) -> int:
    """Backwards-compatible wrapper around the built-in reader."""
    return _extract_with_python(zip_path, dest, log)


def expand_nested_zips(
    root: Path, log: RunLog, ui: UI, tool: Optional[ExtractorTool] = None, max_depth: int = 3
) -> int:
    """Spec 4 -- recursively extract zips found inside the tree."""
    total = 0
    for depth in range(max_depth):
        found = [
            p for p in root.rglob("*.zip")
            if p.is_file() and not p.with_suffix("").is_dir() and not is_junk(p)
        ]
        if not found:
            break
        for zp in found:
            target = zp.with_suffix("")
            try:
                target.mkdir(parents=True, exist_ok=True)
                n, _used = extract_archive(zp, target, log, ui, tool)
                total += n
                log.info(f"nested zip extracted (depth {depth + 1}): {zp} -> {target} ({n} entries)")
                zp.unlink()
            except (zipfile.BadZipFile, OSError) as exc:
                log.error(f"nested zip extraction failed: {zp}", exc)
                ui.print(f"  could not extract nested zip {zp.name}: {exc}", "warn")
    return total


# --------------------------------------------------------------------------
# Output helpers (spec 18, 22)
# --------------------------------------------------------------------------


def output_path_for(rel_path: Path, output_root: Path) -> Path:
    return output_root / rel_path.with_suffix(".md")


def verify_output_writable(output_root: Path) -> None:
    """Spec 49 -- fail fast rather than after converting hundreds of files."""
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        probe = output_root / ".mdbatch_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise FatalError(
            f"output directory is not writable: {output_root}\n  {exc}\n"
            f"  Fix: choose another path with -o, or adjust permissions."
        ) from exc


def write_index(results: List[FileResult], output_root: Path, source_label: str) -> Optional[Path]:
    """Spec 22."""
    converted = [r for r in results if r.ok and r.output]
    if not converted:
        return None

    by_dir: Dict[str, List[FileResult]] = {}
    for r in sorted(converted, key=lambda x: x.rel_path.as_posix()):
        by_dir.setdefault(r.rel_path.parent.as_posix(), []).append(r)

    lines = [
        "# Converted documents",
        "",
        f"Source: `{source_label}`  ",
        f"Generated: {_dt.datetime.now().isoformat(timespec='seconds')} "
        f"by {SCRIPT_NAME} {SCRIPT_VERSION}  ",
        f"Documents: {len(converted)}",
        "",
    ]
    for folder in sorted(by_dir):
        lines.append(f"## {'(root)' if folder in ('.', '') else folder}")
        lines.append("")
        for r in by_dir[folder]:
            rel = r.output.relative_to(output_root).as_posix()
            grade = f" — {r.quality_grade} ({r.quality_score}/100)" if r.quality_score is not None else ""
            lines.append(f"- [{r.output.stem}]({rel}){grade}")
        lines.append("")

    index = output_root / "index.md"
    index.write_text("\n".join(lines), encoding="utf-8")
    return index


# --------------------------------------------------------------------------
# Reporting (spec 54-60, 66)
# --------------------------------------------------------------------------


def group_failures(results: List[FileResult]) -> Dict[str, List[FileResult]]:
    groups: Dict[str, List[FileResult]] = {}
    for r in results:
        if r.ok or r.status == STATUS_SKIPPED:
            continue
        key = r.fix or r.reason or r.status
        groups.setdefault(key, []).append(r)
    return dict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def print_summary(
    ui: UI,
    results: List[FileResult],
    scan: ScanResult,
    elapsed: float,
    output_root: Path,
    interrupted: bool = False,
) -> None:
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    ui.print()
    ui.rule("Summary" + (" (INTERRUPTED)" if interrupted else ""))

    total = len(results)
    ok = counts.get(STATUS_CONVERTED, 0) + counts.get(STATUS_UNCHANGED, 0)
    ui.print(
        f"Processed {total} file(s) in {human_time(elapsed)}"
        + (f"  ({total / elapsed:.1f} files/s)" if elapsed > 0 and total else ""),
        "bold",
    )
    order = [
        (STATUS_CONVERTED, "ok"), (STATUS_UNCHANGED, "dim"), (STATUS_SKIPPED, "dim"),
        (STATUS_DENIED, "warn"), (STATUS_ENCRYPTED, "warn"),
        (STATUS_TIMEOUT, "warn"), (STATUS_FAILED, "err"),
    ]
    for status, style in order:
        n = counts.get(status, 0)
        if n:
            ui.print(f"  {status:<18} {n}", style)

    # --- quality (spec 59) ------------------------------------------------
    graded = [r for r in results if r.quality_score is not None]
    if graded:
        grades: Dict[str, int] = {}
        for r in graded:
            grades[r.quality_grade] = grades.get(r.quality_grade, 0) + 1
        avg = sum(r.quality_score for r in graded) / len(graded)
        ui.print()
        ui.print(
            f"Output quality: average {avg:.0f}/100  "
            f"(good {grades.get('good', 0)}, suspect {grades.get('suspect', 0)}, "
            f"poor {grades.get('poor', 0)})",
            "bold",
        )
        flagged = sorted(
            [r for r in graded if r.quality_grade in ("poor", "suspect")],
            key=lambda r: r.quality_score,
        )
        for r in flagged[:12]:
            style = "err" if r.quality_grade == "poor" else "warn"
            ui.print(f"  {r.rel_path}  [{r.quality_score}/100 {r.quality_grade}]", style)
            if r.diagnosis:
                ui.print(f"      {r.diagnosis}", "dim")
            if r.fix:
                ui.print(f"      fix: {r.fix}", "dim")
        if len(flagged) > 12:
            ui.print(f"  ... and {len(flagged) - 12} more (see the report file)", "dim")

    # --- converted list (spec 56) ------------------------------------------
    converted = [r for r in results if r.status == STATUS_CONVERTED]
    if converted and ui.verbose:
        ui.print()
        ui.print("Converted:", "bold")
        for r in converted:
            ui.print(f"  {r.rel_path}  ->  {r.output.relative_to(output_root)}", "ok")

    # --- failures grouped by fix (spec 57-58) ------------------------------
    groups = group_failures(results)
    if groups:
        ui.print()
        ui.print("Problems:", "bold")
        for key, items in groups.items():
            head = items[0]
            ui.print(f"  {len(items)} file(s) -- {head.status}: {head.reason}", "err")
            for r in items[:5]:
                ui.print(f"      {r.rel_path}", "dim")
            if len(items) > 5:
                ui.print(f"      ... and {len(items) - 5} more", "dim")
            if head.fix:
                ui.print(f"      fix: {head.fix}", "info")

    if scan.unreadable_dirs:
        ui.print()
        ui.print(f"{len(scan.unreadable_dirs)} directory/ies could not be listed:", "warn")
        for name, reason in scan.unreadable_dirs[:5]:
            ui.print(f"  {name} ({reason})", "dim")

    ui.print()
    ui.print(f"Output: {output_root}", "ok" if ok else "warn")


def write_report(
    results: List[FileResult],
    scan: ScanResult,
    elapsed: float,
    output_root: Path,
    source_label: str,
    log_path: Optional[Path],
) -> Path:
    """Spec 60 -- a durable copy of the summary next to the output."""
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    lines = [
        "# Conversion report",
        "",
        f"- **Source**: `{source_label}`",
        f"- **Output**: `{output_root}`",
        f"- **Run at**: {_dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Elapsed**: {human_time(elapsed)}",
        f"- **Tool**: {SCRIPT_NAME} {SCRIPT_VERSION} (markitdown {markitdown_version()})",
    ]
    if log_path:
        lines.append(f"- **Log**: `{log_path}`")
    lines += ["", "## Totals", ""]
    lines.append("| Status | Count |")
    lines.append("| --- | ---: |")
    for status in (
        STATUS_CONVERTED, STATUS_UNCHANGED, STATUS_SKIPPED, STATUS_DENIED,
        STATUS_ENCRYPTED, STATUS_TIMEOUT, STATUS_FAILED,
    ):
        if counts.get(status):
            lines.append(f"| {status} | {counts[status]} |")

    graded = [r for r in results if r.quality_score is not None]
    if graded:
        avg = sum(r.quality_score for r in graded) / len(graded)
        lines += ["", "## Quality", "", f"Average score: **{avg:.0f}/100** across {len(graded)} document(s).", ""]
        lines.append("| File | Score | Grade | Diagnosis |")
        lines.append("| --- | ---: | --- | --- |")
        for r in sorted(graded, key=lambda x: x.quality_score):
            lines.append(
                f"| `{r.rel_path.as_posix()}` | {r.quality_score} | {r.quality_grade} | "
                f"{r.diagnosis or '-'} |"
            )

    converted = [r for r in results if r.status == STATUS_CONVERTED]
    if converted:
        lines += ["", "## Converted", ""]
        for r in sorted(converted, key=lambda x: x.rel_path.as_posix()):
            lines.append(
                f"- `{r.rel_path.as_posix()}` -> `{r.output.relative_to(output_root).as_posix()}`"
                f" ({human_time(r.duration)})"
            )

    problems = [r for r in results if not r.ok and r.status != STATUS_SKIPPED]
    if problems:
        lines += ["", "## Problems", ""]
        lines.append("| File | Status | Reason | Suggested fix |")
        lines.append("| --- | --- | --- | --- |")
        for r in sorted(problems, key=lambda x: (x.status, x.rel_path.as_posix())):
            lines.append(
                f"| `{r.rel_path.as_posix()}` | {r.status} | {r.reason or '-'} | {r.fix or '-'} |"
            )

    if scan.unreadable_dirs:
        lines += ["", "## Directories that could not be listed", ""]
        for name, reason in scan.unreadable_dirs:
            lines.append(f"- `{name}` -- {reason}")

    cleanup = [r for r in results if r.cleanup_actions]
    if cleanup:
        lines += ["", "## Cleanup actions", ""]
        for r in cleanup:
            lines.append(f"### `{r.rel_path.as_posix()}`")
            lines.append("")
            for action in r.cleanup_actions:
                lines.append(f"- {action}")
            lines.append("")

    report = output_root / "_conversion_report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def write_manifest(
    results: List[FileResult], output_root: Path, scan_root: Path, source_label: str
) -> Optional[Path]:
    """Spec 66 -- failures only, for --retry-failed.

    scan_root is recorded so a retry reproduces the same relative paths; without
    it the mirrored folder prefix would be lost on the retried files.
    """
    failures = [
        r for r in results
        if r.status in (STATUS_FAILED, STATUS_TIMEOUT, STATUS_DENIED, STATUS_ENCRYPTED)
    ]
    if not failures:
        return None
    payload = {
        "tool": SCRIPT_NAME,
        "version": SCRIPT_VERSION,
        "created": _dt.datetime.now().isoformat(timespec="seconds"),
        "output_root": str(output_root),
        "scan_root": str(scan_root),
        "source_label": source_label,
        "files": [
            {
                "source": str(r.source),
                "rel_path": r.rel_path.as_posix(),
                "status": r.status,
                "reason": r.reason,
                "fix": r.fix,
            }
            for r in failures
        ],
    }
    path = output_root / "_failed_manifest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description=(
            "Batch-convert documents to Markdown with MarkItDown, mirroring the "
            "input folder structure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s reports.zip\n"
            "  %(prog)s ~/Documents/contracts -o ~/md-out --verbose\n"
            "  %(prog)s ~/Documents --dry-run\n"
            "  %(prog)s --retry-failed md-out/_failed_manifest.json\n"
        ),
    )
    p.add_argument("input", nargs="?", help="zip file, folder, or single file to convert")
    p.add_argument("-o", "--output", help="output folder (default: <input>_markdown)")

    p.add_argument("-v", "--verbose", action="store_true", help="per-file progress lines")
    p.add_argument("--no-color", action="store_true", help="disable coloured output")
    p.add_argument("-y", "--yes", action="store_true", help="answer yes to all prompts")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")
    p.add_argument("--version", action="store_true", help="print versions and exit")
    p.add_argument("--doctor", action="store_true",
                   help="check the environment, offer to install what is missing, and exit")
    p.add_argument("--no-install", action="store_true",
                   help="never offer to install anything; just report what is missing")
    p.add_argument("--allow-system-install", action="store_true",
                   help="permit installing into an externally managed system Python "
                        "(adds pip --break-system-packages; a venv is safer)")

    p.add_argument("--force", action="store_true", help="re-convert files even if output is newer")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="per-file timeout in seconds (0 disables; default: 120)")
    p.add_argument("--keep-extracted", action="store_true",
                   help="keep the temporary zip extraction folder")
    p.add_argument("--recursive-zip", action="store_true",
                   help="also extract zips found inside the tree")
    p.add_argument("--extractor", choices=["auto", "7z", "unzip", "bsdtar", "python"],
                   default="auto",
                   help="which unzip tool to use (default: auto -- prefers an "
                        "installed system tool, falls back to python)")
    p.add_argument("--plugins", action="store_true", help="enable markitdown 3rd-party plugins")

    p.add_argument("--no-clean", dest="clean", action="store_false", default=True,
                   help="skip the markdown cleanup pass")
    p.add_argument("--keep-raw", action="store_true",
                   help="also write the uncleaned output as <name>.raw.md")

    g = p.add_mutually_exclusive_group()
    g.add_argument("--sudo", dest="sudo_mode", action="store_const", const="always",
                   help="elevate for unreadable files without asking")
    g.add_argument("--no-sudo", dest="sudo_mode", action="store_const", const="never",
                   help="never attempt privilege escalation")
    p.set_defaults(sudo_mode="ask")

    p.add_argument("--retry-failed", metavar="MANIFEST",
                   help="re-run only the files listed in a previous _failed_manifest.json")
    p.add_argument("--log", help="log file path (default: <output>/_run_<timestamp>.log)")
    return p


def resolve_output_root(args, input_path: Path) -> Path:
    if args.output:
        return Path(args.output).expanduser().resolve()
    base = input_path.stem if input_path.is_file() else input_path.name
    return (input_path.parent / f"{base}_markdown").resolve()


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------


def run(args) -> int:
    ui = UI(color=not args.no_color, verbose=args.verbose)

    if args.version:
        print(version_banner())
        return EXIT_OK
    if args.doctor:
        return run_doctor(args, ui, RunLog(None))
    if not args.input and not args.retry_failed:
        build_parser().print_usage()
        sys.stderr.write(f"{SCRIPT_NAME}: an input path is required\n")
        return EXIT_FATAL

    ui.rule(f"{SCRIPT_NAME} {SCRIPT_VERSION}")

    # ---- retry mode --------------------------------------------------------
    retry_paths: Optional[List[Path]] = None
    if args.retry_failed:
        manifest = Path(args.retry_failed).expanduser()
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            retry_paths = [Path(f["source"]) for f in data.get("files", [])]
        except (OSError, ValueError, KeyError) as exc:
            raise FatalError(f"could not read manifest {manifest}: {exc}") from exc
        if not retry_paths:
            raise FatalError(f"manifest {manifest} lists no files to retry")

        if not args.input:
            # Reuse the original scan root so relative paths -- and therefore
            # the mirrored output layout -- come out identical to the first run.
            recorded = data.get("scan_root")
            if recorded and Path(recorded).is_dir():
                args.input = recorded
            else:
                if recorded:
                    ui.print(
                        f"Original scan root is gone ({recorded}); "
                        f"output paths may be flattened.",
                        "warn",
                    )
                    if data.get("source_label"):
                        ui.print(
                            f"  Re-run against the original source instead: "
                            f"{data['source_label']}",
                            "dim",
                        )
                args.input = os.path.commonpath([str(p.parent) for p in retry_paths])
        if not args.output:
            args.output = data.get("output_root") or None
        ui.print(f"Retry mode: {len(retry_paths)} file(s) from {manifest}", "info")

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise FatalError(f"input path does not exist: {input_path}")

    output_root = resolve_output_root(args, input_path)

    log_path = Path(args.log).expanduser() if args.log else (
        output_root / f"_run_{_dt.datetime.now():%Y-%m-%d_%H%M%S}.log"
    )
    if not args.dry_run:
        verify_output_writable(output_root)          # spec 49
    log = RunLog(None if args.dry_run else log_path)
    log.info(f"command: {' '.join(sys.argv)}")
    log.info(f"input={input_path} output={output_root}")

    try:
        check_environment(args, ui, log)             # spec 62

        if hasattr(os, "geteuid") and os.geteuid() == 0:   # spec 48
            ui.print(
                "WARNING: running as root -- every output file will be owned by root.\n"
                "         Prefer fixing source permissions, or chown the output afterwards.",
                "warn",
            )
            log.warn("running as root")

        # ---- resolve the scan root (zip vs folder vs file) ----------------
        temp_dir: Optional[Path] = None
        scan_root = input_path
        source_label = str(input_path)
        single_file: Optional[Path] = None

        if input_path.is_file():
            # Spec 2: detect by content, not just the extension -- but a .docx
            # or .xlsx is also a valid zip, and those are documents to convert,
            # not archives to unpack.
            treat_as_archive = (
                zipfile.is_zipfile(input_path)
                and input_path.suffix.lower() not in ZIP_DOCUMENT_EXTENSIONS
            )
            if treat_as_archive:
                size = human_bytes(input_path.stat().st_size)
                ui.print()
                ui.print(f"Found zip file: {input_path.name} ({size})", "info")
                try:
                    with zipfile.ZipFile(input_path) as zf:
                        members = [m for m in zf.infolist() if not m.is_dir()]
                    ui.print(f"  {len(members)} entries inside.", "dim")
                except zipfile.BadZipFile as exc:
                    raise FatalError(f"zip archive is damaged: {exc}") from exc

                # Prefer a system unzip tool when one is installed -- it copes
                # with archive variants the stdlib reader cannot read.
                extractor = choose_extractor(args.extractor)
                others = [t.key for t in detect_extractors() if t.key != extractor.key]
                ui.print(f"  Extracting with: {extractor.label}", "dim")
                if others:
                    ui.print(f"  (also available: {', '.join(others)} -- see --extractor)", "dim")

                if not ui.confirm("Proceed with extraction?", assume_yes=args.yes):  # spec 9
                    ui.print("Aborted -- nothing was extracted or written.", "warn")
                    return EXIT_OK

                temp_dir = Path(tempfile.mkdtemp(prefix="mdbatch_"))
                n, used = extract_archive(input_path, temp_dir, log, ui, extractor)
                ui.print(f"Extracted {n} file(s) to {temp_dir} using {used.key}", "ok")
                log.info(f"extracted {n} files from {input_path} to {temp_dir} via {used.label}")
                scan_root = temp_dir
                if args.recursive_zip:                # spec 4
                    extra = expand_nested_zips(temp_dir, log, ui, used)
                    if extra:
                        ui.print(f"Expanded nested zips ({extra} additional file(s)).", "ok")
            else:
                single_file = input_path
                scan_root = input_path.parent
        elif args.recursive_zip:
            expand_nested_zips(scan_root, log, ui, choose_extractor(args.extractor))

        # ---- scan ---------------------------------------------------------
        ui.print()
        ui.print("Scanning...", "dim")
        if single_file is not None:
            size = single_file.stat().st_size
            scan = ScanResult(root=scan_root)
            scan.files.append(
                ScanEntry(single_file, Path(single_file.name), size,
                          os.access(single_file, os.R_OK))
            )
        else:
            scan = scan_tree(scan_root, log)          # spec 3, 40

        if retry_paths is not None:
            wanted = {p.resolve() for p in retry_paths}
            scan.files = [e for e in scan.files if e.path.resolve() in wanted]

        if not scan.files:
            ui.print("No convertible files found.", "warn")
            if scan.unsupported:
                ui.print(f"  ({len(scan.unsupported)} unsupported file(s) ignored)", "dim")
            if scan.unreadable_dirs:
                ui.print(f"  ({len(scan.unreadable_dirs)} directory/ies could not be listed)", "warn")
            return EXIT_OK

        # ---- pre-flight report (spec 8, 10, 11) ---------------------------
        by_ext = scan.by_extension()
        breakdown = ", ".join(f"{n} {ext}" for ext, n in list(by_ext.items())[:8])
        folders = len({e.rel_path.parent for e in scan.files})
        est = scan.estimated_seconds()

        ui.print()
        ui.print(
            f"Found {len(scan.files)} convertible file(s) ({breakdown}) "
            f"across {folders} folder(s)",
            "bold",
        )
        ui.print(f"  total size      : {human_bytes(scan.total_bytes)}")
        ui.print(f"  estimated time  : {human_time(est * 0.5)} - {human_time(est * 2)}", "dim")
        if scan.unsupported:
            ui.print(f"  unsupported     : {len(scan.unsupported)} file(s) will be ignored", "dim")
        if scan.junk_count:
            ui.print(f"  junk ignored    : {scan.junk_count}", "dim")
        if scan.denied:
            ui.print(f"  unreadable      : {len(scan.denied)} file(s) (permission denied)", "warn")
        if scan.unreadable_dirs:
            ui.print(
                f"  unlistable dirs : {len(scan.unreadable_dirs)} "
                f"-- their contents are NOT included",
                "warn",
            )

        missing = missing_extras_for(by_ext.keys())   # spec 63
        for pip_name, fix, exts in missing:
            ui.print(
                f"  WARNING: {', '.join(exts)} file(s) found but support is not installed "
                f"-- {fix}",
                "warn",
            )
            log.warn(f"missing extra {pip_name} for {exts}")
        if missing:
            # Offer to fix it here rather than letting every file of that type
            # fail and making the user re-run.
            if offer_extras_install(missing, args, ui, log):
                still = missing_extras_for(by_ext.keys())
                if still:
                    ui.print(
                        "  Some format support is still missing after install; "
                        "those files may fail.",
                        "warn",
                    )
                else:
                    ui.print("  Format support installed.", "ok")

        # ---- dry run (spec 64) --------------------------------------------
        if args.dry_run:
            ui.print()
            ui.rule("Dry run -- nothing will be written")
            for entry in scan.files[:200]:
                out = output_path_for(entry.rel_path, output_root)
                mark = "     " if entry.readable else "DENY "
                ui.print(f"  {mark}{entry.rel_path}  ->  {out.relative_to(output_root)}", "dim")
            if len(scan.files) > 200:
                ui.print(f"  ... and {len(scan.files) - 200} more", "dim")
            ui.print()
            ui.print(f"Would write into: {output_root}", "info")
            return EXIT_OK

        if not ui.confirm("Proceed with conversion?", assume_yes=args.yes):
            ui.print("Aborted -- nothing was written.", "warn")
            return EXIT_OK

        # ---- elevation (spec 45-47) ---------------------------------------
        elevated: Dict[Path, Path] = {}
        staging_dir: Optional[Path] = None
        if scan.denied:
            staging_dir = Path(tempfile.mkdtemp(prefix="mdbatch_elevated_"))
            elevated = elevate_denied_files(
                scan.denied, staging_dir, ui, log, args.yes, args.sudo_mode
            )

        # ---- convert ------------------------------------------------------
        converter = Converter(args, log)
        results: List[FileResult] = []
        started = time.monotonic()
        interrupted = False

        ui.print()
        try:
            with ui.progress(len(scan.files), "Converting") as advance:
                for entry in scan.files:
                    read_path = elevated.get(entry.path, entry.path)
                    if not entry.readable and entry.path not in elevated:
                        results.append(
                            FileResult(
                                source=entry.path, rel_path=entry.rel_path,
                                status=STATUS_DENIED,
                                reason=f"permission denied ({entry.denial})",
                                fix=f"sudo chown $USER '{entry.path}'  --or re-run with --sudo",
                            )
                        )
                        advance()
                        continue

                    out_path = output_path_for(entry.rel_path, output_root)
                    if args.verbose:
                        ui.print(f"  [{len(results) + 1}/{len(scan.files)}] {entry.rel_path}", "dim")

                    res = converter.convert_one(entry, read_path, out_path)
                    results.append(res)

                    if args.verbose:
                        if res.ok:
                            q = f" [{res.quality_score}/100 {res.quality_grade}]" if res.quality_score is not None else ""
                            ui.print(f"      OK {human_time(res.duration)}{q}", "ok")
                        else:
                            ui.print(f"      {res.status}: {res.reason}", "err")
                    advance()
        except KeyboardInterrupt:                    # spec 53
            interrupted = True
            ui.print()
            ui.print("Interrupted -- reporting what completed so far.", "warn")
            log.warn("interrupted by user (SIGINT)")

        elapsed = time.monotonic() - started

        # ---- outputs -------------------------------------------------------
        index = write_index(results, output_root, source_label)      # spec 22
        report = write_report(results, scan, elapsed, output_root, source_label, log_path)
        manifest_path = write_manifest(                              # spec 66
            results, output_root, scan_root, source_label
        )

        print_summary(ui, results, scan, elapsed, output_root, interrupted)
        ui.print(f"Report: {report}", "dim")
        if index:
            ui.print(f"Index:  {index}", "dim")
        if manifest_path:
            ui.print(f"Retry:  {SCRIPT_NAME} --retry-failed {manifest_path}", "info")
        ui.print(f"Log:    {log_path}", "dim")

        # ---- cleanup -------------------------------------------------------
        if temp_dir is not None:
            if args.keep_extracted:
                ui.print(f"Extracted files kept at: {temp_dir}", "dim")
            else:
                shutil.rmtree(temp_dir, ignore_errors=True)
                log.info(f"removed temp extraction dir {temp_dir}")
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
            log.info(f"removed elevated staging dir {staging_dir}")

        failed = [r for r in results if not r.ok and r.status != STATUS_SKIPPED]
        if interrupted:
            return EXIT_PARTIAL
        return EXIT_PARTIAL if failed else EXIT_OK

    finally:
        log.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except FatalError as exc:
        sys.stderr.write(f"\nFATAL: {exc}\n")
        return EXIT_FATAL
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted before conversion started.\n")
        return EXIT_PARTIAL
    except Exception as exc:  # last-resort net so users always get a traceback
        sys.stderr.write(f"\nUNEXPECTED ERROR: {type(exc).__name__}: {exc}\n\n")
        traceback.print_exc()
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
