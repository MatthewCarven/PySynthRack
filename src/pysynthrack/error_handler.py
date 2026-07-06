"""
Python ErrorHandler - one function to surface everything knowable about an exception.

Usage inside an except clause:

    from error_handler import describe_error

    try:
        risky()
    except Exception as e:
        report = describe_error(e)
        log.error(report)                    # uses .to_string() via __str__
        log.error(report.for_claude())       # heavy / LLM-friendly edition (stub)
        send_to_metrics(report.to_dict())    # structured

Or wire the global uncaught-error hooks once and skip the boilerplate:

    import error_handler
    error_handler.install()    # sys.excepthook / threading / unraisable
    # ... error_handler.uninstall() restores the prior hooks

Design contract: this function NEVER raises. If introspection of the exception
fails partway through, the returned report records the partial failure in
`partial_failures` and carries on. If the handler itself collapses entirely
(e.g. MemoryError mid-walk), the report falls back to the most primitive
description possible: repr(exc) and type(exc).__name__.
"""

from __future__ import annotations

import contextvars
import difflib
import functools
import inspect
import itertools
import json
import linecache
import logging
import os
import platform
import re
import socket
import subprocess
import sys
import sysconfig
import threading
import time
from datetime import datetime, timezone

try:
    import ssl
except ImportError:  # pragma: no cover — rare builds without OpenSSL
    ssl = None
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional


# ---------------------------------------------------------------------------
# Safety primitives
# ---------------------------------------------------------------------------

_REPR_MAX_LEN_DEFAULT = 200


def _safe_capture(label, fn, default, failures):
    """Run fn() and return its result. If it raises, record the failure in
    `failures` and return `default`. Single chokepoint for all introspection."""
    try:
        return fn()
    except BaseException as inner:
        try:
            failures.append({"step": label, "error": repr(inner)})
        except BaseException:
            pass
        return default


def _safe_repr(value, max_len=_REPR_MAX_LEN_DEFAULT):
    """repr(value) that survives broken __repr__, applies active redactors,
    then truncates long output. Redaction runs BEFORE truncation so a long
    secret can't get partially exposed by the truncation cut."""
    try:
        s = repr(value)
    except BaseException:
        try:
            return "<unrepresentable: " + type(value).__name__ + ">"
        except BaseException:
            return "<unrepresentable>"
    s = _redact(s)
    if len(s) > max_len:
        return s[:max_len] + "... [truncated, full len=" + str(len(s)) + "]"
    return s


# ---------------------------------------------------------------------------
# Redaction hooks
# ---------------------------------------------------------------------------
#
# A redactor is a callable: str -> str. Registered redactors run on every
# string the handler captures (locals, args, source lines, source context,
# exception messages, notes). Designed so that `include_locals=True` can be
# used in production without leaking secrets that happen to live in frame
# locals or hardcoded source.
#
# Active redactor state lives in a ContextVar so concurrent describe_error
# calls (threads / asyncio tasks) don't stomp on each other's lists. The
# module-level _DEFAULT_REDACTORS is the registry used when describe_error
# is called without an explicit `redactors=` argument.
#
# Every redactor call is individually try/except'd so a broken redactor
# falls back to the un-redacted string rather than breaking the report.

_DEFAULT_REDACTORS: List[Callable[[str], str]] = []
_active_redactors: contextvars.ContextVar = contextvars.ContextVar(
    "error_handler_active_redactors", default=()
)


def register_redactor(fn: Callable[[str], str]) -> Callable[[str], str]:
    """Add a redactor (str -> str) to the global default list. Returns the
    function for decorator-style use:

        @register_redactor
        def hide_my_secret(s):
            return s.replace("hunter2", "<redacted>")
    """
    _DEFAULT_REDACTORS.append(fn)
    return fn


def clear_redactors() -> None:
    """Empty the global default redactor list. Mostly useful in tests."""
    _DEFAULT_REDACTORS.clear()


def redact_pattern(
    pattern, replacement: str = "<redacted>", flags: int = 0,
) -> Callable[[str], str]:
    """Helper: turn a regex into a registered-ready redactor.

        register_redactor(redact_pattern(r"sk-[A-Za-z0-9]{20,}"))
        register_redactor(redact_pattern(r"password=\\S+", "password=<redacted>"))

    `pattern` is a string or a pre-compiled re.Pattern. Compilation failures
    return a no-op redactor (so a bad pattern can't break the registry call
    site). All matches are replaced.
    """
    try:
        compiled = pattern if hasattr(pattern, "sub") else re.compile(pattern, flags)
    except BaseException:
        return lambda s: s
    def redactor(s):
        try:
            return compiled.sub(replacement, s)
        except BaseException:
            return s
    return redactor


# ---------------------------------------------------------------------------
# Observer hooks (task 18)
# ---------------------------------------------------------------------------
#
# An observer is a callable: ErrorReport -> None. Every report built by
# describe_error() — including the error_handler_failed fallback, but NOT
# the no-active-exception marker — fires each registered observer with the
# finished report. This is the log-pipeline / metrics integration point:
# register once, and every entry path (install() hooks, @capture,
# capturing(), direct describe_error calls) feeds it automatically.
#
# Safety mirrors the redactor registry: each observer call individually
# try/except'd, failures silently swallowed, the report never withheld.
# One addition: a ContextVar reentrancy guard — an observer that itself
# calls describe_error() won't re-fire the observer list, so a
# misbehaving observer can't recurse the module to death.

_OBSERVERS: List[Callable[["ErrorReport"], None]] = []
_notifying_observers: contextvars.ContextVar = contextvars.ContextVar(
    "error_handler_notifying_observers", default=False
)


def register_observer(fn: Callable[["ErrorReport"], None]) -> Callable:
    """Add an observer (ErrorReport -> None) fired for every report built.
    Returns the function for decorator-style use:

        @register_observer
        def ship_to_metrics(report):
            metrics.send(report.to_dict())
    """
    _OBSERVERS.append(fn)
    return fn


def unregister_observer(fn) -> bool:
    """Remove a previously registered observer. Returns True if it was
    present, False otherwise. Never raises."""
    try:
        _OBSERVERS.remove(fn)
        return True
    except ValueError:
        return False


def clear_observers() -> None:
    """Empty the observer list. Mostly useful in tests."""
    _OBSERVERS.clear()


def _notify_observers(report):
    """Fire each observer with the finished report. Reentrancy-guarded:
    describe_error calls made INSIDE an observer build reports normally
    but do not re-fire the observer list. Never raises."""
    try:
        if _notifying_observers.get():
            return
        token = _notifying_observers.set(True)
        try:
            for obs in tuple(_OBSERVERS):
                try:
                    obs(report)
                except BaseException:
                    pass
        finally:
            _notifying_observers.reset(token)
    except BaseException:
        pass


def _redact(s):
    """Apply the active redactors in order. Each call individually wrapped
    so a broken redactor falls back to the prior value, never raises."""
    if not isinstance(s, str):
        return s
    redactors = _active_redactors.get()
    if not redactors:
        return s
    for r in redactors:
        try:
            s = r(s)
        except BaseException:
            pass
    return s


# ---------------------------------------------------------------------------
# Environment snapshot
# ---------------------------------------------------------------------------

# Task 24: fallback basis for uptime when the OS can't give real process
# elapsed time (Windows os.times().elapsed is 0). For apps that import
# error_handler at startup, since-import ≈ process uptime anyway.
_MODULE_LOAD_MONOTONIC = time.monotonic()


def _capture_uptime():
    """Returns (uptime_seconds, basis). basis "process" = real time since
    process start: /proc/self/stat starttime on Linux, GetProcessTimes
    via ctypes on Windows. Anywhere else (or on any failure) falls back
    to "module_import" — monotonic seconds since this module loaded. The
    basis field keeps the number honest about what it measures.

    NB deliberately NOT os.times().elapsed: POSIX times(2) counts from an
    arbitrary fixed point (system boot), not process start — it reported
    a 49-day "uptime" for a 0.1s process when we tried it."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            handle = k32.GetCurrentProcess()
            creation = wintypes.FILETIME()
            exit_t = wintypes.FILETIME()
            kernel_t = wintypes.FILETIME()
            user_t = wintypes.FILETIME()
            ok = k32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_t),
                ctypes.byref(kernel_t), ctypes.byref(user_t),
            )
            if ok:
                ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
                # FILETIME = 100ns units since 1601-01-01 UTC; 11644473600s
                # separates the 1601 and 1970 epochs.
                created_unix = ticks / 1e7 - 11644473600.0
                up = time.time() - created_unix
                if up >= 0:
                    return round(up, 3), "process"
        elif os.path.exists("/proc/self/stat"):
            with open("/proc/self/stat", "rb") as f:
                stat = f.read().decode("ascii", "replace")
            # comm (field 2) may contain spaces/parens — split after the
            # LAST ')'. Fields after it start at field 3; starttime is
            # field 22 overall → index 19 in the remainder.
            after = stat.rsplit(")", 1)[1].split()
            starttime_ticks = float(after[19])
            clk = float(os.sysconf("SC_CLK_TCK"))
            with open("/proc/uptime", "rb") as f:
                sys_uptime = float(f.read().split()[0])
            up = sys_uptime - (starttime_ticks / clk)
            if up >= 0:
                return round(up, 3), "process"
    except BaseException:
        pass
    return round(time.monotonic() - _MODULE_LOAD_MONOTONIC, 3), "module_import"


def _capture_environment(env_vars, failures):
    """Capture Python/platform/process basics. Safe-wrapped end-to-end - if
    any single field blows up it gets recorded in partial_failures and the
    snapshot continues with the rest.

    env_vars (iterable of names) is the only way env vars are captured;
    passing None or [] means no env vars in the snapshot (default).
    """
    env = {}
    # Task 24: capture timestamp + process uptime lead the block — the
    # first things a log reader wants for correlation.
    env["timestamp_utc"] = _safe_capture(
        "env.timestamp_utc",
        lambda: datetime.now(timezone.utc).isoformat(),
        "<unknown>", failures,
    )
    env["uptime_seconds"], env["uptime_basis"] = _safe_capture(
        "env.uptime", _capture_uptime, (None, "<unknown>"), failures,
    )
    env["python_version"] = _safe_capture(
        "env.python_version", lambda: sys.version.split("\n")[0],
        "<unknown>", failures,
    )
    env["python_implementation"] = _safe_capture(
        "env.python_implementation", platform.python_implementation,
        "<unknown>", failures,
    )
    env["platform"] = _safe_capture(
        "env.platform", platform.platform, "<unknown>", failures,
    )
    env["system"] = _safe_capture(
        "env.system", platform.system, "<unknown>", failures,
    )
    env["machine"] = _safe_capture(
        "env.machine", platform.machine, "<unknown>", failures,
    )
    env["cwd"] = _safe_capture(
        "env.cwd", os.getcwd, "<unknown>", failures,
    )
    env["pid"] = _safe_capture(
        "env.pid", os.getpid, "<unknown>", failures,
    )
    env["argv"] = _safe_capture(
        "env.argv", lambda: list(sys.argv), [], failures,
    )
    env["executable"] = _safe_capture(
        "env.executable", lambda: sys.executable, "<unknown>", failures,
    )
    if env_vars:
        captured = {}
        for name in env_vars:
            try:
                val = os.environ.get(name)
            except BaseException as inner:
                try:
                    failures.append({
                        "step": "env.var[" + str(name) + "]",
                        "error": repr(inner),
                    })
                except BaseException:
                    pass
                continue
            if val is not None:
                captured[name] = _redact(val)
        env["env_vars"] = captured
    return env


# ---------------------------------------------------------------------------
# Return object
# ---------------------------------------------------------------------------

@dataclass
class ErrorReport:
    """Result of describe_error. Stringifies to the concise human-readable form
    by default so it drops into log.error(...) and f-strings cleanly.

    Output flavors:
      to_dict()      structured, suitable for metrics pipelines
      to_string()    concise, traceback-style human format (also __str__)
      for_claude()   heavy / LLM-friendly edition (Task 9)
      to_json()      JSON string with non-serializable-value fallback (Task 19)
      to_markdown()  GitHub-issue-ready markdown with collapsible detail (Task 19)
    """
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return dict(self.data)

    def to_string(self):
        return _format_concise(self.data)

    def for_claude(self):
        return _format_heavy(self.data)

    def to_json(self, *, indent=None, sort_keys=False):
        return _format_json(self.data, indent=indent, sort_keys=sort_keys)

    def to_markdown(self):
        return _format_markdown(self.data)

    def __str__(self):
        return self.to_string()

    def __repr__(self):
        kind = self.data.get("type", "?")
        msg = self.data.get("message", "")
        return "ErrorReport(" + str(kind) + ": " + repr(msg) + ")"


# ---------------------------------------------------------------------------
# Task 2: Generic exception extractor
# ---------------------------------------------------------------------------

def _extract_notes(exc):
    """Return __notes__ as a list of strings, surviving non-iterable junk.
    Each note is run through the active redactors."""
    notes = getattr(exc, "__notes__", None)
    if notes is None:
        return []
    try:
        return [_redact(str(n)) for n in notes]
    except TypeError:
        return [_safe_repr(notes)]


def _extra_attrs(exc):
    """Non-dunder attributes off the exception's __dict__, each safe-repr'd.
    Many built-in exceptions have no __dict__ - return {} in that case."""
    out = {}
    try:
        d = vars(exc)
    except TypeError:
        return out
    for name, value in d.items():
        if name.startswith("_"):
            continue
        out[name] = _safe_repr(value)
    return out


# ---------------------------------------------------------------------------
# Task 3: Traceback walker (no locals yet - Task 6 wires in the flag)
# ---------------------------------------------------------------------------

def _walk_traceback(exc, include_locals, source_context_lines, failures):
    """Walk exc.__traceback__ linked list, oldest frame first. Each frame
    capture is wrapped so a single bad frame can't break the whole walk."""
    frames = []
    tb = getattr(exc, "__traceback__", None)
    while tb is not None:
        frame_data = _safe_capture(
            "frame",
            lambda tb=tb: _build_frame(tb, include_locals, source_context_lines, failures),
            None,
            failures,
        )
        if frame_data is not None:
            frames.append(frame_data)
        tb = tb.tb_next
    return frames


def _build_frame(tb, include_locals, source_context_lines, failures):
    """Extract a single traceback frame into a dict. Delegates to _frame_dict
    using the traceback's lineno (which can differ from frame.f_lineno when
    the frame is paused mid-call). On 3.11+ adds `col_anchors` (task 22) —
    traceback frames only; caller-context frames have no failing
    instruction to anchor."""
    out = _frame_dict(
        tb.tb_frame, tb.tb_lineno, include_locals, source_context_lines, failures
    )
    if _HAS_CO_POSITIONS:
        anchors = _safe_capture(
            "col_anchors",
            lambda: _capture_col_anchors(tb),
            None,
            failures,
        )
        if anchors:
            out["col_anchors"] = anchors
    return out


# Task 22: fine-grained error location. co_positions() appeared in 3.11
# (PEP 657); on older versions frames simply never get `col_anchors`.
_HAS_CO_POSITIONS = sys.version_info >= (3, 11)


def _byte_to_char_offset(line, byte_offset):
    """co_positions() column offsets count utf-8 BYTES of the source line;
    convert to character offsets so consumers can slice the line directly.
    Falls back to the raw value when the line is unavailable or the math
    goes sideways (then char == byte for the ASCII-only case anyway)."""
    if not line:
        return byte_offset
    try:
        return len(
            line.encode("utf-8")[:byte_offset].decode("utf-8", errors="replace")
        )
    except BaseException:
        return byte_offset


def _capture_col_anchors(tb):
    """Resolve tb_lasti through co_positions() to the failing instruction's
    exact source span — the data behind CPython 3.11+'s ~~~^^^ carets.

    Returns {lineno, end_lineno, colno, end_colno, anchor_text?} or None
    when positions aren't available (synthesized code, lasti < 0, <3.11).
    Columns are 0-based CHARACTER offsets (converted from byte offsets).
    anchor_text is the failing expression itself, single-line spans only,
    redacted like every other captured source string."""
    lasti = tb.tb_lasti
    if lasti is None or lasti < 0:
        return None
    code = tb.tb_frame.f_code
    pos = next(
        itertools.islice(code.co_positions(), lasti // 2, lasti // 2 + 1),
        None,
    )
    if pos is None:
        return None
    start_line, end_line, start_col, end_col = pos
    if start_line is None or start_col is None or end_col is None:
        return None
    filename = code.co_filename
    start_text = linecache.getline(filename, start_line).rstrip("\r\n")
    end_lineno = end_line if end_line is not None else start_line
    if end_lineno == start_line:
        end_text = start_text
    else:
        end_text = linecache.getline(filename, end_lineno).rstrip("\r\n")
    colno = _byte_to_char_offset(start_text, start_col)
    end_colno = _byte_to_char_offset(end_text, end_col)
    out = {
        "lineno": start_line,
        "end_lineno": end_lineno,
        "colno": colno,
        "end_colno": end_colno,
    }
    if end_lineno == start_line and start_text:
        snippet = start_text[colno:end_colno]
        if snippet:
            out["anchor_text"] = _redact(snippet)
    return out


# ---------------------------------------------------------------------------
# Task 23: frame origin tagging + skip_modules
# ---------------------------------------------------------------------------
#
# Every frame gets an `origin` tag: "user" / "stdlib" / "site-packages" /
# "error_handler" (our own wrapper frames — @capture and friends — are
# precisely the noise this feature exists to classify, so they get their
# own tag beyond the three in the original spec).
#
# `skip_modules=` on describe_error marks matching frames `hidden` in the
# DICT (nothing is ever dropped — the dict keeps everything); the concise
# formatter collapses runs of hidden frames into one line, while the heavy
# formatter only ANNOTATES — its contract is completeness.
#
# The active skip list rides a ContextVar exactly like redactors: zero
# signature churn through the walkers, same thread/async safety.

def _compute_origin_paths():
    """Resolve stdlib and site/dist-packages roots once, normcased for
    Windows-safe prefix comparison. Failures degrade to empty tuples
    (every frame then tags as "user" — wrong but harmless)."""
    stdlib_paths = []
    site_paths = []
    try:
        paths = sysconfig.get_paths()
        for key in ("stdlib", "platstdlib"):
            p = paths.get(key)
            if p:
                stdlib_paths.append(os.path.normcase(os.path.normpath(p)))
        for key in ("purelib", "platlib"):
            p = paths.get(key)
            if p:
                site_paths.append(os.path.normcase(os.path.normpath(p)))
    except BaseException:
        pass
    return tuple(stdlib_paths), tuple(site_paths)


_STDLIB_PATHS, _SITE_PATHS = _compute_origin_paths()
try:
    _OWN_FILE_NORM = os.path.normcase(os.path.normpath(os.path.abspath(__file__)))
except BaseException:
    _OWN_FILE_NORM = ""

_ORIGIN_TAGS = ("user", "stdlib", "site-packages", "error_handler")


def _tag_frame_origin(filename):
    """Classify a frame's filename. Order matters: our own file first,
    then site-packages BEFORE stdlib (site-packages usually lives inside
    the stdlib prefix tree), then stdlib, else user. Never raises."""
    try:
        if not filename:
            return "user"
        if filename.startswith("<frozen"):
            return "stdlib"  # frozen importlib bootstrap machinery
        if filename.startswith("<"):
            return "user"    # <string>, <stdin> — the user's dynamic code
        f = os.path.normcase(os.path.normpath(filename))
        if _OWN_FILE_NORM and f == _OWN_FILE_NORM:
            return "error_handler"
        if "site-packages" in f or "dist-packages" in f:
            return "site-packages"
        for p in _SITE_PATHS:
            if p and f.startswith(p + os.sep):
                return "site-packages"
        for p in _STDLIB_PATHS:
            if p and f.startswith(p + os.sep):
                return "stdlib"
        return "user"
    except BaseException:
        return "user"


_active_skip_modules: contextvars.ContextVar = contextvars.ContextVar(
    "error_handler_skip_modules", default=()
)


def _match_skip(filename, origin, skip_modules):
    """Return the skip_modules entry that matches this frame, or None.
    An entry equal to an origin tag matches by tag; anything else matches
    as a (normcased) substring of the filename — so "threading" hides
    .../lib/python3.x/threading.py and "django" hides site-packages
    django frames. Never raises."""
    try:
        f = os.path.normcase(str(filename or ""))
        for entry in skip_modules:
            e = str(entry)
            if e in _ORIGIN_TAGS:
                if origin == e:
                    return e
            elif e and os.path.normcase(e) in f:
                return e
        return None
    except BaseException:
        return None


def _frame_dict(frame, lineno, include_locals, source_context_lines, failures):
    """Shared frame-to-dict converter. Used by the exception traceback walker
    (which passes tb.tb_lineno) and the caller-context walker (which passes
    frame.f_lineno). When include_locals is True, frame.f_locals is captured
    with each value passed through _safe_repr; the whole locals grab is
    wrapped in _safe_capture so a pathological frame can't break extraction.
    When source_context_lines > 0, a window of N lines either side of the
    line is captured (dedented for legibility) in `source_context`."""
    code = frame.f_code
    filename = code.co_filename
    function = code.co_name
    raw_source = linecache.getline(filename, lineno).strip()
    source = _redact(raw_source) if raw_source else None
    origin = _tag_frame_origin(filename)
    out = {
        "file": filename,
        "line": lineno,
        "function": function,
        "code": source,
        "origin": origin,
    }
    skip_modules = _active_skip_modules.get()
    if skip_modules:
        matched = _match_skip(filename, origin, skip_modules)
        if matched is not None:
            out["hidden"] = matched
    if source_context_lines > 0:
        out["source_context"] = _safe_capture(
            "source_context",
            lambda: _capture_source_context(filename, lineno, source_context_lines),
            [],
            failures,
        )
    if include_locals:
        out["locals"] = _safe_capture(
            "frame_locals",
            lambda: {k: _safe_repr(v) for k, v in frame.f_locals.items()},
            {},
            failures,
        )
    return out


def _walk_caller_context(include_locals, source_context_lines, max_frames, failures):
    """Walk the call stack above describe_error, skipping frames inside this
    module so the result begins at the user's `except` block (frame 0) and
    proceeds outward to the caller, the caller's caller, etc.

    Order: nearest-to-oldest. Frame 0 is the most immediate user code (the
    catch block), matching how you'd read the stack interactively.

    Caps at max_frames; if more exist beyond the cap, a {'truncated': ...}
    marker is appended so the formatter can show that fact rather than
    silently dropping frames.

    Wrapped in its own try/except so a broken stack walk (extremely rare but
    possible with certain C extensions or frame-mutating debuggers) lands
    as a partial_failure entry rather than breaking the whole report."""
    frames = []
    own_file = __file__
    try:
        depth = 1
        # Skip past all frames in this module - get to the user's catch site.
        while True:
            try:
                f = sys._getframe(depth)
            except ValueError:
                return frames  # stack ended inside our module - nothing to show
            if f.f_code.co_filename != own_file:
                break
            depth += 1
        # Now walk outward up to max_frames.
        while len(frames) < max_frames:
            try:
                f = sys._getframe(depth)
            except ValueError:
                break
            frame_data = _safe_capture(
                "caller_frame",
                lambda f=f: _frame_dict(
                    f, f.f_lineno, include_locals, source_context_lines, failures,
                ),
                None,
                failures,
            )
            if frame_data is not None:
                frames.append(frame_data)
            depth += 1
        # If more frames exist beyond the cap, note it.
        try:
            sys._getframe(depth)
            frames.append({"truncated": "max_caller_frames_reached"})
        except ValueError:
            pass
    except BaseException as inner:
        try:
            failures.append({"step": "caller_context.walk", "error": repr(inner)})
        except BaseException:
            pass
    return frames


def _capture_source_context(filename, lineno, n_lines):
    """Capture n_lines either side of the error line, dedent common leading
    whitespace across the window for legibility, return list of
    {lineno, text, is_error_line} dicts. Empty list when linecache returns
    nothing (dynamic code, missing file, etc.)."""
    raw = linecache.getlines(filename)
    if not raw:
        return []
    err_idx = lineno - 1  # 1-indexed -> 0-indexed
    start = max(0, err_idx - n_lines)
    end = min(len(raw), err_idx + n_lines + 1)
    window = raw[start:end]
    if not window:
        return []
    cleaned = [line.rstrip("\r\n") for line in window]
    non_blank = [l for l in cleaned if l.strip()]
    if non_blank:
        common = min(len(l) - len(l.lstrip()) for l in non_blank)
    else:
        common = 0
    out = []
    for i, line in enumerate(cleaned):
        ln = start + i + 1  # back to 1-indexed
        text = line[common:] if len(line) >= common else line
        out.append({
            "lineno": ln,
            "text": _redact(text),
            "is_error_line": (ln == lineno),
        })
    return out


# ---------------------------------------------------------------------------
# Task 4: Chain walker with cycle and depth guards
# ---------------------------------------------------------------------------

def _walk_chain(exc, max_depth, include_locals, source_context_lines, failures, max_group_depth=10):
    """Follow __cause__ first, then __context__ (unless __suppress_context__).

    Returns links from nearest to oldest. Each link is the same shape as the
    top-level report minus its own `chain` key (to avoid infinite recursion).
    Cycles are detected via id()-based visited set; depth overflow and cycles
    are recorded as truncation markers in the chain itself."""
    chain = []
    visited = {id(exc)}
    current = exc
    depth = 0

    while depth < max_depth:
        nxt = None
        relation = None

        cause = getattr(current, "__cause__", None)
        if cause is not None:
            nxt, relation = cause, "cause"
        else:
            ctx = getattr(current, "__context__", None)
            suppressed = getattr(current, "__suppress_context__", False)
            if ctx is not None and not suppressed:
                nxt, relation = ctx, "context"

        if nxt is None:
            break

        if id(nxt) in visited:
            chain.append({
                "relation": relation,
                "truncated": "cycle_detected",
                "type": _safe_capture(
                    "chain.cycle.type",
                    lambda nxt=nxt: type(nxt).__name__,
                    "<unknown>",
                    failures,
                ),
            })
            break

        visited.add(id(nxt))

        link = _safe_capture(
            "chain.link",
            lambda nxt=nxt: _build_data(
                nxt, failures, max_depth,
                with_chain=False, include_locals=include_locals,
                source_context_lines=source_context_lines,
                max_group_depth=max_group_depth,
            ),
            {},
            failures,
        )
        link["relation"] = relation
        chain.append(link)

        current = nxt
        depth += 1

    # Hit the depth cap with more chain remaining?
    if depth >= max_depth:
        has_more = (
            getattr(current, "__cause__", None) is not None
            or (
                getattr(current, "__context__", None) is not None
                and not getattr(current, "__suppress_context__", False)
            )
        )
        if has_more:
            chain.append({"truncated": "max_depth_reached"})

    return chain


# ---------------------------------------------------------------------------
# ExceptionGroup walker (Python 3.11+ BaseExceptionGroup, with duck-type
# fallback so the module still works on pre-3.11 plus the `exceptiongroup`
# backport without an explicit import).
# ---------------------------------------------------------------------------

try:
    _BaseExceptionGroup = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:
    _BaseExceptionGroup = None


def _is_exception_group(exc):
    """True if exc behaves like an ExceptionGroup.

    Prefer isinstance against the stdlib class when available (3.11+).
    Otherwise duck-type: a tuple-valued `exceptions` attribute and a class
    name that mentions ExceptionGroup. The duck-type path lets the module
    work on 3.10 with the `exceptiongroup` backport without importing it.
    """
    try:
        if _BaseExceptionGroup is not None and isinstance(exc, _BaseExceptionGroup):
            return True
        members = getattr(exc, "exceptions", None)
        if not isinstance(members, tuple):
            return False
        return "ExceptionGroup" in type(exc).__name__
    except BaseException:
        return False


def _walk_group(
    exc, max_group_depth, max_chain_depth, include_locals,
    source_context_lines, failures, visited, current_depth,
):
    """Recurse through `exc.exceptions`, returning a list of full data
    dicts (one per child). Each child is run through `_build_data` so it
    gets its own type-specific block, traceback, chain, and (if itself a
    group) its own group_children.

    Top-down ordering: child 1 is rendered before child 2 - groups are
    sibling sets, not chains, so the natural reading order is the order
    Python collected them.

    Guards:
      - `visited` (id-keyed) prevents cycles in pathological groups
      - `current_depth` against `max_group_depth` prevents runaway nesting;
        a {'truncated': 'max_group_depth_reached'} marker is left in place
        so the formatter shows that fact rather than dropping the child.
    """
    if not _is_exception_group(exc):
        return []
    try:
        members = list(getattr(exc, "exceptions", ()))
    except BaseException as inner:
        try:
            failures.append({"step": "group.members", "error": repr(inner)})
        except BaseException:
            pass
        return []

    children = []
    for child in members:
        try:
            child_id = id(child)
        except BaseException:
            child_id = None
        if child_id is not None and child_id in visited:
            children.append({
                "truncated": "cycle_detected",
                "type": _safe_capture(
                    "group.cycle.type",
                    lambda c=child: type(c).__name__,
                    "<unknown>", failures,
                ),
            })
            continue
        if current_depth + 1 >= max_group_depth:
            children.append({
                "truncated": "max_group_depth_reached",
                "type": _safe_capture(
                    "group.depth.type",
                    lambda c=child: type(c).__name__,
                    "<unknown>", failures,
                ),
            })
            continue
        if child_id is not None:
            visited.add(child_id)
        child_data = _safe_capture(
            "group.child",
            lambda c=child: _build_data(
                c, failures, max_chain_depth,
                with_chain=True,
                include_locals=include_locals,
                source_context_lines=source_context_lines,
                max_group_depth=max_group_depth,
                _group_visited=visited,
                _group_depth=current_depth + 1,
            ),
            {},
            failures,
        )
        children.append(child_data)
    return children


# ---------------------------------------------------------------------------
# Task 5 / Task 15: Type-specific dispatch table (public registration)
# ---------------------------------------------------------------------------
#
# Maps exception class -> extractor function. Lookup walks the type's MRO so
# subclasses inherit (FileNotFoundError gets the OSError extractor for free).
# Each extractor must return a dict and should be robust to missing attributes
# (built-in exceptions are remarkably inconsistent about which attrs they set).
#
# Registration is public (task 15): register_extractor() mirrors the redactor
# registry so users can teach the dispatch table their own exception types.
# Extractor calls are routed through _safe_capture at dispatch time, so a
# broken user extractor lands in partial_failures instead of raising.

_TYPE_EXTRACTORS = {}


def register_extractor(exc_type):
    """Decorator: register a type-specific extractor for `exc_type`.

        @register_extractor(MyAppError)
        def _extract_myapperror(e):
            return {"request_id": getattr(e, "request_id", None)}

    The extractor receives the exception instance and must return a dict,
    which lands in the report under `type_specific`. Lookup walks the MRO,
    so registering a base class covers all its subclasses; registering a
    type that already has an extractor (e.g. OSError) replaces the seeded
    one. Use _safe_repr() for any values with untrusted reprs.

    Raises TypeError at registration time if `exc_type` is not an exception
    type — a silently-never-matching entry would be worse. (The never-raises
    contract applies to report building, not registration.)
    """
    if not (isinstance(exc_type, type) and issubclass(exc_type, BaseException)):
        raise TypeError(
            "register_extractor expects an exception type, got "
            + repr(exc_type)
        )
    def deco(fn):
        _TYPE_EXTRACTORS[exc_type] = fn
        return fn
    return deco


def unregister_extractor(exc_type):
    """Remove the extractor registered for exactly `exc_type` (no MRO walk).
    Returns the removed extractor, or None if nothing was registered. Handy
    for tests and for restoring a seeded extractor after an override."""
    return _TYPE_EXTRACTORS.pop(exc_type, None)


# Internal alias kept for backward compatibility (pre-task-15 docs pointed
# projects at _register).
_register = register_extractor


# ---------------------------------------------------------------------------
# Did-you-mean suggestions (difflib) - task 26
# ---------------------------------------------------------------------------
#
# Some exceptions name something that wasn't found: a missing attribute, an
# unbound name, an absent module. When the *correct* set of names is knowable,
# difflib.get_close_matches turns a typo into an actionable "did you mean?".
# Suggestions read NAMES ONLY (attribute / variable / module names) - never
# values - so there is no secret-leak surface. The enabled flag rides a
# ContextVar like redactors and skip_modules, so the seed extractors honor the
# per-call `suggestions=` argument without any change to the (exc) -> dict
# extractor contract.

_SUGGEST_MAX = 3        # most names get_close_matches may return
_SUGGEST_CUTOFF = 0.6   # difflib similarity floor (its own default)

_suggestions_enabled: contextvars.ContextVar = contextvars.ContextVar(
    "error_handler_suggestions", default=True
)


def _suggest(missing, candidates):
    """Return up to _SUGGEST_MAX close-match names for `missing` drawn from
    `candidates`. Fully defensive: any failure yields []. The typed name is
    never echoed back as its own suggestion. Matches pass through the active
    redactors for consistency with every other captured string."""
    try:
        if not isinstance(missing, str) or not missing:
            return []
        # Dedupe, preserving order: at module scope f_locals IS f_globals, so
        # a raw concat would let get_close_matches return the same name twice
        # ("did you mean: 'x' or 'x'?").
        pool = list(dict.fromkeys(c for c in candidates if isinstance(c, str)))
        matches = difflib.get_close_matches(
            missing, pool, n=_SUGGEST_MAX, cutoff=_SUGGEST_CUTOFF
        )
        return [_redact(m) for m in matches if m != missing]
    except BaseException:
        return []


def _suggest_for_attribute(e):
    """Close attribute names for an AttributeError, from dir() of the object
    the lookup failed on (carried on the exception, 3.10+). dir() can run a
    custom __dir__, so the whole thing is guarded."""
    try:
        if not _suggestions_enabled.get() or not hasattr(e, "obj"):
            return []
        return _suggest(getattr(e, "name", None), dir(e.obj))
    except BaseException:
        return []


def _suggest_for_name(e):
    """Close names for a NameError, from the locals, globals and builtins of
    the frame where it was raised (the innermost traceback frame). Reads key
    names only, never values."""
    try:
        if not _suggestions_enabled.get():
            return []
        name = getattr(e, "name", None)
        if not name:
            return []
        tb = getattr(e, "__traceback__", None)
        frame = None
        while tb is not None:
            frame = tb.tb_frame
            tb = tb.tb_next
        if frame is None:
            return []
        pool = list(frame.f_locals) + list(frame.f_globals) + list(frame.f_builtins)
        return _suggest(name, pool)
    except BaseException:
        return []


def _suggest_for_module(e):
    """Close module names for a ModuleNotFoundError, from the stdlib module
    names (a static frozenset, 3.10+) plus already-imported top-level modules.
    Scoped to ModuleNotFoundError on purpose: a generic ImportError from
    `from pkg import thing` has no clean candidate set to match against."""
    try:
        if not _suggestions_enabled.get() or not isinstance(e, ModuleNotFoundError):
            return []
        name = getattr(e, "name", None)
        if not name:
            return []
        names = set(getattr(sys, "stdlib_module_names", ()) or ())
        for m in list(sys.modules):
            names.add(m.split(".")[0])
        # Drop private impl modules (_json, _collections_abc) unless the typo
        # itself was private - nobody typos a public import meaning the
        # underscore module behind it.
        cand = names
        if not name.startswith("_"):
            cand = [n for n in names if not n.startswith("_")]
        return _suggest(name, cand)
    except BaseException:
        return []


def _format_did_you_mean(names):
    """Render suggestion names CPython-style: 'a'? or 'a', 'b' or 'c'?."""
    try:
        quoted = ["'" + str(n) + "'" for n in names]
        if not quoted:
            return ""
        if len(quoted) == 1:
            body = quoted[0]
        else:
            body = ", ".join(quoted[:-1]) + " or " + quoted[-1]
        return body + "?"
    except BaseException:
        return ""


@register_extractor(OSError)
def _extract_oserror(e):
    return {
        "errno": getattr(e, "errno", None),
        "strerror": getattr(e, "strerror", None),
        "filename": getattr(e, "filename", None),
        "filename2": getattr(e, "filename2", None),
        "winerror": getattr(e, "winerror", None),
    }


@register_extractor(SyntaxError)
def _extract_syntaxerror(e):
    return {
        "msg": getattr(e, "msg", None),
        "filename": getattr(e, "filename", None),
        "lineno": getattr(e, "lineno", None),
        "offset": getattr(e, "offset", None),
        "text": getattr(e, "text", None),
        "end_lineno": getattr(e, "end_lineno", None),
        "end_offset": getattr(e, "end_offset", None),
    }


@register_extractor(AttributeError)
def _extract_attributeerror(e):
    out = {"name": getattr(e, "name", None)}
    if hasattr(e, "obj"):
        out["obj"] = _safe_repr(e.obj)
    dym = _suggest_for_attribute(e)
    if dym:
        out["did_you_mean"] = dym
    return out


@register_extractor(NameError)
def _extract_nameerror(e):
    # Covers UnboundLocalError via MRO; .name is set on NameError (3.10+).
    out = {"name": getattr(e, "name", None)}
    dym = _suggest_for_name(e)
    if dym:
        out["did_you_mean"] = dym
    return out


@register_extractor(KeyError)
def _extract_keyerror(e):
    args = getattr(e, "args", ())
    return {"missing_key": _safe_repr(args[0]) if args else None}


@register_extractor(UnicodeError)
def _extract_unicodeerror(e):
    out = {
        "encoding": getattr(e, "encoding", None),
        "start": getattr(e, "start", None),
        "end": getattr(e, "end", None),
        "reason": getattr(e, "reason", None),
    }
    if hasattr(e, "object"):
        out["object_repr"] = _safe_repr(e.object)
    return out


# --- Task 21 seed extractors ----------------------------------------------

@register_extractor(subprocess.CalledProcessError)
def _extract_calledprocesserror(e):
    out = {
        "returncode": getattr(e, "returncode", None),
        "cmd": _safe_repr(getattr(e, "cmd", None)),
    }
    # .output / .stdout are aliases; bytes, str, or None. Process output
    # earns a longer repr budget than the default 200 — the actionable
    # part of stderr is usually worth keeping.
    if getattr(e, "output", None) is not None:
        out["stdout"] = _safe_repr(e.output, max_len=500)
    if getattr(e, "stderr", None) is not None:
        out["stderr"] = _safe_repr(e.stderr, max_len=500)
    return out


@register_extractor(json.JSONDecodeError)
def _extract_jsondecodeerror(e):
    # Subclasses ValueError; instances reach here first via the MRO walk.
    out = {
        "msg": getattr(e, "msg", None),
        "pos": getattr(e, "pos", None),
        "lineno": getattr(e, "lineno", None),
        "colno": getattr(e, "colno", None),
    }
    doc = getattr(e, "doc", None)
    if isinstance(doc, str) and doc:
        pos = getattr(e, "pos", 0) or 0
        start = max(0, pos - 40)
        end = min(len(doc), pos + 40)
        # Raw window (not repr'd) around the failure point; a new capture
        # surface, so it goes through the active redactors like source.
        out["doc_snippet"] = _redact(doc[start:end])
        out["doc_length"] = len(doc)
    return out


@register_extractor(ImportError)
def _extract_importerror(e):
    # Covers ModuleNotFoundError via MRO.
    out = {
        "name": getattr(e, "name", None),
        "path": getattr(e, "path", None),
    }
    dym = _suggest_for_module(e)
    if dym:
        out["did_you_mean"] = dym
    return out


@register_extractor(socket.gaierror)
def _extract_gaierror(e):
    # gaierror subclasses OSError; reuse its extractor and add the
    # resolved EAI_* constant name (errno here is a getaddrinfo code).
    out = _extract_oserror(e)
    code = getattr(e, "errno", None)
    if code is not None:
        for name in dir(socket):
            if name.startswith("EAI_") and getattr(socket, name, None) == code:
                out["gai_constant"] = name
                break
    return out


if ssl is not None:
    @register_extractor(ssl.SSLError)
    def _extract_sslerror(e):
        # SSLError subclasses OSError; SSLCertVerificationError extras
        # picked up via hasattr (it reaches this extractor through MRO).
        out = _extract_oserror(e)
        out["library"] = getattr(e, "library", None)
        out["reason"] = getattr(e, "reason", None)
        if hasattr(e, "verify_code"):
            out["verify_code"] = getattr(e, "verify_code", None)
        if hasattr(e, "verify_message"):
            out["verify_message"] = getattr(e, "verify_message", None)
        return out


def _apply_dispatch(exc, failures):
    """Walk MRO of type(exc) and run the first matching extractor.
    Returns {} if no extractor matches (or if MRO lookup itself blew up)."""
    try:
        mro = type(exc).__mro__
    except BaseException as inner:
        try:
            failures.append({"step": "dispatch.mro", "error": repr(inner)})
        except BaseException:
            pass
        return {}
    for cls in mro:
        if cls in _TYPE_EXTRACTORS:
            extractor = _TYPE_EXTRACTORS[cls]
            return _safe_capture(
                "dispatch[" + cls.__name__ + "]",
                lambda exc=exc, extractor=extractor: extractor(exc),
                {},
                failures,
            )
    return {}


# ---------------------------------------------------------------------------
# Pipeline: build the full data dict for one exception
# ---------------------------------------------------------------------------

def _build_data(
    exc, failures, max_chain_depth=10,
    *,
    with_chain=True,
    include_locals=False,
    source_context_lines=3,
    max_group_depth=10,
    _group_visited=None,
    _group_depth=0,
):
    """Assemble the introspection dict for one exception.

    Called by describe_error (with_chain=True) for the primary exception,
    by _walk_chain (with_chain=False) for each chain link, and by
    _walk_group for each group child. Chain links and group children both
    get full introspection (including their own group_children if they're
    themselves groups).

    _group_visited / _group_depth are recursion-state for nested groups.
    They are NOT public params; describe_error never passes them, only
    _walk_group does. The id-keyed visited set is shared across the entire
    group recursion so a cycle anywhere in the tree is caught."""
    data = {}
    data["type"] = _safe_capture("type", lambda: type(exc).__name__, "<unknown>", failures)
    data["module"] = _safe_capture("module", lambda: type(exc).__module__, "<unknown>", failures)
    data["message"] = _safe_capture("message", lambda: _redact(str(exc)), "<unrenderable>", failures)
    data["repr"] = _safe_capture("repr", lambda: _redact(repr(exc)), "<unrepresentable>", failures)
    data["args"] = _safe_capture(
        "args",
        lambda: tuple(_safe_repr(a) for a in getattr(exc, "args", ())),
        (),
        failures,
    )
    data["notes"] = _safe_capture("notes", lambda: _extract_notes(exc), [], failures)
    data["extra_attrs"] = _safe_capture("extra_attrs", lambda: _extra_attrs(exc), {}, failures)
    data["type_specific"] = _apply_dispatch(exc, failures)
    data["traceback"] = _safe_capture(
        "traceback",
        lambda: _walk_traceback(exc, include_locals, source_context_lines, failures),
        [],
        failures,
    )
    if _is_exception_group(exc):
        if _group_visited is None:
            try:
                _group_visited = {id(exc)}
            except BaseException:
                _group_visited = set()
        data["group_children"] = _safe_capture(
            "group_children",
            lambda: _walk_group(
                exc, max_group_depth, max_chain_depth,
                include_locals, source_context_lines,
                failures, _group_visited, _group_depth,
            ),
            [],
            failures,
        )
    if with_chain:
        data["chain"] = _safe_capture(
            "chain",
            lambda: _walk_chain(exc, max_chain_depth, include_locals, source_context_lines, failures, max_group_depth),
            [],
            failures,
        )
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def describe_error(
    exc=None,
    *,
    include_locals=False,
    max_chain_depth=10,
    source_context_lines=3,
    caller_context=True,
    max_caller_frames=32,
    max_group_depth=10,
    environment_snapshot=True,
    env_vars=None,
    redactors=None,
    skip_modules=None,
    max_report_bytes=None,
    suggestions=True,
):
    """Inspect an exception and return an ErrorReport. NEVER raises.

    Args:
        exc: the exception to describe; if None, falls back to sys.exc_info()
        include_locals: capture frame.f_locals (default off, security)
        max_chain_depth: cap on cause/context chain walking
        source_context_lines: lines of source either side of the error line
            captured per frame (0 disables; default 3 produces a 7-line window
            including the error line, common leading whitespace stripped)
        caller_context: if True (default), also capture frames above the
            catch site - useful for seeing who called the function that's
            now handling the exception. Skips frames inside this module.
        max_caller_frames: cap on caller_context walk (default 32; enough
            for typical handlers, with a truncation marker if exceeded so
            deeply recursive callers don't silently lose information)
        max_group_depth: cap on nested ExceptionGroup recursion (Python
            3.11+ groups, or the 3.10 `exceptiongroup` backport detected
            via duck typing). Cycle protection is automatic.
        environment_snapshot: capture a small runtime context block
            (Python version, platform, cwd, pid, argv). Default on.
        env_vars: optional iterable of environment variable names to
            include in the snapshot. None / empty => no env vars captured
            (the default; secrets often live in env vars).
        redactors: optional iterable of (str -> str) callables, used INSTEAD
            of the module-level registry for this call. Pass [] to disable
            redaction entirely. Default None means "use whatever has been
            registered via register_redactor()".
        skip_modules: optional iterable of strings marking matching frames
            `hidden` in the dict (nothing is dropped). An entry equal to an
            origin tag ("user" / "stdlib" / "site-packages" /
            "error_handler") matches by tag; anything else matches as a
            filename substring (e.g. "threading", "django"). The concise
            formatter collapses runs of hidden frames; the heavy formatter
            only annotates them.
        max_report_bytes: optional byte budget (compact-JSON utf-8 size)
            for log shipping. Over budget, the report degrades
            progressively — locals dropped from every frame first, then
            source_context (the single-line `code` always survives) — and
            a top-level `report_truncation` dict records the budget, what
            was dropped, the final size, and whether the budget was met.
            None / 0 (default) disables.
        suggestions: if True (default), attach a `did_you_mean` list to the
            type_specific block for AttributeError (close attribute names),
            NameError (close in-scope names) and ModuleNotFoundError (close
            module names), computed with difflib. Reads names only, never
            values. Set False to skip the lookup entirely.
    """
    try:
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is None:
            return ErrorReport({
                "error_handler_failed": False,
                "no_active_exception": True,
            })

        # Activate redactors for the duration of this call. ContextVar so
        # concurrent describe_error calls (threads / asyncio tasks) don't
        # stomp on each other. Reset in finally to leave no residue.
        if redactors is None:
            active = tuple(_DEFAULT_REDACTORS)
        else:
            active = tuple(redactors)
        token = _active_redactors.set(active)
        skip_token = None
        suggest_token = None

        try:
            failures = []
            # skip_modules rides a ContextVar like redactors, reaching every
            # frame builder (traceback / chain / group / caller) without
            # signature churn. Bad values degrade to "no skipping" with a
            # partial_failures entry rather than violating never-raises.
            sm = ()
            if skip_modules:
                sm = _safe_capture(
                    "skip_modules",
                    lambda: tuple(str(s) for s in skip_modules),
                    (),
                    failures,
                )
            skip_token = _active_skip_modules.set(sm)
            suggest_token = _suggestions_enabled.set(bool(suggestions))
            data = _build_data(
                exc, failures, max_chain_depth,
                include_locals=include_locals,
                source_context_lines=source_context_lines,
                max_group_depth=max_group_depth,
            )
            if caller_context:
                data["caller_context"] = _safe_capture(
                    "caller_context",
                    lambda: _walk_caller_context(
                        include_locals, source_context_lines, max_caller_frames, failures,
                    ),
                    [],
                    failures,
                )
            if environment_snapshot:
                data["environment"] = _safe_capture(
                    "environment",
                    lambda: _capture_environment(env_vars, failures),
                    {},
                    failures,
                )
            data["partial_failures"] = failures
            # Budget runs LAST so the measurement covers the whole dict
            # (incl. partial_failures); failures appended here still land
            # in the report because the list is shared by reference.
            if max_report_bytes:
                _safe_capture(
                    "report_budget",
                    lambda: _apply_report_budget(data, int(max_report_bytes)),
                    None,
                    failures,
                )
            report = ErrorReport(data)
        finally:
            _active_redactors.reset(token)
            if skip_token is not None:
                _active_skip_modules.reset(skip_token)
            if suggest_token is not None:
                _suggestions_enabled.reset(suggest_token)
        # Observers fire AFTER the redactor reset: they receive the
        # finished, already-redacted report. Reentrancy-guarded inside.
        _notify_observers(report)
        return report

    except BaseException as handler_failure:
        try:
            fallback_repr = repr(exc)
        except BaseException:
            fallback_repr = "<repr unavailable>"
        try:
            fallback_type = type(exc).__name__ if exc is not None else "<no exc>"
        except BaseException:
            fallback_type = "<type unavailable>"
        try:
            handler_failure_repr = repr(handler_failure)
        except BaseException:
            handler_failure_repr = "<handler failure unrepresentable>"
        report = ErrorReport({
            "error_handler_failed": True,
            "fallback_repr": fallback_repr,
            "fallback_type": fallback_type,
            "handler_failure": handler_failure_repr,
        })
        # The handler choking is exactly what a metrics pipeline wants to
        # know about — fire observers for fallback reports too.
        _notify_observers(report)
        return report


# ---------------------------------------------------------------------------
# Task 7: Concise formatter (heavy edition still stubbed - Task 9)
# ---------------------------------------------------------------------------

def _format_concise(data):
    """Concise, traceback-style human output. Matches Python's chained-exception
    printing convention: oldest exception first, relation phrases between each
    link, primary exception last.

    Per-exception layout:
      Traceback (most recent call last):
        File "...", line N, in func
          source line
            local_name = value      (only when include_locals was True)
      module.ExceptionType: message
        [type_specific_key=value, ...]
        note: ...

    Partial failures inside error_handler are reported at the end so they
    don't disrupt the reading flow of the actual exception."""
    if data.get("error_handler_failed"):
        return (
            "[error_handler failed]\n"
            "  original: " + str(data.get("fallback_type", "?")) + " "
            + str(data.get("fallback_repr", "?")) + "\n"
            "  handler:  " + str(data.get("handler_failure", "?"))
        )
    if data.get("no_active_exception"):
        return "[no active exception]"

    lines = []

    # Chain is walked newest-to-oldest; reverse so oldest prints first
    # (matches Python's traceback module convention).
    chain = list(reversed(data.get("chain") or []))
    for link in chain:
        if link.get("truncated"):
            marker = link["truncated"]
            note = (
                "(more exceptions exist beyond depth limit)"
                if marker == "max_depth_reached"
                else "(chain cycles back to an earlier exception)"
            )
            lines.append("... earlier chain truncated: " + marker + " " + note)
            lines.append("")
            continue
        _render_one_concise(link, lines)
        rel = link.get("relation")
        lines.append("")
        if rel == "cause":
            lines.append(
                "The above exception was the direct cause of the following exception:"
            )
        else:
            lines.append(
                "During handling of the above exception, another exception occurred:"
            )
        lines.append("")

    _render_one_concise(data, lines)

    cc = data.get("caller_context") or []
    if cc:
        lines.append("")
        lines.append("Caller context (frames above the catch, nearest-to-oldest):")
        pending_hidden = []
        for frame in cc:
            if frame.get("truncated"):
                _flush_hidden_run(pending_hidden, lines)
                lines.append(
                    "  ... more frames exist beyond max_caller_frames"
                )
                continue
            if frame.get("hidden"):
                pending_hidden.append(str(frame["hidden"]))
                continue
            _flush_hidden_run(pending_hidden, lines)
            _render_concise_frame(frame, lines)
        _flush_hidden_run(pending_hidden, lines)

    trunc = data.get("report_truncation")
    if trunc:
        lines.append("")
        note = (
            "[report degraded to fit " + str(trunc.get("budget_bytes", "?"))
            + "-byte budget: dropped " + ", ".join(trunc.get("dropped") or ["nothing"])
            + "; final " + str(trunc.get("final_bytes", "?")) + " bytes"
        )
        if not trunc.get("within_budget", True):
            note += " — STILL OVER BUDGET"
        lines.append(note + "]")

    failures = data.get("partial_failures") or []
    if failures:
        lines.append("")
        lines.append(
            "[" + str(len(failures))
            + " partial capture failure(s) inside error_handler:]"
        )
        for f in failures:
            lines.append(
                "  - " + str(f.get("step", "?")) + ": " + str(f.get("error", "?"))
            )

    return "\n".join(lines)


def _render_concise_frame(frame, lines):
    """Shared per-frame body for the concise formatter (traceback frames
    and caller-context frames render identically)."""
    lines.append(
        '  File "' + str(frame.get("file", "?")) + '", line '
        + str(frame.get("line", "?")) + ", in "
        + str(frame.get("function", "?"))
    )
    ctx = frame.get("source_context") or []
    if ctx:
        _render_source_context(ctx, lines, indent="    ")
    elif frame.get("code"):
        lines.append("    " + str(frame["code"]))
    anchors = frame.get("col_anchors")
    if anchors:
        a = (
            "    [error at line " + str(anchors.get("lineno", "?"))
            + ", cols " + str(anchors.get("colno", "?"))
            + "-" + str(anchors.get("end_colno", "?"))
        )
        if anchors.get("anchor_text"):
            a += ": " + str(anchors["anchor_text"])
        lines.append(a + "]")
    locs = frame.get("locals")
    if locs:
        for k, v in locs.items():
            lines.append("      " + str(k) + " = " + str(v))


def _flush_hidden_run(pending, lines):
    """Emit one collapse line for a run of hidden frames (task 23), then
    clear the run. No-op when the run is empty. Labels = the skip_modules
    entries that matched, deduplicated in first-seen order."""
    if not pending:
        return
    labels = []
    for lab in pending:
        if lab not in labels:
            labels.append(lab)
    lines.append(
        "  [" + str(len(pending)) + " frame(s) hidden: "
        + ", ".join(labels) + "]"
    )
    pending.clear()


def _render_one_concise(d, lines):
    """Render one exception's traceback + header + type-specific + notes into
    the running `lines` list. Used for both the primary exception and each
    chain link, so the layout is consistent throughout the report."""
    tb = d.get("traceback") or []
    if tb:
        lines.append("Traceback (most recent call last):")
        pending_hidden = []
        for frame in tb:
            if frame.get("hidden"):
                pending_hidden.append(str(frame["hidden"]))
                continue
            _flush_hidden_run(pending_hidden, lines)
            _render_concise_frame(frame, lines)
        _flush_hidden_run(pending_hidden, lines)

    typ = d.get("type", "?")
    module = d.get("module", "")
    if module and module not in ("builtins", "__main__"):
        header_type = module + "." + typ
    else:
        header_type = typ
    msg = d.get("message", "")
    if msg:
        lines.append(header_type + ": " + msg)
    else:
        lines.append(header_type)

    ts = d.get("type_specific") or {}
    dym = ts.get("did_you_mean")
    ts_parts = [
        str(k) + "=" + str(v)
        for k, v in ts.items()
        if v is not None and k != "did_you_mean"
    ]
    if ts_parts:
        lines.append("  [" + ", ".join(ts_parts) + "]")
    if dym:
        hint = _format_did_you_mean(dym)
        if hint:
            lines.append("  Did you mean: " + hint)

    for note in d.get("notes") or []:
        lines.append("  note: " + str(note))

    children = d.get("group_children") or []
    if children:
        n_real = sum(1 for c in children if not c.get("truncated"))
        lines.append("")
        lines.append(
            "  --- group children (" + str(n_real) + " sub-exception"
            + ("" if n_real == 1 else "s") + ") ---"
        )
        for i, child in enumerate(children, start=1):
            lines.append("")
            if child.get("truncated"):
                marker = child["truncated"]
                note = (
                    "(more nested groups exist beyond max_group_depth)"
                    if marker == "max_group_depth_reached"
                    else "(child cycles back to an earlier exception)"
                )
                lines.append(
                    "  +-- child " + str(i) + ": truncated (" + marker + ") "
                    + note
                )
                continue
            lines.append(
                "  +---------- group child " + str(i) + " of "
                + str(len(children)) + " ----------"
            )
            _render_one_concise(child, lines)


def _render_source_context(ctx, lines, indent):
    """Render a source_context list with line numbers and an error-line marker.
    Width of the line-number column is sized to the largest lineno so the bar
    stays aligned even when ranges span 99->100 boundaries."""
    max_ln = max((c.get("lineno", 0) for c in ctx), default=0)
    width = max(2, len(str(max_ln)))
    for c in ctx:
        marker = ">>" if c.get("is_error_line") else "  "
        ln = str(c.get("lineno", "?")).rjust(width)
        text = c.get("text", "")
        lines.append(indent + marker + " " + ln + " | " + text)


def _format_heavy(data):
    """Heavy / LLM-friendly edition. Fully labeled section by section, with
    every chain link rendered with its own where-it-happened block, and
    partial-failures explicitly called out so an LLM reader knows what was
    missed.

    Chain ordering here is nearest-to-oldest (the walker's natural order),
    NOT chronological. Rationale: the LLM has already seen the primary; the
    natural next question is "what's the most direct cause?", then "what's
    further back?". A structured-data view, not a narrative."""
    if data.get("error_handler_failed"):
        return (
            "=== ERROR REPORT (heavy edition) ===\n\n"
            "ERROR HANDLER FAILED\n"
            "  The error handler itself raised while trying to describe the\n"
            "  original exception. The most primitive information available:\n\n"
            "  Original exception type: "
            + str(data.get("fallback_type", "?")) + "\n"
            "  Original exception repr: "
            + str(data.get("fallback_repr", "?")) + "\n"
            "  Handler failure: "
            + str(data.get("handler_failure", "?")) + "\n\n"
            "=== END REPORT ==="
        )
    if data.get("no_active_exception"):
        return (
            "=== ERROR REPORT (heavy edition) ===\n\n"
            "NO ACTIVE EXCEPTION\n"
            "  describe_error() was called with no argument and no active\n"
            "  exception in sys.exc_info(). Nothing to describe.\n\n"
            "=== END REPORT ==="
        )

    lines = []
    lines.append("=== ERROR REPORT (heavy edition) ===")
    lines.append("")
    lines.append("PRIMARY EXCEPTION")
    _render_one_heavy(data, lines, indent="  ")

    cc = data.get("caller_context") or []
    lines.append("")
    if cc:
        real = [f for f in cc if not f.get("truncated")]
        lines.append(
            "CALLER CONTEXT (" + str(len(real))
            + " frame(s) above the catch site, nearest-to-oldest)"
        )
        for i, frame in enumerate(cc, start=1):
            if frame.get("truncated"):
                lines.append(
                    "  Frame " + str(i)
                    + ": truncated (max_caller_frames reached; more frames exist)"
                )
                continue
            lines.append("  Frame " + str(i) + ":")
            lines.append("    File: " + str(frame.get("file", "?")))
            lines.append("    Line: " + str(frame.get("line", "?")))
            lines.append("    Function: " + str(frame.get("function", "?")))
            origin = frame.get("origin")
            if origin:
                o_line = "    Origin: " + str(origin)
                if frame.get("hidden"):
                    o_line += (
                        " (hidden by skip_modules: '"
                        + str(frame["hidden"])
                        + "' — heavy edition shows everything)"
                    )
                lines.append(o_line)
            code = frame.get("code")
            if code:
                lines.append("    Code: " + str(code))
            ctx = frame.get("source_context") or []
            if ctx:
                first = ctx[0].get("lineno", "?")
                last = ctx[-1].get("lineno", "?")
                lines.append(
                    "    Source context (lines " + str(first) + "-" + str(last) + "):"
                )
                _render_source_context(ctx, lines, indent="      ")
            locs = frame.get("locals")
            if locs:
                lines.append("    Locals:")
                for k, v in locs.items():
                    lines.append("      " + str(k) + " = " + str(v))
    else:
        lines.append("CALLER CONTEXT")
        lines.append("  (not captured - caller_context=False, or no frames above the catch)")

    chain = data.get("chain") or []
    lines.append("")
    if chain:
        real_links = [c for c in chain if not c.get("truncated")]
        lines.append(
            "CAUSE / CONTEXT CHAIN (" + str(len(real_links))
            + " chained exception(s); listed nearest-to-oldest as walked via "
            "__cause__ / __context__)"
        )
        for idx, link in enumerate(chain, start=1):
            lines.append("")
            if link.get("truncated"):
                marker = link["truncated"]
                explain = (
                    "(more exceptions exist beyond max_chain_depth)"
                    if marker == "max_depth_reached"
                    else "(chain cycles back to an earlier exception)"
                )
                lines.append(
                    "  --- Link " + str(idx) + ": truncated ("
                    + marker + ") " + explain + " ---"
                )
                continue
            rel = link.get("relation", "?")
            rel_phrase = {
                "cause": "explicit cause (raise ... from ...)",
                "context": "implicit context (exception raised while handling another)",
            }.get(rel, str(rel))
            lines.append("  --- Link " + str(idx) + ": " + rel_phrase + " ---")
            _render_one_heavy(link, lines, indent="    ")
    else:
        lines.append("CAUSE / CONTEXT CHAIN")
        lines.append("  (no chained exceptions)")

    env = data.get("environment") or {}
    if env:
        lines.append("")
        lines.append("ENVIRONMENT")
        for key in (
            "python_version", "python_implementation", "platform",
            "system", "machine", "executable", "cwd", "pid", "argv",
        ):
            if key in env:
                lines.append("  " + key + ": " + str(env[key]))
        evars = env.get("env_vars") or {}
        if evars:
            lines.append("  env_vars:")
            for k, v in evars.items():
                lines.append("    " + str(k) + " = " + str(v))

    trunc = data.get("report_truncation")
    if trunc:
        lines.append("")
        lines.append("REPORT BUDGET (max_report_bytes)")
        lines.append("  Budget: " + str(trunc.get("budget_bytes", "?")) + " bytes")
        lines.append("  Final size: " + str(trunc.get("final_bytes", "?")) + " bytes")
        dropped = trunc.get("dropped") or []
        if dropped:
            lines.append("  Dropped to fit: " + ", ".join(str(x) for x in dropped))
        lines.append(
            "  Within budget: " + ("yes" if trunc.get("within_budget") else
                                   "NO — degradation stages exhausted")
        )

    lines.append("")
    failures = data.get("partial_failures") or []
    if failures:
        lines.append("INTERNAL CAPTURE ISSUES (" + str(len(failures)) + ")")
        lines.append(
            "  The error handler caught the following failures while introspecting."
        )
        lines.append(
            "  These are surprises in the exception itself (broken __repr__, etc.),"
        )
        lines.append(
            "  not problems in the original calling code. Affected fields used"
        )
        lines.append("  fallback values.")
        for f in failures:
            lines.append(
                "    - " + str(f.get("step", "?")) + ": " + str(f.get("error", "?"))
            )
    else:
        lines.append("INTERNAL CAPTURE ISSUES")
        lines.append("  None - the error handler captured everything successfully.")

    lines.append("")
    lines.append("=== END REPORT ===")
    return "\n".join(lines)


def _render_one_heavy(d, lines, indent):
    """Render one exception in heavy/labeled format into the running `lines`
    list. Used for both the primary exception and each chain link, just with
    different indent levels so chain links nest visually under their headers."""
    typ = d.get("type", "?")
    module = d.get("module", "")
    fq = (module + "." + typ) if module else typ
    lines.append(indent + "Fully-qualified type: " + fq)
    lines.append(indent + "Message: " + str(d.get("message", "")))
    lines.append(indent + "Repr: " + str(d.get("repr", "<missing>")))

    args = d.get("args") or ()
    if args:
        lines.append(indent + "Args:")
        for i, a in enumerate(args):
            lines.append(indent + "  [" + str(i) + "] " + str(a))
    else:
        lines.append(indent + "Args: (none)")

    notes = d.get("notes") or []
    if notes:
        lines.append(indent + "Notes:")
        for n in notes:
            lines.append(indent + "  - " + str(n))
    else:
        lines.append(indent + "Notes: (none)")

    extra = d.get("extra_attrs") or {}
    if extra:
        lines.append(indent + "Extra attributes:")
        for k, v in extra.items():
            lines.append(indent + "  " + str(k) + " = " + str(v))
    else:
        lines.append(indent + "Extra attributes: (none)")

    ts = d.get("type_specific") or {}
    dym = ts.get("did_you_mean")
    ts_items = [(k, v) for k, v in ts.items() if k != "did_you_mean"]
    if ts_items:
        lines.append(indent + "Type-specific details:")
        for k, v in ts_items:
            lines.append(indent + "  " + str(k) + ": " + str(v))
    elif not dym:
        lines.append(
            indent
            + "Type-specific details: (no extractor registered for this exception type)"
        )
    if dym:
        hint = _format_did_you_mean(dym)
        if hint:
            lines.append(indent + "Did you mean: " + hint)

    tb = d.get("traceback") or []
    if tb:
        lines.append(
            indent + "Where it happened (most recent call last, "
            + str(len(tb)) + " frame(s)):"
        )
        for i, frame in enumerate(tb, start=1):
            lines.append(indent + "  Frame " + str(i) + ":")
            lines.append(indent + "    File: " + str(frame.get("file", "?")))
            lines.append(indent + "    Line: " + str(frame.get("line", "?")))
            lines.append(indent + "    Function: " + str(frame.get("function", "?")))
            origin = frame.get("origin")
            if origin:
                o_line = indent + "    Origin: " + str(origin)
                if frame.get("hidden"):
                    o_line += (
                        " (hidden by skip_modules: '"
                        + str(frame["hidden"])
                        + "' — heavy edition shows everything)"
                    )
                lines.append(o_line)
            code = frame.get("code")
            if code:
                lines.append(indent + "    Code: " + str(code))
            anchors = frame.get("col_anchors")
            if anchors:
                lines.append(
                    indent + "    Column anchors: line "
                    + str(anchors.get("lineno", "?"))
                    + (
                        "-" + str(anchors["end_lineno"])
                        if anchors.get("end_lineno") not in (None, anchors.get("lineno"))
                        else ""
                    )
                    + ", cols " + str(anchors.get("colno", "?"))
                    + "-" + str(anchors.get("end_colno", "?"))
                    + " (0-based character offsets)"
                )
                if anchors.get("anchor_text"):
                    lines.append(
                        indent + "    Failing expression: "
                        + str(anchors["anchor_text"])
                    )
            ctx = frame.get("source_context") or []
            if ctx:
                first = ctx[0].get("lineno", "?")
                last = ctx[-1].get("lineno", "?")
                lines.append(
                    indent + "    Source context (lines "
                    + str(first) + "-" + str(last) + "):"
                )
                _render_source_context(ctx, lines, indent=indent + "      ")
            locs = frame.get("locals")
            if locs:
                lines.append(indent + "    Locals:")
                for k, v in locs.items():
                    lines.append(indent + "      " + str(k) + " = " + str(v))
    else:
        lines.append(indent + "Where it happened: (no traceback available)")

    children = d.get("group_children") or []
    if children:
        n_real = sum(1 for c in children if not c.get("truncated"))
        lines.append("")
        lines.append(
            indent + "Group children (" + str(n_real) + " sub-exception"
            + ("" if n_real == 1 else "s") + ", listed top-down):"
        )
        for i, child in enumerate(children, start=1):
            lines.append("")
            if child.get("truncated"):
                marker = child["truncated"]
                explain = (
                    "(more nested groups exist beyond max_group_depth)"
                    if marker == "max_group_depth_reached"
                    else "(child cycles back to an earlier exception)"
                )
                lines.append(
                    indent + "  --- Child " + str(i) + ": truncated ("
                    + marker + ") " + explain + " ---"
                )
                continue
            lines.append(
                indent + "  --- Child " + str(i) + " of "
                + str(len(children)) + " ---"
            )
            _render_one_heavy(child, lines, indent=indent + "    ")


# ---------------------------------------------------------------------------
# Task 25: max_report_bytes — progressive degradation budget
# ---------------------------------------------------------------------------
#
# Keeps dicts bounded for log shipping. Size = compact-JSON utf-8 bytes
# (the shape that actually ships). Degradation order per spec: drop
# `locals` from every frame everywhere first, then `source_context` the
# same way — the single-line `code` field always survives. A top-level
# `report_truncation` dict records what happened; if the report STILL
# exceeds the budget after both stages, within_budget=False says so
# honestly rather than hack-chopping strings into invalid structure.

def _iter_frame_lists(d):
    """Yield every frame list reachable from a report data dict:
    traceback, caller_context, chain links' tracebacks, and group
    children recursively (each child is a full data dict)."""
    yield d.get("traceback") or []
    yield d.get("caller_context") or []
    for link in d.get("chain") or []:
        if isinstance(link, dict):
            for frames in _iter_frame_lists(link):
                yield frames
    for child in d.get("group_children") or []:
        if isinstance(child, dict):
            for frames in _iter_frame_lists(child):
                yield frames


def _strip_key_everywhere(data, key):
    """Delete `key` from every frame dict in the report. Returns how many
    frames were stripped."""
    count = 0
    for frames in _iter_frame_lists(data):
        for f in frames:
            if isinstance(f, dict) and key in f:
                del f[key]
                count += 1
    return count


def _apply_report_budget(data, max_report_bytes):
    """Mutates `data` in place to (try to) fit the byte budget. Adds a
    `report_truncation` marker dict whenever any degradation happened."""
    def measure():
        return len(json.dumps(data, default=_json_default).encode("utf-8"))

    size = measure()
    if size <= max_report_bytes:
        return
    dropped = []
    n = _strip_key_everywhere(data, "locals")
    if n:
        dropped.append("locals (" + str(n) + " frame(s))")
        size = measure()
    if size > max_report_bytes:
        n = _strip_key_everywhere(data, "source_context")
        if n:
            dropped.append("source_context (" + str(n) + " frame(s))")
            size = measure()
    data["report_truncation"] = {
        "budget_bytes": max_report_bytes,
        "dropped": dropped,
        "final_bytes": size,          # provisional; updated below
        "within_budget": True,        # provisional; updated below
    }
    final = measure()  # include the marker itself in the final size
    data["report_truncation"]["final_bytes"] = final
    data["report_truncation"]["within_budget"] = final <= max_report_bytes


# ---------------------------------------------------------------------------
# Task 19: serializers — to_json() / to_markdown()
# ---------------------------------------------------------------------------

def _json_default(o):
    """json.dumps default= hook: str() the unserializable, and if even
    str() raises (hostile __str__), fall back to a typed placeholder."""
    try:
        return str(o)
    except BaseException:
        try:
            return "<unjsonable: " + type(o).__name__ + ">"
        except BaseException:
            return "<unjsonable>"


def _format_json(data, indent=None, sort_keys=False):
    """JSON string of the report dict. args/extra values aren't always
    JSON-native — anything unserializable goes through str(), with a
    typed placeholder if str() itself is broken. Never raises: if
    json.dumps still finds a way to fail, returns a minimal failure
    document instead."""
    try:
        return json.dumps(
            data, default=_json_default, indent=indent, sort_keys=sort_keys,
        )
    except BaseException as inner:
        try:
            failure = repr(inner)
        except BaseException:
            failure = "<unrepresentable>"
        return json.dumps({
            "error_handler_failed": True,
            "serializer_failure": failure,
            "type": str(data.get("type", "?")) if isinstance(data, dict) else "?",
        })


# Four backticks: a source line in the report could itself contain a
# ``` fence (markdown in docstrings); GitHub treats the longer fence as
# the delimiter, so the report can't break out of its code block.
_MD_FENCE = "````"


def _format_markdown(data):
    """GitHub-issue-ready markdown. Layout: heading with type+message,
    location line, fenced concise traceback, chain bullets, a VISIBLE
    partial-failures warning (it flags data gaps), and the heavy report
    in a collapsed <details> section. Never raises."""
    try:
        if data.get("error_handler_failed"):
            return (
                "## error_handler failed\n\n"
                "- **original:** `" + str(data.get("fallback_type", "?")) + "` "
                + str(data.get("fallback_repr", "?")) + "\n"
                "- **handler failure:** " + str(data.get("handler_failure", "?"))
                + "\n"
            )
        if data.get("no_active_exception"):
            return "## No active exception\n\n_describe_error() was called with nothing to describe._\n"

        lines = []
        kind = str(data.get("type", "UnknownError"))
        msg = str(data.get("message", "") or "")
        title = kind + (": " + msg if msg else "")
        if len(title) > 120:
            title = title[:120] + "..."
        lines.append("## " + title)
        lines.append("")

        module = data.get("module")
        if module and module not in ("builtins", "__main__"):
            lines.append("**Type:** `" + str(module) + "." + kind + "`")

        tb = data.get("traceback") or []
        if tb:
            last = tb[-1]
            loc = (
                "**Location:** `" + str(last.get("file", "?")) + ":"
                + str(last.get("line", "?")) + "` in `"
                + str(last.get("function", "?")) + "`"
            )
            lines.append(loc)
        lines.append("")

        lines.append("### Traceback")
        lines.append("")
        lines.append(_MD_FENCE + "text")
        lines.append(_format_concise(data))
        lines.append(_MD_FENCE)
        lines.append("")

        chain = data.get("chain") or []
        real_links = [c for c in chain if isinstance(c, dict) and "type" in c]
        if real_links:
            lines.append("### Exception chain")
            lines.append("")
            for link in real_links:
                relation = str(link.get("relation", "context"))
                lines.append(
                    "- **" + str(link.get("type", "?")) + "**: "
                    + str(link.get("message", "")) + " _(" + relation + ")_"
                )
            lines.append("")

        failures = data.get("partial_failures") or []
        if failures:
            lines.append(
                "> ⚠️ **" + str(len(failures)) + " internal capture issue(s)**"
                " — parts of this report may be incomplete. See the heavy"
                " report below for the step-by-step list."
            )
            lines.append("")

        lines.append("<details>")
        lines.append("<summary>Full report (heavy edition)</summary>")
        lines.append("")
        lines.append(_MD_FENCE + "text")
        lines.append(_format_heavy(data))
        lines.append(_MD_FENCE)
        lines.append("")
        lines.append("</details>")
        lines.append("")
        return "\n".join(lines)
    except BaseException as inner:
        try:
            failure = repr(inner)
        except BaseException:
            failure = "<unrepresentable>"
        return "## error_handler markdown formatter failed\n\n`" + failure + "`\n"


# ---------------------------------------------------------------------------
# Task 16: install() / uninstall() — global uncaught-error hook wiring
# ---------------------------------------------------------------------------
#
# One call wires Python's uncaught-error hooks (sys.excepthook,
# threading.excepthook, sys.unraisablehook) to print full reports, so a
# script gets rich crash output with zero try/except boilerplate. Prior
# hooks are stashed for a clean uninstall(), and double as the fallback
# path: if our own hook fails for any reason (report building, stream
# write), the prior hook runs so the crash still surfaces SOMEWHERE.
#
# The asyncio loop exception handler is deliberately separate
# (install_asyncio) because it's per-loop state, not a global hook.

_VALID_HOOKS = ("excepthook", "threading", "unraisable")
_VALID_STYLES = ("concise", "heavy")
_installed_state: Dict[str, Any] = {}  # hook name -> prior hook callable


def _validate_hook_params(style, describe_kwargs):
    """Eager validation shared by install() and install_asyncio(). Raises
    at wiring time rather than letting a typo surface (or worse, get
    swallowed by the fallback path) at crash time."""
    if style not in _VALID_STYLES:
        raise ValueError(
            "style must be one of " + repr(_VALID_STYLES) + ", got " + repr(style)
        )
    # Probe call: unknown keyword args raise TypeError at call binding,
    # BEFORE describe_error's never-raises body is entered. The probe
    # builds a real report, so observer notification is suppressed via
    # the reentrancy guard — pipelines must never see probe reports.
    token = _notifying_observers.set(True)
    try:
        describe_error(ValueError("install() kwargs probe"), **describe_kwargs)
    finally:
        _notifying_observers.reset(token)


def _render_report(exc, style, describe_kwargs):
    report = describe_error(exc, **describe_kwargs)
    return report.for_claude() if style == "heavy" else report.to_string()


def _emit(text, stream):
    """Write one report to the chosen stream. stream=None binds late to
    sys.stderr so redirections (pytest, contextlib.redirect_stderr) work."""
    s = stream if stream is not None else sys.stderr
    print(text, file=s, flush=True)


def _attach_tb(exc, tb):
    """Best-effort: make sure the exception carries the traceback the hook
    was handed (they can diverge for hand-built calls)."""
    try:
        if tb is not None and exc.__traceback__ is None:
            exc.__traceback__ = tb
    except BaseException:
        pass


def install(
    *,
    hooks=_VALID_HOOKS,
    style="concise",
    stream=None,
    **describe_kwargs,
):
    """Wire global uncaught-error hooks to print full describe_error reports.

        import error_handler
        error_handler.install()                      # concise to stderr
        error_handler.install(style="heavy")         # LLM-friendly edition
        error_handler.install(include_locals=True)   # kwargs pass through

    Parameters:
        hooks: which hooks to wire — any subset of ("excepthook",
            "threading", "unraisable"), or a single name as a string.
        style: "concise" (to_string) or "heavy" (for_claude).
        stream: file-like target. None means sys.stderr, resolved at
            crash time rather than install time.
        **describe_kwargs: forwarded to describe_error. caller_context
            defaults to False here (frames above a global hook are
            interpreter internals, not useful context); pass
            caller_context=True to override.

    Calling install() while already installed restores the prior hooks
    first, then re-wires — last install wins, and the original hooks are
    never lost to self-stashing. Validation is eager: bad hook names,
    bad style, or unknown describe_error kwargs raise here, not at crash
    time. uninstall() restores whatever was in place before."""
    if isinstance(hooks, str):
        hooks = (hooks,)
    hooks = tuple(hooks)
    bad = [h for h in hooks if h not in _VALID_HOOKS]
    if bad:
        raise ValueError(
            "unknown hook name(s) " + repr(bad) + "; valid: " + repr(_VALID_HOOKS)
        )
    describe_kwargs.setdefault("caller_context", False)
    _validate_hook_params(style, describe_kwargs)

    if _installed_state:
        uninstall()

    if "excepthook" in hooks:
        prior = sys.excepthook
        _installed_state["excepthook"] = prior

        def _eh_excepthook(exc_type, exc_value, exc_tb, *, _prior=prior):
            try:
                exc = exc_value if exc_value is not None else exc_type()
                _attach_tb(exc, exc_tb)
                _emit(_render_report(exc, style, describe_kwargs), stream)
            except BaseException:
                try:
                    _prior(exc_type, exc_value, exc_tb)
                except BaseException:
                    pass

        sys.excepthook = _eh_excepthook

    if "threading" in hooks:
        prior = threading.excepthook
        _installed_state["threading"] = prior

        def _eh_threading_hook(args, *, _prior=prior):
            try:
                exc = args.exc_value
                if exc is None and args.exc_type is not None:
                    exc = args.exc_type()
                _attach_tb(exc, args.exc_traceback)
                name = getattr(args.thread, "name", None) if args.thread else None
                header = "Uncaught exception in thread" + (
                    " " + repr(name) if name else ""
                ) + ":"
                _emit(
                    header + "\n" + _render_report(exc, style, describe_kwargs),
                    stream,
                )
            except BaseException:
                try:
                    _prior(args)
                except BaseException:
                    pass

        threading.excepthook = _eh_threading_hook

    if "unraisable" in hooks:
        prior = sys.unraisablehook
        _installed_state["unraisable"] = prior

        def _eh_unraisable_hook(args, *, _prior=prior):
            try:
                header = args.err_msg or "Exception ignored in"
                if args.object is not None:
                    header += ": " + _safe_repr(args.object)
                if args.exc_value is not None:
                    exc = args.exc_value
                    _attach_tb(exc, args.exc_traceback)
                    _emit(
                        header + "\n" + _render_report(exc, style, describe_kwargs),
                        stream,
                    )
                else:
                    _emit(header, stream)
            except BaseException:
                try:
                    _prior(args)
                except BaseException:
                    pass

        sys.unraisablehook = _eh_unraisable_hook


def uninstall():
    """Restore the hooks stashed by install(). Safe no-op when nothing is
    installed. Note: if something else replaced a hook AFTER install(),
    uninstall() still restores the pre-install hook (stash semantics)."""
    prior = _installed_state.pop("excepthook", None)
    if prior is not None:
        sys.excepthook = prior
    prior = _installed_state.pop("threading", None)
    if prior is not None:
        threading.excepthook = prior
    prior = _installed_state.pop("unraisable", None)
    if prior is not None:
        sys.unraisablehook = prior
    _installed_state.clear()


def install_asyncio(loop=None, *, style="concise", stream=None, **describe_kwargs):
    """Wire an asyncio event loop's exception handler to print full reports.

    Separate from install() because the handler is per-loop state, not a
    global hook. Call from inside the running loop (loop=None resolves via
    asyncio.get_running_loop(), raising RuntimeError outside one) or pass
    a loop explicitly. Returns the prior handler (possibly None); restore
    with loop.set_exception_handler(prior)."""
    import asyncio  # lazy: only consumer of asyncio in the module

    if loop is None:
        loop = asyncio.get_running_loop()
    describe_kwargs.setdefault("caller_context", False)
    _validate_hook_params(style, describe_kwargs)
    prior = loop.get_exception_handler()

    def _eh_asyncio_handler(loop_, context, *, _prior=prior):
        try:
            exc = context.get("exception")
            header = context.get("message") or "asyncio exception"
            if exc is not None:
                _emit(
                    header + "\n" + _render_report(exc, style, describe_kwargs),
                    stream,
                )
            else:
                _emit(
                    header + " (no exception object; context keys: "
                    + ", ".join(sorted(context)) + ")",
                    stream,
                )
        except BaseException:
            try:
                if _prior is not None:
                    _prior(loop_, context)
                else:
                    loop_.default_exception_handler(context)
            except BaseException:
                pass

    loop.set_exception_handler(_eh_asyncio_handler)
    return prior


# ---------------------------------------------------------------------------
# Task 17: @capture decorator + capturing() context manager
# ---------------------------------------------------------------------------
#
# Wrap a callable or a block; on exception, build a report and hand it to a
# callback (or emit it), then re-raise (default) or swallow per flag.
# Re-raise is the default so decorating a function observes without changing
# control flow; swallowing is an explicit opt-in. Only `catch` types
# (default Exception) are handled — KeyboardInterrupt / SystemExit pass
# through untouched unless you explicitly widen to BaseException.

def _validate_catch(catch):
    """Eager check that `catch` is an exception type or tuple thereof."""
    types_ = catch if isinstance(catch, tuple) else (catch,)
    if not types_:
        raise TypeError("catch must name at least one exception type")
    for t in types_:
        if not (isinstance(t, type) and issubclass(t, BaseException)):
            raise TypeError(
                "catch must be an exception type or tuple of them, got "
                + repr(t)
            )


def _deliver(report, on_report, style, stream):
    """Hand the report to the callback, or emit it when there's none. A
    broken callback falls back to emitting — the report must not vanish.
    Never raises."""
    try:
        if on_report is not None:
            on_report(report)
        else:
            _emit(
                report.for_claude() if style == "heavy" else report.to_string(),
                stream,
            )
    except BaseException:
        try:
            _emit(
                report.for_claude() if style == "heavy" else report.to_string(),
                stream,
            )
        except BaseException:
            pass


def capture(
    fn=None,
    *,
    on_report=None,
    reraise=True,
    default=None,
    catch=Exception,
    style="concise",
    stream=None,
    **describe_kwargs,
):
    """Decorator: build a full report whenever the wrapped callable raises.

        @capture                                  # bare: report to stderr, re-raise
        def risky(): ...

        @capture(on_report=crash_log.append)      # hand reports to a callback
        def risky(): ...

        @capture(reraise=False, default=-1)       # swallow: return default instead
        def risky(): ...

    Parameters:
        on_report: callable receiving the ErrorReport. None means emit
            (style/stream as in install()). A broken callback falls back
            to emitting — the report never vanishes.
        reraise: True (default) re-raises after reporting, so behavior is
            observably identical to the undecorated function. False
            swallows and returns `default`.
        default: return value when an exception is swallowed.
        catch: exception type (or tuple) to handle. Default Exception —
            KeyboardInterrupt / SystemExit pass through unreported.
        **describe_kwargs: forwarded to describe_error. (caller_context
            keeps its normal default here — unlike global hooks, a
            decorated function has meaningful callers.)

    async def functions are wrapped with an async wrapper (the await is
    inside the try), so awaited failures are captured the same way.
    Generator functions: only call-time errors are seen (the body runs at
    iteration time, outside the wrapper) — wrap the consuming loop with
    capturing() instead. Validation is eager at decoration time."""
    if fn is None:
        # Parameterized form: @capture(...) — return the real decorator.
        return functools.partial(
            capture,
            on_report=on_report,
            reraise=reraise,
            default=default,
            catch=catch,
            style=style,
            stream=stream,
            **describe_kwargs,
        )
    if not callable(fn):
        raise TypeError(
            "@capture must wrap a callable, got " + repr(type(fn).__name__)
        )
    _validate_catch(catch)
    _validate_hook_params(style, describe_kwargs)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def _async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except catch as e:
                _deliver(
                    describe_error(e, **describe_kwargs),
                    on_report, style, stream,
                )
                if reraise:
                    raise
                return default
        return _async_wrapper

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except catch as e:
            _deliver(
                describe_error(e, **describe_kwargs),
                on_report, style, stream,
            )
            if reraise:
                raise
            return default
    return _wrapper


class capturing:
    """Context manager twin of @capture for wrapping a block.

        with capturing(on_report=crash_log.append):
            risky()                       # reports, then re-raises

        with capturing(reraise=False) as cap:
            risky()                       # reports, then suppresses
        if cap.report is not None:
            ...                           # inspect what happened

    `.report` is None until an exception in `catch` is captured. Exceptions
    outside `catch` (KeyboardInterrupt, SystemExit by default) propagate
    unreported. One-shot: use a fresh instance per block."""

    def __init__(
        self,
        *,
        on_report=None,
        reraise=True,
        catch=Exception,
        style="concise",
        stream=None,
        **describe_kwargs,
    ):
        _validate_catch(catch)
        _validate_hook_params(style, describe_kwargs)
        self._on_report = on_report
        self._reraise = reraise
        self._catch = catch
        self._style = style
        self._stream = stream
        self._describe_kwargs = describe_kwargs
        self.report = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        if exc_value is None or not isinstance(exc_value, self._catch):
            return False  # nothing to do, or not ours — propagate
        _attach_tb(exc_value, exc_tb)
        self.report = describe_error(exc_value, **self._describe_kwargs)
        _deliver(self.report, self._on_report, self._style, self._stream)
        return not self._reraise  # True suppresses


# ---------------------------------------------------------------------------
# Task 20: logging integration — ReportFormatter
# ---------------------------------------------------------------------------
#
# A logging.Formatter whose exception rendering goes through describe_error,
# so `log.error("x", exc_info=True)` emits our report automatically:
#
#     handler.setFormatter(error_handler.ReportFormatter(
#         "%(levelname)s %(name)s: %(message)s"))
#
# Cache isolation: stdlib Formatter.format() caches rendered exception text
# on record.exc_text, shared across ALL handlers of the record. That leaks
# in both directions — our long report into plain handlers, or a plain
# traceback into ours (whichever formats first wins). Our format() override
# neither reads nor writes record.exc_text, so each handler renders
# independently and plain handlers keep their stdlib output.

class ReportFormatter(logging.Formatter):
    """logging.Formatter that expands exc_info through describe_error.

        handler.setFormatter(ReportFormatter(
            "%(levelname)s %(message)s",
            report_style="concise",              # or "heavy"
            describe_kwargs={"include_locals": True},
        ))
        log.error("db write failed", exc_info=True)

    Parameters beyond logging.Formatter's (fmt / datefmt / style / etc.):
        report_style: "concise" (default) or "heavy".
        describe_kwargs: dict forwarded to describe_error. Explicit dict
            rather than **kwargs so it can't collide with Formatter's own
            keyword args across Python versions. caller_context defaults
            off here (frames above the formatter are logging machinery);
            pass {"caller_context": True} to override.

    Safety: if report building fails, falls back to stdlib exception
    formatting; if THAT fails, emits a marker string. Never raises out of
    the logging call."""

    def __init__(
        self,
        fmt=None,
        datefmt=None,
        style="%",
        *,
        report_style="concise",
        describe_kwargs=None,
        **formatter_kwargs,
    ):
        super().__init__(fmt, datefmt, style, **formatter_kwargs)
        dk = dict(describe_kwargs or {})
        dk.setdefault("caller_context", False)
        _validate_hook_params(report_style, dk)  # eager, observer-suppressed
        self._report_style = report_style
        self._describe_kwargs = dk

    def formatException(self, ei):
        """Render the exc_info tuple as a describe_error report. Falls back
        to stdlib rendering on any internal failure."""
        try:
            exc = ei[1]
            if exc is None and ei[0] is not None:
                exc = ei[0]()
            if exc is None:
                return ""
            _attach_tb(exc, ei[2])
            report = describe_error(exc, **self._describe_kwargs)
            if self._report_style == "heavy":
                return report.for_claude()
            return report.to_string()
        except BaseException:
            try:
                return logging.Formatter.formatException(self, ei)
            except BaseException:
                return "<error_handler ReportFormatter failed>"

    def format(self, record):
        """Mirror of stdlib Formatter.format() minus the record.exc_text
        cache: we neither read it (a plain handler formatting first must
        not suppress our rendering) nor write it (our long report must not
        leak into other handlers' output)."""
        record.message = record.getMessage()
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)
        s = self.formatMessage(record)
        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            if exc_text:
                if s[-1:] != "\n":
                    s = s + "\n"
                s = s + exc_text
        if record.stack_info:
            if s[-1:] != "\n":
                s = s + "\n"
            s = s + self.formatStack(record.stack_info)
        return s


# ---------------------------------------------------------------------------
# Smoke test: run this file directly to see the handler in action.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    def deep(x):
        return shallow(x)

    def shallow(x):
        return int(x)

    try:
        deep("not a number")
    except Exception as e:
        report = describe_error(e)
        print("=" * 60)
        print("to_string():")
        print("=" * 60)
        print(report)
        print()
        print("=" * 60)
        print("to_dict():")
        print("=" * 60)
        for k, v in report.to_dict().items():
            print("  " + str(k) + ": " + repr(v))
