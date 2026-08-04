"""WavetableMorph — scanning wavetable oscillator.

Pins the contract: the analog stack's endpoints land the pure shapes
(position 0 = a sine to spectral purity; 1 = a square, odd harmonics
in the 1/k law with evens suppressed; the interior thirds land
triangle and saw); the crossfade is linear at a probe harmonic; a
position sweep is click-free; high notes stay alias-free (the mip
band machinery); a single-cycle WAV round-trips through the file
loader; a bad path falls back to the built-in stack; position_cv adds
with its depth; voices are independent; block splits don't change the
render.
"""
from __future__ import annotations

import numpy as np
from scipy.io import wavfile

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types, get_module_type
from pysynthrack.modules.wavetable_morph import WT_STACKS

SR = 8000
F0 = 261.6256


def _driver(params=None, block=512, sr=SR):
    patch = Patch()
    wm = patch.add_module("wavetable_morph", params=params or {})
    kb = patch.add_module("cv_keyboard")
    lfo = patch.add_module("lfo")
    patch.connect(kb.id, "pitch_cv", wm.id, "freq_cv")
    patch.connect(lfo.id, "cv", wm.id, "position_cv")
    b = NumpyBackend(sample_rate=sr, block_size=block)
    b.compile(patch)

    def step(frames, freq_cv=None, pos_cv=None):
        bufs = {}
        if freq_cv is not None:
            bufs[(kb.id, "pitch_cv")] = np.asarray(freq_cv, dtype=np.float32)
        if pos_cv is not None:
            bufs[(lfo.id, "cv")] = np.asarray(pos_cv, dtype=np.float32)
        return b._render_wavetable_morph(patch.get(wm.id), frames, bufs, patch)

    step.wm = wm
    step.backend = b
    step.patch = patch
    return step


def _harmonics(x, n=8, f0=F0):
    x = np.asarray(x, dtype=np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / SR)
    out = []
    for k in range(1, n + 1):
        m = (freqs > k * f0 - 15) & (freqs < k * f0 + 15)
        out.append(float(spec[m].max()) if m.any() else 0.0)
    return out


# ----- registration / model --------------------------------------------------


def test_registered_with_ports_and_params():
    cls = all_module_types()["wavetable_morph"]
    assert cls is get_module_type("wavetable_morph")
    assert cls.CATEGORY == "Sources"
    m = cls(1)
    assert [p.name for p in m.input_ports] == [
        "freq_cv",
        "position_cv",
        "amp_cv",
    ]
    assert [p.name for p in m.output_ports] == ["out"]
    assert m.params["table"] == "analog"
    assert m.params["position"] == 0.0
    assert m.params["position_cv_depth"] == 1.0
    assert m.params["file"] == ""
    assert set(WT_STACKS) == {"analog", "vowel", "metallic"}


def test_serialization_round_trip():
    cls = all_module_types()["wavetable_morph"]
    m = cls(3, params={"table": "vowel", "position": 0.6})
    m2 = cls.from_dict(m.to_dict())
    assert m2.params == m.params


# ----- the analog stack endpoints -------------------------------------------


def test_position_zero_is_a_pure_sine():
    h = _harmonics(_driver({"position": 0.0, "amp": 1.0})(8192))
    assert h[0] > 100 * max(h[1], h[2], h[3])


def test_position_one_is_a_square():
    h = _harmonics(_driver({"position": 1.0, "amp": 1.0})(8192))
    assert h[1] < 0.02 * h[0]  # evens suppressed
    assert h[3] < 0.02 * h[0]
    assert 0.2 < h[2] / h[0] < 0.45  # 1/3 law, loose
    assert 0.1 < h[4] / h[0] < 0.3  # 1/5 law, loose


def test_interior_thirds_land_triangle_and_saw():
    tri = _harmonics(_driver({"position": 1.0 / 3.0, "amp": 1.0})(8192))
    saw = _harmonics(_driver({"position": 2.0 / 3.0, "amp": 1.0})(8192))
    # Triangle: odd-only, steep 1/k² rolloff.
    assert tri[1] < 0.05 * tri[0]
    assert tri[2] / tri[0] < 0.2
    # Saw: both parities, gentle 1/k.
    assert 0.3 < saw[1] / saw[0] < 0.7
    assert 0.15 < saw[2] / saw[0] < 0.5


def test_crossfade_is_linear_at_a_probe_harmonic():
    """Between saw (2/3) and square (1): the 2nd harmonic fades
    linearly to zero — sample the midpoint."""

    def h2(pos):
        return _harmonics(_driver({"position": pos, "amp": 1.0})(8192))[1]

    a, mid, b = h2(2.0 / 3.0), h2(5.0 / 6.0), h2(1.0)
    expected = 0.5 * (a + b)
    assert abs(mid - expected) / a < 0.15


def test_position_sweep_is_click_free():
    F = SR * 4
    sweep = np.linspace(0.0, 1.0, F).astype(np.float32)
    step = _driver({"position": 0.0, "amp": 1.0})
    out = np.empty(F, dtype=np.float32)
    for s in range(0, F, 512):
        seg = sweep[s : s + 512]
        out[s : s + len(seg)] = step(len(seg), pos_cv=seg)
    d = np.abs(np.diff(out.astype(np.float64)))
    # A naive table SWITCH would step the waveform mid-cycle; the
    # crossfade keeps every delta within the stack's own edgiest
    # waveform (the square end's band-limited edge is the natural
    # ceiling — measured, not guessed).
    square = _driver({"position": 1.0, "amp": 1.0})(8192).astype(np.float64)
    natural = np.abs(np.diff(square)).max()
    assert d.max() <= natural + 0.05


def test_high_notes_stay_alias_free():
    """+3 octaves on the square end: everything audible must sit on a
    harmonic — inter-harmonic bins are the alias floor."""
    F = 16384
    cv = np.full(F, 3.0, dtype=np.float32)
    out = _driver({"position": 1.0, "amp": 1.0})(F, freq_cv=cv).astype(
        np.float64
    )
    spec = np.abs(np.fft.rfft(out * np.hanning(F)))
    freqs = np.fft.rfftfreq(F, 1.0 / SR)
    f_base = F0 * 8
    harm_mask = np.zeros(len(freqs), dtype=bool)
    k = 1
    while k * f_base < SR / 2:
        harm_mask |= np.abs(freqs - k * f_base) < 25
        k += 1
    signal = spec[harm_mask].max()
    floor = spec[~harm_mask & (freqs > 100)].max()
    assert signal > 100 * floor  # > 40 dB alias suppression


# ----- the other stacks ------------------------------------------------------


def test_vowel_and_metallic_render_and_differ():
    F = 8192
    v = _driver({"table": "vowel", "position": 0.5, "amp": 1.0})(F)
    m = _driver({"table": "metallic", "position": 0.5, "amp": 1.0})(F)
    assert float(np.abs(v).max()) > 0.05
    assert float(np.abs(m).max()) > 0.05
    assert not np.array_equal(v, m)


# ----- file import -----------------------------------------------------------


def test_single_cycle_wav_round_trip(tmp_path):
    """Write a saw cycle, load it, and get a saw spectrum back."""
    L = 512
    cycle = (2.0 * np.arange(L) / L - 1.0).astype(np.float32)
    path = tmp_path / "saw_cycle.wav"
    wavfile.write(path, 44100, cycle)
    h = _harmonics(
        _driver({"file": str(path), "amp": 1.0, "position": 0.0})(8192)
    )
    assert 0.3 < h[1] / h[0] < 0.7  # 1/2
    assert 0.15 < h[2] / h[0] < 0.5  # 1/3


def test_bad_file_falls_back_to_the_table_stack():
    good = _driver({"table": "analog", "position": 0.0, "amp": 1.0})(4096)
    bad = _driver(
        {
            "table": "analog",
            "position": 0.0,
            "amp": 1.0,
            "file": "Z:/no/such/cycle.wav",
        }
    )(4096)
    assert np.array_equal(good, bad)


# ----- CV --------------------------------------------------------------------


def test_position_cv_adds_with_depth():
    F = 8192
    # position 0 + cv 0.5 · depth 2 = position 1 → the square end.
    via_cv = _driver(
        {"position": 0.0, "position_cv_depth": 2.0, "amp": 1.0}
    )(F, pos_cv=np.full(F, 0.5, dtype=np.float32))
    direct = _driver({"position": 1.0, "amp": 1.0})(F)
    assert np.array_equal(via_cv, direct)


# ----- voices / blocks -------------------------------------------------------


def test_per_voice_independence():
    F = 4096
    cv = np.zeros((2, F), dtype=np.float32)
    cv[1] = 1.0
    r = _driver({"position": 0.6, "amp": 1.0})(F, freq_cv=cv)
    assert r.shape == (2, F)
    solo0 = _driver({"position": 0.6, "amp": 1.0})(F, freq_cv=cv[0])
    solo1 = _driver({"position": 0.6, "amp": 1.0})(F, freq_cv=cv[1])
    assert np.array_equal(r[0], solo0)
    assert np.array_equal(r[1], solo1)


def test_block_size_independent_constant_freq():
    F = 4096

    def chunked(block):
        step = _driver({"position": 0.4, "amp": 1.0}, block=block)
        out = np.empty(F, dtype=np.float32)
        for s in range(0, F, block):
            out[s : s + block] = step(block)
        return out

    assert np.array_equal(chunked(64), chunked(1024))
