"""The float32-reduction sweep, part 2: voice collapses, freeze.pitch_cv,
and the CV meter's NaN.

Part 1 (``test_cv_scrub.py``) moved every block-mean CV read to float64
behind ``_finite_mean``. Three reductions were left:

* **The (V, F) -> (F,) voice collapses.** ``_input_buffer``'s house
  poly->mono rule, the speakers, the sidechain keys and the shared-CV
  means summed/averaged the voice axis IN float32. That is not a
  block-size question (a collapse is per sample) -- it is an ORDER
  question: float32 addition is not associative, so the mix depended on
  which slot the voice allocator handed each note. ``_voice_sum`` /
  ``_voice_mean`` accumulate in float64 (exact for <= 16 float32 values)
  and cast back to the buffer's dtype: one rounding, order-independent,
  and no dtype change downstream.
* **``freeze.pitch_cv``** read a float32 block mean -- the part-1 wart,
  left for the freeze's own pass. It goes through the door now.
* **The CV meter.** The backend's readout keeps a NaN (float64 mean, no
  scrub); the UI paints it as ``nan`` and keeps it out of the bar's
  auto-range window, which one NaN used to poison for good.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from unittest import mock

import numpy as np
import pytest

import pysynthrack.audio.numpy_backend as nb_mod
import pysynthrack.audio.renderers as renderers_pkg
import pysynthrack.modules  # noqa: F401  (registers every type)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch

SR = 44100
SMALL, LARGE = 64, 512
SRC = 99


class _Cable:
    def __init__(self, src_port, dst_port):
        self.src_module_id = SRC
        self.src_port = src_port
        self.dst_port = dst_port


class _FakePatch:
    def __init__(self, cables):
        self._cables = cables

    def cables_into(self, module_id):
        return self._cables


def _old_sum(buf):
    return buf.sum(axis=0)


def _old_mean(buf):
    return buf.mean(axis=0)


def _voices(v=8, frames=4096, seed=1):
    rng = np.random.default_rng(seed)
    return rng.uniform(-1.0, 1.0, (v, frames)).astype(np.float32)


# ----- the helpers ------------------------------------------------------------

def test_voice_sum_keeps_the_buffer_dtype():
    a = _voices()
    assert NumpyBackend._voice_sum(a).dtype == np.float32
    assert NumpyBackend._voice_mean(a).dtype == np.float32
    d = a.astype(np.float64)
    assert NumpyBackend._voice_sum(d).dtype == np.float64
    assert NumpyBackend._voice_mean(d).dtype == np.float64
    assert NumpyBackend._voice_sum(a).shape == (a.shape[1],)


def test_voice_sum_is_the_once_rounded_exact_sum():
    a = _voices()
    exact = a.astype(np.float64).sum(axis=0)          # exact: 8 float32s
    assert np.array_equal(NumpyBackend._voice_sum(a), exact.astype(np.float32))
    assert np.array_equal(NumpyBackend._voice_mean(a),
                          (exact / 8).astype(np.float32))


def test_two_voices_were_already_exact():
    """A float32 add of two float32s IS the rounded exact sum, so a
    two-voice patch cannot move -- stated so the finding is pinned."""
    a = _voices(v=2)
    assert np.array_equal(NumpyBackend._voice_sum(a), _old_sum(a))


@pytest.mark.parametrize("v", [3, 4, 8, 16])
def test_voice_sum_is_order_independent(v):
    a = _voices(v=v)
    perm = np.random.default_rng(v).permutation(v)
    assert np.array_equal(NumpyBackend._voice_sum(a),
                          NumpyBackend._voice_sum(a[perm]))
    assert np.array_equal(NumpyBackend._voice_mean(a),
                          NumpyBackend._voice_mean(a[perm]))


@pytest.mark.parametrize("v", [3, 4, 8, 16])
def test_the_float32_sum_it_replaces_is_not(v):
    """The self-test: on the same data the old reduction DOES depend on
    the voice order, so the pin above is a witness."""
    a = _voices(v=v)
    perm = np.random.default_rng(v).permutation(v)
    assert not np.array_equal(_old_sum(a), _old_sum(a[perm]))


def test_non_float_buffers_keep_the_old_reduction():
    g = np.array([[True, False], [True, True]])
    assert np.array_equal(NumpyBackend._voice_sum(g), g.sum(axis=0))


def test_a_nan_voice_poisons_the_collapse_honestly():
    """Not scrubbed: a collapse is a mix, and a NaN voice poisons it just
    as a NaN on a mono cable would. The consuming doors scrub."""
    a = _voices(v=4, frames=8)
    a[2, 3] = np.nan
    s = NumpyBackend._voice_sum(a)
    assert np.isnan(s[3]) and np.all(np.isfinite(np.delete(s, 3)))


# ----- _input_buffer, the house rule -------------------------------------------

def test_input_buffer_collapse_is_order_independent():
    a = _voices(v=8)
    fp = _FakePatch([_Cable("out", "in")])
    got = NumpyBackend._input_buffer(fp, {(SRC, "out"): a}, 1, "in")
    rev = NumpyBackend._input_buffer(fp, {(SRC, "out"): a[::-1].copy()}, 1, "in")
    assert got.dtype == np.float32
    assert np.array_equal(got, rev)
    raw = NumpyBackend._input_buffer(fp, {(SRC, "out"): a}, 1, "in", collapse=False)
    assert raw is a


def test_input_buffer_collapse_goes_through_the_door(monkeypatch):
    """Monkeypatching the helper back to float32 restores the old
    collapse -- the lever the example-render proof pulls."""
    a = _voices(v=8)
    fp = _FakePatch([_Cable("out", "in")])
    monkeypatch.setattr(NumpyBackend, "_voice_sum", staticmethod(_old_sum))
    got = NumpyBackend._input_buffer(fp, {(SRC, "out"): a}, 1, "in")
    assert np.array_equal(got, _old_sum(a))


# ----- counting the doors -------------------------------------------------------

# Raw axis-0 reductions that are NOT a voice collapse of a float32 buffer:
# (enclosing def, a substring of the line).
_NOT_A_VOICE_COLLAPSE = {
    ("_voice_sum", "buf.sum(axis=0"),     # the doors themselves
    ("_voice_mean", "buf.mean(axis=0"),
    ("_render_granular", "contrib"),      # sums GRAINS, in float64
    ("_render_vocoder", "car_bands"),     # sums BANDS
    ("_ring_match_voices", "c.sum(axis=0, keepdims=True)"),  # already float64
}


def test_every_voice_collapse_goes_through_a_door():
    """Count the doors: a new ``.sum(axis=0)`` / ``.mean(axis=0)`` in the
    backend is either a voice collapse (use ``_voice_sum`` /
    ``_voice_mean``) or belongs on the allow-list above with a reason.
    Scans the backend AND every renderer family split out of it."""
    mods = [nb_mod] + [
        importlib.import_module(f"{renderers_pkg.__name__}.{info.name}")
        for info in pkgutil.iter_modules(renderers_pkg.__path__)
    ]
    assert len(mods) > 1, "no renderer modules found: the scan would be partial"
    src = "\n".join(inspect.getsource(m) for m in mods).split("\n")
    owner = ""
    offenders = []
    for line in src:
        m = re.match(r"    def (\w+)", line)
        if m:
            owner = m.group(1)
        if re.search(r"\.(sum|mean)\(axis=0", line) and "``" not in line:
            if not any(owner == o and sub in line
                       for o, sub in _NOT_A_VOICE_COLLAPSE):
                offenders.append((owner, line.strip()))
    assert not offenders, offenders
    # the self-test: run on the pre-sweep source (9ec4462) this scan
    # flags 24 lines in 17 defs; it must at least see the idiom
    assert re.search(r"\.(sum|mean)\(axis=0", "x = key.sum(axis=0)")


# ----- freeze.pitch_cv ---------------------------------------------------------

FREEZE_TOTAL = 16384
FREEZE_AT = 4096        # the gate rises on a block boundary at 64 AND 512


def _render_freeze(block, pitch_cv, fp_ports=("in", "freeze", "pitch_cv")):
    p = Patch()
    m = p.add_module("freeze")
    m.params.update({"size": 1024, "freeze": False, "level": 0.7, "dry": 0.0})
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(p)
    t = np.arange(FREEZE_TOTAL) / SR
    feeds = {
        "in": 0.3 * np.sin(2 * np.pi * 220.0 * t) + 0.2 * np.sin(2 * np.pi * 331.0 * t),
        "pitch_cv": np.full(FREEZE_TOTAL, pitch_cv),
        "freeze": (np.arange(FREEZE_TOTAL) >= FREEZE_AT).astype(np.float64),
    }
    fp = _FakePatch([_Cable(port, port) for port in fp_ports])
    np.random.seed(0)
    acc = []
    for k in range(FREEZE_TOTAL // block):
        sl = slice(k * block, (k + 1) * block)
        bufs = {(SRC, port): np.ascontiguousarray(feeds[port][sl], dtype=np.float32)
                for port in fp_ports}
        acc.append(np.asarray(b._render_freeze(m, block, bufs, fp)["out"]).copy())
    return np.concatenate(acc)


def test_freeze_constant_pitch_cv_is_block_size_exact():
    small = _render_freeze(SMALL, 0.3)
    large = _render_freeze(LARGE, 0.3)
    assert np.max(np.abs(large)) > 0.01, "the freeze held nothing: vacuous pin"
    assert np.array_equal(small, large), (
        f"max|64-512| = {np.abs(small.astype(float) - large).max():.3e}")


def test_freeze_pitch_cv_actually_transposes():
    assert not np.array_equal(_render_freeze(LARGE, 0.3),
                              _render_freeze(LARGE, 0.0))


def test_freeze_old_float32_mean_fails_that_pin(monkeypatch):
    """The self-test: with the pre-fix float32 mean behind the door, the
    same constant CV renders differently at 64 than at 512."""
    def old(cv, default=0.0, axis=None):
        m = float(np.mean(np.asarray(cv)))
        return m if np.isfinite(m) else default
    monkeypatch.setattr(NumpyBackend, "_finite_mean", staticmethod(old))
    assert not np.array_equal(_render_freeze(SMALL, 0.3),
                              _render_freeze(LARGE, 0.3))


@pytest.mark.parametrize("bad", [float("nan"), 1e6, -1e6], ids=["nan", "1e6", "-1e6"])
def test_freeze_absurd_pitch_cv_renders_finite(bad):
    assert np.all(np.isfinite(_render_freeze(LARGE, bad)))


def test_freeze_reads_pitch_cv_through_the_door():
    src = inspect.getsource(NumpyBackend._render_freeze)
    line = [ln for ln in src.split("\n") if ln.strip().startswith("cv_mean = ") and "0.0" not in ln]
    assert line and all("_finite_mean(" in ln for ln in line), line


# ----- the CV meter readout -------------------------------------------------------

def _meter_backend_with_nan_lfo():
    b = NumpyBackend(sample_rate=SR, block_size=LARGE)
    p = Patch()
    lfo = p.add_module("lfo")
    b.compile(p)
    return b, lfo


def test_backend_meter_keeps_a_nan_honest(monkeypatch):
    """The readout is a diagnostic, not a clamp: a NaN cable publishes
    NaN (the UI decides how to paint it); a healthy one is the float64
    block mean."""
    b, lfo = _meter_backend_with_nan_lfo()
    real = b._render_lfo_mono if hasattr(b, "_render_lfo_mono") else None
    assert real is not None

    def poisoned(module, frames, *a, **k):
        out = real(module, frames, *a, **k)
        out = dict(out) if isinstance(out, dict) else {"cv": out}
        cv = np.array(out["cv"], dtype=np.float32, copy=True)
        cv[..., 5] = np.nan
        out["cv"] = cv
        return out

    b.render_block(LARGE)
    healthy = b.snapshot_meter_levels()[(lfo.id, "cv")]
    assert np.isfinite(healthy)
    monkeypatch.setattr(b, "_render_lfo_mono", poisoned)
    b.render_block(LARGE)
    assert np.isnan(b.snapshot_meter_levels()[(lfo.id, "cv")])


def _make_app(monkeypatch):
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.backend = mock.MagicMock()
    app.backend.is_running = False
    app.patch = Patch()
    return app, app_mod.dpg


def test_auto_range_window_survives_a_nan(monkeypatch):
    """One NaN used to make lo/hi NaN for good -- the bar then sat at 0
    (``max(0.0, nan)`` is 0.0) long after the cable healed."""
    app, _ = _make_app(monkeypatch)
    key = (1, "cv")
    for v in (0.0, 1.0, 0.5):
        app._auto_range_fill(key, v)
    before = list(app._meter_bounds[key])
    for bad in (float("nan"), float("inf"), -float("inf")):
        assert app._auto_range_fill(key, bad) == 0.5
    assert app._meter_bounds[key] == before
    assert 0.0 < app._auto_range_fill(key, 0.75) <= 1.0


def test_ui_meter_paints_nan_as_nan_and_holds_the_bar(monkeypatch):
    app, dpg = _make_app(monkeypatch)
    key = (7, "cv")
    app._cv_meter_bars = {key: 1234}
    app.backend.snapshot_meter_levels = lambda: {key: float("nan")}
    app._update_cv_meters()
    dpg.set_value.assert_not_called()                 # the bar holds
    dpg.configure_item.assert_called_once_with(1234, overlay="nan")
    assert key not in app._meter_bounds               # window untouched
    overlay = dpg.configure_item.call_args.kwargs["overlay"]
    assert overlay.isascii()
