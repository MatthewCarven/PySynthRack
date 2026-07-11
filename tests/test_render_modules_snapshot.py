"""Regression tests for the audio-callback 'dictionary changed size' crash.

``render_block_multi`` walks ``patch.modules`` while the *GUI thread* may be
adding or removing a module in place (add/delete a node). Iterating the live
dict raised ``RuntimeError: dictionary changed size during iteration`` inside
the audio callback. The fix snapshots the module map atomically under the lock
and iterates the snapshot; these tests pin that a concurrent edit mid-render no
longer crashes.
"""
from __future__ import annotations

import threading

import numpy as np

import pysynthrack.modules  # noqa: F401  (registers module types)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.patch import Patch

SR = 44100
FRAMES = 256


def _compiled_backend(patch):
    be = NumpyBackend(sample_rate=SR, block_size=FRAMES)
    be.compile(patch)
    return be


class _MutatingChannels:
    """Wraps the backend's ``_SPEAKER_CHANNELS`` map so the first lookup in
    the second render loop deletes a module from the *live* patch — the exact
    concurrent mutation the GUI thread would make, but deterministic."""

    def __init__(self, real, patch, victim_id):
        self._real = real
        self._patch = patch
        self._victim = victim_id
        self.fired = False

    # Membership / iteration / indexing just delegate (used by the first
    # render loop) — only .get, called in the second loop, fires the mutation.
    def __contains__(self, key):
        return key in self._real

    def __iter__(self):
        return iter(self._real)

    def __getitem__(self, key):
        return self._real[key]

    def get(self, key, default=None):
        if not self.fired:
            self.fired = True
            self._patch.modules.pop(self._victim, None)
        return self._real.get(key, default)


def test_delete_during_second_loop_does_not_crash():
    # osc1 -> speaker; osc2 present as a deletable middle element.
    patch = Patch()
    osc1 = patch.add_module("oscillator")
    osc2 = patch.add_module("oscillator")
    spk = patch.add_module("speaker_output")
    patch.connect(osc1.id, "out", spk.id, "in")
    be = _compiled_backend(patch)

    # Make the very first speaker-channel lookup (module osc1 in the second
    # loop) delete osc2 from patch.modules mid-iteration.
    be._SPEAKER_CHANNELS = _MutatingChannels(
        be._SPEAKER_CHANNELS, patch, osc2.id
    )

    out, device_blocks = be.render_block_multi(FRAMES)  # must not raise

    assert be._SPEAKER_CHANNELS.fired
    assert osc2.id not in patch.modules  # the mutation really happened
    assert out is not None and out.shape == (FRAMES, 2)


def test_render_uses_snapshot_not_live_dict():
    # A module removed after the snapshot is taken is still rendered this
    # block (proof the loop reads a snapshot, not the live dict).
    patch = Patch()
    osc = patch.add_module("oscillator")
    spk = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", spk.id, "in")
    be = _compiled_backend(patch)

    seen = []
    real_render = be._render_module

    def spy(module, frames, buffers, p):
        seen.append(module.id)
        # Drop a module from the live dict as soon as rendering starts; the
        # in-flight block must still complete over its snapshot.
        patch.modules.pop(osc.id, None)
        return real_render(module, frames, buffers, p)

    be._render_module = spy
    out, _ = be.render_block_multi(FRAMES)  # must not raise
    assert out.shape == (FRAMES, 2)
    assert seen  # rendering ran


def test_concurrent_add_remove_while_rendering():
    """Stress: hammer add/remove on one thread while another renders. Post-fix
    the snapshot makes a mid-render size change structurally impossible to
    raise, so this passes deterministically."""
    patch = Patch()
    osc = patch.add_module("oscillator")
    spk = patch.add_module("speaker_output")
    patch.connect(osc.id, "out", spk.id, "in")
    be = _compiled_backend(patch)

    errors: list[BaseException] = []
    stop = threading.Event()

    def churn():
        try:
            while not stop.is_set():
                m = patch.add_module("oscillator")
                patch.modules.pop(m.id, None)
        except BaseException as exc:  # noqa: BLE001 — report, don't swallow
            errors.append(exc)

    writer = threading.Thread(target=churn)
    writer.start()
    try:
        for _ in range(400):
            be.render_block_multi(FRAMES)  # must never raise
    finally:
        stop.set()
        writer.join()

    assert errors == []
