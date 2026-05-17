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

Design contract: this function NEVER raises. If introspection of the exception
fails partway through, the returned report records the partial failure in
`partial_failures` and carries on. If the handler itself collapses entirely
(e.g. MemoryError mid-walk), the report falls back to the most primitive
description possible: repr(exc) and type(exc).__name__.
"""

from __future__ import annotations

import contextvars
import linecache
import os
import platform
import re
import sys
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

def _capture_environment(env_vars, failures):
    """Capture Python/platform/process basics. Safe-wrapped end-to-end - if
    any single field blows up it gets recorded in partial_failures and the
    snapshot continues with the rest.

    env_vars (iterable of names) is the only way env vars are captured;
    passing None or [] means no env vars in the snapshot (default).
    """
    env = {}
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

    Three output flavors:
      to_dict()      structured, suitable for JSON / metrics pipelines
      to_string()    concise, traceback-style human format (also __str__)
      for_claude()   heavy / LLM-friendly edition (Task 9)
    """
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return dict(self.data)

    def to_string(self):
        return _format_concise(self.data)

    def for_claude(self):
        return _format_heavy(self.data)

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
    the frame is paused mid-call)."""
    return _frame_dict(
        tb.tb_frame, tb.tb_lineno, include_locals, source_context_lines, failures
    )


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
    out = {
        "file": filename,
        "line": lineno,
        "function": function,
        "code": source,
    }
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
# Task 5: Type-specific dispatch table
# ---------------------------------------------------------------------------
#
# Maps exception class -> extractor function. Lookup walks the type's MRO so
# subclasses inherit (FileNotFoundError gets the OSError extractor for free).
# Each extractor must return a dict and should be robust to missing attributes
# (built-in exceptions are remarkably inconsistent about which attrs they set).

_TYPE_EXTRACTORS = {}


def _register(exc_type):
    """Decorator to register a type-specific extractor."""
    def deco(fn):
        _TYPE_EXTRACTORS[exc_type] = fn
        return fn
    return deco


@_register(OSError)
def _extract_oserror(e):
    return {
        "errno": getattr(e, "errno", None),
        "strerror": getattr(e, "strerror", None),
        "filename": getattr(e, "filename", None),
        "filename2": getattr(e, "filename2", None),
        "winerror": getattr(e, "winerror", None),
    }


@_register(SyntaxError)
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


@_register(AttributeError)
def _extract_attributeerror(e):
    out = {"name": getattr(e, "name", None)}
    if hasattr(e, "obj"):
        out["obj"] = _safe_repr(e.obj)
    return out


@_register(KeyError)
def _extract_keyerror(e):
    args = getattr(e, "args", ())
    return {"missing_key": _safe_repr(args[0]) if args else None}


@_register(UnicodeError)
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

        try:
            failures = []
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
            return ErrorReport(data)
        finally:
            _active_redactors.reset(token)

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
        return ErrorReport({
            "error_handler_failed": True,
            "fallback_repr": fallback_repr,
            "fallback_type": fallback_type,
            "handler_failure": handler_failure_repr,
        })


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
        for frame in cc:
            if frame.get("truncated"):
                lines.append(
                    "  ... more frames exist beyond max_caller_frames"
                )
                continue
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
            locs = frame.get("locals")
            if locs:
                for k, v in locs.items():
                    lines.append("      " + str(k) + " = " + str(v))

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


def _render_one_concise(d, lines):
    """Render one exception's traceback + header + type-specific + notes into
    the running `lines` list. Used for both the primary exception and each
    chain link, so the layout is consistent throughout the report."""
    tb = d.get("traceback") or []
    if tb:
        lines.append("Traceback (most recent call last):")
        for frame in tb:
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
            locs = frame.get("locals")
            if locs:
                for k, v in locs.items():
                    lines.append("      " + str(k) + " = " + str(v))

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
    ts_parts = [str(k) + "=" + str(v) for k, v in ts.items() if v is not None]
    if ts_parts:
        lines.append("  [" + ", ".join(ts_parts) + "]")

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
    if ts:
        lines.append(indent + "Type-specific details:")
        for k, v in ts.items():
            lines.append(indent + "  " + str(k) + ": " + str(v))
    else:
        lines.append(
            indent
            + "Type-specific details: (no extractor registered for this exception type)"
        )

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
            code = frame.get("code")
            if code:
                lines.append(indent + "    Code: " + str(code))
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
