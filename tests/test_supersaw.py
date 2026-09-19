"""Supersaw — seven detuned PolyBLEP saws per voice.

Pins the contract: blend 0 is the center saw alone (detune-inert,
bit-exact across detune settings); the detune cluster around a high
harmonic widens monotonically with the knob; spread 0 makes the two
outs bit-identical while spread 1 decorrelates them; the seeded
per-(slot, saw) free phases are deterministic (fresh renders match)
and per-voice independent; RMS normalisation holds the level as blend
moves; block-size independence; the 16-voice worst case is measured.

``detune_cv`` (2026-09-19 love pass): unpatched / a cable of zeros /
depth 0 are all bit-exact with the pre-CV render; a constant CV at
depth 1 is bit-exact with the knob set to ``detune + mean cv``; the
cluster widens monotonically with the CV; the rails clamp (huge → detune
1, negative → detune 0, both finite); a CV sweep does not pump the RMS
beyond the knob's own wobble; per-voice from a ``(V, F)`` source with
single-row ≡ mono parity; a ``(V, F)`` source on a mono stack averages;
block-size independent at constant CV; the widget sweep; the riser
example widens the cluster from start to end.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.supersaw import (
    SUPERSAW_N,
    SUPERSAW_OFFSETS,
    SUPERSAW_PAN,
)

SR = 8000
F0 = 261.6256


def _driver(params=None, block=512, sr=SR, detune_cable=False):
    """A supersaw fed by a keyboard on ``freq_cv`` and -- only when
    ``detune_cable`` -- an LFO on ``detune_cv``. Buffers are handed to
    the renderer directly, so ``step(F)`` with no ``detune_cv`` on a
    cabled driver is a cable carrying nothing (unpatched by the
    renderer's ``_input_buffer`` rule); the no-cable driver is the
    control for the "unpatched == today" pins."""
    patch = Patch()
    ss = patch.add_module("supersaw", params=params or {})
    kb = patch.add_module("cv_keyboard")
    patch.connect(kb.id, "pitch_cv", ss.id, "freq_cv")
    lfo = None
    if detune_cable:
        lfo = patch.add_module("lfo")
        patch.connect(lfo.id, "cv", ss.id, "detune_cv")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(frames, freq_cv=None, amp_cv=None, detune_cv=None):
        bufs = {}
        if freq_cv is not None:
            bufs[(kb.id, "pitch_cv")] = np.asarray(freq_cv, dtype=np.float32)
        if detune_cv is not None:
            assert lfo is not None, "build the driver with detune_cable=True"
            bufs[(lfo.id, "cv")] = np.asarray(detune_cv, dtype=np.float32)
        return b._render_supersaw(patch.get(ss.id), frames, bufs, patch)

    step.ss = ss
    step.backend = b
    step.patch = patch
    return step


def _spec(x):
    x = np.asarray(x, dtype=np.float64)
    return np.abs(np.fft.rfft(x * np.hanning(len(x))))


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["supersaw"]
    assert cls is get_module_type("supersaw")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == ["freq_cv", "detune_cv", "amp_cv"]
    assert [p.name for p in m.output_ports] == ["out_l", "out_r"]
    assert {p.name: p.signal_kind for p in m.input_ports}["detune_cv"] == "cv"
    assert m.params["detune"] == 0.35
    assert m.params["detune_cv_depth"] == 1.0
    assert m.params["blend"] == 0.75
    assert m.params["spread"] == 0.5
    assert len(SUPERSAW_OFFSETS) == SUPERSAW_N == len(SUPERSAW_PAN)
    assert SUPERSAW_OFFSETS[3] == 0.0  # the center saw


def test_pre_detune_cv_patch_loads_with_default_depth():
    """A patch saved before detune_cv existed has no depth key; it must
    load and get the default (and, unpatched, render as it did)."""
    d = Patch()
    d.add_module("supersaw", params={"detune": 0.6})
    raw = d.to_dict()
    for m in raw["modules"]:
        m["params"].pop("detune_cv_depth", None)
    restored = Patch.from_dict(raw)
    ss = next(m for m in restored if m.TYPE == "supersaw")
    assert ss.params["detune_cv_depth"] == 1.0
    assert ss.params["detune"] == 0.6


def test_serialization_round_trip():
    cls = all_module_types()["supersaw"]
    m = cls(3, params={"detune": 0.9, "spread": 0.0})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the stack -------------------------------------------------------------


def test_blend_zero_is_the_center_saw_alone():
    """detune moves only the sides — at blend 0 it must change nothing."""
    F = 4096
    a = _driver({"blend": 0.0, "detune": 0.1, "spread": 0.0})(F)["out_l"]
    b = _driver({"blend": 0.0, "detune": 0.9, "spread": 0.0})(F)["out_l"]
    assert np.array_equal(a, b)


def test_detune_cluster_widens_monotonically():
    """The supersaw signature: each harmonic becomes a CLUSTER whose
    skirt grows with detune (measured around the 8th harmonic)."""
    F = 16384

    def skirt(detune):
        out = _driver({"detune": detune, "blend": 1.0, "spread": 0.0})(F)[
            "out_l"
        ]
        spec = _spec(out)
        freqs = np.fft.rfftfreq(F, 1.0 / SR)
        fc = F0 * 8
        m = (np.abs(freqs - fc) > 12) & (np.abs(freqs - fc) < 60)
        return float(spec[m].sum())

    s0, s3, s6 = skirt(0.0), skirt(0.3), skirt(0.6)
    assert s3 > 20 * s0
    assert s6 > 1.5 * s3


def test_spread_zero_outs_bit_identical():
    F = 4096
    r = _driver({"spread": 0.0})(F)
    assert np.array_equal(r["out_l"], r["out_r"])


def test_spread_one_decorrelates():
    F = 16384
    r = _driver({"spread": 1.0, "detune": 0.7, "blend": 1.0})(F)
    l = r["out_l"].astype(np.float64)
    rr = r["out_r"].astype(np.float64)
    corr = np.corrcoef(l, rr)[0, 1]
    assert corr < 0.9  # genuinely different channels
    assert not np.array_equal(r["out_l"], r["out_r"])


def test_rms_stable_across_blend():
    """The RMS normalisation: blend moves timbre, not level."""
    F = 16384

    def rms(blend):
        out = _driver(
            {"blend": blend, "detune": 0.5, "spread": 0.0}
        )(F)["out_l"].astype(np.float64)
        return float(np.sqrt(np.mean(out**2)))

    r0, r5, r1 = rms(0.0), rms(0.5), rms(1.0)
    assert abs(r5 - r0) / r0 < 0.35
    assert abs(r1 - r0) / r0 < 0.35


def test_seeded_phases_deterministic():
    F = 4096
    a = _driver({})(F)
    b = _driver({})(F)
    assert np.array_equal(a["out_l"], b["out_l"])
    assert np.array_equal(a["out_r"], b["out_r"])


# ----- voices ----------------------------------------------------------------


def test_single_voice_row_equals_mono_shape_contract():
    F = 2048
    cv = np.full(F, 0.25, dtype=np.float32)
    mono = _driver({})(F, freq_cv=cv)
    voiced = _driver({})(F, freq_cv=cv[None, :])
    assert mono["out_l"].shape == (F,)
    assert voiced["out_l"].shape == (1, F)
    assert np.array_equal(mono["out_l"], voiced["out_l"][0])


def test_per_voice_rows_independent_and_distinct():
    F = 4096
    cv = np.zeros((2, F), dtype=np.float32)
    cv[1] = 0.5
    r = _driver({})(F, freq_cv=cv)
    assert r["out_l"].shape == (2, F)
    # Row 0 matches a solo render at the same pitch (same slot seeds).
    solo = _driver({})(F, freq_cv=cv[0])
    assert np.array_equal(r["out_l"][0], solo["out_l"])
    # Rows differ (different pitch AND different slot phase seeds).
    assert not np.array_equal(r["out_l"][0], r["out_l"][1])


def test_amp_cv_scales():
    F = 2048
    patch = Patch()
    ss = patch.add_module("supersaw", params={"spread": 0.0})
    lfo = patch.add_module("lfo")
    patch.connect(lfo.id, "cv", ss.id, "amp_cv")
    b = NumpyBackend(sample_rate=SR, block_size=512)
    b.compile(patch)
    half = np.full(F, 0.5, dtype=np.float32)
    r = b._render_supersaw(
        patch.get(ss.id), F, {(lfo.id, "cv"): half}, patch
    )
    full = _driver({"spread": 0.0})(F)
    assert np.allclose(
        r["out_l"].astype(np.float64),
        0.5 * full["out_l"].astype(np.float64),
        atol=1e-6,
    )


# ----- block-size independence ----------------------------------------------


def test_block_size_independent_constant_freq():
    F = 4096

    def chunked(block):
        step = _driver({}, block=block)
        out = np.empty(F, dtype=np.float32)
        for s in range(0, F, block):
            out[s : s + block] = step(block)["out_l"]
        return out

    assert np.array_equal(chunked(64), chunked(1024))


def test_block_size_independent_under_cv():
    F = 4096
    cv = (0.1 * np.sin(2 * np.pi * 2 * np.arange(F) / SR)).astype(np.float32)

    def chunked(block):
        step = _driver({}, block=block)
        out = np.empty(F, dtype=np.float32)
        for s in range(0, F, block):
            out[s : s + block] = step(block, freq_cv=cv[s : s + block])[
                "out_l"
            ]
        return out

    a, b = chunked(64), chunked(1024)
    assert np.abs(a.astype(np.float64) - b.astype(np.float64)).max() < 1e-5


# ----- detune_cv -------------------------------------------------------------


def _skirt(out, F, sr=SR, fc=F0 * 8):
    """Energy in the 12..60 Hz skirt around the 8th harmonic -- the
    detune cluster's width (the existing knob test's observable)."""
    spec = _spec(out)
    freqs = np.fft.rfftfreq(F, 1.0 / sr)
    m = (np.abs(freqs - fc) > 12) & (np.abs(freqs - fc) < 60)
    return float(spec[m].sum())


def test_detune_cv_unpatched_zero_cable_and_depth_zero_are_bit_exact():
    """The feature ships OFF: no cable, a cable of zeros and a live cable
    at depth 0 all render the pre-CV stack bit for bit."""
    F = 4096
    ref = _driver({"spread": 0.3})(F)
    zeros = _driver({"spread": 0.3}, detune_cable=True)(
        F, detune_cv=np.zeros(F, dtype=np.float32)
    )
    d0 = _driver({"spread": 0.3, "detune_cv_depth": 0.0}, detune_cable=True)(
        F, detune_cv=np.full(F, 0.7, dtype=np.float32)
    )
    for r in (zeros, d0):
        assert np.array_equal(ref["out_l"], r["out_l"])
        assert np.array_equal(ref["out_r"], r["out_r"])


def test_detune_cv_at_depth_one_equals_knob_plus_block_mean():
    """cv +0.3 at depth 1 == the knob at detune + mean cv. Bit-exact
    against the float64 block mean (the mean is a scalar that enters
    the very same expression the knob does); allclose against the
    literal 0.65, because float32 0.3 sits 1.2e-8 above 0.3."""
    F = 4096
    cv = np.full(F, 0.3, dtype=np.float32)
    live = _driver({"detune": 0.35, "spread": 0.4}, detune_cable=True)(
        F, detune_cv=cv
    )
    mean = float(np.mean(cv.astype(np.float64)))
    knob_exact = _driver({"detune": 0.35 + 1.0 * mean, "spread": 0.4})(F)
    assert np.array_equal(live["out_l"], knob_exact["out_l"])
    assert np.array_equal(live["out_r"], knob_exact["out_r"])
    knob_literal = _driver({"detune": 0.65, "spread": 0.4})(F)
    assert np.allclose(
        live["out_l"].astype(np.float64),
        knob_literal["out_l"].astype(np.float64),
        atol=1e-5,
    )


def test_detune_cv_depth_scales_the_offset():
    """depth 2 at cv 0.15 lands where depth 1 at cv 0.3 does (doubling a
    float is exact, so this is bit-exact too)."""
    F = 4096
    a = _driver({"detune": 0.2, "detune_cv_depth": 2.0}, detune_cable=True)(
        F, detune_cv=np.full(F, 0.15, dtype=np.float32)
    )
    b = _driver({"detune": 0.2, "detune_cv_depth": 1.0}, detune_cable=True)(
        F, detune_cv=np.full(F, 0.3, dtype=np.float32)
    )
    assert np.array_equal(a["out_l"], b["out_l"])


def test_detune_cv_cluster_widens_monotonically():
    """The riser, measured: the knob at 0, the CV alone opens the cluster
    around the 8th harmonic (same observable and thresholds as the knob
    test)."""
    F = 16384

    def skirt(cv):
        out = _driver(
            {"detune": 0.0, "blend": 1.0, "spread": 0.0}, detune_cable=True
        )(F, detune_cv=np.full(F, cv, dtype=np.float32))["out_l"]
        return _skirt(out, F)

    s0, s3, s6 = skirt(0.0), skirt(0.3), skirt(0.6)
    assert s3 > 20 * s0
    assert s6 > 1.5 * s3


def test_detune_cv_clamps_at_the_rails():
    """A huge CV saturates at detune 1, a negative one at 0 -- bit-exact
    with the knob at the rail, and finite."""
    F = 4096
    base = {"detune": 0.2, "blend": 1.0, "spread": 0.0}
    hi = _driver(base, detune_cable=True)(
        F, detune_cv=np.full(F, 1e6, dtype=np.float32)
    )["out_l"]
    lo = _driver(base, detune_cable=True)(
        F, detune_cv=np.full(F, -50.0, dtype=np.float32)
    )["out_l"]
    one = _driver({**base, "detune": 1.0})(F)["out_l"]
    zero = _driver({**base, "detune": 0.0})(F)["out_l"]
    assert np.all(np.isfinite(hi)) and np.all(np.isfinite(lo))
    assert np.array_equal(hi, one)
    assert np.array_equal(lo, zero)


def test_detune_cv_sweep_does_not_pump():
    """The amp normalisation never depended on the detune, so a CV sweep
    holds the level to the knob's own wobble (measured 0.18 max/min
    over 0.1..1.0; the blend test's 0.35 tolerance)."""
    F = 16384
    rms = []
    for cv in np.linspace(0.0, 0.9, 10):
        out = _driver(
            {"detune": 0.1, "blend": 0.75, "spread": 0.0}, detune_cable=True
        )(F, detune_cv=np.full(F, float(cv), dtype=np.float32))["out_l"]
        out = out.astype(np.float64)
        rms.append(float(np.sqrt(np.mean(out**2))))
    assert (max(rms) - min(rms)) / min(rms) < 0.35


def test_detune_cv_per_voice_from_a_voice_source():
    """A (V, F) detune_cv on a voice-aware stack is a detune per voice:
    row 0 (cv 0) is the plain stack, row 1 (cv 0.5) is what a SHARED
    0.5 gives that row, and the two rows are not each other's."""
    F = 4096
    fcv = np.zeros((2, F), dtype=np.float32)
    dcv = np.zeros((2, F), dtype=np.float32)
    dcv[1] = 0.5
    p = {"detune": 0.1, "spread": 0.0}
    per_voice = _driver(p, detune_cable=True)(F, freq_cv=fcv, detune_cv=dcv)
    plain = _driver(p)(F, freq_cv=fcv)
    shared = _driver(p, detune_cable=True)(
        F, freq_cv=fcv, detune_cv=np.full(F, 0.5, dtype=np.float32)
    )
    assert per_voice["out_l"].shape == (2, F)
    assert np.array_equal(per_voice["out_l"][0], plain["out_l"][0])
    assert np.array_equal(per_voice["out_l"][1], shared["out_l"][1])
    assert not np.array_equal(per_voice["out_l"][0], shared["out_l"][0])


def test_detune_cv_single_voice_row_equals_mono():
    """Voice parity: a (1, F) detune_cv on a (1, F) stack is bit-identical
    to the mono render with the same cable (the vector path is the
    scalar expression one axis wider, same operation order)."""
    F = 2048
    fcv = np.full(F, 0.25, dtype=np.float32)
    dcv = np.full(F, 0.4, dtype=np.float32)
    mono = _driver({"detune": 0.2}, detune_cable=True)(
        F, freq_cv=fcv, detune_cv=dcv
    )
    voiced = _driver({"detune": 0.2}, detune_cable=True)(
        F, freq_cv=fcv[None, :], detune_cv=dcv[None, :]
    )
    assert voiced["out_l"].shape == (1, F)
    assert np.array_equal(mono["out_l"], voiced["out_l"][0])
    assert np.array_equal(mono["out_r"], voiced["out_r"][0])


def test_detune_cv_voice_source_on_mono_stack_is_averaged():
    """A (V, F) detune_cv arriving at a MONO stack (no voice-aware pitch)
    is the mean over both axes -- one shared detune, like the filter."""
    F = 2048
    dcv = np.zeros((2, F), dtype=np.float32)
    dcv[1] = 0.5
    two_d = _driver({"detune": 0.1}, detune_cable=True)(F, detune_cv=dcv)
    avg = _driver({"detune": 0.1}, detune_cable=True)(
        F, detune_cv=np.full(F, 0.25, dtype=np.float32)
    )
    assert two_d["out_l"].shape == (F,)
    assert np.array_equal(two_d["out_l"], avg["out_l"])


def test_detune_cv_block_size_independent_constant_cv():
    F = 4096

    def chunked(block):
        step = _driver({"detune": 0.2, "spread": 0.0}, block=block,
                       detune_cable=True)
        out = np.empty(F, dtype=np.float32)
        for s in range(0, F, block):
            out[s : s + block] = step(
                block, detune_cv=np.full(block, 0.4, dtype=np.float32)
            )["out_l"]
        return out

    assert np.array_equal(chunked(64), chunked(1024))


# ----- UI --------------------------------------------------------------------


def _widgets(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("supersaw")
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
    w = _widgets(monkeypatch)
    labels = list(w)
    for name in get_module_type("supersaw").DEFAULT_PARAMS:
        hits = [lb for lb in labels if lb == name or lb.startswith(name + " ")]
        assert hits, (name, labels)
        assert w[hits[0]][0] != "add_input_text", (name, w[hits[0]])
    assert w["freq"][1].endswith(" Hz")
    assert "det/unit" in w["detune_cv_depth"][1]


# ----- example ---------------------------------------------------------------


def test_the_riser_example_widens_the_cluster():
    """examples/supersaw_detune_rise.json: a gate-mode function generator
    sweeps detune_cv over a held sus2 chord. The A2 root voice's cluster
    around its 8th harmonic must be far wider in the last second of the
    rise than in the first, and the master bus must stay sane."""
    from pysynthrack.io_patch import load_patch

    path = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "supersaw_detune_rise.json"
    )
    patch = load_patch(path)
    ss = next(m for m in patch if m.TYPE == "supersaw")
    assert ss.params["detune_cv_depth"] > 0
    sr = 44100
    b = NumpyBackend(sample_rate=sr, block_size=512)
    b.compile(patch)
    cap = []
    orig = b._render_supersaw

    def spy(module, frames, buffers, p):
        r = orig(module, frames, buffers, p)
        cap.append(np.asarray(r["out_l"]).copy())
        return r

    b._render_supersaw = spy
    peak = 0.0
    for _ in range(int(sr * 7.6 / 512)):
        out, _devices = b.render_block_multi(512)
        assert out is not None and np.all(np.isfinite(out))
        peak = max(peak, float(np.abs(out).max()))
    assert 0.1 < peak < 1.0
    sig = np.concatenate(cap, axis=-1)
    assert sig.ndim == 2 and sig.shape[0] == 4  # the sus2 chord's voices
    root = sig[0]  # voice 0 carries the root, A2 on step 1
    a2 = F0 * 2 ** (-15 / 12)
    F = sr  # one-second windows, 1 Hz bins
    early = root[int(0.5 * sr):int(0.5 * sr) + F]
    late = root[int(6.4 * sr):int(6.4 * sr) + F]
    s_early = _skirt(early, F, sr=sr, fc=a2 * 8)
    s_late = _skirt(late, F, sr=sr, fc=a2 * 8)
    assert s_late > 20 * s_early, (s_early, s_late)
