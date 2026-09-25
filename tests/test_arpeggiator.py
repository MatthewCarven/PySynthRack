"""Arpeggiator — poly→mono clocked note collapser.

Pins the contract: up/down/updown/order walk the held set exactly
(updown endpoints unrepeated, order = arrival order even across slot
reuse), octaves stack passes (descending stacks descend), random is
seeded and draws only held notes, joins/leaves mid-arp take effect on
the next step, the position rewinds when the chord empties (and on a
``reset`` edge), ``hold`` latches with press-from-silence starting a
new chord, the pitch is sampled at the gate rise (later pitch movement
doesn't retune a held note) and holds through silence, ``gate_len``
runs the measured clock fraction (mirroring the clock before an
interval exists), mono 1D inputs work as V = 1, an unpatched gate is
silence, and everything is block-size independent.

2026-09-14: the internal clock -- with ``clock`` unpatched the arp
steps at bpm x division on an exact integer period (no drift), the
gate runs gate_len of that period, ``reset`` re-phases it so the next
sample is note 1, a patched clock makes bpm/division inert (bit-exact
against the old render), and the free-run is block-size independent.

Drives the renderer directly with hand-fed buffers (the clockwork-test
harness pattern).
"""
from __future__ import annotations

import numpy as np
import pytest

from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.core.patch import Patch
from pysynthrack.modules.arpeggiator import ARP_MODES

SR = 1000
GAP = 20  # clock period in samples for _clock()


def _driver(params=None, block=64):
    """Backend + cv_keyboard/clock/reset → arpeggiator patch."""
    patch = Patch()
    arp = patch.add_module("arpeggiator", params=params or {})
    kb = patch.add_module("cv_keyboard")
    clk = patch.add_module("clock")
    rst = patch.add_module("clock")
    patch.connect(kb.id, "pitch_cv", arp.id, "pitch_cv")
    patch.connect(kb.id, "gate", arp.id, "gate")
    patch.connect(clk.id, "out", arp.id, "clock")
    patch.connect(rst.id, "out", arp.id, "reset")
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(patch)

    def step(pitch=None, gate=None, clock=None, reset=None, frames=None):
        for a in (pitch, gate, clock, reset):
            if a is not None:
                frames = np.asarray(a).shape[-1]
                break
        buffers = {}
        if pitch is not None:
            buffers[(kb.id, "pitch_cv")] = np.asarray(pitch, dtype=np.float32)
        if gate is not None:
            buffers[(kb.id, "gate")] = np.asarray(gate, dtype=np.float32)
        if clock is not None:
            buffers[(clk.id, "out")] = np.asarray(clock, dtype=np.float32)
        if reset is not None:
            buffers[(rst.id, "out")] = np.asarray(reset, dtype=np.float32)
        return b._render_arpeggiator(patch.get(arp.id), frames, buffers, patch)

    step.arp = arp
    step.patch = patch
    step.backend = b
    return step


def _clock(n_edges, gap=GAP, width=5):
    out = np.zeros(n_edges * gap, dtype=np.float32)
    for k in range(n_edges):
        out[k * gap : k * gap + width] = 1.0
    return out


def _voices(frames, *notes):
    """Build (16, F) pitch+gate buffers from (voice, semitones, on, off)."""
    pitch = np.zeros((16, frames), dtype=np.float32)
    gate = np.zeros((16, frames), dtype=np.float32)
    for v, st, on, off in notes:
        pitch[v, :] = st / 12.0
        gate[v, on:off] = 1.0
    return pitch, gate


def _steps_st(out, n_edges, gap=GAP, probe=2):
    """Output pitch in semitones sampled just after each clock edge."""
    return [
        round(float(out["pitch_cv"][k * gap + probe]) * 12.0, 3)
        for k in range(n_edges)
    ]


def _gated(out, n_edges, gap=GAP, probe=2):
    return [bool(out["gate"][k * gap + probe] > 0.5) for k in range(n_edges)]


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["arpeggiator"]
    assert cls is get_module_type("arpeggiator")
    assert cls.CATEGORY == "Modulation"
    m = cls(1)
    assert [p.name for p in m.input_ports] == [
        "pitch_cv", "gate", "clock", "reset",
    ]
    assert [p.name for p in m.output_ports] == ["pitch_cv", "gate"]
    assert m.params["mode"] == "up"
    assert m.params["octaves"] == 1
    assert m.params["hold"] is False


def test_serialization_round_trip():
    cls = all_module_types()["arpeggiator"]
    m = cls(3, params={"mode": "updown", "octaves": 3, "hold": True})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


def test_every_mode_name_is_known():
    assert ARP_MODES == ("up", "down", "updown", "order", "random")


# ----- note order ------------------------------------------------------------


def _held_chord(step, mode_edges, semis=(0, 4, 7)):
    """Render one block: chord held throughout, n clock edges."""
    F = mode_edges * GAP
    notes = [(v, st, 0, F) for v, st in enumerate(semis)]
    pitch, gate = _voices(F, *notes)
    return step(pitch=pitch, gate=gate, clock=_clock(mode_edges))


def test_up_walks_ascending():
    step = _driver({"mode": "up"})
    out = _held_chord(step, 7)
    assert _steps_st(out, 7) == [0, 4, 7, 0, 4, 7, 0]


def test_down_walks_descending():
    step = _driver({"mode": "down"})
    out = _held_chord(step, 7)
    assert _steps_st(out, 7) == [7, 4, 0, 7, 4, 0, 7]


def test_updown_palindrome_endpoints_unrepeated():
    step = _driver({"mode": "updown"})
    out = _held_chord(step, 9)
    assert _steps_st(out, 9) == [0, 4, 7, 4, 0, 4, 7, 4, 0]


def test_order_is_arrival_order_not_slot_order():
    step = _driver({"mode": "order"})
    F = 8 * GAP
    # G lands in slot 0 first, then C (slot 1), then E (slot 2): played
    # order G C E must win over slot/pitch order.
    pitch, gate = _voices(F, (0, 7, 0, F), (1, 0, 1, F), (2, 4, 2, F))
    out = step(pitch=pitch, gate=gate, clock=_clock(8))
    assert _steps_st(out, 8) == [7, 0, 4, 7, 0, 4, 7, 0]


def test_random_is_seeded_and_stays_in_the_chord():
    a = _held_chord(_driver({"mode": "random", "seed": 7}), 12)
    b = _held_chord(_driver({"mode": "random", "seed": 7}), 12)
    c = _held_chord(_driver({"mode": "random", "seed": 8}), 12)
    seq_a, seq_b, seq_c = (
        _steps_st(x, 12) for x in (a, b, c)
    )
    assert seq_a == seq_b  # same seed, same walk
    assert seq_a != seq_c  # different seed, different walk
    assert set(seq_a) <= {0.0, 4.0, 7.0}


# ----- octaves ---------------------------------------------------------------


def test_octaves_stack_ascending_passes():
    step = _driver({"mode": "up", "octaves": 2})
    out = _held_chord(step, 7)
    assert _steps_st(out, 7) == [0, 4, 7, 12, 16, 19, 0]


def test_octaves_descend_from_the_top_in_down_mode():
    step = _driver({"mode": "down", "octaves": 2})
    out = _held_chord(step, 7)
    assert _steps_st(out, 7) == [19, 16, 12, 7, 4, 0, 19]


def test_mono_inputs_make_a_one_finger_octave_arp():
    step = _driver({"mode": "up", "octaves": 2})
    F = 5 * GAP
    pitch = np.full(F, 2 / 12.0, dtype=np.float32)  # 1D mono D4
    gate = np.ones(F, dtype=np.float32)
    out = step(pitch=pitch, gate=gate, clock=_clock(5))
    assert _steps_st(out, 5) == [2, 14, 2, 14, 2]


# ----- joins / leaves / silence ---------------------------------------------


def test_note_joining_mid_arp_lands_on_the_next_pass():
    step = _driver({"mode": "up"})
    F = 8 * GAP
    # C+G held from the start; E joins during step 3.
    pitch, gate = _voices(
        F, (0, 0, 0, F), (1, 7, 0, F), (2, 4, 2 * GAP + 10, F)
    )
    out = step(pitch=pitch, gate=gate, clock=_clock(8))
    assert _steps_st(out, 8) == [0, 7, 0, 4, 7, 0, 4, 7]


def test_note_leaving_mid_arp_drops_from_the_next_step():
    step = _driver({"mode": "up"})
    F = 8 * GAP
    pitch, gate = _voices(
        F, (0, 0, 0, F), (1, 4, 0, 2 * GAP + 10), (2, 7, 0, F)
    )
    out = step(pitch=pitch, gate=gate, clock=_clock(8))
    # E leaves after step 3 (G) played; the wrapped index lands on G
    # again for step 4, then the two-note walk continues C G C G.
    assert _steps_st(out, 8) == [0, 4, 7, 7, 0, 7, 0, 7]


def test_empty_chord_is_silent_and_pitch_holds():
    step = _driver({"mode": "up"})
    F = 8 * GAP
    pitch, gate = _voices(F, (0, 4, 0, 3 * GAP + 10))
    out = step(pitch=pitch, gate=gate, clock=_clock(8))
    gates = _gated(out, 8)
    assert gates[:4] == [True] * 4 and gates[4:] == [False] * 4
    # The line stays on the last note through the silence — release
    # tails downstream stay in tune.
    assert float(out["pitch_cv"][-1]) * 12.0 == pytest.approx(4.0)


def test_new_chord_restarts_from_note_one():
    step = _driver({"mode": "up"})
    F = 4 * GAP
    pitch, gate = _voices(F, (0, 0, 0, F), (1, 4, 0, F), (2, 7, 0, F))
    step(pitch=pitch, gate=gate, clock=_clock(4))  # ends mid-pattern (E next)
    # Everything released, then a new chord: must restart at its lowest.
    pitch2, gate2 = _voices(2 * GAP)
    step(pitch=pitch2, gate=gate2, clock=np.zeros(2 * GAP, dtype=np.float32))
    pitch3, gate3 = _voices(4 * GAP, (0, 2, 0, 4 * GAP), (1, 9, 0, 4 * GAP))
    out = step(pitch=pitch3, gate=gate3, clock=_clock(4))
    assert _steps_st(out, 4) == [2, 9, 2, 9]


def test_reset_edge_rewinds_to_note_one():
    step = _driver({"mode": "up"})
    F = 8 * GAP
    pitch, gate = _voices(F, (0, 0, 0, F), (1, 4, 0, F), (2, 7, 0, F))
    reset = np.zeros(F, dtype=np.float32)
    reset[3 * GAP + 10] = 1.0  # between steps 4 and 5
    out = step(pitch=pitch, gate=gate, clock=_clock(8), reset=reset)
    assert _steps_st(out, 8) == [0, 4, 7, 0, 0, 4, 7, 0]


def test_unpatched_gate_is_silent():
    step = _driver()
    F = 4 * GAP
    pitch, _ = _voices(F, (0, 4, 0, F))
    out = step(pitch=pitch, clock=_clock(4), frames=F)
    assert not np.any(out["gate"] > 0.5)


# ----- hold latch ------------------------------------------------------------


def test_hold_keeps_released_notes_playing():
    step = _driver({"mode": "up", "hold": True})
    F = 8 * GAP
    pitch, gate = _voices(F, (0, 0, 0, 30), (1, 4, 0, 30), (2, 7, 0, 30))
    out = step(pitch=pitch, gate=gate, clock=_clock(8))
    assert _steps_st(out, 8) == [0, 4, 7, 0, 4, 7, 0, 4]
    assert all(_gated(out, 8))


def test_hold_press_from_silence_starts_a_new_chord():
    step = _driver({"mode": "up", "hold": True})
    F = 8 * GAP
    # Latch C+E early (released by 30); D pressed at 4×GAP from silence
    # — the latch clears and only D plays from step 5.
    pitch, gate = _voices(
        F, (0, 0, 0, 30), (1, 4, 0, 30), (2, 2, 4 * GAP + 1, F)
    )
    out = step(pitch=pitch, gate=gate, clock=_clock(8))
    assert _steps_st(out, 8) == [0, 4, 0, 4, 0, 2, 2, 2]


def test_hold_off_toggle_drops_latched_notes():
    step = _driver({"mode": "up", "hold": True})
    F = 4 * GAP
    pitch, gate = _voices(F, (0, 0, 0, 30), (1, 4, 0, 30))
    out = step(pitch=pitch, gate=gate, clock=_clock(4))
    assert all(_gated(out, 4))
    step.arp.params["hold"] = False  # live toggle: latch must drop
    pitch2, gate2 = _voices(F)
    out2 = step(pitch=pitch2, gate=gate2, clock=_clock(4))
    assert not any(_gated(out2, 4))


# ----- pitch sampling --------------------------------------------------------


def test_pitch_is_sampled_at_the_gate_rise():
    step = _driver({"mode": "up"})
    F = 6 * GAP
    pitch, gate = _voices(F, (0, 0, 0, F))
    pitch[0, GAP:] = 10 / 12.0  # the source wobbles AFTER the press
    out = step(pitch=pitch, gate=gate, clock=_clock(6))
    # The held note stays what it was at the rise.
    assert _steps_st(out, 6) == [0, 0, 0, 0, 0, 0]


# ----- gate length -----------------------------------------------------------


def test_gate_len_runs_the_measured_clock_fraction():
    step = _driver({"gate_len": 0.25})
    out = _held_chord(step, 8)
    # Edge 3 onward the 20-sample period is measured: 5-sample gates.
    seg = out["gate"][2 * GAP : 3 * GAP]
    assert float(seg.sum()) == 5.0
    assert np.all(seg[:5] == 1.0)


def test_gate_mirrors_clock_before_an_interval_exists():
    step = _driver({"gate_len": 0.25})
    F = GAP
    pitch, gate = _voices(F, (0, 0, 0, F))
    clock = np.zeros(F, dtype=np.float32)
    clock[0:7] = 1.0  # single edge, 7-sample high
    out = step(pitch=pitch, gate=gate, clock=clock)
    assert float(out["gate"].sum()) == 7.0


# ----- block-size independence ----------------------------------------------


@pytest.mark.parametrize("mode", ["up", "updown", "order", "random"])
def test_block_size_independent(mode):
    F = 8 * GAP
    notes = [(0, 0, 0, F), (1, 4, 10, F - 30), (2, 7, 25, F)]
    clock = _clock(8)

    def render(block):
        step = _driver({"mode": mode, "octaves": 2, "seed": 5}, block=block)
        pitch, gate = _voices(F, *notes)
        if block >= F:
            out = step(pitch=pitch, gate=gate, clock=clock)
            return out["pitch_cv"], out["gate"]
        ps, gs = [], []
        for i in range(0, F, block):
            s = slice(i, i + block)
            out = step(
                pitch=pitch[:, s], gate=gate[:, s], clock=clock[s]
            )
            ps.append(out["pitch_cv"])
            gs.append(out["gate"])
        return np.concatenate(ps), np.concatenate(gs)

    p_big, g_big = render(F)
    p_small, g_small = render(16)
    assert np.array_equal(p_big, p_small)
    assert np.array_equal(g_big, g_small)


# ----- integration -----------------------------------------------------------


def test_full_graph_render_with_downstream_voice():
    """cv_keyboard → arp → oscillator/adsr/vca chain compiles + renders."""
    patch = Patch()
    kb = patch.add_module("cv_keyboard")
    clk = patch.add_module("clock", params={"bpm": 600.0})
    arp = patch.add_module("arpeggiator")
    osc = patch.add_module("oscillator")
    env = patch.add_module("adsr")
    vca = patch.add_module("vca")
    out = patch.add_module("speaker_output")
    patch.connect(kb.id, "pitch_cv", arp.id, "pitch_cv")
    patch.connect(kb.id, "gate", arp.id, "gate")
    patch.connect(clk.id, "out", arp.id, "clock")
    patch.connect(arp.id, "pitch_cv", osc.id, "freq_cv")
    patch.connect(arp.id, "gate", env.id, "gate")
    patch.connect(osc.id, "out", vca.id, "audio")
    patch.connect(env.id, "cv", vca.id, "cv")
    patch.connect(vca.id, "out", out.id, "in")
    kb.note_on(60)
    kb.note_on(64)
    kb.note_on(67)
    b = NumpyBackend(sample_rate=44100, block_size=256)
    b.compile(patch)
    for _ in range(8):
        mix, _dev = b.render_block_multi(256)
        assert mix is not None and np.all(np.isfinite(mix))


# ----- 2026-09-14: internal clock -------------------------------------------


def _edges(x):
    hi = np.asarray(x) > 0.5
    return sorted(([0] if hi[0] else []) + (np.flatnonzero(hi[1:] & ~hi[:-1]) + 1).tolist())


def test_internal_clock_defaults_and_period():
    m = all_module_types()["arpeggiator"](1)
    assert m.params["bpm"] == 120.0 and m.params["division"] == 4.0
    # SR 1000: 120 bpm x 4 = 8 steps/s = period 125, half-high.
    step = _driver()
    frames = 1000
    pitch, gate = _voices(frames, (0, 0, 0, frames), (1, 4, 0, frames), (2, 7, 0, frames))
    out = step(pitch=pitch, gate=gate)          # no clock buffer at all
    edges = _edges(out["gate"])
    assert edges[:4] == [0, 125, 250, 375]
    assert set(np.diff(edges).tolist()) == {125}
    assert [round(float(out["pitch_cv"][e + 1]) * 12) for e in edges[:6]] == [0, 4, 7, 0, 4, 7]


def test_internal_clock_gate_len_uses_the_measured_period():
    step = _driver({"gate_len": 0.2})
    frames = 1000
    pitch, gate = _voices(frames, (0, 0, 0, frames))
    out = step(pitch=pitch, gate=gate)
    g = out["gate"]
    # First step mirrors the half-period high (no interval yet): 62 samples.
    assert g[:62].tolist() == [1.0] * 62 and g[62] == 0.0
    # From the second edge: 0.2 x 125 = 25 samples.
    assert g[125:150].tolist() == [1.0] * 25 and g[150] == 0.0


def test_internal_clock_never_drifts():
    """bpm 97 / division 3 at SR 1000 asks for 206.2 samples: the period
    is round()ed once and every step is exactly that -- no fractional
    accumulation (the fgen lesson)."""
    step = _driver({"bpm": 97.0, "division": 3.0}, block=64)
    frames = 64 * 80
    pitch, gate = _voices(frames, (0, 0, 0, frames))
    outs = [step(pitch=pitch[:, i:i + 64], gate=gate[:, i:i + 64]) for i in range(0, frames, 64)]
    edges = _edges(np.concatenate([o["gate"] for o in outs]))
    assert len(edges) > 20
    assert set(np.diff(edges).tolist()) == {206}


def test_reset_rephases_the_internal_clock_to_note_one():
    # gate_len 0.2 so the step at 250 has released (275) before the reset
    # at 300 -- with the default 0.5 the gate is still high there and the
    # re-phased step shows only in the pitch, not as a fresh edge.
    step = _driver({"gate_len": 0.2})
    frames = 600
    pitch, gate = _voices(frames, (0, 0, 0, frames), (1, 4, 0, frames), (2, 7, 0, frames))
    reset = np.zeros(frames, np.float32)
    reset[300:303] = 1.0             # between the edges at 250 and 375
    out = step(pitch=pitch, gate=gate, reset=reset)
    edges = _edges(out["gate"])
    assert edges == [0, 125, 250, 300, 425, 550]
    assert round(float(out["pitch_cv"][301]) * 12) == 0      # note 1 on the reset sample
    assert round(float(out["pitch_cv"][426]) * 12) == 4


def test_patched_clock_makes_bpm_and_division_inert():
    frames = 400
    pitch, gate = _voices(frames, (0, 0, 0, frames), (1, 4, 0, frames))
    clock = _clock(frames // GAP)
    a = _driver()(pitch=pitch, gate=gate, clock=clock)
    b = _driver({"bpm": 33.0, "division": 0.5})(pitch=pitch, gate=gate, clock=clock)
    assert np.array_equal(a["pitch_cv"], b["pitch_cv"])
    assert np.array_equal(a["gate"], b["gate"])
    assert set(np.diff(_edges(a["gate"])).tolist()) == {GAP}


@pytest.mark.parametrize("block", [16, 64, 256])
def test_internal_clock_is_block_size_independent(block):
    frames = 1024
    pitch, gate = _voices(frames, (0, 0, 0, frames), (1, 7, 100, frames))
    reset = np.zeros(frames, np.float32)
    reset[700] = 1.0
    ref = _driver({"bpm": 150.0})(pitch=pitch, gate=gate, reset=reset)
    step = _driver({"bpm": 150.0}, block=block)
    outs = [
        step(pitch=pitch[:, i:i + block], gate=gate[:, i:i + block], reset=reset[i:i + block])
        for i in range(0, frames, block)
    ]
    for key in ("pitch_cv", "gate"):
        assert np.array_equal(np.concatenate([o[key] for o in outs]), ref[key]), key


def test_internal_clock_free_runs_in_a_real_patch():
    """cv_keyboard -> arpeggiator (no clock) -> oscillator, compiled and
    rendered: the arp steps without a clock module in the rack."""
    patch = Patch()
    kb = patch.add_module("cv_keyboard")
    arp = patch.add_module("arpeggiator", params={"bpm": 240.0})
    osc = patch.add_module("oscillator")
    spk = patch.add_module("speaker_output")
    patch.connect(kb.id, "pitch_cv", arp.id, "pitch_cv")
    patch.connect(kb.id, "gate", arp.id, "gate")
    patch.connect(arp.id, "pitch_cv", osc.id, "freq_cv")
    patch.connect(osc.id, "out", spk.id, "in")
    b = NumpyBackend(sample_rate=SR, block_size=64)
    b.compile(patch)
    for _ in range(4):
        out = b.render_block(64)
        assert out is None or np.all(np.isfinite(out))
    assert b._state[arp.id]["int_phase"] == (4 * 64) % round(SR * 60 / (240 * 4))
