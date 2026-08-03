"""Logic — two-input gate algebra, every jack live at once.

Boolean glue for the clockwork family: two gate inputs, five gate
outputs (``and`` ``or`` ``xor`` ``nand`` ``not_a``), all computed every
block — no mode combo, you swap cables, not settings. Cross a clock
with a ``euclidean`` pattern and ``xor`` is the complementary rhythm
(hits where the pattern rests), ``and`` is the pattern itself gated to
the clock's width, ``nand`` is a rest-run trigger source; ``not_a``
inverts a gate for "everything except" patching.

Zero parameters — deliberately. The old idea-list bullet had a
"comparator mode with threshold for CVs", but port kinds are strict in
this rack (a cv out cannot cable into a gate in), so the knob would be
unreachable: cv→gate conversion is :class:`Schmitt`'s whole job. Feed
this module gates; feed Schmitt voltages.

An unpatched input reads **low**, so with only ``a`` connected:
``or``/``xor`` pass ``a`` through, ``and`` stays low, and ``nand``
idles **high** (the classic normalled-NAND trick — a free "gate off"
signal). Both inputs unpatched: ``nand`` and ``not_a`` idle high,
the rest low. Stateless and exact; a voice-aware ``(V, F)`` gate
source collapses on fetch (any-voice-high semantics after the house
sum), like every mono consumer.

Ports:
  * ``a``, ``b`` (gate, in): the two operands. Unpatched → low.
  * ``and`` (gate, out): ``a ∧ b``.
  * ``or`` (gate, out): ``a ∨ b``.
  * ``xor`` (gate, out): ``a ⊕ b``.
  * ``nand`` (gate, out): ``¬(a ∧ b)``.
  * ``not_a`` (gate, out): ``¬a`` (swap cables for ¬b).

Params: none.
"""
from __future__ import annotations

from ..core.module import Module, register_module_type
from ..core.port import Port


@register_module_type
class Logic(Module):
    """Two-in gate algebra: and / or / xor / nand / not_a, all live.

    Parameters: none (see the module docstring for why the comparator
    mode was dropped — cv→gate is Schmitt's job).

    Ports:
        a (in, gate): operand A. Unpatched → low.
        b (in, gate): operand B. Unpatched → low.
        and (out, gate): a ∧ b.
        or (out, gate): a ∨ b.
        xor (out, gate): a ⊕ b.
        nand (out, gate): ¬(a ∧ b) — idles high when unpatched.
        not_a (out, gate): ¬a.
    """

    TYPE = "logic"
    CATEGORY = "Modulation"
    DEFAULT_PARAMS: dict = {}
    INPUT_PORTS = [
        Port("a", "in", "gate"),
        Port("b", "in", "gate"),
    ]
    OUTPUT_PORTS = [
        Port("and", "out", "gate"),
        Port("or", "out", "gate"),
        Port("xor", "out", "gate"),
        Port("nand", "out", "gate"),
        Port("not_a", "out", "gate"),
    ]
