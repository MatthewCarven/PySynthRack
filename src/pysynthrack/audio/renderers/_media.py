"""Media helpers: finding and decoding the audio files a patch names.

``_resolve_media_path`` (patch-relative, then the bundled resources),
``media_load_failures`` (the UI's "what didn't load" list),
``_decode_audio`` and ``_load_wav``, moved verbatim out of
``numpy_backend.py`` (2026-09-28) into a mixin ``NumpyBackend``
inherits. The engine's ``compile``, the file player / disk writer
(``io``), the sampler (``physical``) and the convolver (``spectral``)
reach them through ``self``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

from ..._resources import resource_root
from .. import media


class MediaHelpers:
    # ----- media path resolution ------------------------------------------

    def _resolve_media_path(self, path) -> str:
        """Turn a patch's media ``path`` param into one that actually opens.

        Patches store media paths (a sampler's sample, a convolver's IR, a
        file player's track) as written by whoever made them, which in
        practice means RELATIVE -- and until 2026-08-29 a relative path was
        resolved against the process working directory alone. That made a
        patch's audio depend on where the app happened to be launched from:
        `examples/sampler_breaks.json` played perfectly from the project
        root and rendered pure silence from anywhere else, including from
        `dist/`. Silence, not an error, because every loader here fails
        soft so the audio thread never raises.

        So a relative path is now tried against an ordered list of bases
        and the FIRST ONE THAT EXISTS wins:

          1. the patch's own folder -- the DAW convention, and what makes a
             patch plus its samples portable as a unit;
          2. that folder's parent -- because the shipped examples live in
             ``examples/`` while naming their media from the project root
             (``examples/samples/breaks.wav``), and rewriting them would
             break every patch a user has already saved in that style;
          3. the process working directory -- the historical behaviour, so
             nothing that worked before stops working;
          4. the resource root -- the install/bundle directory, which is
             where the examples and their media live in a frozen build.

        An ABSOLUTE path is returned untouched: the user named an exact
        file and second-guessing them would be worse than failing. An empty
        path stays empty (an unpatched slot, not an error). If nothing
        matches, the original string comes back unchanged so the failure
        message names what the patch actually asked for.

        Results are cached per compile generation. That is not (only) about
        the stat calls -- the renderers use this string as the cache key
        for "is the loaded buffer still the right one", so it MUST be
        stable within a compile or they would reload forever. The cache is
        dropped on every ``compile()``, so moving a missing file into place
        and hitting recompile finds it.
        """
        raw = str(path or "")
        if not raw:
            return ""
        key = (raw, self._patch_dir)
        hit = self._media_path_cache.get(key)
        if hit is not None:
            return hit

        resolved = raw
        candidate = Path(raw)
        if not candidate.is_absolute():
            bases: list[Path] = []
            if self._patch_dir:
                patch_dir = Path(self._patch_dir)
                bases.append(patch_dir)
                bases.append(patch_dir.parent)
            bases.append(Path.cwd())
            try:
                bases.append(resource_root())
            except Exception:  # pragma: no cover - defensive; never fatal
                pass
            for base in bases:
                try:
                    found = base / candidate
                    if found.is_file():
                        resolved = str(found)
                        break
                except (OSError, ValueError):  # pragma: no cover - odd paths
                    continue

        self._media_path_cache[key] = resolved
        return resolved

    def media_load_failures(self) -> list[tuple[int, str, str]]:
        """UI hook: every module whose media file did not load.

        Returns ``(module_id, module_type, path_as_written)`` for each
        ``sampler`` / ``convolver`` whose background load has finished
        and come back empty -- a missing file, an unreadable one, or an
        encoding nothing here can decode.

        This exists because the loaders all fail SOFT: they must, or a
        typo'd path would raise on the audio thread. The cost of that
        choice is that a missing sample is indistinguishable from silence
        unless someone asks, and on 2026-08-29 it cost a listening pass --
        `sampler_breaks.json` played nothing and looked perfectly healthy
        doing it. So the GUI asks, once per failure, and says so.

        The reported path is the one WRITTEN IN THE PATCH, not the
        resolved one: it is what the user typed and what they will go
        looking for. Resolution having failed is precisely the news.

        ``file_player`` is deliberately not included -- it has its own
        per-module hook (:meth:`file_player_failed`) that the playlist
        advancer already uses to skip a dud track, and folding it in here
        would report every skipped file twice.
        """
        patch = self._patch
        if patch is None:
            return []
        out: list[tuple[int, str, str]] = []
        for mid, module in patch.modules.items():
            if module.TYPE not in ("sampler", "convolver"):
                continue
            state = self._state.get(mid)
            if not state:
                continue
            # A load that has RESOLVED (loaded_path set, nothing pending)
            # but produced no audio is a failure. Still-pending or never
            # attempted is not news yet.
            if state.get("pending") is not None or not state.get("loaded_path"):
                continue
            loaded = (
                state.get("samples") if module.TYPE == "sampler"
                else state.get("ir_l")
            )
            if loaded is None:
                out.append((mid, module.TYPE, str(module.params.get("path", ""))))
        return out

    # ----- audio file decoding --------------------------------------------

    def _decode_audio(self, path, target_sr):
        """Decode any supported media file to ``(2, N)`` float32 or None.

        WAV takes the zero-dependency scipy fast path. Anything that
        isn't a readable WAV — mp3/flac/ogg/m4a, the audio track of a
        video (mp4/mkv/mov/webm), or even a 24-bit WAV scipy can't
        open — falls back to ffmpeg when it's available (bundled via
        the ``[media]`` extra, or a system ffmpeg). ``None`` on total
        failure, so the player renders silence rather than raising.
        """
        samples = self._load_wav(path, target_sr)
        if samples is not None:
            return samples
        return media.decode_with_ffmpeg(path, target_sr)

    @staticmethod
    def _load_wav(path, target_sr):
        """Decode a WAV file to a contiguous ``(2, N)`` float32 array.

        Returns ``None`` on any failure (empty/missing path, unreadable or
        unsupported encoding) so the audio thread renders silence rather
        than raising. Integer PCM is normalised to [-1, 1] by dtype; mono is
        duplicated to stereo; >2 channels keep the first two; the audio is
        resampled to ``target_sr`` when the file's native rate differs (a
        one-time cost at load, not per block). 24-bit PCM is unsupported by
        scipy and surfaces here as a caught read error -> silence.
        """
        import os

        if not path or not os.path.isfile(path):
            return None
        try:
            file_sr, data = wavfile.read(path)
        except Exception as exc:  # pragma: no cover - filesystem/codec-specific
            print(f"[FilePlayer] cannot read {path}: {exc}")
            return None

        data = np.asarray(data)
        if data.dtype == np.int16:
            flo = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            flo = data.astype(np.float32) / 2147483648.0
        elif data.dtype == np.uint8:
            flo = (data.astype(np.float32) - 128.0) / 128.0
        elif data.dtype in (np.float32, np.float64):
            flo = data.astype(np.float32)
        else:  # pragma: no cover - exotic dtype; best-effort peak-normalise
            flo = data.astype(np.float32)
            peak = float(np.max(np.abs(flo))) or 1.0
            flo = flo / peak

        # -> (channels, N)
        chans = flo[np.newaxis, :] if flo.ndim == 1 else flo.T

        if int(file_sr) != int(target_sr) and chans.shape[1] > 0:
            from math import gcd
            g = gcd(int(file_sr), int(target_sr))
            up = int(target_sr) // g
            down = int(file_sr) // g
            chans = resample_poly(chans, up, down, axis=1).astype(np.float32)

        if chans.shape[0] == 1:
            stereo = np.repeat(chans, 2, axis=0)
        elif chans.shape[0] >= 2:
            stereo = chans[:2]
        else:
            return None
        return np.ascontiguousarray(stereo, dtype=np.float32)
