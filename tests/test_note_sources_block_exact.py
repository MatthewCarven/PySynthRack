"""cv_to_frequency / keyboard / midi_input -- block-size exactness (2026-10-12).

The oscillator's phase stopped being a per-block float accumulator on
2026-09-24 (``test_oscillator_block_exact.py``); these three note sources
kept their own copies of the old one -- ``phase += frames * inc`` with no
CV, ``(start + cumsum(inc)) % 1`` wrapped at every block end with one --
and so rendered a different sample at block 64 than at 512 within
0.05-1.5 s. They now run the oscillator's own engines:

  * a constant pitch (cv_to_frequency unpatched, every keyboard / MIDI
    voice slot) is ``(origin + inc * k) % 1`` over the INTEGER sample
    count since the origin, re-anchored only when the increment changes
    (a pitch-bend move) -- :meth:`_osc_mono_phase`;
  * a per-sample pitch (cv_to_frequency with ``cv`` patched, mono and
    every voice) is the carried running sum :meth:`_osc_carried_phase`,
    wrapped only at absolute epoch boundaries.

A second mechanism fell out of measuring it: the keyboard / MIDI voices'
attack / release ramp was a level carried block to block (``env +
frames * delta``), which rounded differently under a different partition
through every attack and every release. It is now ``origin + delta * k``
over the integer count since the slope began; the slope is still chosen
once per block, exactly as before.

Key and MIDI events reach the renderer once per block, so a note change
is block-quantised by nature. The pins put every event on a sample that
is a block boundary in every partition (multiples of 64000 =
lcm(64, 128, 512, 1000)). Exposure is part of the assertion: every pin
runs 6 s at 44.1 kHz -- four events, four epoch wraps, and 4-30x the
time the old accumulators took to part.
"""
from __future__ import annotations

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch
from pysynthrack.modules.keyboard import midi_to_freq

SR = 44100
N = int(SR * 6.0)
E = 64000  # lcm(64, 128, 512, 1000): a block boundary in every partition
BLOCKS = (64, 128, 512, 1000)
# The phase is what is under test; the shapes are how it is read: sine
# (the phase itself), a PolyBLEP (a per-sample dt), the wavetables (a
# per-sample band, the highest voice's), and the naive saw (a step at the
# wrap, for the note sources).
WAVES = ("sine", "saw_blep", "saw_wt", "square_wt")
NOTE_WAVES = ("sine", "saw", "saw_blep", "square_wt")


def _assert_block_exact(label, render):
    ref = render(BLOCKS[0])
    for block in BLOCKS[1:]:
        out = render(block)
        diff = np.flatnonzero((out != ref).reshape(-1, out.shape[-1]).any(axis=0))
        assert diff.size == 0, (
            f"{label}: block {block} parts from block {BLOCKS[0]} at "
            f"{diff[0] / SR:.3f} s ({diff.size} samples)"
        )


# ----- cv_to_frequency ------------------------------------------------------


def _ctf_render(block, cv=None, waveform="sine", n=N, params=None):
    """Drive cv_to_frequency in ``block``-sized chunks, slicing a
    full-length CV per block (the oscillator test's idiom)."""
    patch = Patch()
    ctf = patch.add_module(
        "cv_to_frequency", params={"waveform": waveform, **(params or {})}
    )
    src = patch.add_module("cv_keyboard")
    if cv is not None:
        patch.connect(src.id, "pitch_cv", ctf.id, "cv")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)
    m = patch.get(ctf.id)
    outs = []
    for pos in range(0, n, block):
        f = min(block, n - pos)
        bufs = {}
        if cv is not None:
            bufs[(src.id, "pitch_cv")] = cv[..., pos:pos + f]
        outs.append(b._render_cv_to_frequency(m, f, bufs, patch))
    return np.concatenate(outs, axis=-1)


def _seq(n, V=None):
    """A 4-step sequence, a step every 0.37 s (not block aligned)."""
    steps = np.array([0.1, 0.45, 0.3, 0.8], dtype=np.float32)
    cv = steps[(np.arange(n) // int(0.37 * SR)) % 4]
    if V is None:
        return cv
    return np.stack([cv + np.float32(v * 0.03) for v in range(V)])


def _vib(n, V=None):
    """Vibrato around cv 0.4 (~350 Hz) wide enough to swing the wt band
    across its 320 Hz edge."""
    cv = (0.4 + 0.12 * np.sin(2 * np.pi * 5.5 * np.arange(n) / SR)).astype(
        np.float32)
    if V is None:
        return cv
    return np.stack([cv + np.float32(v * 0.05) for v in range(V)])


@pytest.mark.parametrize("waveform", WAVES)
def test_cv_to_frequency_is_block_size_exact(waveform):
    """Unpatched (the ``freq`` knob), a mono sequence, mono vibrato, a
    4-voice sequence and 4-voice vibrato: the identical sample at
    64 / 128 / 512 / 1000 for 6 s."""
    _assert_block_exact(f"{waveform} knob", lambda b: _ctf_render(b, None, waveform))
    _assert_block_exact(f"{waveform} seq", lambda b: _ctf_render(b, _seq(N), waveform))
    _assert_block_exact(f"{waveform} vib", lambda b: _ctf_render(b, _vib(N), waveform))
    _assert_block_exact(f"{waveform} seq 4v",
                        lambda b: _ctf_render(b, _seq(N, 4), waveform))
    _assert_block_exact(f"{waveform} vib 4v",
                        lambda b: _ctf_render(b, _vib(N, 4), waveform))


@pytest.mark.parametrize("waveform", WAVES)
def test_cv_to_frequency_single_voice_row_is_mono(waveform):
    """The house invariant: a ``(1, F)`` CV renders the mono ``(F,)``
    CV's samples bit for bit, across blocks and across epoch wraps
    (4 s = 2.7 epochs), at an odd block size."""
    n = 4 * SR
    cv = np.concatenate([_seq(n)[: n // 2], _vib(n)[n // 2:]])
    mono = _ctf_render(1000, cv, waveform, n=n)
    voice = _ctf_render(1000, cv[None, :], waveform, n=n)
    assert voice.shape == (1, n)
    assert np.array_equal(voice[0], mono)


@pytest.mark.parametrize("waveform", ("sine", "saw_blep"))
def test_cv_to_frequency_chord_rows_are_mono(waveform):
    """Every row of a 4-voice render is the mono render of that row's CV
    (the wavetable shapes are excluded by design: their band is the
    highest voice's, the conservative pick)."""
    n = 3 * SR
    cv4 = _vib(n, 4)
    chord = _ctf_render(512, cv4, waveform, n=n)
    for v in range(4):
        assert np.array_equal(chord[v], _ctf_render(512, cv4[v], waveform, n=n)), v


def test_cv_to_frequency_bipolar_is_block_size_exact():
    """The negative-side curve rides the same integrator."""
    cv = (0.9 * np.sin(2 * np.pi * 0.7 * np.arange(N) / SR)).astype(np.float32)
    params = {"negative_enabled": True, "f0_neg": 220.0, "fm_neg": 90.0,
              "f1_neg": 40.0, "mode_neg": "linear"}
    _assert_block_exact(
        "bipolar", lambda b: _ctf_render(b, cv, "saw_blep", params=params))


def test_cv_to_frequency_switching_engines_is_continuous():
    """Patching / unpatching ``cv`` hands the phase straight across, as the
    oscillator's does: one unbroken sine when the CV pitch equals the
    knob's. (The old pair skipped a sample going knob -> CV and repeated
    one coming back.)"""
    patch = Patch()
    ctf = patch.add_module(
        "cv_to_frequency",
        params={"freq": 440.0, "fm": 440.0, "waveform": "sine"},
    )
    src = patch.add_module("cv_keyboard")
    b = NumpyBackend(sample_rate=SR, block_size=256)
    b.compile(patch)
    m = patch.get(ctf.id)
    half = {(src.id, "pitch_cv"): np.full(256, 0.5, dtype=np.float32)}
    blocks = [b._render_cv_to_frequency(m, 256, {}, patch) for _ in range(3)]
    patch.connect(src.id, "pitch_cv", ctf.id, "cv")
    blocks += [b._render_cv_to_frequency(m, 256, half, patch) for _ in range(3)]
    assert patch.disconnect(src.id, "pitch_cv", ctf.id, "cv")
    blocks += [b._render_cv_to_frequency(m, 256, {}, patch) for _ in range(3)]
    x = np.concatenate(blocks).astype(np.float64)
    ref = np.sin(2 * np.pi * 440.0 * np.arange(x.size) / SR)
    assert np.abs(x - ref).max() < 1e-6


# ----- keyboard / midi_input ------------------------------------------------

_EMPTY = {"note": -1, "velocity": 0.0, "held": False, "sustained": False,
          "gating": False}


def _slots_at(pos):
    """A voice-slot schedule that changes only at multiples of ``E``:
    every event the renderer treats differently."""
    seg = pos // E
    s = [dict(_EMPTY) for _ in range(16)]

    def put(i, note, gating, vel):
        s[i] = {"note": note, "velocity": vel, "held": gating,
                "sustained": False, "gating": gating}

    if seg == 0:
        put(0, 60, True, 1.0)
        put(1, 64, True, 0.7)
        put(2, 67, True, 0.5)
    elif seg == 1:
        put(0, 62, True, 0.9)   # reassigned: phase + env reset
        put(1, 64, False, 0.7)  # released: the tail
        put(2, 67, True, 0.5)
    elif seg == 2:
        put(0, 62, True, 0.9)
        put(1, 64, True, 0.7)   # re-pressed, same note: phase carries on
        #                         slot 2 empty: reset
    elif seg == 3:
        put(0, 62, False, 0.9)  # released mid-hold
        put(1, 64, True, 0.7)
        put(2, 71, True, 0.6)   # a fresh voice in a cleared slot
    else:
        put(0, 62, True, 0.9)   # re-pressed after its tail ended
        put(1, 64, True, 0.7)
        put(2, 71, True, 0.6)
    return s


_BENDS = (0.0, 0.3, -0.5, 0.0, 0.8)  # one per segment: a re-anchor each


class _NoteStub:
    """Just what ``_render_keyboard`` / ``_render_midi_input`` read: the
    driver sets ``pos`` (the block's first absolute sample) per block."""

    def __init__(self, waveform="sine", amp=0.5, slots=_slots_at):
        self.id = 4242
        self.params = {"waveform": waveform, "amp": amp}
        self.pos = 0
        self._slots = slots

    def snapshot_voice_slots(self):
        return self._slots(self.pos)

    def snapshot_pitch_bend(self):
        return _BENDS[min(self.pos // E, len(_BENDS) - 1)]

    def snapshot_mod_wheel(self):
        return 0.0

    def snapshot_aftertouch(self):
        return 0.0


def _note_render(kind, block, waveform="sine", n=N, backend_out=None):
    b = NumpyBackend(sample_rate=SR, block_size=block)
    stub = _NoteStub(waveform)
    fn = b._render_keyboard if kind == "keyboard" else b._render_midi_input
    outs = []
    for pos in range(0, n, block):
        f = min(block, n - pos)
        stub.pos = pos
        outs.append(fn(stub, f)["out"])
    if backend_out is not None:
        backend_out.append(b)
    return np.concatenate(outs, axis=-1)


@pytest.mark.parametrize("waveform", NOTE_WAVES)
def test_keyboard_is_block_size_exact(waveform):
    """Held chord, a slot reassigned, a release tail, a re-press of the
    same note, a cleared slot, a fresh voice: every voice row identical
    at 64 / 128 / 512 / 1000 for 6 s -- phase and attack / release."""
    _assert_block_exact(f"keyboard {waveform}",
                        lambda b: _note_render("keyboard", b, waveform))


@pytest.mark.parametrize("waveform", NOTE_WAVES)
def test_midi_input_is_block_size_exact(waveform):
    """The same schedule with velocity and a pitch-bend move at every
    event (each one re-anchors every sounding voice's phase)."""
    _assert_block_exact(f"midi {waveform}",
                        lambda b: _note_render("midi_input", b, waveform))


def test_real_keyboard_is_block_size_exact():
    """The real Keyboard's allocator, driven by note_on / note_off on the
    common boundaries."""
    events = {
        0: [("on", 60), ("on", 64)],
        E: [("off", 60), ("on", 67)],
        2 * E: [("on", 60), ("off", 64)],
        3 * E: [("off", 67)],
        4 * E: [("on", 72), ("off", 60)],
    }

    def render(block):
        patch = Patch()
        kb = patch.add_module("keyboard", params={"waveform": "saw_blep"})
        b = NumpyBackend(sample_rate=SR, block_size=block)
        b.compile(patch)
        outs = []
        for pos in range(0, N, block):
            for kind, note in events.get(pos, ()):
                (kb.note_on if kind == "on" else kb.note_off)(note)
            outs.append(b._render_keyboard(kb, min(block, N - pos))["out"])
        return np.concatenate(outs, axis=-1)

    _assert_block_exact("real keyboard", render)


# ----- note-on behaviour, unchanged ------------------------------------------


@pytest.mark.parametrize("kind", ("keyboard", "midi_input"))
def test_note_on_starts_at_phase_zero_with_a_rising_ramp(kind):
    """A note's first block is exactly what the shipped renderer
    computed: phase 0.0 on the first sample, ``inc * k`` after it, the
    attack ``k / attack_samples`` from 0 -- so attacks keep their
    character. Checked for the chord's first block and for slot 0's
    reassigned note at ``E`` (its phase and envelope reset there)."""
    b = NumpyBackend(sample_rate=SR, block_size=512)
    stub = _NoteStub("sine", amp=0.5)
    fn = b._render_keyboard if kind == "keyboard" else b._render_midi_input
    att = 1.0 / max(1, int(b._KB_ATTACK_S * SR))
    k = np.arange(512, dtype=np.float64)

    def expect(note, vel, bend):
        mult = float(2.0 ** (bend * 2.0 / 12.0)) if kind == "midi_input" else 1.0
        inc = midi_to_freq(note) * mult / SR
        wave = np.sin(2.0 * np.pi * ((0.0 + inc * k) % 1.0))
        env = np.clip(0.0 + att * k, 0.0, 1.0)
        gain = vel if kind == "midi_input" else 1.0
        row = (wave * env * gain) if kind == "midi_input" else (wave * env)
        row = row.astype(np.float32)
        row *= 0.5
        return row

    first = fn(stub, 512)["out"]
    for slot, note, vel in ((0, 60, 1.0), (1, 64, 0.7), (2, 67, 0.5)):
        assert np.array_equal(first[slot], expect(note, vel, 0.0)), slot
    for pos in range(512, E, 512):
        stub.pos = pos
        fn(stub, 512)
    stub.pos = E
    assert np.array_equal(fn(stub, 512)["out"][0], expect(62, 0.9, _BENDS[1]))


def test_held_and_repressed_note_phase_is_one_unbroken_ramp():
    """Slot 1 holds note 64 throughout -- released at ``E``, re-pressed at
    ``2E`` without the slot being reused -- so its phase never resets: at
    every block size the last sample's phase is ``(inc * s) % 1`` for
    the absolute sample ``s``, the integer-count ramp from the note-on."""
    inc = midi_to_freq(64) / SR
    want = (inc * float(N - 1)) % 1.0
    for block in BLOCKS:
        held = []
        _note_render("keyboard", block, "sine", backend_out=held)
        st = held[0]._state[4242]
        assert st["voice"][1]["phase"] == want, block


def test_pitch_bend_reanchors_where_the_ramp_was_heading():
    """Each bend move re-anchors a sounding voice at the phase its old
    ramp was heading for (the next sample's), then counts from there --
    the oscillator's knob-move rule. Recomputed by hand from the five
    bends, at every block size."""
    origin = 0.0
    incs = [midi_to_freq(64) * float(2.0 ** (bd * 2.0 / 12.0)) / SR
            for bd in _BENDS]
    for seg in range(1, len(_BENDS)):
        origin = (origin + incs[seg - 1] * float(E)) % 1.0
    want = (origin + incs[-1] * float(N - 1 - 4 * E)) % 1.0
    for block in BLOCKS:
        held = []
        _note_render("midi_input", block, "sine", backend_out=held)
        assert held[0]._state[4242]["voice"][1]["phase"] == want, block


# ----- the mechanisms ----------------------------------------------------------


def _ramp(block, releasing_at, n, att=220, rel=882):
    vs = NumpyBackend._note_voice_fresh()
    out = []
    for pos in range(0, n, block):
        out.append(NumpyBackend._note_env_ramp(
            vs, min(block, n - pos), releasing_at(pos), att, rel))
    return np.concatenate(out)


def test_attack_release_ramp_is_exact_in_float64():
    """The voice ramp itself, in float64 -- the float32 output hides all
    but a couple of the old ramp's partings. The old carried level
    (``env + frames * delta``) parted at ~1600 samples of the 6 s
    schedule (measured 2026-10-12). A release mid-attack and a re-press
    mid-release need events closer than any block of the main set, so
    the short schedule runs on blocks that divide 100."""
    def short(pos):
        return 200 <= pos < 600 or 2000 <= pos < 5000

    ref = _ramp(10, short, 8000)
    for block in (20, 25, 50):
        assert np.array_equal(_ramp(block, short, 8000), ref), block
    # The first attack is the closed form the shipped ramp computed, and
    # the shape is the old one: up in 220 samples, down in 882.
    k = np.arange(200, dtype=np.float64)
    assert np.array_equal(ref[:200], np.clip(0.0 + (1.0 / 220) * k, 0.0, 1.0))
    assert ref[1000] == 1.0 and ref[2000 + 441] == pytest.approx(0.5, abs=1e-9)
    assert ref[2000 + 883] == 0.0 and ref[4999] == 0.0

    def long(pos):
        return pos // E in (1, 3)

    ref = _ramp(BLOCKS[0], long, N)
    for block in BLOCKS[1:]:
        assert np.array_equal(_ramp(block, long, N), ref), block


def test_constant_pitch_lands_on_the_true_edge():
    """440 Hz at 44.1 kHz completes exactly 44 cycles every 4410 samples.
    The integer count lands those samples on phase 0.0 -- a naive saw's
    post-edge -1 -- where the old per-block accumulator could land an
    ulp short of the wrap, on the pre-edge side. That full-step flip on
    a naive saw / square at exactly A (cv_to_frequency's default 440, an
    unbent keyboard / MIDI A) is the one move this change makes above
    rounding; the oscillator made the same one on 2026-09-24."""
    out = _ctf_render(512, None, "saw", n=3 * SR, params={"freq": 440.0})
    edges = np.arange(0, 3 * SR, 4410)
    assert np.all(out[edges] == np.float32(-1.0))

    a4 = dict(_EMPTY, note=69, velocity=1.0, held=True, gating=True)

    def slots(pos):
        return [a4] + [dict(_EMPTY) for _ in range(15)]

    b = NumpyBackend(sample_rate=SR, block_size=1000)
    stub = _NoteStub("saw", amp=0.5, slots=slots)
    rows = []
    for pos in range(0, 3 * SR, 1000):
        stub.pos = pos
        rows.append(b._render_keyboard(stub, min(1000, 3 * SR - pos))["out"][0])
    kb = np.concatenate(rows)
    assert np.all(kb[edges[1:]] == np.float32(-0.5))
