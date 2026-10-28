import subprocess
import sys
import time

import numpy as np
import pytest
from test_library import make_song

from radio247.audio import AudioSource, decode_source
from radio247.library import Track
from radio247.playout import AudioBlock, Playout


def source_metadata(settings, name):
    path = settings.media_dir / f"{name}.m4a"
    path.write_bytes(b"test source")
    stamp = path.stat()
    source = AudioSource(
        source=path,
        filters="anull",
        samples=settings.sample_rate * 60,
        size=stamp.st_size,
        mtime_ns=stamp.st_mtime_ns,
    )
    metadata = settings.cache_dir / f"{name}.json"
    metadata.write_text(source.model_dump_json())
    return source, Track(
        id=name, title=name, audio=str(metadata), video="unused.mp4", samples=source.samples
    )


@pytest.mark.parametrize("position", ["start", "end"])
def test_timed_picture_resets_for_repeats_and_new_tracks(settings, position):
    settings = settings.model_copy(
        update={
            "default_image": settings.images_dir / "default.png",
            "default_image_seconds": 0.5,
            "default_image_position": position,
        }
    )
    first = source_metadata(settings, "first")[1]
    second = source_metadata(settings, "second")[1]
    default = settings.cache_dir / "default.mp4"
    player = Playout(settings)
    player.close()
    assert player.picture(str(default)) == default
    length = settings.sample_rate * 2
    threshold = (
        settings.sample_rate // 2 if position == "start" else length - settings.sample_rate // 2
    )
    for track in (first, first, second):
        track = track.model_copy(update={"samples": length})
        player.queue.put(
            AudioBlock(
                player.generation,
                track,
                0,
                np.zeros((length, settings.channels), dtype="float32"),
            )
        )
        player.read(threshold - 1, [track])
        before = str(default) if position == "start" else track.video
        after = track.video if position == "start" else str(default)
        assert str(player.picture(str(default))) == before
        player.read(1, [track])
        assert str(player.picture(str(default))) == after
        player.read(length - threshold, [track])
        assert str(player.picture(str(default))) == after


@pytest.mark.parametrize(
    "configured,seconds,expected",
    [(True, 30, "default.mp4"), (True, 0, "unused.mp4"), (False, 30, "unused.mp4")],
)
@pytest.mark.parametrize("position", ["start", "end"])
def test_short_track_timed_picture_and_disabled_modes(
    settings, configured, seconds, expected, position
):
    settings = settings.model_copy(
        update={
            "default_image": settings.images_dir / "default.png" if configured else None,
            "default_image_seconds": seconds,
            "default_image_position": position,
        }
    )
    track = source_metadata(settings, "short")[1].model_copy(update={"samples": 1024})
    player = Playout(settings)
    player.close()
    player.queue.put(
        AudioBlock(
            player.generation, track, 0, np.zeros((1024, settings.channels), dtype="float32")
        )
    )
    player.read(1, [track])
    assert player.picture("default.mp4").name == expected
    player.read(1023, [track])
    assert player.picture("default.mp4").name == expected


def test_playout_buffer_is_bounded_and_skip_reaches_next_track(settings, monkeypatch):
    _, first = source_metadata(settings, "first")
    _, second = source_metadata(settings, "second")

    def decode(source, settings, cancelled):
        for _ in range(10000):
            if cancelled():
                return
            yield np.full((1024, settings.channels), 0.1, dtype="float32")

    monkeypatch.setattr("radio247.playout.decode_source", decode)
    player = Playout(settings.model_copy(update={"decode_buffer_seconds": 0.1}))
    try:
        deadline = time.monotonic() + 5
        while player.current is None and time.monotonic() < deadline:
            player.read(1024, [first])
            time.sleep(0.01)
        assert player.current is not None and player.current.id == "first"
        time.sleep(0.1)
        assert player.queue.full()
        player.read(1024, [first, second])
        player.skip()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            samples = player.read(1024, [first, second])
            assert np.isfinite(samples).all()
            if player.current is not None and player.current.id == "second":
                break
            time.sleep(0.01)
        assert player.current is not None and player.current.id == "second"
    finally:
        player.close()
    assert not player.thread.is_alive()


def test_decoder_stall_is_bounded_and_child_is_reaped(settings, monkeypatch):
    source, _ = source_metadata(settings, "stalled")
    popen = subprocess.Popen
    children = []

    def stalled_process(*args, **kwargs):
        child = popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    monkeypatch.setattr("radio247.audio.subprocess.Popen", stalled_process)
    with pytest.raises(TimeoutError, match="no progress"):
        list(
            decode_source(source, settings.model_copy(update={"decode_timeout": 1}), lambda: False)
        )
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdout is not None
    assert children[0].stdout.closed


def test_changed_source_is_rejected_before_decoding(settings):
    source, _ = source_metadata(settings, "changed")
    source.source.write_bytes(b"replaced source")
    with pytest.raises(ValueError, match="source changed"):
        list(decode_source(source, settings, lambda: False))


def test_atomic_source_replacement_does_not_change_opened_audio(settings, monkeypatch):
    path = settings.media_dir / "original.wav"
    make_song(path)
    stamp = path.stat()
    source = AudioSource(
        source=path, filters="anull", samples=44100, size=stamp.st_size, mtime_ns=stamp.st_mtime_ns
    )
    replacement = settings.media_dir / "replacement"
    replacement.write_bytes(b"not an audio file")
    popen = subprocess.Popen

    def replace_before_child_opens_file(*args, **kwargs):
        replacement.replace(path)
        return popen(*args, **kwargs)

    monkeypatch.setattr("radio247.audio.subprocess.Popen", replace_before_child_opens_file)
    samples = np.concatenate(list(decode_source(source, settings, lambda: False)))
    assert len(samples) == 44100
    assert np.max(np.abs(samples)) > 0.1
    assert path.read_bytes() == b"not an audio file"


@pytest.mark.parametrize("already_playing", [False, True])
def test_specific_track_selection_and_replay(settings, monkeypatch, already_playing):
    tracks = [source_metadata(settings, name)[1] for name in ("first", "other", "selected")]

    def decode(source, settings, cancelled):
        while not cancelled():
            yield np.full((1024, settings.channels), 0.1, dtype="float32")

    monkeypatch.setattr("radio247.playout.decode_source", decode)
    player = Playout(settings.model_copy(update={"decode_buffer_seconds": 0.1}))

    def wait_until(check, available):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            player.read(1024, available)
            if check():
                return
            time.sleep(0.01)
        raise AssertionError("Selected track was not played")

    try:
        if already_playing:
            wait_until(lambda: player.current is not None, tracks[:1])
        assert player.play("selected", tracks)
        wait_until(lambda: player.current is not None and player.current.id == "selected", tracks)
        wait_until(lambda: player.position > 20000, tracks)
        old_generation = player.generation
        assert player.play("selected", tracks)
        wait_until(
            lambda: (
                player.generation > old_generation
                and player.current is not None
                and player.current.id == "selected"
                and player.position < 20000
            ),
            tracks,
        )
        assert not player.play("missing", tracks)
        assert player.current is not None and player.current.id == "selected"
        assert player.error == "Selected track is no longer ready"
    finally:
        player.close()
