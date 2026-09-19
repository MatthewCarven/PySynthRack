"""Tests for the ADSR envelope module."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.core.module import get_module_type
from pysynthrack.modules.adsr import ADSR

SR = 44100


def _adsr_with_gate_source(attack=0.01, decay=0.1, sustain=0.5, release=0.1):
    """Build a patch: keyboard(gate) → adsr. Returns (patch, kb, adsr)."""
    patch = Patch()
    kb = patch.add_module("keyboard")
    env = patch.add_module(
        "adsr",
        params={"attack": attack, "decay": decay, "sustain": sustain, "release": release},
    )
    patch.connect(kb.id, "gate", env.id, "gate")
    return patch, kb, env


def _render_cv(backend, patch, env, frames: int) -> np.ndarray:
    """Render one block through the keyboard's gate into the ADSR.

    The backend's _render_module path produces both the gate and the
    envelope CV in the right order; we collect just the envelope's output.

    Slice 4: Keyboard now emits a per-voice (V, F) gate buffer, so the
    ADSR (which is voice-aware as of slice 3a) returns a per-voice (V, F)
    envelope. These tests press one note at a time, so summing across the
    voice axis collapses to a 1D envelope identical in shape to the pre-
    slice-4 mono path -- the same implicit-sum-at-mono-sinks rule the
    SpeakerOutput uses.
    """
    kb = next(m for m in patch if m.TYPE == "keyboard")
    buffers: dict = {}
    kb_out = backend._render_keyboard(kb, frames=frames)
    buffers[(kb.id, "out")] = kb_out["out"]
    buffers[(kb.id, "gate")] = kb_out["gate"]
    cv = backend._render_adsr(env, frames, buffers, patch)
    if cv.ndim == 2:
        cv = cv.sum(axis=0)
    return cv


class TestADSRModel:
    def test_register_and_defaults(self):
        patch = Patch()
        env = patch.add_module("adsr")
        assert isinstance(env, ADSR)
        assert env.params == {
            "attack": 0.01,
            "decay": 0.10,
            "sustain": 0.70,
            "release": 0.30,
        }
        assert [p.name for p in env.input_ports] == ["gate", "vel"]
        assert env.input_ports[0].signal_kind == "gate"
        assert env.input_ports[1].signal_kind == "cv"
        assert [p.name for p in env.output_ports] == ["cv"]
        assert env.output_ports[0].signal_kind == "cv"

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module(
            "adsr",
            params={"attack": 0.5, "decay": 0.2, "sustain": 0.3, "release": 1.0},
        )
        restored = Patch.from_dict(patch.to_dict())
        env = next(m for m in restored if m.TYPE == "adsr")
        assert env.params["attack"] == 0.5
        assert env.params["decay"] == 0.2
        assert env.params["sustain"] == 0.3
        assert env.params["release"] == 1.0


class TestADSRBehavior:
    def test_idle_with_no_gate_is_zero(self):
        """A patched-but-not-triggered ADSR should hold at 0."""
        patch = Patch()
        env = patch.add_module("adsr")
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        out = backend._render_adsr(env, 512, {}, patch)
        assert np.allclose(out, 0.0)

    def test_attack_reaches_one_after_attack_time(self):
        """With attack=10ms at 44.1kHz, the envelope should hit 1.0 within
        ~440 samples of gate-on."""
        sr = 44100
        patch, kb, env = _adsr_with_gate_source(attack=0.01, decay=0.1, sustain=0.7)
        backend = NumpyBackend(sample_rate=sr, block_size=2048)
        backend.compile(patch)
        kb.note_on(60)
        # Render enough samples to span attack + start of decay.
        cv = _render_cv(backend, patch, env, frames=2048)
        peak = float(np.max(cv))
        assert peak >= 0.99, f"attack peak was {peak:.3f}"

    def test_sustain_holds_value_while_gate_high(self):
        """After attack+decay, the envelope should sit on the sustain level."""
        sr = 44100
        patch, kb, env = _adsr_with_gate_source(
            attack=0.005, decay=0.01, sustain=0.4, release=0.1
        )
        backend = NumpyBackend(sample_rate=sr, block_size=4096)
        backend.compile(patch)
        kb.note_on(60)
        # First render: passes through attack + decay.
        _ = _render_cv(backend, patch, env, frames=4096)
        # Second render: should be entirely sustain.
        cv = _render_cv(backend, patch, env, frames=4096)
        # All values should be close to sustain.
        assert float(np.min(cv)) >= 0.39
        assert float(np.max(cv)) <= 0.41

    def test_release_decays_to_zero(self):
        """Releasing the gate ramps the envelope back to 0."""
        sr = 44100
        patch, kb, env = _adsr_with_gate_source(
            attack=0.001, decay=0.001, sustain=0.5, release=0.05
        )
        backend = NumpyBackend(sample_rate=sr, block_size=2048)
        backend.compile(patch)
        kb.note_on(60)
        # Settle at sustain.
        _ = _render_cv(backend, patch, env, frames=2048)
        kb.all_notes_off()
        # 50ms release at 44.1kHz = 2205 samples. Render 4096 to fully decay.
        cv = _render_cv(backend, patch, env, frames=4096)
        # Last sample should be ~0 — env has had time to release fully.
        assert abs(float(cv[-1])) < 1e-3

    def test_no_retrigger_during_held_gate(self):
        """A second note on top of a held one should NOT reset the envelope.
        Master-envelope semantics: the gate already-high stays high."""
        sr = 44100
        patch, kb, env = _adsr_with_gate_source(attack=0.05, decay=0.1, sustain=0.8)
        backend = NumpyBackend(sample_rate=sr, block_size=4096)
        backend.compile(patch)
        kb.note_on(60)
        # Render through attack + decay → settled at sustain.
        for _ in range(3):
            cv = _render_cv(backend, patch, env, frames=4096)
        before_chord = float(cv[-1])
        # Add second note; gate stays high so envelope shouldn't snap back.
        kb.note_on(64)
        cv = _render_cv(backend, patch, env, frames=4096)
        # Sustain value must remain — no attack restart.
        assert abs(float(cv[0]) - before_chord) < 0.05

    def test_no_nan_with_zero_durations(self):
        """An ADSR with all-zero times shouldn't divide by zero."""
        patch, kb, env = _adsr_with_gate_source(
            attack=0.0, decay=0.0, sustain=0.5, release=0.0
        )
        backend = NumpyBackend(sample_rate=44100, block_size=512)
        backend.compile(patch)
        kb.note_on(60)
        cv = _render_cv(backend, patch, env, frames=512)
        assert not np.any(np.isnan(cv))
        assert not np.any(np.isinf(cv))


# ----- vel: velocity, latched at the gate edge (2026-09-19 love pass) --------


def _vel_patch(**params):
    """clock(gate) + lfo(cv) -> adsr with ``vel`` patched.

    The sources are only there so the cables land on real ports; the
    tests hand the ADSR its gate and vel buffers directly, so the shapes
    (mono ``(F,)`` or voice-aware ``(V, F)``) are whatever each test says.
    """
    p = Patch()
    g = p.add_module("clock")
    v = p.add_module("lfo")
    env = p.add_module("adsr", params=params)
    p.connect(g.id, "out", env.id, "gate")
    p.connect(v.id, "cv", env.id, "vel")
    return p, g, v, env


def _run_vel(p, g, v, env, gate, vel, block=512):
    """Render ``gate``/``vel`` through the ADSR in ``block``-sized pieces."""
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(p)
    n = gate.shape[-1]
    out = []
    for i in range(0, n, block):
        frames = min(block, n - i)
        bufs = {(g.id, "out"): gate[..., i:i + frames]}
        if vel is not None:
            bufs[(v.id, "cv")] = vel[..., i:i + frames]
        out.append(b._render_adsr(env, frames, bufs, p))
    return np.concatenate(out, axis=-1)


def _gate(n, spans):
    g = np.zeros(n, dtype=np.float32)
    for a, b in spans:
        g[a:b] = 1.0
    return g


_N = 8192
_P = dict(attack=0.01, decay=0.05, sustain=0.5, release=0.1)
_ATTACK_STEP = 1.0 / (_P["attack"] * SR)
_DECAY_STEP = (1.0 - _P["sustain"]) / (_P["decay"] * SR)


class TestADSRVelocity:
    def test_vel_is_read_at_the_edge_sample_and_latched(self):
        """The edge-latch rule: only vel[edge] matters for a note."""
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(1000, 5000)])
        ref = _run_vel(p, g, v, env, gate, np.full(_N, 0.5, np.float32))
        # 1.0 everywhere except the one edge sample.
        spike = np.ones(_N, np.float32)
        spike[1000] = 0.5
        assert np.array_equal(_run_vel(p, g, v, env, gate, spike), ref)
        # A bus that moves mid-note (0.5 -> 1.0 at sample 2000) changes
        # nothing until the next edge: not a block mean, not a re-read.
        moving = np.full(_N, 0.5, np.float32)
        moving[2000:] = 1.0
        assert np.array_equal(_run_vel(p, g, v, env, gate, moving), ref)
        assert float(ref.max()) == pytest.approx(0.5)

    def test_unpatched_vel_is_exactly_one(self):
        """The unpatched contract, both paths: bit-exact with vel wired at 1.0.

        This is the recipe's claim in test form -- the feature ships OFF.
        """
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(500, 2990), (3000, 6000)])
        ones = np.ones(_N, np.float32)
        assert np.array_equal(
            _run_vel(p, g, v, env, gate, None), _run_vel(p, g, v, env, gate, ones)
        )
        gate_v = np.zeros((3, _N), np.float32)
        gate_v[0] = gate
        gate_v[2, 1500:4000] = 1.0
        assert np.array_equal(
            _run_vel(p, g, v, env, gate_v, None),
            _run_vel(p, g, v, env, gate_v, np.ones((3, _N), np.float32)),
        )

    def test_vel_scales_the_whole_note_including_release(self):
        """cv = shape x vel: peak, plateau and the release tail all scale."""
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(1000, 3700)])
        one = _run_vel(p, g, v, env, gate, None).astype(np.float64)
        for s in (0.3, 0.75, 1.5):
            out = _run_vel(p, g, v, env, gate, np.full(_N, s, np.float32))
            # To within one decay step: the 441-sample attack sits on a
            # float knife-edge (441 * (step * s) rounds either side of s),
            # so the crossing may land a sample later than at vel 1 and
            # the decay ramp then trails by one step. Not a scaling error.
            assert np.allclose(out, s * one, atol=_DECAY_STEP * s * 1.01)
            assert int((np.abs(out - s * one) > 1e-6).sum()) <= int(_P["decay"] * SR) + 2
            assert float(out.max()) == pytest.approx(s, abs=1e-6)
            # plateau = sustain * vel (attack 441 + decay 2205 samples are
            # done by 3646); the tail is still ringing 800 samples into
            # the 4410-sample release and has reached 0 by the end (8110).
            assert float(out[3699]) == pytest.approx(_P["sustain"] * s, abs=1e-6)
            assert out[4500] > 0.0 and out[-1] == 0.0
        # The times do not move with velocity: a soft note's attack still
        # takes `attack` seconds (441 samples, +-1 for the knife-edge) to
        # reach its peak, so its slope is gentler.
        soft = _run_vel(p, g, v, env, gate, np.full(_N, 0.3, np.float32))
        assert abs(int(np.argmax(soft)) - 1000 - (int(_P["attack"] * SR) - 1)) <= 1
        assert float(np.diff(soft[1000:1400].astype(np.float64)).max()) == pytest.approx(
            0.3 * _ATTACK_STEP, rel=1e-3
        )

    def test_negative_vel_is_a_silent_note_and_the_latch_rearms(self):
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(1000, 3000), (4000, 6000)])
        vel = np.full(_N, -0.3, np.float32)
        vel[4000:] = 0.8
        out = _run_vel(p, g, v, env, gate, vel)
        assert np.all(out[:4000] == 0.0)
        assert float(out[4000:].max()) == pytest.approx(0.8, abs=1e-6)

    def test_retrigger_softer_falls_to_the_new_peak_without_a_jump(self):
        """Re-struck at vel 0.3 while ringing at sustain (0.5): the level
        falls to 0.3 at the full-velocity attack slope, then decays to
        0.15. No sample-to-sample step exceeds the attack slope."""
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(500, 2990), (3000, 6000)])
        vel = np.ones(_N, np.float32)
        vel[3000] = 0.3
        out = _run_vel(p, g, v, env, gate, vel).astype(np.float64)
        assert out[2999] == pytest.approx(0.5, abs=0.05)  # ringing near sustain
        steps = np.abs(np.diff(out))
        assert float(steps.max()) <= _ATTACK_STEP * (1.0 + 1e-3)
        assert abs(out[3000] - out[2999]) <= _ATTACK_STEP * (1.0 + 1e-3)
        assert float(out[5900]) == pytest.approx(0.3 * _P["sustain"], abs=1e-6)
        # And the mirror: re-struck LOUDER from a release tail rises from
        # where it is (no jump either) to the new peak of 1.0.
        gate2 = _gate(_N, [(500, 2000), (3000, 6000)])
        vel2 = np.full(_N, 0.4, np.float32)
        vel2[3000] = 1.0
        up = _run_vel(p, g, v, env, gate2, vel2).astype(np.float64)
        assert 0.0 < up[2999] < 0.4
        assert float(np.abs(np.diff(up)).max()) <= _ATTACK_STEP * (1.0 + 1e-3)
        assert float(up.max()) == pytest.approx(1.0)

    def test_retrigger_at_vel_zero_fades_over_one_attack_time(self):
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(500, 2990), (3000, 6000)])
        vel = np.ones(_N, np.float32)
        vel[3000] = 0.0
        out = _run_vel(p, g, v, env, gate, vel).astype(np.float64)
        first_zero = 3000 + int(np.argmax(out[3000:] == 0.0))
        assert out[2999] > 0.4
        assert 3000 < first_zero <= 3000 + int(_P["attack"] * SR)
        assert np.all(out[first_zero:] == 0.0)
        assert float(np.abs(np.diff(out)).max()) <= _ATTACK_STEP * (1.0 + 1e-3)

    def test_mono_vel_broadcasts_to_every_voice(self):
        p, g, v, env = _vel_patch(**_P)
        gate = np.zeros((4, _N), np.float32)
        gate[0, 1000:5000] = 1.0
        gate[1, 1000:5000] = 1.0
        gate[2, 1500:4000] = 1.0
        out = _run_vel(p, g, v, env, gate, np.full(_N, 0.5, np.float32))
        assert out.shape == (4, _N)
        # The voice path cascades the attack crossing into decay (a
        # documented, pre-existing divergence from the mono path), so the
        # peak sits one decay step below the latched scale.
        assert np.allclose(out[:3].max(axis=1), 0.5, atol=_DECAY_STEP * 0.5 * 1.01)
        assert np.all(out[3] == 0.0)

    def test_voice_vel_latches_per_voice_from_its_own_row(self):
        p, g, v, env = _vel_patch(**_P)
        gate = np.zeros((4, _N), np.float32)
        gate[0, 1000:5000] = 1.0
        gate[1, 1000:5000] = 1.0
        gate[2, 1500:6000] = 1.0
        vel = np.ones((4, _N), np.float32)
        vel[0] = 0.25
        vel[1] = 1.0
        vel[2] = 0.6
        out = _run_vel(p, g, v, env, gate, vel)
        for row, s, last in ((0, 0.25, 4999), (1, 1.0, 4999), (2, 0.6, 5999)):
            assert float(out[row].max()) == pytest.approx(s, abs=_DECAY_STEP * s * 1.01)
            assert float(out[row, last]) == pytest.approx(_P["sustain"] * s, abs=1e-6)
        assert np.all(out[3] == 0.0)

    def test_single_voice_row_matches_mono(self):
        """A one-voice (V, F) render is the mono render, up to the voice
        path's documented crossing-sample cascade (its decay ramp runs one
        sample ahead of mono's, so the two differ by one decay step for
        the length of the decay) -- which vel scales but does not add to:
        the differing stretch is the decay, and its size is decay_step*vel."""
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(1000, 5000)])
        decay_samples = int(_P["decay"] * SR)
        for s in (0.4, 1.0):
            vel = np.full(_N, s, np.float32)
            mono = _run_vel(p, g, v, env, gate, vel)
            voice = _run_vel(p, g, v, env, gate[None, :], vel[None, :])[0]
            diff = np.abs(voice.astype(np.float64) - mono)
            assert int((diff > 0).sum()) <= decay_samples + 2
            assert float(diff.max()) <= _DECAY_STEP * s * 1.01
            # Outside the decay ramp the two paths agree to the bit.
            assert np.array_equal(voice[:1000 + 400], mono[:1000 + 400])
            assert np.array_equal(voice[4000:], mono[4000:])

    def test_voice_vel_on_a_mono_gate_collapses_to_the_loudest_voice(self):
        """The drums' rule: a (V, F) velocity bus on a mono gate reads the
        max across voices at the edge (idle slots carry 0)."""
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(1000, 5000)])
        vel = np.zeros((4, _N), np.float32)
        vel[2] = 0.7
        vel[3] = 0.2
        out = _run_vel(p, g, v, env, gate, vel)
        assert out.shape == (_N,)
        assert np.array_equal(
            out, _run_vel(p, g, v, env, gate, np.full(_N, 0.7, np.float32))
        )

    def test_block_size_independence_with_the_edge_mid_stream(self):
        """Mono path: 64 vs 512 bit-exact (edges at 500/2990/3000 land
        mid-block for both sizes). Voice path: within one stage step --
        its analytic crossing can land a sample apart when a block
        boundary re-reads a rounded level, a pre-existing property that
        vel neither adds to nor fixes; the latched scale itself is the
        same either way."""
        p, g, v, env = _vel_patch(**_P)
        gate = _gate(_N, [(500, 2990), (3000, 6000)])
        vel = np.ones(_N, np.float32)
        vel[3000] = 0.3
        assert np.array_equal(
            _run_vel(p, g, v, env, gate, vel, 64), _run_vel(p, g, v, env, gate, vel, 512)
        )
        gate_v = np.zeros((2, _N), np.float32)
        gate_v[0] = gate
        gate_v[1, 1500:5000] = 1.0
        vel_v = np.ones((2, _N), np.float32)
        vel_v[0, 3000] = 0.3
        vel_v[1] = 0.6
        a = _run_vel(p, g, v, env, gate_v, vel_v, 64)
        b = _run_vel(p, g, v, env, gate_v, vel_v, 512)
        assert np.allclose(a, b, atol=max(_ATTACK_STEP, _DECAY_STEP) * 1.01)
        # The plateaus (sustain * latched vel) agree exactly; the peaks to
        # within the crossing cascade (a boundary ON the crossing emits
        # the bare peak, a crossing inside a run emits one decay step
        # below it -- again pre-existing).
        assert float(a[0, 5900]) == float(b[0, 5900]) == pytest.approx(0.15, abs=1e-6)
        assert float(a[1, 4999]) == float(b[1, 4999]) == pytest.approx(0.3, abs=1e-6)
        assert np.allclose(a.max(axis=1), [1.0, 0.6], atol=_DECAY_STEP * 1.01)
        assert np.allclose(b.max(axis=1), [1.0, 0.6], atol=_DECAY_STEP * 1.01)


# ----- UI ----------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("adsr")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_input_text",
             "add_input_float", "add_checkbox", "add_drag_int")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs.get("format"))
    return out


def test_every_param_gets_a_bounded_widget(monkeypatch):
    """No new params with vel, but the node must still sweep clean."""
    w = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("adsr").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])


# ----- example -----------------------------------------------------------------


def test_the_velocity_example_accents_each_note():
    """examples/adsr_velocity.json: a shift_random velocity loop into
    adsr.vel. Every note's envelope peak IS the bus value at its gate
    edge, and the loop has a real dynamic spread."""
    from pysynthrack.io_patch import load_patch

    path = Path(__file__).resolve().parent.parent / "examples" / "adsr_velocity.json"
    patch = load_patch(path)
    env = next(m for m in patch if m.TYPE == "adsr")
    seq = next(m for m in patch if m.TYPE == "sequencer")
    off = next(m for m in patch if m.TYPE == "cv_offset")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    envs, vels, gates = [], [], []
    orig = b._render_adsr

    def spy(module, frames, buffers, p):
        vels.append(np.asarray(buffers[(off.id, "out")]).copy())
        gates.append(np.asarray(buffers[(seq.id, "gate")]).copy())
        r = orig(module, frames, buffers, p)
        envs.append(np.asarray(r).copy())
        return r

    b._render_adsr = spy
    peak = 0.0
    for _ in range(int(SR * 4 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.3 < peak < 0.8
    e, v = np.concatenate(envs), np.concatenate(vels)
    g = np.concatenate(gates) > 0.5
    edges = np.flatnonzero(g[1:] & ~g[:-1]) + 1
    assert len(edges) >= 12
    peaks = []
    for a, z in zip(edges[:-1], edges[1:]):
        peaks.append(float(e[a:z].max()))
        assert peaks[-1] == pytest.approx(float(v[a]), abs=1e-6)
    assert min(peaks[:8]) < 0.45 and max(peaks[:8]) > 0.75
