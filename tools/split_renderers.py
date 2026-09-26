"""Move a family of renderers out of ``numpy_backend.py`` into a mixin.

The backend split (docs/architecture.md, "Renderer families") moves one
family at a time into ``audio/renderers/<module>.py`` as a class
``NumpyBackend`` inherits. This does the mechanical part the same way
every time:

    python tools/split_renderers.py analyse START_RE END_RE [START_RE END_RE ...]
    python tools/split_renderers.py move    CONFIG.json

``START_RE`` / ``END_RE`` are regexes for the first line of a block and
the first line AFTER it (usually a ``    # ----- X`` section marker or the
next family's ``    def``). A family scattered through the file is
several blocks; they land in the new module in file order. ``analyse`` prints what would move, what it
uses (module-level names need importing in the new file; a bare
``NumpyBackend`` reference needs a lazy import, since the backend imports
the mixin), and who outside the block calls into it.

``move`` takes a JSON config::

    {"blocks": [["START_RE", "END_RE"], ...], "module": "dynamics",
     "cls": "DynamicsRenderers", "imports": "import numpy as np",
     "doc": "module docstring",
     "subs": [["old text", "new text", expected_count], ...]}

``subs`` are the only edits allowed inside the moved block (typically a
relative import gaining a dot, or a lazy ``NumpyBackend`` import); each
must match exactly ``expected_count`` times.

Then prove it is behaviour-neutral, every time:

1. ``git diff`` of the old block against the new file shows only ``subs``;
2. no class-level name is defined both in the mixin and the backend
   (the backend's would silently win) -- ``move`` refuses one itself;
3. ``tools/render_audit.py`` over every example, HEAD vs working tree
   (render HEAD from a ``git worktree`` with ``--repo``): moved 0;
4. the full suite, which includes the source-scanning tripwires (they
   scan ``audio/renderers/`` too).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "src" / "pysynthrack" / "audio" / "numpy_backend.py"
RENDERERS = ROOT / "src" / "pysynthrack" / "audio" / "renderers"


def _block(lines, start_re, end_re):
    start = next(i for i, ln in enumerate(lines) if re.match(start_re, ln))
    end = next(i for i, ln in enumerate(lines) if i > start and re.match(end_re, ln))
    return start, end


def _blocks(lines, pairs):
    """Sorted, non-overlapping ``(start, end)`` line ranges."""
    spans = sorted(_block(lines, a, b) for a, b in pairs)
    for (_, e1), (s2, _) in zip(spans, spans[1:]):
        if s2 < e1:
            sys.exit("blocks overlap")
    return spans


def _class_members(cls_node) -> set[str]:
    out = set()
    for n in cls_node.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:            # `A = 1` and `A, B = 1, 2` alike
                out |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
    return out


def analyse(pairs) -> None:
    src = BACKEND.read_text(encoding="utf-8")
    lines = src.split("\n")
    tree = ast.parse(src)
    spans = _blocks(lines, pairs)
    for start, end in spans:
        print(f"block: lines {start + 1}..{end} ({end - start} lines)")

    def in_block(n):
        return any(s <= n.lineno - 1 < e for s, e in spans)

    top = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            top.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            top |= {t.id for t in targets if isinstance(t, ast.Name)}

    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NumpyBackend")
    inside = [n for n in cls.body if in_block(n)]
    outside = [n for n in cls.body if not in_block(n)]
    moving = _class_members(ast.ClassDef(name="", bases=[], keywords=[], body=inside,
                                         decorator_list=[]))
    print("moving:", ", ".join(sorted(moving)))

    used_top, attrs = set(), set()
    for n in inside:
        for x in ast.walk(n):
            if isinstance(x, ast.Name) and x.id in top:
                used_top.add(x.id)
            if (isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
                    and x.value.id in ("self", "cls", "NumpyBackend")):
                attrs.add(x.attr)
    print("module-level names used (import these; NumpyBackend needs a lazy import):",
          ", ".join(sorted(used_top)))
    print("backend attributes used from outside the block (fine via self):",
          ", ".join(sorted(attrs - moving)))

    callers: dict[str, set[str]] = {}
    for n in outside:
        for x in ast.walk(n):
            if isinstance(x, ast.Attribute) and x.attr in moving:
                callers.setdefault(x.attr, set()).add(getattr(n, "name", "?"))
    print("called from outside the block:",
          {k: sorted(v) for k, v in sorted(callers.items())})
    for start, end in spans:
        for i in range(start, end):
            if re.match(r"\s+(from \S+ import|import )", lines[i]):
                print(f"lazy import at line {i + 1}: {lines[i].strip()}")


def move(config_path: str) -> None:
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    lines = BACKEND.read_text(encoding="utf-8").split("\n")
    pairs = cfg.get("blocks") or [[cfg["start"], cfg["end"]]]
    spans = _blocks(lines, pairs)
    parts = []
    for start, end in spans:
        body = lines[start:end]
        while body and body[-1] == "":
            body.pop()
        parts.append("\n".join(body) + "\n")
    text = "\n".join(parts)
    for old, new, count in cfg.get("subs", []):
        found = text.count(old)
        if found != count:
            sys.exit(f"sub expected {count} match(es), found {found}: {old!r}")
        text = text.replace(old, new)

    target = RENDERERS / f"{cfg['module']}.py"
    if target.exists():
        sys.exit(f"{target} already exists")
    new_src = (f'"""{cfg["doc"]}"""\nfrom __future__ import annotations\n\n'
               f'{cfg["imports"]}\n\n\nclass {cfg["cls"]}:\n{text}')

    for start, end in reversed(spans):
        del lines[start:end]
    s = "\n".join(lines)

    # A class-level name on both sides would resolve to the backend's copy
    # (or an earlier mixin's) and silently change behaviour: refuse.
    def members(source, name):
        tree = ast.parse(source)
        return _class_members(next(n for n in tree.body
                                   if isinstance(n, ast.ClassDef) and n.name == name))
    moving = members(new_src, cfg["cls"])
    others = {"NumpyBackend": members(s, "NumpyBackend")}
    for path in RENDERERS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in tree.body:
            if isinstance(n, ast.ClassDef):
                others[n.name] = _class_members(n)
    for owner, names in others.items():
        if moving & names:
            sys.exit(f"name clash with {owner}: {sorted(moving & names)}")
    target.write_text(new_src, encoding="utf-8")
    m = re.search(r"^class NumpyBackend\((.*)\):$", s, re.M)
    bases = [b.strip() for b in m.group(1).split(",")]
    bases.insert(len(bases) - 1, cfg["cls"])          # mixins before AudioBackend
    s = s[: m.start()] + f"class NumpyBackend({', '.join(bases)}):" + s[m.end():]
    m = re.search(r"^from \.renderers import (.*)$", s, re.M)
    names = sorted(set(m.group(1).split(", ")) | {cfg["cls"]})
    s = s[: m.start()] + "from .renderers import " + ", ".join(names) + s[m.end():]
    BACKEND.write_text(s, encoding="utf-8")

    init = RENDERERS / "__init__.py"
    t = init.read_text(encoding="utf-8")
    imports = sorted(set(re.findall(r"^from \.\w+ import \w+$", t, re.M))
                     | {f"from .{cfg['module']} import {cfg['cls']}"})
    classes = sorted(ln.rsplit(" ", 1)[1] for ln in imports)
    head = t[: t.index("from .")]
    init.write_text(head + "\n".join(imports) + "\n\n__all__ = ["
                    + ", ".join(f'"{c}"' for c in classes) + "]\n", encoding="utf-8")
    moved = sum(e - s for s, e in spans)
    print(f"moved {moved} lines ({len(spans)} block(s)) into {target.relative_to(ROOT)}")


def main(argv: list[str]) -> None:
    if len(argv) >= 3 and len(argv) % 2 == 1 and argv[0] == "analyse":
        analyse(list(zip(argv[1::2], argv[2::2])))
    elif len(argv) == 2 and argv[0] == "move":
        move(argv[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
