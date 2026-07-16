"""The buffered sink's patchable ring governor — fill cv out, ratio_cv in.

Covers the three engine pieces that make a feedback governor patch legal
and useful:

  * the topological sort ignores cables leaving the sink's delayed
    ``fill`` port, so a fill -> controller -> ratio_cv loop doesn't drop
    the controller chain into arbitrary leftover order;
  * ``fill`` is seeded into the buffer store every block (neutral 0.5
    with no live stream), one block delayed, visible to the CV meters;
  * a cabled ``ratio_cv`` varispeed-resamples the block pushed to the
    sink's secondary stream (smoothed, clamped), while an unpatched
    ratio_cv leaves the push bit-identical to the pre-governor engine.

No audio hardware is touched: everything runs through render_block_multi
on an uncompiled-stream backend, same as the routing tests in
test_buffered_specific_speaker.py.
"""
from __future__ import annotations

import numpy as np

from pysynthrack.core.patch import Patch
from pysynthrack.audio.numpy_backend import NumpyBackend

# Importing the module files registers their types with the Patch factory.
from pysynthrack.modules import constant as _constant  # noqa: F401
from pysynthrack.modules import lfo as _lfo  # noqa: F401
from pysynthrack.modules import oscillator as _osc  # noqa: F401
from pysynthrack.modules import output as _output  # noqa: F401

SINK = "buffered_specific_speaker_output"


# ----- topological sort with a feedback patch --------------------------------


class TestDelayedEdgeTopoSort:
    def _loop_patch(self):
        """sink.fill -> lfoA -> lfoB -> sink.ratio_cv, built in an
        adversarial creation order (downstream lfoB added BEFORE its
        upstream lfoA) so the old leftover-append order would run the
        controllers backwards."""
        patch = Patch()
        sink = patch.add_module(SINK)
        lfo_b = patch.add_module("lfo")   # downstream, created first
        lfo_a = patch.add_module("lfo")   # upstream, created second
        patch.connect(sink.id, "fill", lfo_a.id, "rate_cv")
        patch.connect(lfo_a.id, "cv", lfo_b.id, "rate_cv")
        patch.connect(lfo_b.id, "cv", sink.id, "ratio_cv")
        return patch, sink, lfo_a, lfo_b

    def test_controller_chain_orders_upstream_first(self):
        patch, _sink, lfo_a, lfo_b = self._loop_patch()
        order = NumpyBackend._topological_sort(patch)
        assert order.index(lfo_a.id) < order.index(lfo_b.id)

    def test_every_module_ordered_exactly_once(self):
        patch, *_ = self._loop_patch()
        order = NumpyBackend._topological_sort(patch)
        assert sorted(order) == sorted(patch.modules)

    def test_sink_orders_after_its_governor(self):
        # ratio_cv is a REAL within-block dependency (only fill is
        # delayed), so the sink sorts after the controller that feeds it.
        patch, sink, _lfo_a, lfo_b = self._loop_patch()
        order = NumpyBackend._topological_sort(patch)
        assert order.index(lfo_b.id) < order.index(sink.id)

    def test_acyclic_patches_unaffected(self):
        # No feedback: plain source -> sink still sorts source-first.
        patch = Patch()
        sink = patch.add_module(SINK)
        osc = patch.add_module("oscillator")
        patch.connect(osc.id, "out", sink.id, "in_l")
        order = NumpyBackend._topological_sort(patch)
        assert order.index(osc.id) < order.index(sink.id)
