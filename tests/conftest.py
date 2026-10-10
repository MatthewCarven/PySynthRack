"""Suite-wide fixtures.

Recordings: a ``disk_writer`` with a relative ``path`` records into the
user's Music folder (``audio/recordings.py``). The examples sweep renders
``record_a_take.json`` and ``mic_karaoke_recorder.json``, both armed with
relative names, so without this every test run would leave takes in the
real ``<Music>/PySynthRack`` -- the old cwd rule is how ``take_01.wav``
ended up in the repo root. Point the whole folder at a temp dir instead.
"""
from __future__ import annotations

import os

import pytest

from pysynthrack.audio.recordings import RECORDINGS_ENV


@pytest.fixture(autouse=True, scope="session")
def _recordings_into_a_temp_dir(tmp_path_factory):
    folder = tmp_path_factory.mktemp("recordings")
    previous = os.environ.get(RECORDINGS_ENV)
    os.environ[RECORDINGS_ENV] = str(folder)
    yield folder
    if previous is None:
        os.environ.pop(RECORDINGS_ENV, None)
    else:
        os.environ[RECORDINGS_ENV] = previous
