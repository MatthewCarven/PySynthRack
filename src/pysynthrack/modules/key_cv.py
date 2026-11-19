"""KeyCV — bind one computer key to a CV that moves between two values.

Matthew, 2026-11-19: "do we have a module that can be bound to a key and set
when active to output 0, 1, 2, -1, -2 cv ... or a slow drift between? ...
initial value and activated value and just let the user specify" -- and
"let one key driven cv feed into another key driven cv please".

:class:`KeyTrigger`'s sibling. It binds one physical key the same way (the
node's **Learn** button, a portable key *name*), but instead of a 0/1 gate
its ``out`` jack is a CV that sits at ``idle`` while the key is up and moves
to ``active`` while it is down -- any two numbers, so +1 is an octave up on a
1 V/oct pitch input, -2 two octaves down, 0.5 half a filter sweep. ``glide``
sets how long the move takes, so a key can snap (0) or drift slowly there
and back.

**Chaining.** The ``in`` jack takes a CV, so one key's ``out`` can feed the
next key's ``in``:

* ``chain = add`` (default): ``out = in + this key's value``. Keys stack --
  three keys at +1, +7/12 and -1 on an oscillator's ``freq_cv`` are an
  octave key, a fifth key and an octave-down key that combine.
* ``chain = override``: while this key is down it takes over --
  ``out`` glides from ``in + idle`` to ``active`` and back. The last key in
  a chain that is held wins.

With ``in`` unpatched the two are the same: ``idle`` up, ``active`` down.

Parameters:
    key: Bound key name (``""`` = unbound: sits at ``idle``). Set by Learn.
    mode: ``gate`` -- active while the key is held; ``latch`` -- each press
        toggles between idle and active.
    units: ``cv`` (default) -- idle and active are CV as written;
        ``semitones`` -- they are semitones and the module divides by 12,
        exactly, so 7 is a fifth on a 1 V/oct pitch input and any decimal
        holds (7.02 is a pure fifth, 3.86 a pure major third). Switching
        in the node converts the two values, so the output does not move.
        ``in`` is always CV.
    idle: The value with the key up (default 0).
    active: The value with the key down (default 1).
    glide: Seconds for a full move from idle to active (or back); 0 =
        instant. The rate is constant, so letting go half way comes back in
        half the time.
    chain: ``add`` or ``override`` -- how ``in`` combines with this key.

Ports:
    in (in, cv): optional CV to chain onto (another key_cv, an LFO, ...).
    out (out, cv): the key's CV (combined with ``in`` per ``chain``).
    gate (out, gate): 1 while the key is active (held, or latched on), for
        firing an envelope off the same key.
"""
from __future__ import annotations

from ..core.module import register_module_type
from ..core.port import Port
from .key_trigger import KeyTrigger

KEY_CV_MODES = ("gate", "latch")
KEY_CV_CHAINS = ("add", "override")
KEY_CV_UNITS = ("cv", "semitones")


@register_module_type
class KeyCV(KeyTrigger):
    """One bound computer key → a CV between ``idle`` and ``active``.

    Reuses :class:`KeyTrigger`'s raw-key plumbing (``raw_key_down`` /
    ``raw_key_up`` / ``snapshot`` / ``all_notes_off`` and the
    ``ACCEPTS_RAW_KEYS`` flag the UI routes by); only the ports, the
    parameters and the renderer differ.
    """

    TYPE = "key_cv"
    CATEGORY = "Sources"

    DEFAULT_PARAMS = {
        "key": "",          # unbound until Learn assigns a key name
        "mode": "gate",     # one of KEY_CV_MODES
        "units": "cv",      # one of KEY_CV_UNITS: how idle/active read
        "idle": 0.0,        # value with the key up
        "active": 1.0,      # value with the key down
        "glide": 0.0,       # seconds for a full idle <-> active move
        "chain": "add",     # one of KEY_CV_CHAINS
    }

    INPUT_PORTS = [Port("in", "in", "cv")]
    OUTPUT_PORTS = [Port("out", "out", "cv"), Port("gate", "out", "gate")]
