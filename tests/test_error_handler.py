"""
Unit tests for error_handler.describe_error.

Run with:
    python -m unittest test_error_handler.py

These tests pin down the function's CONTRACT, not implementation details.
Each test exercises one promise: never raises, dict shape stable, safety net
catches broken __repr__/__str__, chain walking honors guards, dispatch table
finds the right extractor via MRO, flags toggle behavior as documented.

For visual examples of actual output, see test_chain.py / test_dispatch.py /
test_locals.py / test_formatter.py / test_heavy.py / test_caller_context.py
/ test_group.py - those are runnable documentation rather than assertion
tests.
"""

import sys
import unittest

# Vendored verbatim from the standalone "Python ErrorHandler" repo's
# test_error_handler.py. Upstream imports the module under the bare top-level
# name ``error_handler``; in PySynthRack it lives at ``pysynthrack.error_handler``.
# Alias it under the bare name here so the upstream imports below (and the few
# ``import error_handler`` sites deeper in the file) resolve unchanged, which
# keeps this file a clean copy that's trivial to re-sync from upstream.
from pysynthrack import error_handler as _pysr_error_handler
sys.modules.setdefault("error_handler", _pysr_error_handler)

from error_handler import (
    describe_error,
    ErrorReport,
    register_redactor,
    redact_pattern,
    clear_redactors,
    register_extractor,
    unregister_extractor,
    install,
    uninstall,
    capture,
    capturing,
    register_observer,
    unregister_observer,
    clear_observers,
    ReportFormatter,
)


# ---------------------------------------------------------------------------
# Adversarial classes used across multiple tests
# ---------------------------------------------------------------------------

class BrokenStr(Exception):
    """Exception where str() raises - exercises the message-step safety net."""
    def __str__(self):
        raise RuntimeError("str() is broken")


class BrokenRepr:
    """Non-exception class with a broken __repr__. Used to test that locals
    capture survives values with hostile reprs without breaking the rest."""
    def __repr__(self):
        raise RuntimeError("repr is broken")


class HostileNonException:
    """Not a BaseException subclass. describe_error should still produce a
    usable report when handed one of these instead of crashing."""
    pass


# ---------------------------------------------------------------------------
# Happy path - the basic contract
# ---------------------------------------------------------------------------

class HappyPathTests(unittest.TestCase):

    def test_returns_error_report_instance(self):
        try:
            int("nope")
        except Exception as e:
            report = describe_error(e)
        self.assertIsInstance(report, ErrorReport)
        self.assertIsInstance(report.to_dict(), dict)
        self.assertIsInstance(report.to_string(), str)
        self.assertIsInstance(report.for_claude(), str)
        # __str__ should delegate to to_string().
        self.assertEqual(str(report), report.to_string())

    def test_dict_shape_contains_expected_keys(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e).to_dict()
        for key in (
            "type", "module", "message", "repr", "args", "notes",
            "extra_attrs", "type_specific", "traceback", "chain",
            "partial_failures",
        ):
            self.assertIn(key, d, "missing key: " + key)

    def test_basic_fields_populated(self):
        try:
            int("not a number")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertEqual(d["type"], "ValueError")
        self.assertEqual(d["module"], "builtins")
        self.assertIn("not a number", d["message"])
        self.assertIn("ValueError", d["repr"])
        self.assertEqual(d["partial_failures"], [])

    def test_traceback_frames_captured(self):
        def inner():
            raise RuntimeError("kaboom")
        try:
            inner()
        except Exception as e:
            d = describe_error(e).to_dict()
        # At least the inner() frame and its caller.
        self.assertGreaterEqual(len(d["traceback"]), 2)
        func_names = [f["function"] for f in d["traceback"]]
        self.assertIn("inner", func_names)
        for f in d["traceback"]:
            self.assertIn("file", f)
            self.assertIn("line", f)
            self.assertIn("function", f)
            self.assertIn("code", f)


# ---------------------------------------------------------------------------
# Bare call (sys.exc_info fallback)
# ---------------------------------------------------------------------------

class BareCallTests(unittest.TestCase):

    def test_bare_call_with_active_exception(self):
        try:
            int("nope")
        except Exception:
            d = describe_error().to_dict()
        self.assertEqual(d["type"], "ValueError")

    def test_bare_call_with_no_active_exception_returns_marker(self):
        d = describe_error().to_dict()
        self.assertTrue(d.get("no_active_exception"))
        self.assertFalse(d.get("error_handler_failed"))


# ---------------------------------------------------------------------------
# Safety net - the heart of the design
# ---------------------------------------------------------------------------

class SafetyNetTests(unittest.TestCase):

    def test_broken_str_records_partial_failure(self):
        try:
            raise BrokenStr("real message in args")
        except Exception as e:
            d = describe_error(e).to_dict()
        # message() failed -> fallback string
        self.assertEqual(d["message"], "<unrenderable>")
        # repr() still works (only __str__ is broken)
        self.assertIn("BrokenStr", d["repr"])
        # partial_failures should include a 'message' step entry
        steps = [f["step"] for f in d["partial_failures"]]
        self.assertIn("message", steps)

    def test_broken_repr_in_locals_uses_fallback(self):
        def trigger():
            bomb = BrokenRepr()
            benign = "still ok"
            raise RuntimeError("boom")
        try:
            trigger()
        except Exception as e:
            d = describe_error(e, include_locals=True).to_dict()
        # Find the trigger() frame.
        target = next(f for f in d["traceback"] if f["function"] == "trigger")
        locs = target["locals"]
        self.assertIn("bomb", locs)
        self.assertIn("benign", locs)
        # Benign value comes through cleanly; bomb gets the fallback string.
        self.assertEqual(locs["benign"], "'still ok'")
        self.assertIn("<unrepresentable", locs["bomb"])

    def test_hostile_nonexception_object_doesnt_crash(self):
        # Pass an instance of a class that isn't a BaseException subclass.
        # describe_error should NOT raise; it should still produce a usable report.
        report = describe_error(HostileNonException())
        d = report.to_dict()
        self.assertEqual(d["type"], "HostileNonException")
        # And to_string should also work without crashing.
        self.assertIsInstance(report.to_string(), str)


# ---------------------------------------------------------------------------
# Chain walking
# ---------------------------------------------------------------------------

class ChainTests(unittest.TestCase):

    def test_explicit_cause_chain(self):
        try:
            try:
                {}["missing"]
            except KeyError as e:
                raise ValueError("wrapped") from e
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertEqual(len(d["chain"]), 1)
        self.assertEqual(d["chain"][0]["relation"], "cause")
        self.assertEqual(d["chain"][0]["type"], "KeyError")

    def test_implicit_context_chain(self):
        try:
            try:
                {}["missing"]
            except KeyError:
                raise ValueError("wrapped")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertEqual(len(d["chain"]), 1)
        self.assertEqual(d["chain"][0]["relation"], "context")
        self.assertEqual(d["chain"][0]["type"], "KeyError")

    def test_no_chain_when_unchained(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertEqual(d["chain"], [])

    def test_cycle_detected_in_chain(self):
        try:
            a = RuntimeError("a")
            b = RuntimeError("b")
            a.__cause__ = b
            b.__cause__ = a
            raise a
        except Exception as e:
            d = describe_error(e).to_dict()
        markers = [c for c in d["chain"] if c.get("truncated") == "cycle_detected"]
        self.assertEqual(len(markers), 1)

    def test_max_chain_depth_respected(self):
        try:
            head = RuntimeError("head")
            cur = head
            for i in range(20):
                nxt = RuntimeError("link-" + str(i))
                cur.__cause__ = nxt
                cur = nxt
            raise head
        except Exception as e:
            d = describe_error(e, max_chain_depth=5).to_dict()
        real_links = [c for c in d["chain"] if not c.get("truncated")]
        self.assertEqual(len(real_links), 5)
        truncs = [c for c in d["chain"] if c.get("truncated") == "max_depth_reached"]
        self.assertEqual(len(truncs), 1)


# ---------------------------------------------------------------------------
# Type-specific dispatch
# ---------------------------------------------------------------------------

class DispatchTests(unittest.TestCase):

    def test_keyerror_type_specific_extractor(self):
        try:
            {"a": 1}["missing"]
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertIn("missing_key", d["type_specific"])
        self.assertIn("missing", d["type_specific"]["missing_key"])

    def test_oserror_type_specific_extractor(self):
        try:
            open("/definitely/not/a/real/path/anywhere.txt")
        except OSError as e:
            d = describe_error(e).to_dict()
        ts = d["type_specific"]
        self.assertIn("errno", ts)
        self.assertIn("strerror", ts)
        self.assertIn("filename", ts)

    def test_filenotfounderror_inherits_via_mro(self):
        # FileNotFoundError isn't directly registered; should walk MRO and
        # land on the OSError extractor.
        try:
            open("/nope/nope/nope.txt")
        except FileNotFoundError as e:
            d = describe_error(e).to_dict()
        self.assertEqual(d["type"], "FileNotFoundError")
        # Confirm OSError-shaped fields came through (proves MRO walking).
        self.assertIn("errno", d["type_specific"])

    def test_unregistered_type_returns_empty_dict(self):
        try:
            int("nope")
        except ValueError as e:
            d = describe_error(e).to_dict()
        self.assertEqual(d["type_specific"], {})


# ---------------------------------------------------------------------------
# include_locals flag
# ---------------------------------------------------------------------------

class LocalsFlagTests(unittest.TestCase):

    def test_include_locals_default_off(self):
        try:
            x = "secret"
            int("nope")
        except Exception as e:
            d = describe_error(e).to_dict()
        for f in d["traceback"]:
            self.assertNotIn("locals", f)

    def test_include_locals_captures_frame_locals(self):
        def fn_with_locals():
            magic_number = 42
            magic_string = "abracadabra"
            int("nope")
        try:
            fn_with_locals()
        except Exception as e:
            d = describe_error(e, include_locals=True).to_dict()
        target = next(
            f for f in d["traceback"] if f["function"] == "fn_with_locals"
        )
        self.assertIn("locals", target)
        self.assertEqual(target["locals"]["magic_number"], "42")
        self.assertEqual(target["locals"]["magic_string"], "'abracadabra'")


# ---------------------------------------------------------------------------
# Source context window (new)
# ---------------------------------------------------------------------------

class SourceContextTests(unittest.TestCase):

    def test_source_context_captured_by_default(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e).to_dict()
        for f in d["traceback"]:
            self.assertIn("source_context", f)
            self.assertIsInstance(f["source_context"], list)

    def test_source_context_marks_error_line(self):
        def boom():
            raise RuntimeError("kaboom")
        try:
            boom()
        except Exception as e:
            d = describe_error(e).to_dict()
        target = next(f for f in d["traceback"] if f["function"] == "boom")
        ctx = target["source_context"]
        # Exactly one line should be the error line.
        marked = [c for c in ctx if c["is_error_line"]]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["lineno"], target["line"])

    def test_source_context_is_dedented(self):
        # Function body is indented; the captured window should have the
        # common leading whitespace stripped so it reads cleanly.
        def deeply():
            def nested():
                raise RuntimeError("kaboom")
            nested()
        try:
            deeply()
        except Exception as e:
            d = describe_error(e).to_dict()
        target = next(f for f in d["traceback"] if f["function"] == "nested")
        ctx = target["source_context"]
        # At least one non-blank line should start with non-whitespace after
        # dedent (otherwise dedent didn't run).
        non_blank = [c["text"] for c in ctx if c["text"].strip()]
        self.assertTrue(non_blank, "expected captured non-blank lines")
        self.assertTrue(
            any(not line.startswith(" ") for line in non_blank),
            "expected at least one line to be flush left after dedent",
        )

    def test_source_context_disabled_when_zero(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e, source_context_lines=0).to_dict()
        for f in d["traceback"]:
            self.assertNotIn("source_context", f)


# ---------------------------------------------------------------------------
# Caller context (new)
# ---------------------------------------------------------------------------

class CallerContextTests(unittest.TestCase):

    def test_caller_context_captured_by_default(self):
        def thrower():
            raise RuntimeError("kaboom")
        try:
            thrower()
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertIn("caller_context", d)
        # Test method is itself a caller above the catch -> non-empty list.
        self.assertGreater(len(d["caller_context"]), 0)

    def test_caller_context_skips_error_handler_frames(self):
        import os
        import error_handler as eh_module
        eh_file = os.path.normcase(os.path.abspath(eh_module.__file__))
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e).to_dict()
        # No captured caller frame should be exactly the error_handler module
        # file. (Substring matching is wrong here because the test file path
        # CONTAINS 'error_handler.py' as a substring.)
        for f in d["caller_context"]:
            if f.get("truncated"):
                continue
            cap_file = os.path.normcase(os.path.abspath(f["file"]))
            self.assertNotEqual(
                cap_file, eh_file,
                "caller_context leaked an internal frame: " + str(f),
            )

    def test_caller_context_disabled_when_flag_false(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e, caller_context=False).to_dict()
        self.assertNotIn("caller_context", d)

    def test_caller_context_respects_max_frames(self):
        # Recurse deeper than the cap so we know more frames exist.
        def recurse(n):
            if n <= 0:
                try:
                    int("nope")
                except Exception as e:
                    return describe_error(e, max_caller_frames=3).to_dict()
            return recurse(n - 1)
        d = recurse(10)
        real = [f for f in d["caller_context"] if not f.get("truncated")]
        self.assertEqual(len(real), 3)
        truncs = [
            f for f in d["caller_context"]
            if f.get("truncated") == "max_caller_frames_reached"
        ]
        self.assertEqual(len(truncs), 1)

    def test_caller_context_honors_include_locals(self):
        def helper():
            sentinel_name = "sentinel_value"
            try:
                int("nope")
            except Exception as e:
                return describe_error(e, include_locals=True).to_dict()
        d = helper()
        # Find the helper frame in caller_context (it's the immediate frame
        # above the catch since the catch IS in helper).
        # Actually the catch is in helper, so caller_context[0] is the test
        # method that called helper. The helper frame is inside traceback,
        # not caller_context. Check that caller_context frames carry locals
        # when the flag is on.
        for f in d["caller_context"]:
            if f.get("truncated"):
                continue
            self.assertIn("locals", f)


# ---------------------------------------------------------------------------
# ExceptionGroup support (new) - Python 3.11+
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    sys.version_info >= (3, 11),
    "ExceptionGroup requires Python 3.11+ (or `exceptiongroup` backport)",
)
class GroupTests(unittest.TestCase):

    def _make_group(self):
        children = []
        try:
            raise TypeError("type-child")
        except TypeError as e:
            children.append(e)
        try:
            raise ValueError("value-child")
        except ValueError as e:
            children.append(e)
        return ExceptionGroup("outer", children)

    def test_group_children_field_present_for_group(self):
        try:
            raise self._make_group()
        except BaseException as e:
            d = describe_error(e).to_dict()
        self.assertIn("group_children", d)
        self.assertEqual(len(d["group_children"]), 2)

    def test_group_children_field_absent_for_non_group(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertNotIn("group_children", d)

    def test_group_children_have_full_introspection(self):
        try:
            raise self._make_group()
        except BaseException as e:
            d = describe_error(e).to_dict()
        types = [c["type"] for c in d["group_children"]]
        self.assertIn("TypeError", types)
        self.assertIn("ValueError", types)
        # Each child has its own traceback.
        for child in d["group_children"]:
            self.assertIn("traceback", child)
            self.assertGreater(len(child["traceback"]), 0)

    def test_nested_groups_recurse(self):
        inner_children = []
        try:
            raise KeyError("inner-key")
        except KeyError as e:
            inner_children.append(e)
        inner = ExceptionGroup("inner", inner_children)
        outer = ExceptionGroup("outer", [inner])
        try:
            raise outer
        except BaseException as e:
            d = describe_error(e).to_dict()
        self.assertEqual(len(d["group_children"]), 1)
        inner_data = d["group_children"][0]
        self.assertIn("group_children", inner_data)
        self.assertEqual(inner_data["group_children"][0]["type"], "KeyError")

    def test_type_specific_extractors_fire_on_group_children(self):
        # KeyError's missing_key extractor should still trigger when the
        # KeyError is buried inside a group.
        children = []
        try:
            {}["missing"]
        except KeyError as e:
            children.append(e)
        group = ExceptionGroup("g", children)
        try:
            raise group
        except BaseException as e:
            d = describe_error(e).to_dict()
        child = d["group_children"][0]
        self.assertIn("missing_key", child["type_specific"])
        self.assertIn("missing", child["type_specific"]["missing_key"])

    def test_max_group_depth_caps_nesting(self):
        # Build a 5-deep group nest, ask for depth 2 -> truncation marker.
        inner = ExceptionGroup("d5", [RuntimeError("leaf")])
        for i in range(4, 0, -1):
            inner = ExceptionGroup("d" + str(i), [inner])
        try:
            raise inner
        except BaseException as e:
            d = describe_error(e, max_group_depth=2).to_dict()
        # Drill down until we hit the truncation marker.
        node = d
        depth = 0
        while "group_children" in node and node["group_children"]:
            child = node["group_children"][0]
            if child.get("truncated") == "max_group_depth_reached":
                break
            node = child
            depth += 1
            if depth > 10:
                self.fail("never hit max_group_depth truncation marker")
        else:
            self.fail("never reached a truncation marker")


# ---------------------------------------------------------------------------
# Environment snapshot (new)
# ---------------------------------------------------------------------------

class EnvironmentTests(unittest.TestCase):

    def test_environment_captured_by_default(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertIn("environment", d)
        env = d["environment"]
        for key in ("python_version", "platform", "cwd", "pid", "argv"):
            self.assertIn(key, env, "missing env key: " + key)

    def test_environment_can_be_disabled(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e, environment_snapshot=False).to_dict()
        self.assertNotIn("environment", d)

    def test_env_vars_not_captured_by_default(self):
        try:
            int("nope")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertNotIn("env_vars", d["environment"])

    def test_env_vars_captured_when_requested(self):
        import os
        os.environ["ERROR_HANDLER_TEST_VAR"] = "hello"
        try:
            try:
                int("nope")
            except Exception as e:
                d = describe_error(
                    e, env_vars=["ERROR_HANDLER_TEST_VAR", "PATH"],
                ).to_dict()
            self.assertIn("env_vars", d["environment"])
            self.assertEqual(
                d["environment"]["env_vars"]["ERROR_HANDLER_TEST_VAR"], "hello",
            )
        finally:
            os.environ.pop("ERROR_HANDLER_TEST_VAR", None)


# ---------------------------------------------------------------------------
# Redaction hooks (new)
# ---------------------------------------------------------------------------

class RedactionTests(unittest.TestCase):

    def setUp(self):
        # Each test starts with a clean global registry.
        clear_redactors()

    def tearDown(self):
        clear_redactors()

    def test_redactor_applies_to_locals(self):
        register_redactor(redact_pattern(r"hunter2"))
        def helper():
            secret = "hunter2"
            int("nope")
        try:
            helper()
        except Exception as e:
            d = describe_error(e, include_locals=True).to_dict()
        target = next(f for f in d["traceback"] if f["function"] == "helper")
        self.assertEqual(target["locals"]["secret"], "'<redacted>'")

    def test_redactor_applies_to_message(self):
        register_redactor(redact_pattern(r"hunter2"))
        try:
            raise RuntimeError("login failed with hunter2 token")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertNotIn("hunter2", d["message"])
        self.assertIn("<redacted>", d["message"])

    def test_redactor_applies_to_source_context(self):
        # Hardcoded secret in source - redactor should catch it in both
        # the single `code` line and the source_context window.
        register_redactor(redact_pattern(r"sk-[a-z0-9]+"))
        def helper():
            api_key = "sk-totallysecret123"  # noqa: F841
            int("nope")
        try:
            helper()
        except Exception as e:
            d = describe_error(e).to_dict()
        target = next(f for f in d["traceback"] if f["function"] == "helper")
        for line in target["source_context"]:
            self.assertNotIn("sk-totallysecret123", line["text"])

    def test_per_call_redactors_override_global(self):
        register_redactor(redact_pattern(r"hunter2"))
        try:
            raise RuntimeError("message with hunter2 in it")
        except Exception as e:
            # Pass [] to disable redaction entirely for this call.
            d = describe_error(e, redactors=[]).to_dict()
        self.assertIn("hunter2", d["message"])

    def test_broken_redactor_doesnt_crash(self):
        def bad(s):
            raise RuntimeError("redactor is broken")
        register_redactor(bad)
        try:
            raise RuntimeError("some message")
        except Exception as e:
            # Must not raise; should produce a usable report.
            d = describe_error(e).to_dict()
        self.assertIn("message", d)
        self.assertEqual(d["message"], "some message")

    def test_clear_redactors_works(self):
        register_redactor(redact_pattern(r"hunter2"))
        clear_redactors()
        try:
            raise RuntimeError("hunter2 should be visible")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertIn("hunter2", d["message"])


class ExtractorRegistrationTests(unittest.TestCase):
    """Task 15: public register_extractor / unregister_extractor surface."""

    class AppError(Exception):
        def __init__(self, msg, request_id=None):
            super().__init__(msg)
            self.request_id = request_id

    def tearDown(self):
        # Never leave a test registration behind for other tests.
        unregister_extractor(self.AppError)

    def _raise_and_describe(self):
        try:
            raise self.AppError("boom", request_id="req-42")
        except Exception as e:
            return describe_error(e).to_dict()

    def test_registered_extractor_fires(self):
        @register_extractor(self.AppError)
        def _extract(e):
            return {"request_id": getattr(e, "request_id", None)}
        d = self._raise_and_describe()
        self.assertEqual(d["type_specific"]["request_id"], "req-42")

    def test_subclass_inherits_via_mro(self):
        @register_extractor(self.AppError)
        def _extract(e):
            return {"request_id": getattr(e, "request_id", None)}
        class ChildError(self.AppError):
            pass
        try:
            raise ChildError("boom", request_id="req-child")
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertEqual(d["type_specific"]["request_id"], "req-child")

    def test_unregister_removes_and_returns(self):
        @register_extractor(self.AppError)
        def _extract(e):
            return {"request_id": None}
        removed = unregister_extractor(self.AppError)
        self.assertIs(removed, _extract)
        d = self._raise_and_describe()
        self.assertEqual(d["type_specific"], {})
        # Second unregister is a harmless no-op returning None.
        self.assertIsNone(unregister_extractor(self.AppError))

    def test_broken_extractor_lands_in_partial_failures(self):
        @register_extractor(self.AppError)
        def _extract(e):
            raise RuntimeError("extractor is broken")
        d = self._raise_and_describe()
        # Report still usable, failure recorded, type_specific falls back.
        self.assertEqual(d["message"], "boom")
        self.assertEqual(d["type_specific"], {})
        self.assertTrue(
            any("dispatch[AppError]" in f["step"] for f in d["partial_failures"])
        )

    def test_non_exception_type_raises_typeerror(self):
        with self.assertRaises(TypeError):
            register_extractor(str)
        with self.assertRaises(TypeError):
            register_extractor(ValueError("an instance, not a type"))

    def test_override_seed_then_restore(self):
        # Registering for KeyError replaces the seeded extractor...
        @register_extractor(KeyError)
        def _extract(e):
            return {"custom": True}
        try:
            try:
                {}["nope"]
            except Exception as e:
                d = describe_error(e).to_dict()
            self.assertEqual(d["type_specific"], {"custom": True})
        finally:
            # ...and unregistering restores normal behavior for everyone else.
            unregister_extractor(KeyError)
            from error_handler import _extract_keyerror
            register_extractor(KeyError)(_extract_keyerror)
        try:
            {}["nope"]
        except Exception as e:
            d = describe_error(e).to_dict()
        self.assertEqual(d["type_specific"]["missing_key"], "'nope'")


class InstallHookTests(unittest.TestCase):
    """Task 16: install() / uninstall() global hook wiring."""

    def setUp(self):
        import threading
        self._orig_sys_hook = sys.excepthook
        self._orig_thread_hook = threading.excepthook
        self._orig_unraisable_hook = sys.unraisablehook

    def tearDown(self):
        import threading
        uninstall()
        # Belt and braces: restore originals even if a test broke state.
        sys.excepthook = self._orig_sys_hook
        threading.excepthook = self._orig_thread_hook
        sys.unraisablehook = self._orig_unraisable_hook

    def _make_exc(self):
        try:
            raise ValueError("hook-test boom")
        except ValueError as e:
            return e

    def test_install_replaces_and_uninstall_restores_identity(self):
        import threading
        before = (sys.excepthook, threading.excepthook, sys.unraisablehook)
        install()
        self.assertNotEqual(sys.excepthook, before[0])
        self.assertNotEqual(threading.excepthook, before[1])
        self.assertNotEqual(sys.unraisablehook, before[2])
        uninstall()
        self.assertIs(sys.excepthook, before[0])
        self.assertIs(threading.excepthook, before[1])
        self.assertIs(sys.unraisablehook, before[2])

    def test_excepthook_writes_report_to_stream(self):
        import io
        buf = io.StringIO()
        install(hooks="excepthook", stream=buf)
        e = self._make_exc()
        sys.excepthook(type(e), e, e.__traceback__)
        out = buf.getvalue()
        self.assertIn("hook-test boom", out)
        self.assertIn("ValueError", out)

    def test_heavy_style(self):
        import io
        buf = io.StringIO()
        install(hooks="excepthook", style="heavy", stream=buf)
        e = self._make_exc()
        sys.excepthook(type(e), e, e.__traceback__)
        self.assertIn("PRIMARY EXCEPTION", buf.getvalue())

    def test_double_install_does_not_self_stash(self):
        original = sys.excepthook
        install(hooks="excepthook")
        first_hook = sys.excepthook
        install(hooks="excepthook")  # re-install: restore-then-rewire
        self.assertIsNot(sys.excepthook, first_hook)
        uninstall()
        self.assertIs(sys.excepthook, original)

    def test_uninstall_when_not_installed_is_noop(self):
        before = sys.excepthook
        uninstall()
        self.assertIs(sys.excepthook, before)

    def test_eager_validation(self):
        with self.assertRaises(ValueError):
            install(hooks="not_a_hook")
        with self.assertRaises(ValueError):
            install(style="florid")
        with self.assertRaises(TypeError):
            install(bogus_describe_kwarg=True)
        # Failed installs must leave no partial wiring behind.
        self.assertIs(sys.excepthook, self._orig_sys_hook)

    def test_threading_hook_reports_thread_exception(self):
        import io
        import threading
        buf = io.StringIO()
        install(hooks="threading", stream=buf)
        def boom():
            raise RuntimeError("thread boom")
        t = threading.Thread(target=boom, name="hook-test-thread")
        t.start()
        t.join()
        out = buf.getvalue()
        self.assertIn("Uncaught exception in thread", out)
        self.assertIn("hook-test-thread", out)
        self.assertIn("thread boom", out)

    def test_unraisable_hook_reports(self):
        import io
        import types
        buf = io.StringIO()
        install(hooks="unraisable", stream=buf)
        e = self._make_exc()
        args = types.SimpleNamespace(
            exc_type=type(e), exc_value=e, exc_traceback=e.__traceback__,
            err_msg="Exception ignored while testing", object=object(),
        )
        sys.unraisablehook(args)
        out = buf.getvalue()
        self.assertIn("Exception ignored while testing", out)
        self.assertIn("hook-test boom", out)

    def test_broken_stream_falls_back_to_prior_hook(self):
        calls = []
        def recording_prior(exc_type, exc_value, exc_tb):
            calls.append(exc_value)
        sys.excepthook = recording_prior
        class BrokenStream:
            def write(self, s):
                raise OSError("stream is broken")
            def flush(self):
                raise OSError("stream is broken")
        install(hooks="excepthook", stream=BrokenStream())
        e = self._make_exc()
        sys.excepthook(type(e), e, e.__traceback__)  # must not raise
        self.assertEqual(calls, [e])

    def test_caller_context_defaults_off_in_hooks(self):
        # The heavy formatter always prints the CALLER CONTEXT section
        # header; "off" means the not-captured placeholder, not absence.
        import io
        buf = io.StringIO()
        install(hooks="excepthook", style="heavy", stream=buf)
        e = self._make_exc()
        sys.excepthook(type(e), e, e.__traceback__)
        self.assertIn("(not captured - caller_context=False", buf.getvalue())


class CaptureDecoratorTests(unittest.TestCase):
    """Task 17: @capture decorator."""

    def test_bare_form_reports_and_reraises(self):
        import io
        buf = io.StringIO()
        # Bare form has no stream param, so verify via parameterized
        # form here and bare form separately for wrapping mechanics.
        @capture(stream=buf)
        def boom():
            raise ValueError("decorated boom")
        with self.assertRaises(ValueError):
            boom()
        out = buf.getvalue()
        self.assertIn("decorated boom", out)
        self.assertIn("ValueError", out)

    def test_bare_form_wraps_without_parens(self):
        @capture
        def fine(x):
            return x * 2
        self.assertEqual(fine(21), 42)
        self.assertEqual(fine.__name__, "fine")

    def test_callback_receives_error_report(self):
        reports = []
        @capture(on_report=reports.append)
        def boom():
            raise KeyError("cb")
        with self.assertRaises(KeyError):
            boom()
        self.assertEqual(len(reports), 1)
        self.assertIsInstance(reports[0], ErrorReport)
        self.assertEqual(reports[0].to_dict()["type"], "KeyError")

    def test_swallow_returns_default(self):
        @capture(on_report=lambda r: None, reraise=False, default=-1)
        def boom():
            raise RuntimeError("swallowed")
        self.assertEqual(boom(), -1)

    def test_normal_return_passes_through(self):
        @capture(on_report=lambda r: None, reraise=False, default=-1)
        def fine():
            return "ok"
        self.assertEqual(fine(), "ok")

    def test_catch_filter_lets_others_propagate(self):
        reports = []
        @capture(on_report=reports.append, catch=ValueError)
        def boom():
            raise TypeError("not mine")
        with self.assertRaises(TypeError):
            boom()
        self.assertEqual(reports, [])

    def test_keyboardinterrupt_not_caught_by_default(self):
        reports = []
        @capture(on_report=reports.append, reraise=False)
        def boom():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            boom()
        self.assertEqual(reports, [])

    def test_broken_callback_falls_back_to_emit(self):
        import io
        buf = io.StringIO()
        def bad_callback(report):
            raise RuntimeError("callback is broken")
        @capture(on_report=bad_callback, reraise=False, stream=buf)
        def boom():
            raise ValueError("must not vanish")
        boom()  # must not raise
        self.assertIn("must not vanish", buf.getvalue())

    def test_async_function_captured(self):
        import asyncio
        reports = []
        @capture(on_report=reports.append, reraise=False, default="fell back")
        async def aboom():
            raise ValueError("async boom")
        result = asyncio.run(aboom())
        self.assertEqual(result, "fell back")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].to_dict()["message"], "async boom")

    def test_async_reraise_propagates(self):
        import asyncio
        @capture(on_report=lambda r: None)
        async def aboom():
            raise ValueError("async reraise")
        with self.assertRaises(ValueError):
            asyncio.run(aboom())

    def test_eager_validation(self):
        with self.assertRaises(TypeError):
            capture(lambda: None, catch=42)
        with self.assertRaises(ValueError):
            capture(lambda: None, style="florid")
        with self.assertRaises(TypeError):
            capture(lambda: None, bogus_describe_kwarg=True)
        with self.assertRaises(TypeError):
            capture("not callable")


class CapturingContextManagerTests(unittest.TestCase):
    """Task 17: capturing() context manager."""

    def test_reports_and_reraises_by_default(self):
        reports = []
        with self.assertRaises(ValueError):
            with capturing(on_report=reports.append):
                raise ValueError("cm boom")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].to_dict()["message"], "cm boom")

    def test_suppresses_when_reraise_false(self):
        with capturing(on_report=lambda r: None, reraise=False) as cap:
            raise RuntimeError("suppressed")
        # Execution continues here — suppression worked.
        self.assertIsNotNone(cap.report)
        self.assertEqual(cap.report.to_dict()["message"], "suppressed")

    def test_report_none_when_no_exception(self):
        with capturing(on_report=lambda r: None, reraise=False) as cap:
            pass
        self.assertIsNone(cap.report)

    def test_catch_filter_propagates_others_unreported(self):
        reports = []
        with self.assertRaises(TypeError):
            with capturing(on_report=reports.append, catch=ValueError,
                           reraise=False):
                raise TypeError("not mine")
        self.assertEqual(reports, [])

    def test_eager_validation_at_construction(self):
        with self.assertRaises(TypeError):
            capturing(catch="nope")
        with self.assertRaises(ValueError):
            capturing(style="florid")
        with self.assertRaises(TypeError):
            capturing(bogus_describe_kwarg=True)


class ObserverTests(unittest.TestCase):
    """Task 18: observer hooks."""

    def setUp(self):
        clear_observers()

    def tearDown(self):
        clear_observers()

    def _describe_a_boom(self):
        try:
            raise ValueError("observed boom")
        except ValueError as e:
            return describe_error(e)

    def test_observer_fires_with_the_returned_report(self):
        seen = []
        register_observer(seen.append)
        report = self._describe_a_boom()
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0], report)  # same object, not a copy
        self.assertIsInstance(seen[0], ErrorReport)

    def test_multiple_observers_fire_in_registration_order(self):
        order = []
        register_observer(lambda r: order.append("first"))
        register_observer(lambda r: order.append("second"))
        self._describe_a_boom()
        self.assertEqual(order, ["first", "second"])

    def test_decorator_style_returns_fn(self):
        @register_observer
        def obs(report):
            pass
        self.assertTrue(callable(obs))
        self.assertTrue(unregister_observer(obs))

    def test_broken_observer_does_not_break_report_or_later_observers(self):
        seen = []
        def broken(report):
            raise RuntimeError("observer is broken")
        register_observer(broken)
        register_observer(seen.append)
        report = self._describe_a_boom()
        self.assertEqual(report.to_dict()["message"], "observed boom")
        self.assertEqual(len(seen), 1)

    def test_unregister_observer_returns_flags(self):
        def obs(report):
            pass
        register_observer(obs)
        self.assertTrue(unregister_observer(obs))
        self.assertFalse(unregister_observer(obs))  # second time: not present
        self._describe_a_boom()  # and it really is gone — no error, no call

    def test_clear_observers(self):
        seen = []
        register_observer(seen.append)
        clear_observers()
        self._describe_a_boom()
        self.assertEqual(seen, [])

    def test_no_firing_for_no_active_exception_marker(self):
        seen = []
        register_observer(seen.append)
        report = describe_error()  # bare call, nothing active
        self.assertTrue(report.to_dict().get("no_active_exception"))
        self.assertEqual(seen, [])

    def test_reentrant_describe_error_does_not_refire(self):
        # NB: assertions must live OUTSIDE the observer — exceptions
        # raised inside observers (including AssertionError) are
        # swallowed by design. Collect, then assert.
        calls = []
        inner_reports = []
        def nosy(report):
            calls.append("outer-fire")
            # An observer that itself uses describe_error: must build a
            # normal report but NOT re-fire the observer list.
            try:
                raise KeyError("inner")
            except KeyError as e:
                inner_reports.append(describe_error(e))
        register_observer(nosy)
        self._describe_a_boom()
        self.assertEqual(calls, ["outer-fire"])  # exactly once, no recursion
        self.assertEqual(len(inner_reports), 1)
        self.assertEqual(inner_reports[0].to_dict()["type"], "KeyError")

    def test_observers_fire_through_capture_and_hooks(self):
        seen = []
        register_observer(seen.append)
        @capture(on_report=lambda r: None, reraise=False)
        def boom():
            raise RuntimeError("via capture")
        boom()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].to_dict()["message"], "via capture")

    def test_validation_probe_reports_never_reach_observers(self):
        # Regression: install()/@capture eager validation makes a probe
        # describe_error call. Probe reports must not leak to pipelines.
        seen = []
        register_observer(seen.append)
        @capture(on_report=lambda r: None, include_locals=True)
        def fine():
            return "no crash here"
        fine()
        install(hooks="excepthook", include_locals=True)
        uninstall()
        self.assertEqual(seen, [])


class SerializerTests(unittest.TestCase):
    """Task 19: to_json() / to_markdown()."""

    def _report(self, **kwargs):
        try:
            raise ValueError("serialize me")
        except ValueError as e:
            return describe_error(e, **kwargs)

    def test_to_json_round_trips(self):
        import json
        d = json.loads(self._report().to_json())
        self.assertEqual(d["type"], "ValueError")
        self.assertEqual(d["message"], "serialize me")
        self.assertIsInstance(d["traceback"], list)

    def test_to_json_handles_non_serializable_values(self):
        # args are _safe_repr'd at capture time (always strings), so the
        # default=str hook's real customers are RAW values returned by
        # custom extractors — exercise that path directly.
        import json
        class Widget:
            def __repr__(self):
                return "<widget#7>"
        class CustomError(Exception):
            pass
        @register_extractor(CustomError)
        def _extract(e):
            return {"raw_set": {1, 2, 3}, "raw_obj": Widget()}  # not JSON-native
        try:
            try:
                raise CustomError("with raw extras")
            except CustomError as e:
                out = describe_error(e).to_json()
        finally:
            unregister_extractor(CustomError)
        d = json.loads(out)
        self.assertIn("widget#7", str(d["type_specific"]["raw_obj"]))
        self.assertIn("1", str(d["type_specific"]["raw_set"]))

    def test_to_json_survives_hostile_str_in_args(self):
        import json
        class Hostile:
            def __str__(self):
                raise RuntimeError("str is broken")
            def __repr__(self):
                raise RuntimeError("repr is broken")
        try:
            raise RuntimeError(Hostile())
        except RuntimeError as e:
            out = describe_error(e).to_json()
        d = json.loads(out)  # must still parse
        self.assertEqual(d["type"], "RuntimeError")

    def test_to_json_indent_and_sort(self):
        compact = self._report().to_json()
        pretty = self._report().to_json(indent=2)
        self.assertNotIn("\n", compact)
        self.assertIn("\n", pretty)
        s = self._report().to_json(sort_keys=True)
        import json
        keys = list(json.loads(s).keys())
        self.assertEqual(keys, sorted(keys))

    def test_to_markdown_basic_structure(self):
        md = self._report().to_markdown()
        self.assertIn("## ValueError: serialize me", md)
        self.assertIn("### Traceback", md)
        self.assertIn("````text", md)  # four-backtick fence
        self.assertIn("<details>", md)
        self.assertIn("Full report (heavy edition)", md)
        self.assertIn("PRIMARY EXCEPTION", md)  # heavy inside details

    def test_to_markdown_location_line(self):
        md = self._report().to_markdown()
        self.assertIn("**Location:** `", md)
        self.assertIn("test_error_handler.py", md)
        self.assertIn("in `_report`", md)

    def test_to_markdown_chain_section(self):
        try:
            try:
                raise KeyError("root")
            except KeyError as inner:
                raise RuntimeError("wrapper") from inner
        except RuntimeError as e:
            md = describe_error(e).to_markdown()
        self.assertIn("### Exception chain", md)
        self.assertIn("**KeyError**", md)
        self.assertIn("_(cause)_", md)

    def test_to_markdown_partial_failures_warning(self):
        try:
            raise BrokenStr("bad str")
        except BrokenStr as e:
            md = describe_error(e).to_markdown()
        self.assertIn("internal capture issue(s)", md)

    def test_to_markdown_marker_reports_dont_crash(self):
        md = describe_error().to_markdown()  # no active exception
        self.assertIn("No active exception", md)

    def test_serializers_via_observer_pipeline(self):
        # The advertised use case: observer ships JSON somewhere.
        shipped = []
        register_observer(lambda r: shipped.append(r.to_json()))
        try:
            try:
                raise ValueError("pipeline test")
            except ValueError as e:
                describe_error(e)
        finally:
            clear_observers()
        import json
        self.assertEqual(json.loads(shipped[0])["message"], "pipeline test")


class ReportFormatterTests(unittest.TestCase):
    """Task 20: logging integration."""

    def setUp(self):
        import io
        import logging
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setFormatter(
            ReportFormatter("%(levelname)s %(message)s")
        )
        # Unique logger per test; never propagate to root.
        self.logger = logging.getLogger(
            "eh-test-" + self._testMethodName
        )
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()

    def _log_a_boom(self, **log_kwargs):
        try:
            lookup = {}
            lookup["missing"]
        except KeyError:
            self.logger.error("operation failed", exc_info=True, **log_kwargs)

    def test_exc_info_renders_our_report(self):
        self._log_a_boom()
        out = self.buf.getvalue()
        self.assertIn("ERROR operation failed", out)
        self.assertIn("KeyError", out)
        self.assertIn("[missing_key='missing']", out)  # our concise marker

    def test_heavy_style(self):
        import logging
        self.handler.setFormatter(ReportFormatter(
            "%(message)s", report_style="heavy",
        ))
        self._log_a_boom()
        self.assertIn("PRIMARY EXCEPTION", self.buf.getvalue())

    def test_no_exc_info_is_untouched(self):
        self.logger.warning("plain message")
        self.assertEqual(self.buf.getvalue(), "WARNING plain message\n")

    def test_does_not_write_exc_text_cache(self):
        import logging
        captured = {}
        class Recording(logging.Handler):
            def emit(self, rec):
                captured["record"] = rec
        rec_handler = Recording()
        self.logger.addHandler(rec_handler)
        try:
            self._log_a_boom()
        finally:
            self.logger.removeHandler(rec_handler)
        # Our formatter ran (StreamHandler emits first, insertion order)
        # yet the shared record's cache must remain unset.
        self.assertIn("[missing_key=", self.buf.getvalue())
        self.assertFalse(getattr(captured["record"], "exc_text", None))

    def test_ignores_pre_poisoned_exc_text_cache(self):
        import logging
        try:
            raise ValueError("cache poison test")
        except ValueError:
            import sys as _sys
            record = self.logger.makeRecord(
                self.logger.name, logging.ERROR, __file__, 0,
                "msg", (), _sys.exc_info(),
            )
        # Simulate another handler's formatter having cached plain text.
        record.exc_text = "PLAIN STDLIB TRACEBACK TEXT"
        out = self.handler.format(record)
        self.assertNotIn("PLAIN STDLIB TRACEBACK", out)
        self.assertIn("cache poison test", out)

    def test_plain_handler_alongside_stays_plain(self):
        import io
        import logging
        plain_buf = io.StringIO()
        plain_handler = logging.StreamHandler(plain_buf)
        plain_handler.setFormatter(logging.Formatter("%(message)s"))
        # Plain handler FIRST so any cache it writes would hit ours next —
        # and vice versa our isolation keeps our text out of its cache.
        self.logger.removeHandler(self.handler)
        self.logger.addHandler(plain_handler)
        self.logger.addHandler(self.handler)
        try:
            self._log_a_boom()
        finally:
            self.logger.removeHandler(plain_handler)
        plain_out = plain_buf.getvalue()
        our_out = self.buf.getvalue()
        self.assertIn("Traceback (most recent call last):", plain_out)
        self.assertNotIn("[missing_key=", plain_out)   # ours didn't leak in
        self.assertIn("[missing_key=", our_out)        # and ours is ours

    def test_stack_info_preserved(self):
        self.logger.error("with stack", stack_info=True)
        self.assertIn("Stack (most recent call last):", self.buf.getvalue())

    def test_caller_context_defaults_off(self):
        self.handler.setFormatter(ReportFormatter(
            "%(message)s", report_style="heavy",
        ))
        self._log_a_boom()
        self.assertIn(
            "(not captured - caller_context=False", self.buf.getvalue()
        )

    def test_eager_validation(self):
        with self.assertRaises(ValueError):
            ReportFormatter(report_style="florid")
        with self.assertRaises(TypeError):
            ReportFormatter(describe_kwargs={"bogus_kwarg": 1})


class NewSeedExtractorTests(unittest.TestCase):
    """Task 21: subprocess / json / import / socket / ssl seed extractors."""

    def _ts(self, exc):
        try:
            raise exc
        except type(exc) as e:
            return describe_error(e).to_dict()["type_specific"]

    def test_calledprocesserror_fields(self):
        import subprocess
        exc = subprocess.CalledProcessError(
            3, ["mycmd", "--flag"], output=b"partial out", stderr=b"bad things",
        )
        ts = self._ts(exc)
        self.assertEqual(ts["returncode"], 3)
        self.assertIn("mycmd", ts["cmd"])
        self.assertIn("partial out", ts["stdout"])
        self.assertIn("bad things", ts["stderr"])

    def test_calledprocesserror_without_capture(self):
        import subprocess
        ts = self._ts(subprocess.CalledProcessError(1, "cmd"))
        self.assertEqual(ts["returncode"], 1)
        self.assertNotIn("stdout", ts)
        self.assertNotIn("stderr", ts)

    def test_jsondecodeerror_fields(self):
        import json as _json
        try:
            _json.loads('{"a": 1, "b": }')
        except _json.JSONDecodeError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertIsNotNone(ts["msg"])
        self.assertEqual(ts["lineno"], 1)
        self.assertGreater(ts["pos"], 0)
        self.assertIn('"b": ', ts["doc_snippet"])
        self.assertEqual(ts["doc_length"], len('{"a": 1, "b": }'))

    def test_jsondecodeerror_snippet_is_windowed(self):
        import json as _json
        doc = '{"pad": "' + ("x" * 300) + '", "bad": }'
        try:
            _json.loads(doc)
        except _json.JSONDecodeError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertLessEqual(len(ts["doc_snippet"]), 80)
        self.assertIn('"bad": ', ts["doc_snippet"])

    def test_jsondecodeerror_snippet_is_redacted(self):
        import json as _json
        register_redactor(redact_pattern(r"hunter2"))
        try:
            try:
                _json.loads('{"password": "hunter2", "oops": }')
            except _json.JSONDecodeError as e:
                ts = describe_error(e).to_dict()["type_specific"]
        finally:
            clear_redactors()
        self.assertNotIn("hunter2", ts["doc_snippet"])
        self.assertIn("<redacted>", ts["doc_snippet"])

    def test_modulenotfounderror_inherits_importerror_extractor(self):
        try:
            import nonexistent_module_xyz_for_eh_test  # noqa: F401
        except ImportError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertEqual(ts["name"], "nonexistent_module_xyz_for_eh_test")

    def test_importerror_path_field(self):
        exc = ImportError("broken", name="thing", path="/some/where/thing.py")
        ts = self._ts(exc)
        self.assertEqual(ts["name"], "thing")
        self.assertEqual(ts["path"], "/some/where/thing.py")

    def test_gaierror_constant_resolution(self):
        import socket as _socket
        exc = _socket.gaierror(
            _socket.EAI_NONAME, "Name or service not known"
        )
        ts = self._ts(exc)
        # Several EAI_* constants can share a value (on Windows EAI_NONAME
        # == EAI_NODATA), so reverse-lookup by errno is inherently
        # ambiguous - assert the resolved name maps back to the same code
        # rather than pinning one alias (which is platform-dependent).
        self.assertTrue(ts["gai_constant"].startswith("EAI_"))
        self.assertEqual(getattr(_socket, ts["gai_constant"]), _socket.EAI_NONAME)
        self.assertEqual(ts["errno"], _socket.EAI_NONAME)
        self.assertIn("Name or service", str(ts["strerror"]))

    def test_sslerror_extractor(self):
        try:
            import ssl as _ssl
        except ImportError:
            self.skipTest("ssl not available")
        ts = self._ts(_ssl.SSLError(1, "[SSL: SOMETHING] went wrong"))
        # library/reason may be None on hand-built instances; the
        # contract is that the keys exist and the extractor fired.
        self.assertIn("library", ts)
        self.assertIn("reason", ts)
        self.assertEqual(ts["errno"], 1)

    def test_oserror_dispatch_not_shadowed(self):
        # FileNotFoundError must still resolve to the OSError extractor.
        try:
            open("/definitely/not/a/real/path/eh_test_file_404")
        except OSError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertIn("errno", ts)
        self.assertIn("filename", ts)
        self.assertNotIn("gai_constant", ts)


class ColumnAnchorTests(unittest.TestCase):
    """Task 22: fine-grained error location (3.11+ co_positions)."""

    _HAS_ANCHORS = sys.version_info >= (3, 11)

    def _boom_subscript(self):
        try:
            cfg = {"present": 1}
            total = cfg["present"] + cfg["absent"]  # error in 2nd subscript
            return total
        except KeyError as e:
            return describe_error(e).to_dict()

    @unittest.skipUnless(_HAS_ANCHORS, "co_positions requires 3.11+")
    def test_anchors_present_and_sane(self):
        d = self._boom_subscript()
        frame = d["traceback"][-1]
        self.assertIn("col_anchors", frame)
        a = frame["col_anchors"]
        self.assertEqual(a["lineno"], frame["line"])
        self.assertIsInstance(a["colno"], int)
        self.assertIsInstance(a["end_colno"], int)
        self.assertGreater(a["end_colno"], a["colno"])

    @unittest.skipUnless(_HAS_ANCHORS, "co_positions requires 3.11+")
    def test_anchor_text_is_the_failing_expression(self):
        d = self._boom_subscript()
        a = d["traceback"][-1]["col_anchors"]
        # The failing instruction is the SECOND subscript, not the whole line.
        self.assertEqual(a["anchor_text"], 'cfg["absent"]')

    @unittest.skipUnless(_HAS_ANCHORS, "co_positions requires 3.11+")
    def test_caller_context_frames_have_no_anchors(self):
        d = self._boom_subscript()
        for frame in d.get("caller_context") or []:
            if "truncated" in frame:
                continue
            self.assertNotIn("col_anchors", frame)

    @unittest.skipUnless(_HAS_ANCHORS, "co_positions requires 3.11+")
    def test_anchor_text_is_redacted(self):
        register_redactor(redact_pattern(r"hunter2"))
        try:
            try:
                lookup = {}
                lookup["hunter2"]  # secret inside the failing expression
            except KeyError as e:
                a = describe_error(e).to_dict()["traceback"][-1]["col_anchors"]
        finally:
            clear_redactors()
        self.assertNotIn("hunter2", a.get("anchor_text", ""))

    @unittest.skipUnless(_HAS_ANCHORS, "co_positions requires 3.11+")
    def test_formatters_render_anchor_lines(self):
        try:
            x = {}
            x["nope"]
        except KeyError as e:
            report = describe_error(e)
        self.assertIn("[error at line ", report.to_string())
        heavy = report.for_claude()
        self.assertIn("Column anchors: line ", heavy)
        self.assertIn("Failing expression: ", heavy)

    @unittest.skipIf(_HAS_ANCHORS, "graceful-skip branch is for <3.11")
    def test_pre_311_frames_have_no_anchors_and_no_failures(self):
        d = self._boom_subscript()
        for frame in d["traceback"]:
            self.assertNotIn("col_anchors", frame)
        anchor_failures = [
            f for f in d["partial_failures"]
            if "col_anchors" in str(f.get("step", ""))
        ]
        self.assertEqual(anchor_failures, [])


class OriginTaggingTests(unittest.TestCase):
    """Task 23: frame origin tagging + skip_modules."""

    def test_user_frames_tagged_user(self):
        try:
            raise ValueError("origin test")
        except ValueError as e:
            d = describe_error(e).to_dict()
        self.assertEqual(d["traceback"][-1]["origin"], "user")

    def test_stdlib_frames_tagged_stdlib(self):
        import json as _json
        try:
            _json.loads("{bad")
        except Exception as e:
            d = describe_error(e).to_dict()
        origins = [f["origin"] for f in d["traceback"]]
        self.assertIn("stdlib", origins)   # json decoder frames
        self.assertEqual(origins[0], "user")  # our call site leads

    def test_tag_function_directly(self):
        from error_handler import _tag_frame_origin
        self.assertEqual(_tag_frame_origin(__file__), "user")
        self.assertEqual(
            _tag_frame_origin("/x/lib/python3.12/site-packages/django/db.py"),
            "site-packages",
        )
        self.assertEqual(
            _tag_frame_origin("C:\\Python312\\Lib\\site-packages\\req\\api.py"),
            "site-packages",
        )
        self.assertEqual(
            _tag_frame_origin("<frozen importlib._bootstrap>"), "stdlib"
        )
        self.assertEqual(_tag_frame_origin("<string>"), "user")
        self.assertEqual(_tag_frame_origin(""), "user")
        import error_handler as eh
        self.assertEqual(_tag_frame_origin(eh.__file__), "error_handler")

    def test_capture_wrapper_frame_tagged_error_handler(self):
        # Earmarked case #2 (task 17): @capture's own wrapper frame.
        reports = []
        @capture(on_report=reports.append, reraise=False)
        def boom():
            raise ValueError("wrapper origin")
        boom()
        d = reports[0].to_dict()
        origins = [f["origin"] for f in d["traceback"]]
        self.assertEqual(origins[0], "error_handler")  # _wrapper frame
        self.assertEqual(origins[-1], "user")          # boom() itself

    def test_skip_modules_marks_hidden_in_dict(self):
        reports = []
        @capture(on_report=reports.append, reraise=False,
                 skip_modules=["error_handler"])
        def boom():
            raise ValueError("hide the wrapper")
        boom()
        d = reports[0].to_dict()
        wrapper = d["traceback"][0]
        self.assertEqual(wrapper["origin"], "error_handler")
        self.assertEqual(wrapper["hidden"], "error_handler")
        # Dict completeness: hidden frames keep all their data.
        self.assertIn("file", wrapper)
        self.assertIn("line", wrapper)
        self.assertIn("function", wrapper)
        # User frame not marked.
        self.assertNotIn("hidden", d["traceback"][-1])

    def test_concise_collapses_hidden_runs(self):
        reports = []
        @capture(on_report=reports.append, reraise=False,
                 skip_modules=["error_handler"])
        def boom():
            raise ValueError("collapse me")
        boom()
        out = reports[0].to_string()
        self.assertIn("frame(s) hidden: error_handler", out)
        self.assertNotIn('error_handler.py", line', out.split("hidden")[0])

    def test_heavy_annotates_but_never_hides(self):
        reports = []
        @capture(on_report=reports.append, reraise=False,
                 skip_modules=["error_handler"])
        def boom():
            raise ValueError("annotate me")
        boom()
        heavy = reports[0].for_claude()
        self.assertIn("Origin: error_handler (hidden by skip_modules:", heavy)
        self.assertIn("error_handler.py", heavy)  # frame fully present

    def test_substring_matching(self):
        import json as _json
        try:
            _json.loads("{bad")
        except Exception as e:
            d = describe_error(e, skip_modules=["json"]).to_dict()
        json_frames = [f for f in d["traceback"] if "json" in f["file"]]
        self.assertTrue(json_frames)
        for f in json_frames:
            self.assertEqual(f["hidden"], "json")

    def test_threading_hook_noise_collapses(self):
        # Earmarked case #1 (task 16): stdlib bootstrap frames in
        # worker-thread crash reports.
        import io
        import threading
        buf = io.StringIO()
        install(hooks="threading", stream=buf, skip_modules=["stdlib"])
        try:
            def boom():
                raise RuntimeError("noisy thread")
            t = threading.Thread(target=boom, name="origin-test-thread")
            t.start()
            t.join()
        finally:
            uninstall()
        out = buf.getvalue()
        self.assertIn("frame(s) hidden: stdlib", out)
        self.assertNotIn("threading.py", out)       # bootstrap noise gone
        self.assertIn("noisy thread", out)          # the error itself stays

    def test_bad_skip_modules_degrades_safely(self):
        try:
            raise ValueError("bad skip value")
        except ValueError as e:
            d = describe_error(e, skip_modules=42).to_dict()  # not iterable
        self.assertEqual(d["message"], "bad skip value")  # report intact
        steps = [f["step"] for f in d["partial_failures"]]
        self.assertIn("skip_modules", steps)

    def test_no_skip_no_hidden_keys(self):
        try:
            raise ValueError("clean")
        except ValueError as e:
            d = describe_error(e).to_dict()
        for f in d["traceback"]:
            self.assertNotIn("hidden", f)


class TimestampUptimeTests(unittest.TestCase):
    """Task 24: timestamp + uptime in the environment block."""

    def _env(self, **kwargs):
        try:
            raise ValueError("tick tock")
        except ValueError as e:
            return describe_error(e, **kwargs).to_dict().get("environment")

    def test_timestamp_is_recent_utc_iso(self):
        from datetime import datetime, timezone
        env = self._env()
        ts = datetime.fromisoformat(env["timestamp_utc"])
        self.assertIsNotNone(ts.tzinfo)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        self.assertGreaterEqual(age, 0)
        self.assertLess(age, 60)

    def test_uptime_present_and_sane(self):
        env = self._env()
        self.assertIsInstance(env["uptime_seconds"], float)
        self.assertGreaterEqual(env["uptime_seconds"], 0.0)
        self.assertLess(env["uptime_seconds"], 3600.0)  # test runs are short
        self.assertIn(env["uptime_basis"], ("process", "module_import"))

    def test_absent_when_snapshot_disabled(self):
        env = self._env(environment_snapshot=False)
        self.assertIsNone(env)

    def test_heavy_formatter_shows_timestamp(self):
        try:
            raise ValueError("show time")
        except ValueError as e:
            heavy = describe_error(e).for_claude()
        self.assertIn("timestamp_utc: ", heavy)
        self.assertIn("uptime_seconds: ", heavy)

    def test_uptime_grows_between_reports(self):
        import time as _time
        first = self._env()["uptime_seconds"]
        _time.sleep(0.05)
        second = self._env()["uptime_seconds"]
        self.assertGreaterEqual(second, first)


class ReportBudgetTests(unittest.TestCase):
    """Task 25: max_report_bytes progressive degradation."""

    def _fat_report(self, **kwargs):
        try:
            ballast = ["x" * 100 for _ in range(30)]  # noqa: F841 — locals fat
            cfg = {"a": 1}
            cfg["missing"]
        except KeyError as e:
            return describe_error(e, include_locals=True, **kwargs)

    def _json_size(self, report):
        return len(report.to_json().encode("utf-8"))

    def test_no_budget_no_marker(self):
        d = self._fat_report().to_dict()
        self.assertNotIn("report_truncation", d)

    def test_generous_budget_untouched(self):
        d = self._fat_report(max_report_bytes=10_000_000).to_dict()
        self.assertNotIn("report_truncation", d)
        self.assertTrue(any("locals" in f for f in d["traceback"]))

    def test_locals_dropped_first(self):
        baseline = self._json_size(self._fat_report())
        budget = baseline - 2000  # force stage 1
        report = self._fat_report(max_report_bytes=budget)
        d = report.to_dict()
        self.assertIn("report_truncation", d)
        for f in d["traceback"]:
            self.assertNotIn("locals", f)
        self.assertTrue(
            any("locals" in s for s in d["report_truncation"]["dropped"])
        )

    def test_source_context_dropped_second_code_survives(self):
        report = self._fat_report(max_report_bytes=2500)
        d = report.to_dict()
        dropped = d["report_truncation"]["dropped"]
        self.assertTrue(any("locals" in s for s in dropped))
        self.assertTrue(any("source_context" in s for s in dropped))
        for f in d["traceback"]:
            self.assertNotIn("source_context", f)
            self.assertIn("code", f)  # single line always survives

    def test_absurd_budget_stays_honest_and_valid(self):
        report = self._fat_report(max_report_bytes=50)
        d = report.to_dict()
        self.assertEqual(d["type"], "KeyError")  # structure intact
        trunc = d["report_truncation"]
        self.assertFalse(trunc["within_budget"])
        self.assertIsInstance(trunc["final_bytes"], int)

    def test_within_budget_actually_fits(self):
        baseline = self._json_size(self._fat_report())
        budget = baseline - 2000
        report = self._fat_report(max_report_bytes=budget)
        trunc = report.to_dict()["report_truncation"]
        if trunc["within_budget"]:
            self.assertLessEqual(self._json_size(report), budget)

    def test_chain_and_caller_frames_stripped_too(self):
        def build():
            try:
                try:
                    inner_secretly_large = "y" * 500  # noqa: F841
                    raise ValueError("root")
                except ValueError as ve:
                    raise RuntimeError("wrap") from ve
            except RuntimeError as e:
                return describe_error(
                    e, include_locals=True, max_report_bytes=100,
                )
        d = build().to_dict()
        for link in d["chain"]:
            for f in link.get("traceback") or []:
                self.assertNotIn("locals", f)
        for f in d.get("caller_context") or []:
            if "truncated" not in f:
                self.assertNotIn("locals", f)

    def test_formatters_render_budget_note(self):
        report = self._fat_report(max_report_bytes=2500)
        self.assertIn("report degraded to fit", report.to_string())
        heavy = report.for_claude()
        self.assertIn("REPORT BUDGET", heavy)
        self.assertIn("Dropped to fit:", heavy)

    def test_bad_budget_value_degrades_safely(self):
        report = self._fat_report(max_report_bytes="not a number")
        d = report.to_dict()
        self.assertEqual(d["type"], "KeyError")
        steps = [f["step"] for f in d["partial_failures"]]
        self.assertIn("report_budget", steps)


class SuggestionsTests(unittest.TestCase):
    """did-you-mean suggestions via difflib (task 26)."""

    class _Widget:
        def __init__(self):
            self.message = "hi"
            self.count = 0

    def test_attributeerror_suggests_close_attr(self):
        try:
            self._Widget().mesage
        except AttributeError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertIn("did_you_mean", ts)
        self.assertIn("message", ts["did_you_mean"])

    def test_nameerror_suggests_close_name(self):
        try:
            widht = 5
            print(widt)  # noqa: F821
        except NameError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertIn("did_you_mean", ts)
        self.assertIn("widht", ts["did_you_mean"])

    def test_modulenotfound_suggests_stdlib_name(self):
        try:
            import jsom  # noqa: F401
        except ModuleNotFoundError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertIn("did_you_mean", ts)
        self.assertIn("json", ts["did_you_mean"])
        # Private impl modules (_json) are filtered out of public-name typos.
        self.assertFalse(any(s.startswith("_") for s in ts["did_you_mean"]))

    def test_no_close_match_means_no_key(self):
        try:
            self._Widget().qzxwv_unrelated
        except AttributeError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertNotIn("did_you_mean", ts)

    def test_suggestions_false_disables(self):
        try:
            self._Widget().mesage
        except AttributeError as e:
            ts = describe_error(e, suggestions=False).to_dict()["type_specific"]
        self.assertNotIn("did_you_mean", ts)

    def test_concise_formatter_renders_hint(self):
        try:
            self._Widget().mesage
        except AttributeError as e:
            s = describe_error(e).to_string()
        self.assertIn("Did you mean:", s)
        self.assertIn("message", s)

    def test_heavy_formatter_renders_hint(self):
        try:
            self._Widget().mesage
        except AttributeError as e:
            heavy = describe_error(e).for_claude()
        self.assertIn("Did you mean:", heavy)

    def test_hostile_dir_never_raises(self):
        class Hostile:
            def __dir__(self):
                raise RuntimeError("dir is broken")
        try:
            Hostile().mesage
        except AttributeError as e:
            report = describe_error(e)
        d = report.to_dict()
        self.assertEqual(d["type"], "AttributeError")
        self.assertNotIn("did_you_mean", d["type_specific"])

    def test_flag_leaves_no_residue(self):
        def boom():
            return SuggestionsTests._Widget().mesage
        try:
            boom()
        except AttributeError as e:
            describe_error(e, suggestions=False)
        try:
            boom()
        except AttributeError as e:
            ts = describe_error(e).to_dict()["type_specific"]
        self.assertIn("did_you_mean", ts)

    def test_no_duplicate_suggestions_at_module_scope(self):
        # At module scope f_locals IS f_globals; names must not double up.
        lines_src = [
            "total_count = 1",
            "try:",
            "    totl_count",
            "except NameError as e:",
            "    out['ts'] = describe_error(e).to_dict()['type_specific']",
        ]
        out = {}
        exec(chr(10).join(lines_src), {"describe_error": describe_error, "out": out})
        sug = out["ts"].get("did_you_mean", [])
        self.assertIn("total_count", sug)
        self.assertEqual(len(sug), len(set(sug)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
