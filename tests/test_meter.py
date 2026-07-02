"""Tests for the Meter module (audio level indicator, pass-through).

Coverage:
  - Model: registration, empty params, ports/signal kinds (audio in →
    audio out), JSON round-trip, type walls (audio→audio legal, cv→in
    illegal, out→cv illegal).
  - Pass-through: out is the input untouched (mono + voice), shape
    preserved; disconnected input → silence.
  - Envelope: peak read after a block (instant attack), release decay on
    silence (falls per the time-based ``release`` coefficient, stays > 0),
    attack jumps instantly to a louder block, silence from start → 0;
    compile pre-creates the snapshot key at 0.0.
  - Release: smaller ``release`` falls further per second; the fall is
    block-size independent (same wall-clock rate); ``release`` is clamped.
  - Integration: oscillator → meter → speaker renders audible audio
    (the meter is transparent) and the level snapshot reads nonzero.
  - Stereo: optional ``in_r`` → ``out_r`` pair (silence + a None right
    slot while unpatched; independent L/R indicators when patched).
  - RMS mode: sine reads amp/√2, square reads amp, sits below peak,
    decays after the signal stops, loudest voice wins on 2D input;
    unknown ``mode`` strings fall back to peak; the default ``peak``
    bar stays bit-identical to the pre-``mode`` Meter.
  - Peak-hold tick: sits exactly at the recent peak for ~1.5 s, then
    falls at the ``release`` rate; never below the peak bar; a louder
    peak resets it; still peak-driven in RMS mode.
  - Clip lamp: lights at |sample| ≥ 1.0 (0 dBFS), never below, stays
    ~2 s (block-size independent, sample-counted), re-clip restarts
    the window; detected on any voice of a 2D buffer.
  - LUFS-ish modes: 997 Hz full-scale anchors near the spec's -3.01;
    readings track level linearly; K-weighting discounts bass and
    boosts presence; short-term (3 s) reacts slower than momentary
    (400 ms); unknown modes still fall back to peak; loudest voice
    wins on voice-aware buffers.
  - Stereo link: linked flag + pair readout published (channel-energy
    sum in LUFS modes, louder channel in peak); hold/clip merge
    pair-wide; bars stay per-channel; link without in_r is a no-op.
  - Clip counter: events not samples (a flat-top counts once, a run
    spanning a block boundary counts once); per-channel tallies in the
    snapshot; reset via the API and on recompile.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401  (registers types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch
from pysynthrack.modules.meter import Meter, METER_MODES

SR, F = 44100, 512


def _backend():
    return NumpyBackend(sample_rate=SR, block_size=F)


def _meter_rig():
    """oscillator → meter, compiled. Returns (patch, src, meter, backend)."""
    patch = Patch()
    src = patch.add_module("oscillator")
    m = patch.add_module("meter")
    patch.connect(src.id, "out", m.id, "in")
    b = _backend()
    b.compile(patch)
    return patch, src, m, b


def _drive(b, patch, src, m, block):
    """Render the meter with ``block`` as its input buffer; return out."""
    return b._render_meter(m, block.shape[-1], {(src.id, "out"): block}, patch)["out"]


# ----- Model -----------------------------------------------------------------


class TestModel:
    def test_register_and_defaults(self):
        patch = Patch()
        m = patch.add_module("meter")
        assert isinstance(m, Meter)
        assert m.params == {"release": 0.4, "mode": "peak", "stereo_link": False}

    def test_ports_and_signal_kinds(self):
        m = Patch().add_module("meter")
        assert [(p.name, p.signal_kind) for p in m.input_ports] == [
            ("in", "audio"), ("in_r", "audio")
        ]
        assert [(p.name, p.signal_kind) for p in m.output_ports] == [
            ("out", "audio"), ("out_r", "audio")
        ]

    def test_json_round_trip(self):
        patch = Patch()
        patch.add_module("meter")
        restored = Patch.from_dict(patch.to_dict())
        assert any(mod.TYPE == "meter" for mod in restored)

    def test_unknown_param_rejected(self):
        with pytest.raises(KeyError):
            Patch().add_module("meter", params={"decay": 0.9})

    def test_audio_into_meter_accepted(self):
        patch = Patch()
        osc = patch.add_module("oscillator")
        m = patch.add_module("meter")
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", m.id, "in")   # audio → audio
        patch.connect(m.id, "out", spk.id, "in")   # audio → audio sink

    def test_cv_into_meter_rejected(self):
        patch = Patch()
        nz = patch.add_module("noise")
        m = patch.add_module("meter")
        with pytest.raises(ValueError):
            patch.connect(nz.id, "cv", m.id, "in")  # cv → audio

    def test_meter_out_into_cv_rejected(self):
        patch = Patch()
        m = patch.add_module("meter")
        sh = patch.add_module("sample_hold")
        with pytest.raises(ValueError):
            patch.connect(m.id, "out", sh.id, "in")  # audio → cv


# ----- Pass-through ----------------------------------------------------------


class TestPassThrough:
    def test_out_equals_in_mono(self):
        patch, src, m, b = _meter_rig()
        x = (np.random.RandomState(1).rand(F).astype(np.float32) * 2 - 1)
        out = _drive(b, patch, src, m, x)
        assert out.shape == (F,)
        assert np.array_equal(out, x)

    def test_out_equals_in_voice(self):
        patch, src, m, b = _meter_rig()
        x = (np.random.RandomState(2).rand(4, F).astype(np.float32) * 2 - 1)
        out = _drive(b, patch, src, m, x)
        assert out.shape == (4, F)
        assert np.array_equal(out, x)

    def test_disconnected_is_silence(self):
        patch = Patch()
        m = patch.add_module("meter")
        b = _backend()
        b.compile(patch)
        out = b._render_meter(m, 256, {}, patch)["out"]
        assert out.shape == (256,)
        assert not out.any()


# ----- Envelope --------------------------------------------------------------


class TestEnvelope:
    def test_key_precreated_at_zero(self):
        _, _, m, b = _meter_rig()
        assert b.snapshot_audio_levels().get(m.id) == 0.0

    def test_instant_attack_reads_peak(self):
        patch, src, m, b = _meter_rig()
        block = np.full(F, 0.8, dtype=np.float32)
        _drive(b, patch, src, m, block)
        assert b.snapshot_audio_levels()[m.id] == pytest.approx(0.8, abs=1e-6)

    def test_peak_uses_max_abs(self):
        patch, src, m, b = _meter_rig()
        block = np.zeros(F, dtype=np.float32)
        block[100] = -0.6  # a single negative spike sets the peak
        _drive(b, patch, src, m, block)
        assert b.snapshot_audio_levels()[m.id] == pytest.approx(0.6, abs=1e-6)

    def test_slow_decay_on_silence(self):
        patch, src, m, b = _meter_rig()
        _drive(b, patch, src, m, np.full(F, 0.8, dtype=np.float32))
        before = b.snapshot_audio_levels()[m.id]
        _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))
        after = b.snapshot_audio_levels()[m.id]
        # Falls, but not to zero — a gentle release.
        assert 0.0 < after < before
        coeff = 0.1 ** (F / SR / Meter.DEFAULT_PARAMS["release"])
        assert after == pytest.approx(before * coeff, rel=1e-6)

    def test_attack_overrides_decayed_level(self):
        patch, src, m, b = _meter_rig()
        _drive(b, patch, src, m, np.full(F, 0.3, dtype=np.float32))
        _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))  # decaying
        _drive(b, patch, src, m, np.full(F, 0.9, dtype=np.float32))  # loud
        assert b.snapshot_audio_levels()[m.id] == pytest.approx(0.9, abs=1e-6)

    def test_silence_from_start_is_zero(self):
        patch, src, m, b = _meter_rig()
        _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))
        assert b.snapshot_audio_levels()[m.id] == 0.0

    def test_voice_peak_is_loudest_voice(self):
        patch, src, m, b = _meter_rig()
        block = np.zeros((3, F), dtype=np.float32)
        block[1, :] = 0.5  # only the middle voice carries signal
        _drive(b, patch, src, m, block)
        assert b.snapshot_audio_levels()[m.id] == pytest.approx(0.5, abs=1e-6)

    def test_release_controls_decay(self):
        # Smaller release => faster fall => lower level after the same silence.
        def after_one_silent(release):
            patch = Patch()
            src = patch.add_module("oscillator")
            m = patch.add_module("meter", params={"release": release})
            patch.connect(src.id, "out", m.id, "in")
            b = _backend()
            b.compile(patch)
            _drive(b, patch, src, m, np.full(F, 0.8, dtype=np.float32))
            _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))
            return b.snapshot_audio_levels()[m.id]

        assert after_one_silent(0.1) < after_one_silent(1.5)

    def test_release_block_size_independent(self):
        # Same wall-clock fall rate at any block size: one 1024-sample silent
        # block == two 512-sample silent blocks (equal total silence time).
        def run(bf, n):
            patch = Patch()
            src = patch.add_module("oscillator")
            m = patch.add_module("meter", params={"release": 0.3})
            patch.connect(src.id, "out", m.id, "in")
            b = NumpyBackend(sample_rate=SR, block_size=bf)
            b.compile(patch)
            _drive(b, patch, src, m, np.full(bf, 0.8, dtype=np.float32))
            for _ in range(n):
                _drive(b, patch, src, m, np.zeros(bf, dtype=np.float32))
            return b.snapshot_audio_levels()[m.id]

        assert run(1024, 1) == pytest.approx(run(512, 2), rel=1e-6)

    def test_release_clamped(self):
        # release below the min clamps to a fast but finite, safe fall
        # (no divide-by-zero, no instant wipe to exactly the block peak).
        patch = Patch()
        src = patch.add_module("oscillator")
        m = patch.add_module("meter", params={"release": 0.0})
        patch.connect(src.id, "out", m.id, "in")
        b = _backend()
        b.compile(patch)
        _drive(b, patch, src, m, np.full(F, 0.8, dtype=np.float32))
        before = b.snapshot_audio_levels()[m.id]
        _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))
        after = b.snapshot_audio_levels()[m.id]
        assert np.isfinite(after) and 0.0 <= after < before


# ----- Integration -----------------------------------------------------------


class TestIntegration:
    def test_osc_meter_speaker_renders_and_meters(self):
        patch = Patch()
        osc = patch.add_module("oscillator", params={"waveform": "saw", "freq": 220.0, "amp": 0.7})
        m = patch.add_module("meter")
        spk = patch.add_module("speaker_output")
        patch.connect(osc.id, "out", m.id, "in")
        patch.connect(m.id, "out", spk.id, "in")
        b = _backend()
        b.compile(patch)
        block = None
        for _ in range(4):
            block = b.render_block(F)
        assert block is not None and np.all(np.isfinite(block))
        assert np.abs(block).max() > 0.0          # audio passed through the meter
        assert b.snapshot_audio_levels()[m.id] > 0.0  # and the level registered


def _stereo_rig(mode="peak", release=0.4):
    """osc → meter.in, noise → meter.in_r, compiled."""
    patch = Patch()
    src_l = patch.add_module("oscillator")
    src_r = patch.add_module("noise")
    m = patch.add_module("meter", params={"mode": mode, "release": release})
    patch.connect(src_l.id, "out", m.id, "in")
    patch.connect(src_r.id, "out", m.id, "in_r")
    b = _backend()
    b.compile(patch)
    return patch, src_l, src_r, m, b


def _drive2(b, patch, src_l, src_r, m, block_l, block_r):
    """Render one stereo meter block; returns the full output dict."""
    return b._render_meter(
        m,
        block_l.shape[-1],
        {(src_l.id, "out"): block_l, (src_r.id, "out"): block_r},
        patch,
    )


def _sine(amp, freq=440.0, frames=F, offset=0):
    t = (np.arange(frames) + offset) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _entry(b, m):
    """Full published tuple: (left, right, linked, mode, pair_level)."""
    return b.snapshot_audio_meters()[m.id]


def _channels(b, m):
    """(left, right) channel tuples -- each (level, hold, clip, clips)."""
    e = _entry(b, m)
    return e[0], e[1]


def _rms_rig(mode="rms"):
    patch = Patch()
    src = patch.add_module("oscillator")
    m = patch.add_module("meter", params={"mode": mode})
    patch.connect(src.id, "out", m.id, "in")
    b = _backend()
    b.compile(patch)
    return patch, src, m, b


def _settle(b, patch, src, m, amp, blocks=400, freq=440.0):
    for i in range(blocks):
        _drive(b, patch, src, m, _sine(amp, freq=freq, offset=i * F))
    return _channels(b, m)[0][0]


# ----- LUFS-ish modes ----------------------------------------------------------


def _db(x):
    return 20.0 * math.log10(max(float(x), 1e-12))


def _lufs_rig(mode="lufs_m", extra=None, stereo=False):
    patch = Patch()
    src = patch.add_module("oscillator")
    params = {"mode": mode}
    params.update(extra or {})
    m = patch.add_module("meter", params=params)
    patch.connect(src.id, "out", m.id, "in")
    src2 = None
    if stereo:
        src2 = patch.add_module("oscillator")
        patch.connect(src2.id, "out", m.id, "in_r")
    b = _backend()
    b.compile(patch)
    return patch, src, src2, m, b


def _drive_sig(b, patch, src, m, x, src2=None, x2=None):
    for k in range(x.shape[-1] // F):
        bufs = {(src.id, "out"): x[..., k * F:(k + 1) * F].astype(np.float32)}
        if src2 is not None:
            bufs[(src2.id, "out")] = x2[..., k * F:(k + 1) * F].astype(np.float32)
        b._render_meter(m, F, bufs, patch)
    return _entry(b, m)


def _sine_sig(freq, amp=1.0, secs=4.0):
    t = np.arange(int(secs * SR) // F * F)
    return (amp * np.sin(2 * np.pi * freq * t / SR)).astype(np.float32)


class TestLufsModes:
    def test_modes_registered(self):
        assert METER_MODES == ("peak", "rms", "lufs_m", "lufs_s")

    def test_anchor_997_fullscale(self):
        # BS.1770's anchor: a 997 Hz full-scale sine reads -3.01 LUFS.
        # Our RBJ-approximated K pair lands within a few tenths (-ish).
        patch, src, _, m, b = _lufs_rig()
        e = _drive_sig(b, patch, src, m, _sine_sig(997.0))
        assert -3.8 < _db(e[0][0]) < -2.6

    def test_tracks_level_linearly(self):
        patch, src, _, m, b = _lufs_rig()
        hi = _db(_drive_sig(b, patch, src, m, _sine_sig(997.0))[0][0])
        patch, src, _, m, b = _lufs_rig()
        lo = _db(_drive_sig(b, patch, src, m, _sine_sig(997.0, amp=10 ** (-18 / 20)))[0][0])
        assert hi - lo == pytest.approx(18.0, abs=0.3)

    def test_bass_discounted(self):
        patch, src, _, m, b = _lufs_rig()
        ref = _db(_drive_sig(b, patch, src, m, _sine_sig(997.0))[0][0])
        patch, src, _, m, b = _lufs_rig()
        bass = _db(_drive_sig(b, patch, src, m, _sine_sig(60.0))[0][0])
        assert bass < ref - 2.5

    def test_presence_boosted(self):
        patch, src, _, m, b = _lufs_rig()
        ref = _db(_drive_sig(b, patch, src, m, _sine_sig(997.0))[0][0])
        patch, src, _, m, b = _lufs_rig()
        hf = _db(_drive_sig(b, patch, src, m, _sine_sig(6000.0))[0][0])
        assert hf > ref + 2.0

    def test_short_term_slower_than_momentary(self):
        step = _sine_sig(997.0, secs=0.5)
        patch, src, _, m, b = _lufs_rig("lufs_m")
        fast = _db(_drive_sig(b, patch, src, m, step)[0][0])
        patch, src, _, m, b = _lufs_rig("lufs_s")
        slow = _db(_drive_sig(b, patch, src, m, step)[0][0])
        assert slow < fast - 3.0

    def test_unknown_mode_falls_back_to_peak(self):
        patch, src, _, m, b = _lufs_rig("lufs")   # not a real mode
        e = _drive_sig(b, patch, src, m, _sine_sig(440.0, amp=0.5, secs=1.0))
        assert e[3] == "peak"
        assert e[0][0] == pytest.approx(0.5, rel=0.01)

    def test_voice_aware_loudest_voice_wins(self):
        sig = _sine_sig(997.0, secs=2.0)
        patch, src, _, m, b = _lufs_rig()
        mono = _db(_drive_sig(b, patch, src, m, sig)[0][0])
        stacked = np.vstack([sig, np.zeros_like(sig)])
        patch, src, _, m, b = _lufs_rig()
        voiced = _db(_drive_sig(b, patch, src, m, stacked)[0][0])
        assert voiced == pytest.approx(mono, abs=0.2)


# ----- Stereo link ---------------------------------------------------------------


class TestStereoLink:
    def test_pair_is_energy_sum_in_lufs(self):
        sig = _sine_sig(997.0, secs=2.0)
        patch, src, src2, m, b = _lufs_rig(
            "lufs_m", extra={"stereo_link": True}, stereo=True
        )
        e = _drive_sig(b, patch, src, m, sig, src2=src2, x2=sig)
        assert e[2] is True
        assert _db(e[4]) - _db(e[0][0]) == pytest.approx(3.01, abs=0.1)

    def test_pair_is_louder_channel_in_peak(self):
        l = _sine_sig(440.0, amp=0.8, secs=1.0)
        r = _sine_sig(440.0, amp=0.3, secs=1.0)
        patch, src, src2, m, b = _lufs_rig(
            "peak", extra={"stereo_link": True}, stereo=True
        )
        e = _drive_sig(b, patch, src, m, l, src2=src2, x2=r)
        assert e[4] == pytest.approx(0.8, rel=0.01)
        # Bars stay per-channel: the balance picture survives the link.
        assert e[0][0] == pytest.approx(0.8, rel=0.01)
        assert e[1][0] == pytest.approx(0.3, rel=0.01)

    def test_hold_and_clip_merge_pair_wide(self):
        l = _sine_sig(440.0, amp=0.2, secs=0.5)
        r = l.copy()
        r[100:130] = 1.4   # clip + hold spike on R only
        patch, src, src2, m, b = _lufs_rig(
            "peak", extra={"stereo_link": True}, stereo=True
        )
        e = _drive_sig(b, patch, src, m, l, src2=src2, x2=r)
        left, right = e[0], e[1]
        assert left[2] is True and right[2] is True     # shared lamp
        assert left[1] == right[1]                       # shared hold tick
        assert left[1] == pytest.approx(1.4, rel=0.05)

    def test_unlinked_publishes_no_pair(self):
        patch, src, src2, m, b = _lufs_rig("peak", stereo=True)
        e = _drive_sig(b, patch, src, m, _sine_sig(440.0, secs=0.5),
                       src2=src2, x2=_sine_sig(440.0, secs=0.5))
        assert e[2] is False
        assert e[4] is None

    def test_link_without_right_is_noop(self):
        patch, src, _, m, b = _lufs_rig("peak", extra={"stereo_link": True})
        e = _drive_sig(b, patch, src, m, _sine_sig(440.0, secs=0.5))
        assert e[2] is False
        assert e[1] is None


# ----- Clip counter --------------------------------------------------------------


class TestClipCounter:
    def test_flat_top_counts_once(self):
        patch, src, _, m, b = _lufs_rig("peak")
        sig = np.zeros(2 * F, dtype=np.float32)
        sig[200:250] = 1.5
        e = _drive_sig(b, patch, src, m, sig)
        assert e[0][3] == 1

    def test_separate_bursts_and_boundary_span(self):
        patch, src, _, m, b = _lufs_rig("peak")
        sig = np.zeros(3 * F, dtype=np.float32)
        sig[100:150] = 1.5
        sig[700:702] = -1.2                # negative overs count too
        sig[F - 10:F + 20] = 1.1           # spans a block boundary: once
        e = _drive_sig(b, patch, src, m, sig)
        assert e[0][3] == 3

    def test_no_events_below_fullscale(self):
        patch, src, _, m, b = _lufs_rig("peak")
        e = _drive_sig(b, patch, src, m, _sine_sig(440.0, amp=0.95, secs=0.5))
        assert e[0][3] == 0

    def test_per_channel_tallies(self):
        l = np.zeros(2 * F, dtype=np.float32)
        r = np.zeros(2 * F, dtype=np.float32)
        l[100:110] = 1.2
        l[500:510] = 1.2
        r[300:310] = -1.2
        patch, src, src2, m, b = _lufs_rig("peak", stereo=True)
        e = _drive_sig(b, patch, src, m, l, src2=src2, x2=r)
        assert e[0][3] == 2
        assert e[1][3] == 1

    def test_reset_api_and_recompile(self):
        patch, src, _, m, b = _lufs_rig("peak")
        sig = np.zeros(F, dtype=np.float32)
        sig[10:20] = 1.5
        assert _drive_sig(b, patch, src, m, sig)[0][3] == 1
        b.reset_meter_clips(m.id)
        assert int(b._state[m.id]["clips_l"]) == 0
        assert _drive_sig(b, patch, src, m, sig)[0][3] == 1
        b.compile(patch)   # recompile = fresh tally
        assert int(b._state[m.id]["clips_l"]) == 0


# ----- Stereo (optional in_r) ------------------------------------------------


class TestStereo:
    def test_out_r_silence_when_unpatched(self):
        patch, src, m, b = _meter_rig()
        res = b._render_meter(m, F, {(src.id, "out"): _sine(0.5)}, patch)
        assert res["out_r"].shape == (F,)
        assert not res["out_r"].any()

    def test_right_slot_none_when_unpatched(self):
        patch, src, m, b = _meter_rig()
        _drive(b, patch, src, m, _sine(0.5))
        left, right = _channels(b, m)
        assert right is None
        assert left[0] == pytest.approx(0.5, abs=1e-3)

    def test_precreated_snapshot_matches_patching(self):
        # Unpatched in_r: compile pre-creates (zero, None) …
        patch, src, m, b = _meter_rig()
        assert _channels(b, m) == ((0.0, 0.0, False, 0), None)
        # … patched in_r: both slots exist before any render.
        patch2, sl, sr, m2, b2 = _stereo_rig()
        assert _channels(b2, m2) == ((0.0, 0.0, False, 0), (0.0, 0.0, False, 0))

    def test_both_outputs_pass_through_bit_exact(self):
        patch, sl, sr, m, b = _stereo_rig()
        L, R = _sine(0.8), _sine(0.3, freq=333.0)
        res = _drive2(b, patch, sl, sr, m, L, R)
        assert np.array_equal(res["out"], L)
        assert np.array_equal(res["out_r"], R)

    def test_channels_metered_independently(self):
        patch, sl, sr, m, b = _stereo_rig()
        _drive2(b, patch, sl, sr, m, _sine(0.8), _sine(0.1))
        left, right = _channels(b, m)
        assert left[0] == pytest.approx(0.8, abs=1e-3)
        assert right[0] == pytest.approx(0.1, abs=1e-3)

    def test_right_clip_does_not_light_left(self):
        patch, sl, sr, m, b = _stereo_rig()
        hot = np.zeros(F, dtype=np.float32)
        hot[3] = 1.0
        _drive2(b, patch, sl, sr, m, _sine(0.5), hot)
        left, right = _channels(b, m)
        assert right[2] is True
        assert left[2] is False


# ----- RMS mode --------------------------------------------------------------


class TestRMS:
    def test_sine_reads_amplitude_over_sqrt2(self):
        patch, src, m, b = _rms_rig()
        level = _settle(b, patch, src, m, 0.5)
        assert level == pytest.approx(0.5 / np.sqrt(2.0), rel=0.01)

    def test_square_reads_amplitude(self):
        patch, src, m, b = _rms_rig()
        square = (0.5 * np.sign(_sine(1.0, freq=200.0))).astype(np.float32)
        for _ in range(400):
            _drive(b, patch, src, m, square)
        assert _channels(b, m)[0][0] == pytest.approx(0.5, rel=0.01)

    def test_rms_sits_below_peak_on_sine(self):
        patch, src, m, b = _meter_rig()  # default peak
        patch2, src2, m2, b2 = _rms_rig()
        peak_level = _settle(b, patch, src, m, 0.5)
        rms_level = _settle(b2, patch2, src2, m2, 0.5)
        assert rms_level < peak_level

    def test_rms_decays_after_signal_stops(self):
        patch, src, m, b = _rms_rig()
        loud = _settle(b, patch, src, m, 0.5)
        silence = np.zeros(F, dtype=np.float32)
        # sqrt() halves the dB fall rate of the mean-square EMA, so give
        # it ~2.3 s (>> the ~0.3 s window) to fall well past -26 dB.
        for _ in range(200):
            _drive(b, patch, src, m, silence)
        quiet = _channels(b, m)[0][0]
        assert quiet < loud * 0.05

    def test_voice_rms_loudest_voice_wins(self):
        # One live voice among 15 zero-padded slots must read that
        # voice's RMS, not a 16x-diluted average.
        patch, src, m, b = _rms_rig()
        for i in range(400):
            block = np.zeros((16, F), dtype=np.float32)
            block[0] = _sine(0.5, offset=i * F)
            _drive(b, patch, src, m, block)
        assert _channels(b, m)[0][0] == pytest.approx(0.5 / np.sqrt(2.0), rel=0.01)

    def test_unknown_mode_falls_back_to_peak(self):
        patch, src, m, b = _rms_rig(mode="banana")
        patch2, src2, m2, b2 = _meter_rig()
        rng = np.random.default_rng(7)
        for _ in range(20):
            block = rng.uniform(-0.7, 0.7, F).astype(np.float32)
            _drive(b, patch, src, m, block)
            _drive(b2, patch2, src2, m2, block)
            assert _channels(b, m)[0][0] == _channels(b2, m2)[0][0]

    def test_default_peak_bar_bit_identical_to_old_formula(self):
        # The pre-``mode`` Meter's envelope, recomputed by hand: any
        # drift here is a regression against shipped behaviour.
        patch, src, m, b = _meter_rig()
        rng = np.random.default_rng(42)
        env = 0.0
        coeff = 0.1 ** (F / SR / 0.4)
        for i in range(50):
            block = (
                rng.uniform(-1.0, 1.0, F).astype(np.float32)
                * (0.6 if i % 7 else 0.05)
            )
            _drive(b, patch, src, m, block)
            peak = float(np.max(np.abs(block)))
            env = peak if peak >= env else peak + (env - peak) * coeff
            assert b.snapshot_audio_levels()[m.id] == env
            assert _channels(b, m)[0][0] == env


# ----- Peak-hold tick --------------------------------------------------------


class TestPeakHold:
    def _spike_then_silence(self, blocks, release=0.4):
        patch = Patch()
        src = patch.add_module("oscillator")
        m = patch.add_module("meter", params={"release": release})
        patch.connect(src.id, "out", m.id, "in")
        b = _backend()
        b.compile(patch)
        spike = np.zeros(F, dtype=np.float32)
        spike[0] = 0.9
        _drive(b, patch, src, m, spike)
        silence = np.zeros(F, dtype=np.float32)
        holds = []
        for _ in range(blocks):
            _drive(b, patch, src, m, silence)
            holds.append(_channels(b, m)[0][1])
        return holds, b, patch, src, m

    def test_hold_sits_exactly_at_peak_during_window(self):
        spike = float(np.float32(0.9))  # what the float32 block really holds
        holds, *_ = self._spike_then_silence(int(1.4 * SR / F))
        # Every reading inside the 1.5 s window is the spike, exactly.
        assert all(h == spike for h in holds)

    def test_hold_falls_after_window(self):
        spike = float(np.float32(0.9))
        holds, *_ = self._spike_then_silence(int(2.5 * SR / F))
        assert holds[int(1.2 * SR / F)] == spike
        assert holds[-1] < spike

    def test_hold_never_below_bar(self):
        patch, src, m, b = _meter_rig()
        rng = np.random.default_rng(3)
        for i in range(400):
            amp = 0.8 if i % 50 == 0 else 0.05
            _drive(b, patch, src, m, rng.uniform(-amp, amp, F).astype(np.float32))
            level, hold = _channels(b, m)[0][:2]
            assert hold >= level

    def test_new_peak_resets_hold(self):
        holds, b, patch, src, m = self._spike_then_silence(int(2.5 * SR / F))
        assert holds[-1] < 0.9
        spike = np.zeros(F, dtype=np.float32)
        spike[0] = 0.95
        _drive(b, patch, src, m, spike)
        assert _channels(b, m)[0][1] == pytest.approx(0.95, abs=1e-6)
        # And the window restarts: still held a second later.
        silence = np.zeros(F, dtype=np.float32)
        for _ in range(int(1.0 * SR / F)):
            _drive(b, patch, src, m, silence)
        assert _channels(b, m)[0][1] == pytest.approx(0.95, abs=1e-6)

    def test_hold_is_peak_driven_in_rms_mode(self):
        patch, src, m, b = _rms_rig()
        spike = np.zeros(F, dtype=np.float32)
        spike[0] = 0.9
        _drive(b, patch, src, m, spike)
        for i in range(10):
            _drive(b, patch, src, m, _sine(0.1, offset=i * F))
        level, hold, _ = _channels(b, m)[0][:3]
        assert hold == pytest.approx(0.9, abs=1e-6)  # the transient's true level
        assert level < 0.2  # while the bar reads the quiet RMS

    def test_hold_fall_rate_follows_release(self):
        blocks = int(3.0 * SR / F)
        slow, *_ = self._spike_then_silence(blocks, release=2.0)
        fast, *_ = self._spike_then_silence(blocks, release=0.05)
        assert fast[-1] < slow[-1]


# ----- Clip lamp -------------------------------------------------------------


class TestClip:
    def _clip_rig(self):
        patch, src, m, b = _meter_rig()
        hot = np.zeros(F, dtype=np.float32)
        hot[10] = 1.0
        return patch, src, m, b, hot, np.zeros(F, dtype=np.float32)

    def test_sample_at_unity_lights_lamp(self):
        patch, src, m, b, hot, _ = self._clip_rig()
        _drive(b, patch, src, m, hot)
        assert _channels(b, m)[0][2] is True

    def test_below_unity_never_lights(self):
        patch, src, m, b = _meter_rig()
        loud = np.full(F, 0.99, dtype=np.float32)
        for _ in range(50):
            _drive(b, patch, src, m, loud)
        assert _channels(b, m)[0][2] is False

    def test_fresh_meter_is_unlit(self):
        patch, src, m, b = _meter_rig()
        _drive(b, patch, src, m, np.zeros(F, dtype=np.float32))
        assert _channels(b, m)[0][2] is False

    def test_lamp_stays_lit_under_two_seconds(self):
        patch, src, m, b, hot, silence = self._clip_rig()
        _drive(b, patch, src, m, hot)
        for _ in range(int(1.9 * SR / F)):
            _drive(b, patch, src, m, silence)
        assert _channels(b, m)[0][2] is True

    def test_lamp_clears_after_two_seconds(self):
        patch, src, m, b, hot, silence = self._clip_rig()
        _drive(b, patch, src, m, hot)
        for _ in range(int(2.2 * SR / F)):
            _drive(b, patch, src, m, silence)
        assert _channels(b, m)[0][2] is False

    def test_reclip_restarts_window(self):
        patch, src, m, b, hot, silence = self._clip_rig()
        _drive(b, patch, src, m, hot)
        for _ in range(int(1.5 * SR / F)):
            _drive(b, patch, src, m, silence)
        _drive(b, patch, src, m, hot)  # 1.5 s in: clip again
        for _ in range(int(1.5 * SR / F)):
            _drive(b, patch, src, m, silence)
        # 3 s after the FIRST clip but only 1.5 s after the second.
        assert _channels(b, m)[0][2] is True

    def test_clip_window_block_size_independent(self):
        def samples_until_clear(block):
            patch, src, m, b = _meter_rig()
            hot = np.zeros(block, dtype=np.float32)
            hot[0] = 1.0
            b._render_meter(m, block, {(src.id, "out"): hot}, patch)
            silence = np.zeros(block, dtype=np.float32)
            total = 0
            while _channels(b, m)[0][2]:
                b._render_meter(m, block, {(src.id, "out"): silence}, patch)
                total += block
                assert total < 3 * SR  # safety
            return total

        small = samples_until_clear(512)
        large = samples_until_clear(4096)
        assert abs(small - 2.0 * SR) <= 512
        assert abs(large - 2.0 * SR) <= 4096

    def test_voice_clip_detected(self):
        patch, src, m, b = _meter_rig()
        block = np.zeros((16, F), dtype=np.float32)
        block[5, 100] = -1.0  # negative full-scale on one voice
        _drive(b, patch, src, m, block)
        assert _channels(b, m)[0][2] is True
