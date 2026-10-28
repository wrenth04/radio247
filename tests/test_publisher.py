import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from radio247.publisher import Publisher


def test_stuck_publisher_is_reaped_and_backed_off(settings):
    publisher = Publisher(settings)
    process = publisher.context.Process(target=time.sleep, args=(30,))
    process.start()
    publisher.process = process
    publisher.started = time.monotonic() - settings.connect_timeout - settings.gop_seconds - 1
    started = time.monotonic()
    try:
        publisher.tick(Path("unused.mp4"), b"unused")
        assert time.monotonic() - started < 3
        assert publisher.process is None
        assert publisher.state == "reconnecting"
        assert publisher.reconnects == 1
        assert publisher.next_attempt > time.monotonic()
        assert publisher.error == "Connection timed out"
        publisher.tick(Path("unused.mp4"), b"unused")
        assert publisher.process is None
    finally:
        publisher.close()


def test_local_output_retention_only_removes_owned_segments(settings):
    publisher = Publisher(settings.model_copy(update={"local_keep_segments": 2}))
    unrelated = settings.output_dir / "keep.flv"
    unrelated.write_bytes(b"keep")
    for name in ("radio-001.flv", "radio-002.flv"):
        (settings.output_dir / name).write_bytes(b"old")
    destination = Path(publisher.destination())
    assert destination.parent == settings.output_dir
    assert not (settings.output_dir / "radio-001.flv").exists()
    assert (settings.output_dir / "radio-002.flv").exists()
    assert unrelated.exists()


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("", "RADIO247_STREAM_KEY is missing"),
        (" \n ", "RADIO247_STREAM_KEY is missing"),
        ("test-short", "RADIO247_STREAM_KEY must contain at least 12 characters"),
    ],
)
def test_invalid_stream_key_does_not_fall_back_to_local(settings, caplog, key, message):
    publisher = Publisher(
        settings.model_copy(
            update={
                "ingest_url": "rtmps://a.rtmps.youtube.com/live2",
                "stream_key": SecretStr(key),
            }
        )
    )
    try:
        publisher.tick(Path("unused.mp4"), b"unused")
        assert publisher.process is None
        assert publisher.state == "reconnecting"
        assert publisher.error == message
        assert message in caplog.text
        if key.strip():
            assert key not in caplog.text
        assert publisher.next_attempt > time.monotonic()
        assert not list(settings.output_dir.iterdir())
    finally:
        publisher.close()


@pytest.mark.parametrize("scheme", ["rtmp", "rtmps"])
def test_env_key_destination_strips_surrounding_whitespace(settings, scheme):
    publisher = Publisher(
        settings.model_copy(
            update={
                "ingest_url": f"{scheme}://example.invalid/live/",
                "stream_key": SecretStr(" \n test-only-env-stream-key \n"),
            }
        )
    )
    assert publisher.destination() == f"{scheme}://example.invalid/live/test-only-env-stream-key"


def test_local_output_ignores_invalid_stream_key(settings):
    publisher = Publisher(settings.model_copy(update={"stream_key": SecretStr("short")}))
    assert Path(publisher.destination()).parent == settings.output_dir
