"""``_render_module``'s type -> renderer table agrees with the module registry.

Every registered module type either has a ``_RENDERERS`` entry naming a
renderer the backend really has, or is a speaker-family sink (drained by
the speaker pass, so it renders nothing here). A new module that forgets
its table entry would otherwise render silence without an error.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pysynthrack.modules  # noqa: F401  (registers every module type)
from pysynthrack.audio.numpy_backend import NumpyBackend
from pysynthrack.core.module import all_module_types

SPEAKERS = set(NumpyBackend._SPEAKER_CHANNELS) | set(NumpyBackend._STEREO_SPEAKERS)


def test_every_registered_type_renders_or_is_a_speaker():
    registered = set(all_module_types())
    table = set(NumpyBackend._RENDERERS)
    assert registered - table - SPEAKERS == set()
    assert table - registered == set()
    assert table & SPEAKERS == set()


def test_every_entry_names_a_renderer_with_the_right_signature():
    assert NumpyBackend._FRAMES_ONLY <= set(NumpyBackend._RENDERERS)
    for ty, name in NumpyBackend._RENDERERS.items():
        fn = getattr(NumpyBackend, name)
        sig = inspect.signature(fn)
        if ty in NumpyBackend._FRAMES_ONLY:
            # Exactly (self, module, frames): a renderer that also takes
            # buffers / patch (even defaulted) would silently get none.
            assert list(sig.parameters) == ["self", "module", "frames"], name
        else:
            sig.bind(None, "module", "frames", "buffers", "patch")


def test_speakers_and_unknown_types_render_nothing():
    backend = NumpyBackend()
    for ty in sorted(SPEAKERS) + ["no_such_module"]:
        assert backend._render_module(SimpleNamespace(TYPE=ty), 64, {}, None) is None


def test_dispatch_passes_the_right_arguments(monkeypatch):
    backend = NumpyBackend()
    seen = {}
    monkeypatch.setattr(backend, "_render_keyboard", lambda *a: seen.setdefault("kb", a))
    monkeypatch.setattr(backend, "_render_noise", lambda *a: seen.setdefault("nz", a))
    kb, nz = SimpleNamespace(TYPE="keyboard"), SimpleNamespace(TYPE="noise")
    backend._render_module(kb, 64, {"b": 1}, "patch")
    backend._render_module(nz, 64, {"b": 1}, "patch")
    assert seen == {"kb": (kb, 64), "nz": (nz, 64, {"b": 1}, "patch")}
