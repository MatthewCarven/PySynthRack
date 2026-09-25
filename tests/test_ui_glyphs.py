"""Nothing that reaches a screen may use a glyph the font can't paint.

DearPyGui's built-in font (ProggyClean) covers basic Latin only. Anything
above U+00FF renders as a replacement `?` — which is how, for two months,
every jack on every node read `? in` / `x ?` instead of `<- in` / `x ->`,
and 24 example patches showed `THE BUTTERFLY ? one orbit...` where an em
dash belonged. Cosmetic, but 336 port labels' worth of it, and actively
confusing on a module whose whole idea is a `?`.

The fix was to use ASCII. These are the tripwires that keep it that way:
a `—` typed into a status message or a patch title looks perfectly fine in
the editor and only turns to mush on screen, so the check has to be
mechanical.

Scope is deliberately *display strings only* — docstrings and comments are
developer prose, never painted, and keep their typography.
"""
from __future__ import annotations

import ast
import glob
import json
import os
import unicodedata

import pytest

UI_DIR = os.path.join("src", "pysynthrack", "ui")

# Modules outside ui/ whose messages surface in the GUI status bar (cable
# refusals, audio-start failures) or on the CLI's stdout — which, when piped,
# encodes with the locale codec and *raises* on a non-ASCII glyph rather than
# merely looking wrong.
USER_FACING_ELSEWHERE = [
    os.path.join("src", "pysynthrack", "cli.py"),
    os.path.join("src", "pysynthrack", "core", "patch.py"),
]


def _describe(ch: str) -> str:
    return f"U+{ord(ch):04X} ({unicodedata.name(ch, 'unnamed')})"


def _display_strings(path: str):
    """Every string constant in a file except docstrings.

    Walks the AST rather than regexing quotes, so f-strings are covered
    (their literal halves are `Constant` nodes under `JoinedStr`) and
    docstrings are excluded precisely instead of by guesswork.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.lineno, node.value


def _offenders(path: str):
    return [
        (line, text, ch)
        for line, text in _display_strings(path)
        for ch in sorted(set(text))
        if ord(ch) > 127
    ]


@pytest.mark.parametrize(
    "path", sorted(glob.glob(os.path.join(UI_DIR, "*.py"))) + USER_FACING_ELSEWHERE
)
def test_display_strings_are_ascii(path):
    bad = _offenders(path)
    assert not bad, "\n".join(
        f"{path}:{line} contains {_describe(ch)} in {text!r} — "
        f"DearPyGui's font paints it as '?'"
        for line, text, ch in bad
    )


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join("examples", "*.json"))))
def test_example_patch_names_are_ascii(path):
    """Node names ship to users and are painted straight onto the node."""
    patch_data = json.load(open(path, encoding="utf-8"))
    bad = [
        (module.get("id"), name, ch)
        for module in patch_data.get("modules", [])
        for name in [str(module.get("name", ""))]
        for ch in sorted(set(name))
        if ord(ch) > 127
    ]
    assert not bad, "\n".join(
        f"{os.path.basename(path)} module #{mid} name {name!r} contains "
        f"{_describe(ch)} — it renders as '?' on the node"
        for mid, name, ch in bad
    )


def test_port_labels_use_ascii_arrows():
    """The 336-label one: the jack decorations themselves."""
    source = open(os.path.join(UI_DIR, "app.py"), encoding="utf-8").read()
    assert '"< {port.name}"' in source
    assert '"{port.name} >"' in source


def test_the_tripwire_actually_catches_something(tmp_path):
    """A tripwire nobody has seen fail is a tripwire nobody trusts."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""A docstring with an em dash — which is fine."""\n'
        'status = "Queue empty — nothing to skip to"\n',
        encoding="utf-8",
    )
    bad = _offenders(str(sample))
    assert len(bad) == 1                      # the docstring is exempt...
    assert bad[0][0] == 2                     # ...and the display string is not
    assert bad[0][2] == "—"
