"""The delay's clock sync + stereo echo love pass (spec: docs/MODULE_IDEAS.md,
"`delay`: clock sync + stereo echo").

Coverage (grows with the build, one commit per step):
  - Neutral: SHA pins captured from the pre-edit code (e61daa4) before any
    line of the delay changed -- the delay's own renderer driven block by
    block on synthesized rows, output AND the float64 line state it leaves,
    across the fast path, the old per-sample path (which the hops replace),
    a delay crossing the 4-sample hop floor, the freeze gate and tickbox,
    time_cv and three voices. Never guessed: each digest is what the old
    code computed. A silent clock, one edge and two edges match unpatched;
    an unpatched delay never calls the clock code (raise-if-called).
  - Hops: forced per-sample (the floor raised) equals hops in output, line
    and filter state; a hop one sample too long is caught (self-test).
  - Clock: T for each division and feel; bad params fall back; the real
    clock locks on the third edge and an impulse lands on T; ``time``
    inert and ``time_cv`` live while locked; a locked 4410 equals ``time``
    100 ms bit for bit; one event per 20 s on the spec's five clocks; the
    fold stays on the grid; ``in`` unpatched keeps measuring.
  - Breaks (through the real clock's ``reset`` / ``run``): off-grid,
    swung, repeated and on-grid resets, ``run`` low 1.5..24 ticks -- no
    event, bit for bit the undisturbed render; a tempo step is one event
    on tick 3 at the right span; a moving tempo stays followed; a doubled
    tick and a reset during the lock correct within four ticks. Self-test:
    the break test switched off, the off-grid reset moves the echo.
  - Fade: a tenth of today's knob jump or less on bass, sine and pad
    (self-test: R = 1 trips it); the loop peak never grows in a fade;
    retargets queue; the unpatch fades home and closes the door.
  - Freeze x clock: a locked hold is T with no slide and holds bit for
    bit; edges in a hold are measured, the read waits, the retarget lands
    on the sample the release runs out; a rise inside a fade holds the
    new read.
  - Stereo: ``out`` is the uncabled ``out`` at spread 0 / 0.4 / 1 on both
    paths (freeze, cv and a fade in the run), and ``out_l == out_r ==
    out`` at spread 0; an uncabled delay never runs the side line
    (raise-if-called); an impulse bounces L, R, L ... with the far side
    exactly 0; the textbook cross-coupled pair to float32 rounding up to
    feedback 0.98; the far-side law; ``mix`` 0 is dry on all three;
    ``(V, F)`` shapes, a single row equals mono, no cross-talk.
  - Re-cable: uncabling drops the side line, so no ghost (a kept line,
    planted, reads the old burst back over 100x the mid); old echoes play
    centred at half level with L + R = mid while a new note bounces; one
    jack left cabled keeps the line; a re-cable during a hold stays
    centred and exact; a voice-count change reallocates both lines and
    keeps the clock's span.
  - Stereo freeze: over 10 s of hold the mid repeats and the side negates
    bit for bit every lap, and L on one lap is R on the next.
  - Block size: five scenarios (tempo step + swing + feel + cv + freeze +
    stereo; whole-sample reads; fold; unpatch; three voices with the
    paths flipping, stereo) exact in output and state at 64/128/1000
    against 512, with a planted float-carried fade weight as the
    self-test; the per-sample loop and hops agree through a fade, side
    line included.
  - Panel: ``feel`` is a combo over ``DELAY_FEELS``; ``division`` is the
    siblings' 0.25..64 drag; ``spread`` a 0..1 slider labelled for its
    jacks.
  - Example: delay_pingpong_sync.json locks to 375 ms (16537 samples) on
    its 8th-note clock, and one note's first four repeats land L, R, L, R
    -- counted, the far side 30x down or more.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

import pysynthrack.modules  # noqa: F401
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core import Patch

SR = 44100
F = 512
LR = ("out_l", "out_r")                 # both stereo jacks, for Rig(stereo=)


# ----- harness ----------------------------------------------------------------


def _noise(seed, n, amp=0.3):
    return (np.random.default_rng(seed).standard_normal(n) * amp).astype(np.float32)


def _sine(hz, amp, n):
    return (amp * np.sin(2 * np.pi * hz * np.arange(n) / SR)).astype(np.float32)


def _gate(spans, n):
    g = np.zeros(n, dtype=np.float32)
    for a, b in spans:
        g[a:b] = 1.0
    return g


class Rig:
    """One delay, a source module per patched jack, driven block by block
    with raw rows (the ``test_modfx_block_exact`` idiom). ``stereo`` cables
    ``out_l`` / ``out_r`` (or just the named one) into a stereo sink."""

    def __init__(self, params, jacks, block=F, stereo=(), spy=False):
        self.patch = Patch()
        self.m = self.patch.add_module("delay", params=dict(params))
        self.srcs = {}
        self.jacks = {}
        for port, row in jacks.items():
            self.plug(port, row)
        self.sink = None
        for port in stereo:
            self.cable_out(port)
        self.block = block
        self.b = NumpyBackend(sample_rate=SR, block_size=block)
        self.b.compile(self.patch)
        self.k = 0
        # every span change the measurement reports, as (absolute sample,
        # span) -- the "retargets" the spec's tables count
        self.events = []
        if spy:
            real = self.b._delay_clock_edges

            def _spy(state, clock, frames):
                got = real(state, clock, frames)
                base = int(state["ck_n"]) - frames if "ck_n" in state else 0
                self.events.extend((base + n, s) for n, s in got)
                return got

            self.b._delay_clock_edges = _spy

    def plug(self, port, row):
        """Cable a fresh source into ``port`` and feed it ``row``."""
        if port == "in":
            s, op = self.patch.add_module("oscillator"), "out"
        elif port in ("freeze", "clock"):
            s, op = self.patch.add_module("clock"), "out"
        else:
            s, op = self.patch.add_module("lfo"), "cv"
        self.patch.connect(s.id, op, self.m.id, port)
        self.srcs[port] = (s.id, op)
        self.jacks[port] = row

    def unplug(self, port):
        s, op = self.srcs.pop(port)
        self.patch.disconnect(s, op, self.m.id, port)
        del self.jacks[port]

    def cable_out(self, port):
        """Cable ``out_l`` / ``out_r`` into a stereo sink."""
        if self.sink is None:
            self.sink = self.patch.add_module("stereo_speaker_output")
        self.patch.connect(self.m.id, port, self.sink.id,
                           "in_l" if port == "out_l" else "in_r")

    def uncable_out(self, port):
        self.patch.disconnect(self.m.id, port, self.sink.id,
                              "in_l" if port == "out_l" else "in_r")

    def run(self, n, script=None, ticks=None):
        """Render ``n`` samples. ``script`` = {sample: {param: value}} or
        {sample: callable(rig)}, applied at the head of the block that
        sample falls in; ``ticks`` = list of (k0, k1) SAMPLE ranges with
        the freeze tickbox on, set before each block the way the panel
        writes it. Returns ``out`` (and ``out_l``, ``out_r`` when the
        renderer handed back a dict)."""
        outs = {}
        end = self.k + n
        while self.k < end:
            f = min(self.block, end - self.k)
            if script:
                for at, ch in script.items():
                    if self.k <= at < self.k + f:
                        if callable(ch):
                            ch(self)
                        else:
                            self.m.params.update(ch)
            if ticks is not None:
                self.m.params["freeze"] = any(a <= self.k < b for a, b in ticks)
            bufs = {self.srcs[p]: v[..., self.k:self.k + f]
                    for p, v in self.jacks.items()}
            o = self.b._render_delay(self.m, f, bufs, self.patch)
            if not isinstance(o, dict):
                o = {"out": o}
            for key, v in o.items():
                outs.setdefault(key, []).append(np.asarray(v))
            self.k += f
        got = {key: np.concatenate(v, axis=-1) for key, v in outs.items()}
        if len(got) == 1:
            return got["out"]
        return got["out"], got["out_l"], got["out_r"]

    @property
    def st(self):
        return self.b._state[self.m.id]


def _digest(out, st):
    """sha256 over the float32 output bytes and the float64 line state."""
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(out, dtype=np.float32).tobytes())
    h.update(np.ascontiguousarray(st["buf"], dtype=np.float64).tobytes())
    h.update(np.ascontiguousarray(st["lp"], dtype=np.float64).tobytes())
    h.update(str(int(st["write_idx"])).encode())
    return h.hexdigest()


# ----- neutral: the pre-edit pins -------------------------------------------------

N_PIN = 3 * SR


def _pin_case(name, block=F):
    """The pinned scenarios. Every one uses only the jacks and params the
    pre-edit delay had, so the new code must reach the same bits through
    whichever path it now takes."""
    n = N_PIN
    if name == "long":
        # 400.37 ms +/- 100 ms: the fast path at every block size.
        params = {"time": 400.37, "feedback": 0.9, "cv_depth": 100.0, "mix": 0.5}
        jacks = {"in": _noise(1, n), "time_cv": _sine(0.11, 1.0, n),
                 "freeze": _gate([(90001, 110000)], n)}
        ticks = None
    elif name == "short_poly":
        # 5 ms +/- 3 ms, three voices: the old per-sample path at 512.
        params = {"time": 5.0, "feedback": 0.8, "tone": 0.7, "cv_depth": 3.0,
                  "mix": 0.6}
        jacks = {"in": np.stack([_noise(10 + v, n) for v in range(3)]),
                 "time_cv": np.stack([_sine(0.2 + 0.13 * v, 1.0, n)
                                      for v in range(3)]),
                 "freeze": _gate([(50001, 70003), (90007, 110777)], n)}
        ticks = None
    elif name == "floor":
        # 0.1 ms +/- 0.05 ms: 2.2 .. 6.6 samples, across the hop floor.
        params = {"time": 0.1, "feedback": 0.7, "tone": 0.9, "cv_depth": 0.05,
                  "mix": 0.5}
        jacks = {"in": _noise(3, n), "time_cv": _sine(1.7, 1.0, n)}
        ticks = None
    elif name == "mid_tick":
        # 200 samples, the freeze tickbox: per-sample at 512, fast at 64.
        # The tick lands on a block head shared by 64, 512 and 1000.
        params = {"time": 200 / SR * 1000.0, "feedback": 0.6, "tone": 0.5,
                  "mix": 0.8}
        jacks = {"in": _noise(4, n) * 0.5 + _sine(330.0, 0.1, n)}
        ticks = [(64000, 128000)]
    else:
        raise KeyError(name)
    rig = Rig(params, jacks, block=block)
    out = rig.run(n, ticks=ticks)
    return out, rig.st


# sha256 of ``_digest`` for each scenario, rendered by the pre-edit delay
# (e61daa4) at 512-sample blocks and checked identical at 64 and 1000.
_PIN_SHA = {
    "long": "5443480adcd70f90ebddd0b2d13986c6e919af54c05c31197d934d7f0a341d49",
    "short_poly": "335081adb6acc72969b00641b10b2bd35d9deb82aeafa3ec33c09ddcd2a030df",
    "floor": "b7af3635ae49af9e7e5f7e781b10d7849072542c46259611b7729737652308d9",
    "mid_tick": "ecd9bd0e6aad9e7856f9cabddd0c5f65f0172d61f3a5640017cf14c84c183fee",
}


@pytest.mark.parametrize("name", sorted(_PIN_SHA))
def test_the_pre_edit_renders_are_unchanged(name):
    out, st = _pin_case(name)
    assert np.all(np.isfinite(out)) and float(np.abs(out).max()) > 0.01
    assert _digest(out, st) == _PIN_SHA[name]


@pytest.mark.parametrize("name", sorted(_PIN_SHA))
@pytest.mark.parametrize("block", [64, 1000])
def test_the_pins_are_block_size_independent(name, block):
    out, st = _pin_case(name, block=block)
    assert _digest(out, st) == _PIN_SHA[name]


# ----- hops (the prep commit) ---------------------------------------------------


def _hop_case(name, n=SR):
    """Delays shorter than a block, which the per-sample loop ran before."""
    if name == "poly_cv_freeze":
        params = {"time": 5.0, "feedback": 0.8, "tone": 0.7, "cv_depth": 3.0,
                  "mix": 0.6}
        jacks = {"in": np.stack([_noise(20 + v, n) for v in range(3)]),
                 "time_cv": np.stack([_sine(0.7 + 0.3 * v, 1.0, n)
                                      for v in range(3)]),
                 "freeze": _gate([(9001, 20003), (30007, 33777)], n)}
    elif name == "twelve":
        params = {"time": 12 / SR * 1000.0, "feedback": 0.9, "tone": 0.3,
                  "mix": 0.7}
        jacks = {"in": _noise(21, n)}
    elif name == "four":
        # exactly the floor: hops of 4, whole-sample reads (frac 0)
        params = {"time": 4 / SR * 1000.0, "feedback": 0.95, "tone": 1.0,
                  "mix": 1.0}
        jacks = {"in": _noise(22, n)}
    else:
        raise KeyError(name)
    return params, jacks


@pytest.mark.parametrize("name", ["poly_cv_freeze", "twelve", "four"])
@pytest.mark.parametrize("block", [512, 1000])
def test_hops_equal_the_forced_per_sample_loop(name, block, monkeypatch):
    # Raise the floor past any delay and every block shorter than its
    # delay runs the per-sample loop again, as the delay did before the
    # hops. Same output, same line, same filter state.
    params, jacks = _hop_case(name)
    rig = Rig(params, jacks, block=block)
    hops = rig.run(SR)
    st_h = {k: np.array(rig.st[k]) for k in ("buf", "lp", "write_idx")}
    monkeypatch.setattr(NumpyBackend, "_DELAY_HOP_FLOOR", 10 ** 9)
    rig2 = Rig(params, jacks, block=block)
    loop = rig2.run(SR)
    assert float(np.abs(hops).max()) > 0.01
    assert np.array_equal(hops, loop)
    for k, v in st_h.items():
        assert np.array_equal(np.array(rig2.st[k]), v), k


def _out_and_line(params, jacks, n):
    rig = Rig(params, jacks)
    return rig.run(n), np.array(rig.st["buf"])


@pytest.mark.parametrize("name", ["poly_cv_freeze", "twelve", "four"])
def test_the_hop_comparison_can_fail(name, monkeypatch):
    # Self-test: a hop ONE sample longer than the shortest delay reads a
    # sample of the same hop before it is written. The forced loop must
    # then disagree, or the test above proves nothing. The line is part
    # of the exposure: "12 samples" is 11.999999999999998, so the early
    # read weighs 2e-15 -- it moves the float64 line and never reaches
    # the float32 output.
    params, jacks = _hop_case(name)
    n = SR // 4
    monkeypatch.setattr(NumpyBackend, "_DELAY_HOP_FLOOR", 10 ** 9)
    y_loop, line_loop = _out_and_line(params, jacks, n)
    monkeypatch.undo()
    y, line = _out_and_line(params, jacks, n)
    assert np.array_equal(y, y_loop) and np.array_equal(line, line_loop)
    real = NumpyBackend._delay_hop

    def one_too_long(self, low, frames):
        hop = real(self, low, frames)
        return hop + 1 if 0 < hop < frames else hop

    monkeypatch.setattr(NumpyBackend, "_delay_hop", one_too_long)
    y, line = _out_and_line(params, jacks, n)
    assert not (np.array_equal(y, y_loop) and np.array_equal(line, line_loop))


# ----- clock sync ------------------------------------------------------------------

RAMP = int(round(NumpyBackend._DELAY_FREEZE_RAMP_S * SR))     # the fade's R, 441
TICK = 5512.5                                                 # 120 BPM x4


def _clock_row(n, params=None, reset=None, run=None, block=F):
    """The real ``clock`` module's output, rendered on its own (its
    ``reset`` / ``run`` jacks fed raw rows). Injected into the delay as a
    row, the same at every block size: a moving ``bpm_cv`` upstream is a
    block mean and would not be."""
    p = Patch()
    c = p.add_module("clock", params=dict(params or {}))
    jacks = {}
    for port, row in (("reset", reset), ("run", run)):
        if row is not None:
            s = p.add_module("clock")
            p.connect(s.id, "out", c.id, port)
            jacks[(s.id, "out")] = row
    b = NumpyBackend(sample_rate=SR, block_size=block)
    b.compile(p)
    out, k = [], 0
    while k < n:
        f = min(block, n - k)
        o = b._render_clock(c, f, {kk: v[k:k + f] for kk, v in jacks.items()}, p)
        out.append(np.asarray(o["out"] if isinstance(o, dict) else o,
                              dtype=np.float32).copy())
        k += f
    return np.concatenate(out)


def _ticks(n, edges, width=30):
    """A gate row with a pulse at each edge (an edge list)."""
    g = np.zeros(n, dtype=np.float32)
    for a in edges:
        g[int(a):int(a) + width] = 1.0
    return g


def _edges(row):
    hi = row > 0.5
    return np.flatnonzero(hi & ~np.concatenate(([False], hi[:-1])))


def _impulse(n, at):
    x = np.zeros(n, dtype=np.float32)
    x[at] = 1.0
    return x


class TestClockNeutral:
    @pytest.mark.parametrize("time_ms", [7.3, 300.37])
    def test_a_silent_clock_then_one_edge_then_two_match_unpatched(self, time_ms):
        # Fewer than three edges is no lock, and before the lock ``time``
        # rules bit for bit -- on hops and on one-hop blocks, with a
        # wobbling time_cv and a freeze edge.
        n = 2 * SR
        jacks = {"in": _noise(9, n), "time_cv": _sine(0.7, 1.0, n),
                 "freeze": _gate([(30001, 50000)], n)}
        params = {"time": time_ms, "cv_depth": 2.0, "feedback": 0.7}
        ref = Rig(params, jacks).run(n)
        for ats in ([], [5000], [5000, 15000]):
            y = Rig(params, dict(jacks, clock=_ticks(n, ats))).run(n)
            assert np.array_equal(y, ref), ats

    def test_unpatched_never_calls_the_clock_code(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("the clock code ran on an unpatched delay")

        monkeypatch.setattr(NumpyBackend, "_delay_clock_edges", boom)
        monkeypatch.setattr(NumpyBackend, "_delay_sync_rows", boom)
        n = SR
        for params in ({"time": 300.37}, {"time": 3.0, "cv_depth": 1.0}):
            jacks = {"in": _noise(2, n), "time_cv": _sine(0.5, 1.0, n),
                     "freeze": _gate([(10001, 20000)], n)}
            y = Rig(params, jacks).run(n, ticks=[(25600, 30720)])
            assert np.all(np.isfinite(y))


class TestClock:
    def test_the_feel_ratios_are_the_combo(self):
        from pysynthrack.modules.delay import DELAY_FEEL_RATIOS, DELAY_FEELS
        assert DELAY_FEELS == ("straight", "dotted", "triplet")
        assert DELAY_FEEL_RATIOS == {"straight": (1, 1), "dotted": (3, 2),
                                     "triplet": (2, 3)}

    @pytest.mark.parametrize("feel, division, want", [
        ("straight", 1.0, 11025), ("straight", 0.25, 2756), ("straight", 2.0, 22050),
        ("straight", 1.5, 16538),          # 16537.5 rounds half to even
        ("dotted", 1.0, 16538), ("dotted", 2.0, 33075),
        ("triplet", 1.0, 7350), ("triplet", 3.0, 22050),
        ("straight", 1.33, 14663),         # a dragged 4/3: 37 samples short
        ("triplet", 2.0, 14700),           # ... of the true 4/3 triplet feel
    ])
    def test_the_locked_time_for_each_division_and_feel(self, feel, division, want):
        # T = round(span * division * num / (2 * den)) from a two-tick span
        # of 22050 (a 250 ms tick), floor 2.
        b = NumpyBackend(sample_rate=SR, block_size=F)
        m = Patch().add_module("delay", params={"division": division, "feel": feel})
        assert b._delay_sync_target(m, 22050) == float(want)

    def test_bad_division_and_feel_fall_back(self):
        b = NumpyBackend(sample_rate=SR, block_size=F)
        m = Patch().add_module("delay")
        for bad in (float("nan"), float("inf"), "abc", None):
            m.params["division"] = bad
            assert b._delay_sync_target(m, 22050) == 11025.0, bad
        m.params["division"] = 1000.0                 # the 64-tick rail
        assert b._delay_sync_target(m, 2000) == 64000.0
        m.params["division"] = 0.0                    # the 0.25-tick rail
        assert b._delay_sync_target(m, 22050) == 2756.0
        m.params["division"] = 1.0
        m.params["feel"] = "swung"                    # unknown: straight
        assert b._delay_sync_target(m, 22050) == 11025.0
        assert b._delay_sync_target(m, 2) == 2.0      # floor 2
        assert b._delay_sync_target(m, 1) == 2.0

    def test_the_real_clock_locks_on_the_third_edge_and_an_impulse_lands_on_T(self):
        n = 3 * SR
        clk = _clock_row(n)
        rig = Rig({"division": 1.5, "feedback": 0.0, "mix": 1.0, "tone": 1.0},
                  {"in": _impulse(n, 2 * SR), "clock": clk}, spy=True)
        y = rig.run(n)
        third = int(_edges(clk)[2])
        assert rig.events == [(third, third)]          # span 11024 (5512 + 5512)
        T = rig.st["ck_cur"]
        assert T == round(11024 * 1.5 / 2) == 8268.0
        assert int(np.argmax(np.abs(y))) == 2 * SR + 8268
        assert float(y[2 * SR + 8268]) == 1.0           # whole samples: no smear

    def test_time_is_inert_and_time_cv_live_while_locked(self):
        n = 3 * SR
        clk = _clock_row(n)
        x = _impulse(n, 2 * SR)
        p = {"division": 1.0, "feedback": 0.0, "mix": 1.0, "tone": 1.0,
             "cv_depth": 10.0}
        a = Rig(p, {"in": x, "clock": clk}).run(n)
        b = Rig(dict(p, time=40.0), {"in": x, "clock": clk}).run(n)
        moved = Rig(p, {"in": x, "clock": clk}).run(n, script={SR: {"time": 1500.0}})
        assert np.array_equal(a[SR:], b[SR:]) and np.array_equal(a, moved)
        assert int(np.argmax(np.abs(a))) == 2 * SR + 5512
        cv = np.full(n, 0.5, dtype=np.float32)        # +5 ms = +220.5 samples
        c = Rig(p, {"in": x, "clock": clk, "time_cv": cv}).run(n)
        assert int(np.argmax(np.abs(c))) == 2 * SR + 5512 + 220

    def test_a_locked_echo_equals_time(self):
        # A 10-ticks-a-second clock locks to 4410 samples; ``time`` 100 ms
        # is 4410 too, so the lock is no change at all: the clocked render
        # is the unclocked one bit for bit, every sample.
        n = 4 * SR
        clk = _ticks(n, range(0, n, 4410), 100)
        x = _noise(5, n)
        a = Rig({"time": 100.0, "feedback": 0.7}, {"in": x}).run(n)
        rig = Rig({"time": 100.0, "feedback": 0.7}, {"in": x, "clock": clk})
        b = rig.run(n)
        assert rig.st["ck_cur"] == 4410.0 and rig.st["ck_sync"] is True
        assert np.array_equal(a, b)

    @pytest.mark.parametrize("clock", [
        {}, {"bpm": 97.0}, {"bpm": 133.7, "division": 2.0}, {"swing": 0.33},
        {"bpm": 97.0, "division": 2.0, "swing": 0.5}])
    def test_one_event_per_render_on_the_tables_clocks(self, clock):
        # The real clock's interval alternates (5512 / 5513 at 120 x4) and
        # a swung one alternates by design; the last-two-edges rule would
        # retarget nearly every tick. One event in 20 s: the lock.
        n = 20 * SR
        clk = _clock_row(n, clock)
        assert len(set(np.diff(_edges(clk)).tolist())) >= 2
        rig = Rig({}, {"in": _noise(0, n), "clock": clk}, spy=True)
        rig.run(n)
        assert len(rig.events) == 1

    def test_a_long_target_folds_by_octaves_and_stays_on_the_grid(self):
        # A 100 BPM quarter clock at division 4 asks for 2.4 s: it folds to
        # 1.2 s -- two beats, on the grid (the span IS two ticks) -- where
        # a clamp would sit at 2.0 s, 3.33 beats.
        n = 8 * SR
        clk = _clock_row(n, {"bpm": 100.0, "division": 1.0})
        rig = Rig({"division": 4.0}, {"in": _noise(0, n), "clock": clk})
        rig.run(n)
        span = rig.st["ck_span"]
        assert abs(span - 52920) <= 2
        assert rig.st["ck_cur"] == float(span)
        assert rig.st["ck_cur"] <= NumpyBackend._DELAY_MAX_MS * SR / 1000.0

    def test_in_unpatched_keeps_measuring(self):
        n = 3 * SR
        clk = _clock_row(n)
        rig = Rig({"division": 2.0}, {"clock": clk})
        assert np.array_equal(rig.run(2 * SR), np.zeros(2 * SR, np.float32))
        assert rig.st["ck_span"] == 11024
        rig.plug("in", _noise(1, n))
        rig.run(F)
        # the first block with audio already fades to the clocked time
        assert rig.st["ck_to"] == 11024.0 and rig.st["ck_fn"] == -1
        assert rig.st["ck_cur"] == 11024.0


class TestBreaks:
    """The break rule, through the real clock's ``reset`` and ``run``: no
    disturbance that leaves the tempo alone may move the echo. Each case
    renders bit for bit what the undisturbed clock renders."""

    P = {"division": 2.0, "feedback": 0.6, "mix": 0.7}
    N = 3 * SR

    def _vs_clean(self, clock=None, **jacks):
        clean = Rig(self.P, {"in": _noise(0, self.N),
                             "clock": _clock_row(self.N, clock)}).run(self.N)
        rig = Rig(self.P, {"in": _noise(0, self.N),
                           "clock": _clock_row(self.N, clock, **jacks)}, spy=True)
        y = rig.run(self.N)
        return rig.events, y, clean

    @pytest.mark.parametrize("at", [
        [SR + 3.25 * TICK], [SR + 3.5 * TICK], [SR + 3.75 * TICK],
        [0.9 * SR, 2.93 * SR]])                       # off the grid, repeated
    def test_off_grid_resets_move_nothing(self, at):
        events, y, clean = self._vs_clean(reset=_ticks(self.N, at, 20))
        assert len(events) == 1                      # the lock, nothing after
        assert np.array_equal(y, clean)

    def test_an_off_grid_reset_on_a_swung_clock_moves_nothing(self):
        events, y, clean = self._vs_clean(
            {"swing": 0.33}, reset=_ticks(self.N, [SR + 3.5 * TICK], 20))
        assert len(events) == 1 and np.array_equal(y, clean)

    @pytest.mark.parametrize("every", [4, 8, 16])
    def test_on_grid_resets_move_nothing(self, every):
        at = [round(k * every * TICK) for k in range(1, 40) if k * every * TICK < self.N]
        events, y, clean = self._vs_clean(reset=_ticks(self.N, at, 20))
        assert len(events) == 1 and np.array_equal(y, clean)

    @pytest.mark.parametrize("stop", [1.5, 2.0, 2.4, 2.6, 4.0, 24.0])
    def test_run_low_for_a_while_moves_nothing(self, stop):
        run = np.ones(self.N, np.float32)
        a = int(SR + 2 * TICK + 100)
        run[a:a + int(stop * TICK) - 100] = 0.0
        events, y, clean = self._vs_clean(run=run)
        assert len(events) == 1 and np.array_equal(y, clean)

    @pytest.mark.parametrize("ratio", [1.25, 0.8, 2.0, 0.5])
    def test_a_real_tempo_step_is_one_event_on_tick_3(self, ratio):
        n = 8 * SR
        per, edges, k = 5512.0, [], 0.0
        while k < n:
            edges.append(int(k))
            k += per if k < 3 * SR else per / ratio
        rig = Rig({"division": 1.0}, {"in": _noise(0, n), "clock": _ticks(n, edges)},
                  spy=True)
        rig.run(n)
        step = next(i for i, e in enumerate(edges) if e >= 3 * SR)
        after = [(e, s) for e, s in rig.events if e >= 3 * SR]
        assert len(after) == 1
        assert edges.index(after[0][0]) - step == 3
        assert abs(after[0][1] - 2 * per / ratio) <= 2

    @pytest.mark.parametrize("name, period, bound_ms", [
        ("sweep 120 -> 180 BPM in 4 s",
         lambda s: TICK * 120 / (120 + 60 * min(s / 4.0, 1.0)), 5.0),
        ("+-3% at 0.2 Hz", lambda s: TICK * (1 + 0.03 * np.sin(2 * np.pi * 0.2 * s)), 2.0),
    ])
    def test_a_moving_tempo_stays_followed(self, name, period, bound_ms):
        # Against what three edges could know at each tick (half the last
        # two intervals): 3.7 ms worst on the sweep, 1.2 ms on the wobble
        # (measured; three matching spans lost 40 ms on the sweep).
        n = 8 * SR
        edges, k = [], 0.0
        while k < n:
            edges.append(int(k))
            k += period(k / SR)
        rig = Rig({"division": 1.0}, {"in": _noise(0, n), "clock": _ticks(n, edges)},
                  spy=True)
        rig.run(n)
        ev = dict(rig.events)
        span, worst = None, 0.0
        for i in range(2, len(edges)):
            span = ev.get(edges[i], span)
            if span is not None:
                worst = max(worst, abs(span / 2 - (edges[i] - edges[i - 2]) / 2))
        assert worst / SR * 1000 < bound_ms, worst
        assert len(rig.events) > 5                 # it kept measuring

    def test_a_doubled_tick_and_a_reset_during_the_lock_correct_themselves(self):
        # Two breaks inside three ticks can get one wrong span through;
        # it costs two fades and corrects within four ticks.
        n = 6 * SR
        base = [round(k * TICK) for k in range(int(n / TICK))]
        doubled = sorted(base + [base[10] + 2000])
        rig = Rig({}, {"in": _noise(0, n), "clock": _ticks(n, doubled)}, spy=True)
        rig.run(n)
        assert len(rig.events) == 3                         # lock, wrong, fixed
        wrong, fixed = rig.events[1][0], rig.events[2][0]
        assert fixed - wrong <= 4 * TICK + 1
        assert abs(rig.st["ck_span"] - 11025) <= 2
        early = [0, 5512, 8000] + [round(8000 + k * TICK) for k in range(1, 40)]
        rig = Rig({}, {"in": _noise(0, n), "clock": _ticks(n, early)}, spy=True)
        rig.run(n)
        assert rig.events[0] == (8000, 8000)               # the wrong lock
        assert len(rig.events) == 2
        assert rig.events[1][0] - 8000 <= 4 * TICK + 1     # ... corrected
        assert abs(rig.st["ck_span"] - 11025) <= 2

    def test_without_the_break_test_an_off_grid_reset_moves_the_echo(self, monkeypatch):
        # Self-test: the off-grid reset case above must FAIL with the
        # break rule switched off, or it proves nothing.
        monkeypatch.setattr(NumpyBackend, "_delay_breaks_pattern",
                            staticmethod(lambda *a: False))
        events, y, clean = self._vs_clean(reset=_ticks(self.N, [SR + 3.5 * TICK], 20))
        assert len(events) > 1 and not np.array_equal(y, clean)


# ----- the crossfade ------------------------------------------------------------------

_SOS_HI = []


def _high_band(y):
    """A 4th-order 3 kHz high-pass: what a click adds to a tone."""
    from scipy.signal import butter, sosfilt
    if not _SOS_HI:
        _SOS_HI.append(butter(4, 3000 / (SR / 2), "high", output="sos"))
    return sosfilt(_SOS_HI[0], np.asarray(y, dtype=np.float64))


def _tones(freqs, n, amp=0.25):
    t = np.arange(n) / SR
    return (amp * sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)).astype(np.float32)


class TestFade:
    N = 3 * SR
    P = {"time": 250.0, "division": 1.0, "feedback": 0.5, "mix": 1.0, "tone": 1.0}
    POS = [SR + 512 * k for k in range(0, 40, 8)]
    MATERIAL = {"bass 55": [55.0], "sine 330": [330.0],
                "pad": [220.0, 277.18, 329.63, 440.0]}

    def _clk(self):
        return _ticks(self.N, range(0, self.N, 11025), 100)   # T 11025 = 250 ms

    def _worst(self, x, clocked):
        """The worst high band (> 3 kHz) near a 1 -> 2 tick switch over
        several block heads: the clocked crossfade, or today's ``time``
        knob jumping between the same two times (250 -> 500 ms)."""
        worst = 0.0
        for at in self.POS:
            if clocked:
                y = Rig(self.P, {"in": x, "clock": self._clk()}).run(
                    self.N, script={at: {"division": 2.0}})
            else:
                y = Rig(self.P, {"in": x}).run(self.N, script={at: {"time": 500.0}})
            worst = max(worst, float(np.abs(_high_band(y)[at - 200:at + 3000]).max()))
        return worst

    @pytest.mark.parametrize("name", list(MATERIAL))
    def test_the_fade_is_a_tenth_of_the_jump_or_less(self, name):
        # Measured 100x to 440x under the jump. On tones: a saw or noise
        # swamps this measure with its own highs.
        x = _tones(self.MATERIAL[name], self.N)
        jump = self._worst(x, clocked=False)
        assert self._worst(x, clocked=True) <= jump / 10, name

    def test_the_click_tripwire_trips_on_a_one_sample_fade(self, monkeypatch):
        # Self-test: R = 1 (the fade is the freeze's ramp, so a one-sample
        # ramp) is a jump, and the tripwire must see it.
        x = _tones(self.MATERIAL["sine 330"], self.N)
        jump = self._worst(x, clocked=False)
        monkeypatch.setattr(NumpyBackend, "_DELAY_FREEZE_RAMP_S", 1.0 / SR)
        assert self._worst(x, clocked=True) > jump / 10

    @pytest.mark.parametrize("fb", [0.6, 0.9, 0.98])
    def test_the_loop_peak_never_grows_in_a_fade(self, fb):
        # The blend is linear, not equal-power, so the loop gain never
        # exceeds 1 through it.
        burst = np.zeros(self.N, np.float32)
        burst[:SR // 2] = 0.3 * np.random.default_rng(3).standard_normal(SR // 2)
        at = 2 * SR
        y = Rig(dict(self.P, feedback=fb), {"in": burst, "clock": self._clk()}).run(
            self.N, script={at: {"division": 2.0}})
        assert np.abs(y[at:at + RAMP]).max() <= np.abs(y[at - SR // 2:at]).max()

    def test_retargets_queue_one_fade_at_a_time(self):
        # Two division moves 64 samples apart: the second waits for the
        # first to finish, then starts on the very next sample.
        rig = Rig(self.P, {"in": _noise(0, self.N), "clock": self._clk()}, block=64)
        rig.run(64 * 1000)
        assert rig.st["ck_cur"] == 11025.0 and rig.st["ck_fn"] == -1
        rig.m.params["division"] = 2.0
        rig.run(64)
        rig.m.params["division"] = 3.0
        rig.run(64)
        assert rig.st["ck_to"] == 22050.0 and rig.st["ck_fn"] == 128
        rig.run(RAMP - 128 + 10)                 # 10 samples past the first fade
        assert rig.st["ck_from"] == 22050.0 and rig.st["ck_to"] == 33075.0
        assert rig.st["ck_fn"] == 10
        rig.run(RAMP)
        assert rig.st["ck_cur"] == 33075.0 and rig.st["ck_fn"] == -1

    def test_unpatching_fades_home_and_closes_the_door(self, monkeypatch):
        imp = 2 * SR
        rig = Rig({"time": 100.0, "feedback": 0.0, "mix": 1.0, "tone": 1.0},
                  {"in": _impulse(self.N, imp), "clock": self._clk()})
        rig.run(SR)
        assert rig.st["ck_cur"] == 11025.0
        rig.unplug("clock")
        rig.run(F)
        assert rig.st["ck_to"] == 4410.0 and rig.st["ck_fn"] == -1   # home in 441
        assert rig.st["ck_sync"] is False and "ck_n" not in rig.st

        def boom(*a, **k):
            raise AssertionError("the door stayed open")

        monkeypatch.setattr(rig.b, "_delay_sync_rows", boom)
        monkeypatch.setattr(rig.b, "_delay_clock_edges", boom)
        # the unclocked delay again: the knob rules (and jumps) once more
        rig.m.params["time"] = 120.0                  # 5292 samples
        t0 = rig.k
        y = rig.run(self.N - t0)
        assert int(np.argmax(np.abs(y))) == imp + 5292 - t0


# ----- freeze x clock -------------------------------------------------------------------


class TestFreezeClock:
    N = 6 * SR
    P = {"division": 2.0, "feedback": 0.5, "tone": 0.5, "mix": 1.0}
    FZ = (2 * SR + 37, 3 * SR + 101)

    def _fz(self):
        return _gate([self.FZ], self.N)

    def _clk(self):
        return _ticks(self.N, [k * TICK for k in range(int(self.N / TICK))])

    def test_a_locked_hold_is_T_with_no_slide(self):
        rig = Rig(self.P, {"in": _noise(1, self.N) * 0.5, "clock": self._clk(),
                           "freeze": self._fz()})
        y = rig.run(self.N)
        T = rig.st["ck_cur"]
        assert T == 11025.0 and float(rig.st["fz_dly"][0]) == T
        # the read slides 0 samples: T (1 - e) + T e is T at every weight
        e = np.arange(1, RAMP + 1) / RAMP
        assert np.array_equal(T * (1.0 - e) + T * e, np.full(RAMP, T))
        D = int(T)
        a, b = self.FZ[0] + RAMP + D, self.FZ[1]
        assert b - a > SR // 2
        assert np.array_equal(y[a:b], y[a - D:b - D])        # held, bit for bit

    def test_edges_in_a_hold_are_measured_and_the_retarget_lands_after_release(self):
        # The tempo steps during the hold. The span moves, the read does
        # not; the fade to the new time starts on the sample the release
        # runs out -- never inside the hold or its ramp.
        edges, k = [], 0.0
        while k < self.N:
            edges.append(int(k))
            k += TICK if k < self.FZ[0] + 0.2 * SR else 4410.0
        rig = Rig(self.P, {"in": _noise(1, self.N) * 0.5, "clock": _ticks(self.N, edges),
                           "freeze": self._fz()}, spy=True)
        reads = []
        real = rig.b._delay_sync_rows

        def spy(*a, **kw):
            got = real(*a, **kw)
            reads.append(got[0].copy())
            return got

        rig.b._delay_sync_rows = spy
        y = rig.run(self.N)
        assert any(self.FZ[0] < e < self.FZ[1] for e, _s in rig.events)
        row = np.concatenate(reads)
        release_end = self.FZ[1] + RAMP - 1
        assert np.all(row[self.FZ[0]:release_end] == 11025.0)
        assert row[release_end] == 8820.0 and rig.st["ck_cur"] == 8820.0
        D = 11025
        a = self.FZ[0] + RAMP + D
        assert np.array_equal(y[a:self.FZ[1]], y[a - D:self.FZ[1] - D])

    def test_a_rise_inside_a_fade_finishes_it_and_holds_the_new_read(self):
        rig = Rig(self.P, {"in": _noise(1, self.N) * 0.5, "clock": self._clk(),
                           "freeze": self._fz()})
        at = (self.FZ[0] // F) * F                 # 173 samples before the rise
        assert 0 < self.FZ[0] - at < RAMP
        y = rig.run(self.N, script={at: {"division": 3.0}})
        D = int(rig.st["fz_dly"][0])
        assert D == rig.st["ck_cur"] == 16538.0
        a = self.FZ[0] + RAMP + D
        assert np.array_equal(y[a:self.FZ[1]], y[a - D:self.FZ[1] - D])


# ----- block-size exactness ---------------------------------------------------------

N_BS = 198450                                    # 4.5 s; moves at 64000, 128000


def _swung_edges(n, step_at, per1, per2, swing):
    """An edge list: a swung clock (long, short, long ...) whose tempo
    steps at ``step_at``."""
    edges, k, i = [], 0.0, 0
    while k < n:
        edges.append(int(k))
        per = per1 if k < step_at else per2
        k += per * (1 + swing if i % 2 == 0 else 1 - swing)
        i += 1
    return edges


def _bs_case(name, block):
    """The block-size scenarios: everything the clock touches, with every
    param move and cable change on a block head that 64, 128, 512 and
    1000 share (multiples of 64000), and gate rows injected."""
    n = N_BS
    script = None
    stereo = ()
    if name == "tempo_swing_feel_cv_freeze":
        stereo = LR
        params = {"time": 123.4, "division": 1.5, "feel": "dotted",
                  "feedback": 0.7, "cv_depth": 5.0, "mix": 0.6}
        jacks = {"in": _noise(30, n),
                 "clock": _ticks(n, _swung_edges(n, 2.2 * SR, 5512.5, 4410.0, 0.33)),
                 "time_cv": _sine(0.37, 1.0, n),
                 "freeze": _gate([(120001, 140000)], n)}
        script = {64000: {"division": 3.0}, 128000: {"feel": "triplet"}}
    elif name == "whole":
        params = {"division": 2.0, "feedback": 0.8, "tone": 0.6, "mix": 0.5}
        jacks = {"in": _noise(31, n),
                 "clock": _ticks(n, [k * TICK for k in range(int(n / TICK))])}
    elif name == "fold":
        params = {"division": 4.0, "feedback": 0.6, "mix": 0.5}
        jacks = {"in": _noise(32, n),
                 "clock": _ticks(n, range(0, n, 26460))}       # 100 BPM quarters
        script = {128000: {"division": 16.0}}
    elif name == "unpatch":
        params = {"time": 200.0, "division": 1.0, "feedback": 0.7, "mix": 0.5}
        jacks = {"in": _noise(33, n),
                 "clock": _ticks(n, [k * TICK for k in range(int(n / TICK))])}
        script = {128000: lambda rig: rig.unplug("clock"),
                  192000: {"time": 150.0}}
    elif name == "poly_flip":
        # three voices whose reads sweep 500..1000 samples: hops of a few
        # hundred at 1000, a block-length hop or hops at 512 by turns,
        # block-length hops at 64 and 128 -- the paths flip per size
        stereo = LR
        params = {"division": 1.0, "feedback": 0.75, "tone": 0.8,
                  "cv_depth": 5.0, "mix": 0.7, "spread": 0.8}
        jacks = {"in": np.stack([_noise(40 + v, n) for v in range(3)]),
                 "clock": _ticks(n, range(0, n, 735)),        # T = 735
                 "time_cv": np.stack([_sine(0.3 + 0.2 * v, 1.0, n) for v in range(3)]),
                 "freeze": _gate([(100003, 115000)], n)}
        script = {64000: {"division": 0.5}, 128000: {"division": 1.0}}
    else:
        raise KeyError(name)
    rig = Rig(params, jacks, block=block, stereo=stereo)
    out = rig.run(n, script=script)
    if stereo:
        out = np.stack(out)                      # out, out_l, out_r
    keep = ("buf", "lp", "write_idx", "fz_dly", "ck_cur", "ck_span", "ck_fn",
            "ck_sync", "buf_s", "lp_s")
    st = {k: np.array(rig.st[k]) for k in keep if k in rig.st}
    return out, st


BS_CASES = ["tempo_swing_feel_cv_freeze", "whole", "fold", "unpatch", "poly_flip"]


@pytest.mark.parametrize("name", BS_CASES)
def test_the_clocked_delay_is_block_size_exact(name):
    ref, ref_st = _bs_case(name, 512)
    assert np.all(np.isfinite(ref)) and float(np.abs(ref).max()) > 0.01
    for block in (64, 128, 1000):
        got, st = _bs_case(name, block)
        bad = int(np.count_nonzero(got != ref))
        assert bad == 0, (name, block, bad)
        assert st.keys() == ref_st.keys()
        for key, v in ref_st.items():
            assert np.array_equal(st[key], v), (name, block, key)


def test_the_block_size_probe_catches_a_float_carried_fade_weight(monkeypatch):
    # Self-test: carry the fade weight as a float from block to block
    # (``w += 1 / R`` by cumulative sums) and the probe must see it.
    carry = {"w": 0.0}

    def carried(fn, m, ramp_n):
        if fn == 0:
            carry["w"] = 0.0
        w = carry["w"] + np.cumsum(np.full(m, 1.0 / ramp_n))
        carry["w"] = float(w[-1])
        return w

    monkeypatch.setattr(NumpyBackend, "_delay_xfade_weights", staticmethod(carried))
    ref, ref_st = _bs_case("tempo_swing_feel_cv_freeze", 512)
    got, st = _bs_case("tempo_swing_feel_cv_freeze", 64)
    assert (np.count_nonzero(got != ref)
            or np.count_nonzero(st["buf"] != ref_st["buf"]))


def _tiny_clocked(block, n=SR // 2):
    # a clock every 3 samples locks a 3-sample echo; division moves fade
    # 3 -> 6 -> 4 samples, with a wobble so the reads carry fractions
    params = {"division": 1.0, "feedback": 0.8, "tone": 0.9, "mix": 0.6,
              "cv_depth": 0.4 / SR * 1000.0}
    jacks = {"in": _noise(50, n), "clock": _ticks(n, range(0, n, 3), 1),
             "time_cv": _sine(31.0, 1.0, n)}
    rig = Rig(params, jacks, block=block, stereo=LR)
    # the moves sit on block heads that 64, 128 and 512 share
    y = np.stack(rig.run(n, script={6144: {"division": 2.0},
                                    12800: {"division": 1.5}}))
    lines = np.stack([rig.st["buf"], rig.st["buf_s"]])
    return y, lines, rig.st["ck_cur"]


@pytest.mark.parametrize("block", [64, 128, 512])
def test_the_loop_and_the_hops_agree_through_a_fade(block, monkeypatch):
    # Below the 4-sample floor the per-sample loop carries the fade; with
    # the floor lowered to 2 the same block runs as hops of 2 or 3 (a hop
    # of int(shortest read) is always safe). Same output, same line, and
    # the same at 64 and 128 as at 512. (1000 shares no head with 512
    # under 64000, which the five scenarios above reach.)
    y, line, cur = _tiny_clocked(block)
    assert cur == 4.0                            # 3 * 1.5 = 4.5, half to even
    ref, ref_line, _ = _tiny_clocked(512)
    assert np.array_equal(y, ref) and np.array_equal(line, ref_line)
    monkeypatch.setattr(NumpyBackend, "_DELAY_HOP_FLOOR", 2)
    y2, line2, _ = _tiny_clocked(block)
    assert float(np.abs(y).max()) > 0.01
    assert np.array_equal(y, y2) and np.array_equal(line, line2)


# ----- panel -------------------------------------------------------------------------


def _panel_calls(monkeypatch):
    """Every widget call the delay's node makes, keyed by label, with its
    full kwargs (the matching-a-label-is-not-matching-the-widget rule)."""
    from unittest import mock
    pytest.importorskip("dearpygui.dearpygui")
    import pysynthrack.ui.app as app_mod
    monkeypatch.setenv("PYSYNTHRACK_BACKEND", "numpy")
    monkeypatch.setattr(app_mod, "dpg", mock.MagicMock())
    app = app_mod.App()
    app.patch = Patch()
    module = app.patch.add_module("delay")
    kinds = ("add_combo", "add_drag_float", "add_slider_float", "add_checkbox")
    before = {k: len(getattr(app_mod.dpg, k).call_args_list) for k in kinds}
    app._create_node_for_module(module)
    out = {}
    for k in kinds:
        for call in getattr(app_mod.dpg, k).call_args_list[before[k]:]:
            out[str(call.kwargs.get("label"))] = (k, call.kwargs)
    return out


def test_the_panel_offers_spread_as_a_unit_slider(monkeypatch):
    kind, kw = _panel_calls(monkeypatch)["spread (out_l/r)"]
    assert kind == "add_slider_float"
    assert (kw["min_value"], kw["max_value"], kw["default_value"]) == (0.0, 1.0, 1.0)


def test_the_panel_offers_division_and_feel(monkeypatch):
    from pysynthrack.modules.delay import DELAY_FEELS
    w = _panel_calls(monkeypatch)
    kind, kw = w["feel"]
    assert kind == "add_combo"
    assert list(kw["items"]) == list(DELAY_FEELS) and kw["default_value"] == "straight"
    kind, kw = w["division"]
    assert kind == "add_drag_float"
    assert (kw["min_value"], kw["max_value"]) == (0.25, 64.0)
    assert (NumpyBackend._MOD_DIV_MIN, NumpyBackend._MOD_DIV_MAX) == (0.25, 64.0)
    assert kw["format"].endswith(" ticks") and kw["default_value"] == 1.0


# ----- stereo: the side line -------------------------------------------------------


def _textbook_pair(x, T, fb, tone, mix):
    """A hard ping-pong the textbook way, float64, one sample at a time:
    two lines feeding each other, ``L = x + fb LP(R@T)``, ``R = fb LP(L@T)``,
    each fed-back read through its own damping one-pole; the left output
    plays line L's read."""
    n = len(x)
    Lb, Rb = np.zeros(n), np.zeros(n)
    fc = 200.0 * (18000.0 / 200.0) ** tone
    g = 1.0 - float(np.exp(-2.0 * np.pi * fc / SR))
    back = int(np.ceil(T))
    fr = back - T
    lpa = lpb = 0.0
    ol, orr = np.zeros(n), np.zeros(n)

    def rd(buf, i):
        a = buf[i - back] if i >= back else 0.0
        b = buf[i - back + 1] if i - back + 1 >= 0 else 0.0
        return a * (1 - fr) + b * fr

    for i in range(n):
        rl, rr = rd(Lb, i), rd(Rb, i)
        lpa = g * rr + (1 - g) * lpa
        lpb = g * rl + (1 - g) * lpb
        Lb[i] = x[i] + fb * lpa
        Rb[i] = fb * lpb
        ol[i] = x[i] * (1 - mix) + rl * mix
        orr[i] = x[i] * (1 - mix) + rr * mix
    return ol, orr


class TestStereoNeutral:
    @pytest.mark.parametrize("spread", [0.0, 0.4, 1.0])
    @pytest.mark.parametrize("path", ["hops", "loop"])
    def test_out_is_the_uncabled_out_whatever_the_side_line_does(self, spread, path):
        # Both paths, with freeze, time_cv and a clocked fade in the run.
        n = 2 * SR
        x = _noise(4, n)
        if path == "hops":
            params = {"time": 300.0, "cv_depth": 3.0, "division": 2.0}
            jacks = {"in": x, "time_cv": _sine(0.5, 1.0, n),
                     "freeze": _gate([(50001, 60000)], n),
                     "clock": _ticks(n, range(0, n, 2205))}
        else:
            params = {"time": 0.06, "cv_depth": 0.02}        # 2.2 .. 3.5 samples
            jacks = {"in": x, "time_cv": _sine(0.5, 1.0, n),
                     "freeze": _gate([(50001, 60000)], n)}
        script = {44032: {"division": 3.0}}
        ref = Rig(params, jacks).run(n, script=script)
        y, l, r = Rig(dict(params, spread=spread), jacks, stereo=LR).run(n, script=script)
        assert np.array_equal(y, ref)
        if spread == 0.0:
            assert np.array_equal(l, y) and np.array_equal(r, y)
        else:
            assert not np.array_equal(l, r)

    def test_an_uncabled_delay_never_runs_the_side_line(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("the side line ran uncabled")

        monkeypatch.setattr(NumpyBackend, "_delay_side_line", staticmethod(boom))
        n = SR
        jacks = {"in": _noise(2, n), "time_cv": _sine(0.5, 1.0, n),
                 "freeze": _gate([(10001, 20000)], n), "clock": _ticks(n, range(0, n, 2205))}
        for params in ({"time": 300.37, "spread": 0.3}, {"time": 0.05, "cv_depth": 0.01}):
            y = Rig(params, jacks).run(n)
            assert isinstance(y, np.ndarray) and y.shape == (n,)


class TestStereo:
    def test_an_impulse_bounces_left_right_and_the_far_side_is_silent(self):
        n = SR
        T = 2000
        y, l, r = Rig({"time": T / SR * 1000, "feedback": 0.5, "mix": 1.0, "tone": 1.0},
                      {"in": _impulse(n, 0)}, stereo=LR).run(n)
        for k in range(1, 7):
            near, far = (l, r) if k % 2 else (r, l)
            assert near[k * T] == y[k * T] and near[k * T] > 0.01
            assert far[k * T] == 0.0
        assert np.array_equal(l + r, y)            # every repeat on one side

    @pytest.mark.parametrize("T, fb", [(1234.56, 0.6), (300.25, 0.98), (5000.0, 0.9),
                                       (37.4, 0.95)])
    def test_it_is_the_textbook_cross_coupled_pair(self, T, fb):
        # Measured under 1.5e-8 (float32 rounding of the outputs), up to
        # feedback 0.98, fractional times, on hops and one-hop blocks.
        n = SR // 2
        x = _noise(3, n) * 0.5
        params = {"time": T / SR * 1000.0, "feedback": fb, "tone": 0.6, "mix": 0.7}
        y, l, r = Rig(params, {"in": x}, stereo=LR).run(n)
        tl, tr = _textbook_pair(x.astype(np.float64), params["time"] * SR / 1000.0,
                                fb, 0.6, 0.7)
        assert np.abs(l - tl).max() < 6e-8 and np.abs(r - tr).max() < 6e-8

    def test_the_far_side_law(self):
        # At ``spread`` s each side carries its own echo at full level and
        # the other side's at 1 - s.
        n = SR
        x = _noise(6, n)
        params = {"time": 123.4, "feedback": 0.6, "mix": 0.8}
        y, l1, r1 = Rig(params, {"in": x}, stereo=LR).run(n)
        dry = x.astype(np.float64) * 0.2
        wl, wr = l1 - dry, r1 - dry                     # the pure L / R echoes
        for s in (0.0, 0.25, 0.7):
            _y, ls, rs = Rig(dict(params, spread=s), {"in": x}, stereo=LR).run(n)
            assert np.abs(ls - (dry + wl + (1 - s) * wr)).max() < 1e-6
            assert np.abs(rs - (dry + wr + (1 - s) * wl)).max() < 1e-6

    def test_mix_zero_is_dry_on_all_three(self):
        n = SR // 2
        x = _noise(7, n)
        for t_ms in (300.0, 0.05):
            y, l, r = Rig({"time": t_ms, "mix": 0.0, "feedback": 0.9}, {"in": x},
                          stereo=LR).run(n)
            assert np.array_equal(y, x) and np.array_equal(l, x) and np.array_equal(r, x)

    def test_voices_keep_their_own_lines_and_one_row_is_mono(self):
        n = SR // 2
        x1 = _noise(8, n)
        y, l, r = Rig({"time": 50.0, "feedback": 0.7}, {"in": x1}, stereo=LR).run(n)
        x3 = np.stack([x1, np.zeros(n, np.float32), _noise(9, n)])
        y3, l3, r3 = Rig({"time": 50.0, "feedback": 0.7}, {"in": x3}, stereo=LR).run(n)
        assert y3.shape == l3.shape == r3.shape == (3, n)
        assert np.array_equal(l3[0], l) and np.array_equal(r3[0], r)   # row = mono
        assert np.array_equal(y3[0], y)
        assert not np.any(l3[1]) and not np.any(r3[1])                 # no cross-talk
        assert not np.array_equal(l3[2], l3[0])


class TestRecable:
    P = {"time": 300.0, "feedback": 0.5, "tone": 0.8, "mix": 1.0, "spread": 1.0}
    N = 8 * SR
    T = int(0.3 * SR)

    @staticmethod
    def _burst(n, spans, seed):
        x = np.zeros(n, dtype=np.float32)
        rng = np.random.default_rng(seed)
        for a, b in spans:
            x[a:b] = 0.3 * rng.standard_normal(b - a)
        return x

    def _run(self, x, params=None, keep=False, hold=None):
        """Cabled 0..2 s, uncabled 2..5 s, cabled again 5..8 s."""
        jacks = {"in": x}
        if hold is not None:
            jacks["freeze"] = hold
        rig = Rig(params or self.P, jacks, stereo=LR)
        o1, _l, _r = rig.run(2 * SR)
        saved = (rig.st["buf_s"], rig.st["lp_s"])
        rig.uncable_out("out_l")
        rig.uncable_out("out_r")
        mono = rig.run(3 * SR)
        dropped = "buf_s" not in rig.st and "lp_s" not in rig.st
        if keep:                                   # the planted stale line
            rig.st["buf_s"], rig.st["lp_s"] = saved
        rig.cable_out("out_l")
        rig.cable_out("out_r")
        o2, l2, r2 = rig.run(3 * SR)
        return np.concatenate([o1, mono, o2]), l2, r2, rig, dropped

    def test_uncabling_drops_the_side_line_so_a_recable_has_no_ghost(self):
        # A burst before the uncable, silence after; by the re-cable the mid
        # has decayed (-86.6 dB) and a kept side line would read the old
        # burst back at -4.0 dB (the planted self-test). Dropped: -92.7 dB.
        x = self._burst(self.N, [(int(0.5 * SR), int(1.0 * SR))], 1)
        o, l, r, _rig, dropped = self._run(x)
        assert dropped
        mid = float(np.abs(o[5 * SR:5 * SR + self.T]).max())
        assert 0.0 < mid < 1e-4
        assert float(np.abs(l[:self.T]).max()) <= 0.5 * mid + 1e-12
        o, l, r, _rig, _d = self._run(x, keep=True)
        assert float(np.abs(l[:self.T]).max()) > 100 * mid      # the ghost

    def test_old_echoes_play_centred_and_a_new_note_bounces(self):
        x = self._burst(self.N, [(int(4.8 * SR), int(4.81 * SR)),
                                 (int(5.5 * SR), int(5.51 * SR))], 2)
        o, l, r, _rig, _d = self._run(x, dict(self.P, feedback=0.6))
        m = o[5 * SR:]
        old = slice(0, int(0.45 * SR))
        assert np.array_equal(l[old], r[old])                     # centred
        assert np.abs((l[old] + r[old]) - m[old]).max() < 1e-12   # L + R = mid
        assert np.abs(l[old]).max() > 0.1

        def pk(y, a):
            return float(np.abs(y[int((a - 0.02) * SR):int((a + 0.03) * SR)]).max())

        assert pk(r, 0.8) == 0.0 and pk(l, 0.8) == pk(m, 0.8)    # new: L first
        assert pk(l, 1.1) == 0.0 and pk(r, 1.1) == pk(m, 1.1)    # ... then R

    def test_one_jack_left_cabled_keeps_the_side_line(self):
        n = SR
        rig = Rig(self.P, {"in": _noise(1, n)}, stereo=LR)
        rig.run(n // 2)
        buf_s = rig.st["buf_s"]
        rig.uncable_out("out_r")
        y, l, r = rig.run(n // 2)
        assert rig.st["buf_s"] is buf_s and np.any(buf_s)

    def test_a_recable_during_a_hold_stays_centred_and_exact(self):
        x = self._burst(self.N, [(int(1.5 * SR), int(3.0 * SR))], 3)
        g = _gate([(int(3.2 * SR), int(7.5 * SR))], self.N)
        o, l, r, rig, _d = self._run(x, hold=g)
        k = int(2.4 * SR)
        assert np.all(rig.st["buf_s"] == 0.0)
        assert np.array_equal(l[:k], r[:k])
        assert np.array_equal(l[:k], (0.5 * o[5 * SR:5 * SR + k]).astype(np.float32))
        assert np.abs(l[:k]).max() > 0.1

    def test_a_voice_count_change_lands_in_the_same_clean_state(self):
        n = SR
        clk = _ticks(n, range(0, n, 2205))
        rig = Rig({"division": 2.0}, {"in": _noise(1, n), "clock": clk}, stereo=LR)
        rig.run(n // 2)
        span = rig.st["ck_span"]
        rig.jacks["in"] = np.stack([_noise(1, n), _noise(2, n)])     # now 2 voices
        y, l, r = rig.run(F)
        assert rig.st["buf_s"].shape == rig.st["buf"].shape == (2, rig.st["buf"].shape[1])
        assert rig.st["lp_s"].shape == (2,)
        assert rig.st["ck_span"] == span                 # the clock kept its span
        assert l.shape == (2, F)


class TestStereoFreeze:
    def test_a_held_ping_pong_keeps_bouncing_bit_for_bit(self):
        # The side line holds with gain -1, so over 10 s of hold the mid
        # repeats exactly every lap, the side negates exactly every lap,
        # and L on one lap is R on the next.
        n = 12 * SR
        x = _noise(11, n) * 0.5
        rise = SR + 37
        g = _gate([(rise, n)], n)
        rig = Rig({"time": 250.0, "feedback": 0.5, "mix": 1.0}, {"in": x, "freeze": g},
                  stereo=LR)
        y, l, r = rig.run(n)
        D = int(rig.st["fz_dly"][0])
        assert D == 11025
        a = rise + RAMP + D
        assert np.array_equal(y[a:], y[a - D:-D])              # mid: periodic
        assert np.array_equal(l[a:], r[a - D:-D])              # sides swap per lap
        assert np.array_equal(r[a:], l[a - D:-D])
        assert np.abs(l[a:] - r[a:]).max() > 0.01              # really stereo
        st = rig.st
        w = int(st["write_idx"])
        idx = (w - 1 - np.arange(D)) % st["buf"].shape[1]
        assert np.array_equal(st["buf_s"][0, idx], -st["buf_s"][0, (idx - D) % st["buf"].shape[1]])
        assert np.array_equal(st["buf"][0, idx], st["buf"][0, (idx - D) % st["buf"].shape[1]])


# ----- the example ---------------------------------------------------------------


def _render_example(one_note=False, seconds=6.0):
    from pathlib import Path

    from pysynthrack.io_patch import load_patch

    np.random.seed(0)
    path = Path(__file__).resolve().parent.parent / "examples" / "delay_pingpong_sync.json"
    patch = load_patch(path)
    dl = next(m for m in patch if m.TYPE == "delay")
    if one_note:                      # the bar's first note alone
        seq = next(m for m in patch if m.TYPE == "sequencer")
        for k in range(2, 9):
            seq.params[f"step{k}_on"] = False
    b = NumpyBackend(sample_rate=SR, block_size=F)
    b.compile(patch)
    outs = []
    for _ in range(int(SR * seconds / F)):
        out, _devices = b.render_block_multi(F)
        assert out is not None and np.all(np.isfinite(out))
        outs.append(np.asarray(out).copy())
    return np.concatenate(outs, axis=0), b._state[dl.id], patch


def test_the_pingpong_example_locks_to_the_dotted_eighth():
    # delay_dub_echo's seven modules: the same 375 ms, now derived from
    # the 8th-note clock (division 1, dotted) instead of set by ear.
    y, st, patch = _render_example()
    assert len(patch.modules) == 7
    assert y.shape[1] == 2 and 0.1 < float(np.abs(y).max()) < 0.8
    assert st["ck_sync"] is True
    assert abs(st["ck_cur"] - 16537.5) <= 1.0              # 375 ms
    assert float(np.abs(y[:, 0] - y[:, 1]).max()) > 0.05   # really stereo


def test_the_pingpong_examples_repeats_alternate_left_right():
    # Count them: one note (the bar's first, at 2 s, after the lock) and
    # its first four repeats -- L, R, L, R, the far side 40 dB down or
    # more (measured ~300x). The fifth meets the next bar's note.
    y, st, _patch = _render_example(one_note=True)
    T = int(st["ck_cur"])
    L, R = y[:, 0].astype(np.float64), y[:, 1].astype(np.float64)
    t0 = 2 * SR
    sides = []
    for k in range(1, 5):
        w = slice(t0 + k * T, t0 + k * T + int(0.2 * SR))
        el, er = np.sqrt(np.mean(L[w] ** 2)), np.sqrt(np.mean(R[w] ** 2))
        assert max(el, er) > 1e-3, k
        assert max(el, er) > 30 * min(el, er), (k, el, er)
        sides.append("L" if el > er else "R")
    assert sides == ["L", "R", "L", "R"]
