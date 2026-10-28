import hashlib
import sqlite3
import time
import wave
from pathlib import Path

import av
import numpy as np
import pytest
from PIL import Image, ImageOps

from radio247.audio import AudioSource, decode_source
from radio247.library import Library, Store
from radio247.media import command
from radio247.radio import Playout


def make_song(path, rate=44100, seconds=1):
    samples = np.sin(np.arange(rate * seconds) * 2 * np.pi * 440 / rate) * 0.2
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes((samples * 32767).astype("<i2").tobytes())


def test_progressive_ready_failure_isolation_and_playout(settings):
    make_song(settings.media_dir / "song.wav")
    (settings.media_dir / "broken.mp3").write_bytes(b"not audio")
    store = Store(settings)
    library = Library(settings, store)
    library.default_video = library.picture(None)
    library.scan()
    library.scan()
    snapshot = store.snapshot(library.default_video)
    assert len(snapshot.tracks) == 1
    assert snapshot.tracks[0].title == "song"
    assert {track["state"] for track in store.tracks()} == {"ready", "failed"}
    assert not list(settings.cache_dir.glob("*.preparing.*"))
    player = Playout(settings)
    try:
        blocks = []
        for _ in range(140):
            blocks.append(player.read(1024, snapshot.tracks))
            time.sleep(1024 / settings.sample_rate)
        block = np.concatenate(blocks)
        assert player.current is not None
        assert player.current.id == snapshot.tracks[0].id
    finally:
        player.close()
    assert np.isfinite(block).all()
    assert np.max(np.abs(block)) > 0.01
    assert np.max(np.abs(block)) <= 10 ** (settings.target_peak / 20) + 1e-6
    assert not list(settings.cache_dir.glob("*.pcm"))
    assert list(settings.cache_dir.glob("*.json"))
    assert np.max(np.abs(np.diff(block[:, 0]))) < 0.1
    store.control("stop")
    library.scan()
    assert store.snapshot(library.default_video).enabled is False
    store.control("skip")
    assert store.snapshot(library.default_video).skip == 1


def test_cache_quota_does_not_publish_partial_audio(settings):
    settings = settings.model_copy(update={"cache_max_bytes": 1000000})
    make_song(settings.media_dir / "song.wav")
    store = Store(settings)
    library = Library(settings, store)
    library.default_video = library.picture(None)
    (settings.cache_dir / "occupied.bin").write_bytes(
        b"\0" * (settings.cache_max_bytes - library.usage() - 8)
    )
    library.scan()
    library.scan()
    assert not store.snapshot(library.default_video).tracks
    assert "quota" in store.tracks()[0]["error"]
    assert not list(settings.cache_dir.glob("*.preparing.*"))
    assert not list(settings.cache_dir.glob("*.pcm"))


def test_invalid_picture_not_ready(settings):
    make_song(settings.media_dir / "song.wav")
    (settings.images_dir / "song.png").write_bytes(b"invalid image")
    store = Store(settings)
    library = Library(settings, store)
    library.default_video = library.picture(None)
    library.scan()
    library.scan()
    assert not store.snapshot(library.default_video).tracks
    assert store.tracks()[0]["state"] == "failed"


def test_secret_is_persistent_and_rejects_short_values(tmp_path):
    from radio247.library import secret

    path = tmp_path / "secret"
    value = secret(path, create=True)
    assert secret(path, create=True) == value
    path.write_text("short")
    with pytest.raises(ValueError):
        secret(path)


def test_format_change_invalidates_ready_snapshot_before_scan(settings):
    make_song(settings.media_dir / "song.wav")
    store = Store(settings)
    library = Library(settings, store)
    library.default_video = library.picture(None)
    library.scan()
    library.scan()
    assert store.snapshot(library.default_video).tracks
    changed = settings.model_copy(update={"sample_rate": 48000, "channels": 1})
    Library(changed, store)
    assert not store.snapshot(None).tracks


def test_cover_update_rebuilds_letterboxed_video_without_reanalyzing_audio(settings, monkeypatch):
    image = settings.images_dir / "song.png"
    Image.new("RGB", (90, 180), "red").save(image)
    make_song(settings.media_dir / "song.wav")
    store = Store(settings)
    legacy = Library(settings, store)
    legacy.picture_version = legacy.version

    def letterbox(source, target, settings):
        with Image.open(source) as original:
            ImageOps.pad(original, (settings.width, settings.height), color="black").save(target)

    with monkeypatch.context() as old:
        old.setattr("radio247.library.prepare_image", letterbox)
        legacy.default_video = legacy.picture(image)
        legacy.scan()
        legacy.scan()
    previous = store.snapshot(legacy.default_video).tracks[0]
    old_video = Path(previous.video)
    old_audio = Path(previous.audio)
    audio_stamp = old_audio.stat().st_mtime_ns
    with store.connect() as db:
        db.execute(
            "UPDATE metadata SET value=? WHERE key='processing'",
            (hashlib.sha256(legacy.version + b":streamed-audio-v1").hexdigest(),),
        )
    with av.open(str(old_video)) as cached:
        assert next(cached.decode(video=0)).to_ndarray(format="rgb24")[0, 0, 0] < 10

    def unexpected_analysis(*args):
        pytest.fail("Image-only changes must reuse audio analysis")

    monkeypatch.setattr("radio247.library.analyze", unexpected_analysis)
    updated = Library(settings, store)
    assert not store.snapshot(None).tracks
    updated.default_video = updated.picture(image)
    updated.scan()
    updated.scan()
    current = store.snapshot(updated.default_video).tracks[0]
    assert current.video != previous.video
    assert current.video == str(updated.default_video)
    assert current.audio == previous.audio
    assert old_audio.stat().st_mtime_ns == audio_stamp
    assert old_video.exists()
    with av.open(current.video) as cached:
        pixels = next(cached.decode(video=0)).to_ndarray(format="rgb24")
        assert np.all(pixels[:, :, 0] > 200)


@pytest.mark.parametrize("codec", ["aac", "alac"])
def test_m4a_is_prepared_with_reusable_picture_cache(settings, tmp_path, codec):
    original = tmp_path / "source.wav"
    make_song(original)
    command(
        [
            settings.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(original),
            "-c:a",
            codec,
            str(settings.media_dir / "song.m4a"),
        ]
    )
    store = Store(settings)
    library = Library(settings, store)
    library.default_video = library.picture(None)
    modified = library.default_video.stat().st_mtime_ns
    library.scan()
    library.scan()
    tracks = store.snapshot(library.default_video).tracks
    assert len(tracks) == 1
    assert tracks[0].samples > 0
    assert tracks[0].video.endswith(".mp4")
    metadata = AudioSource.model_validate_json(
        (settings.cache_dir / tracks[0].audio.split("/")[-1]).read_text()
    )
    decoded = np.concatenate(list(decode_source(metadata, settings, lambda: False)))
    assert decoded.shape[1] == settings.channels
    assert len(decoded) >= settings.sample_rate
    assert np.isfinite(decoded).all()
    assert not list(settings.cache_dir.glob("*.pcm"))
    assert library.picture(None).stat().st_mtime_ns == modified


def test_legacy_pcm_database_migrates_without_deleting_data(settings):
    legacy_pcm = settings.cache_dir / "legacy.pcm"
    legacy_pcm.write_bytes(b"old cache")
    make_song(settings.media_dir / "song.wav")
    with sqlite3.connect(settings.data_dir / "radio.sqlite3") as db:
        db.executescript(
            "CREATE TABLE control (id INTEGER PRIMARY KEY, enabled INTEGER, skip INTEGER);"
            "INSERT INTO control VALUES (1,0,0);"
            "CREATE TABLE tracks (id TEXT PRIMARY KEY,title TEXT,fingerprint TEXT,state TEXT,"
            "error TEXT,pcm TEXT,video TEXT,samples INTEGER,active INTEGER DEFAULT 1);"
            "INSERT INTO tracks(id,title,fingerprint,state) VALUES('old','song','old','ready');"
        )
    store = Store(settings)
    assert not store.snapshot(None).tracks
    library = Library(settings, store)
    library.default_video = library.picture(None)
    library.scan()
    library.scan()
    snapshot = store.snapshot(library.default_video)
    assert not snapshot.enabled
    assert len(snapshot.tracks) == 1
    assert snapshot.tracks[0].audio.endswith(".json")
    assert legacy_pcm.read_bytes() == b"old cache"
