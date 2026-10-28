import pytest
from pydantic import ValidationError

from radio247.settings import Settings


def test_old_env_prefix_requires_migration(monkeypatch):
    monkeypatch.setenv("AIRADIO_WEB_PORT", "8123")
    with pytest.raises(ValueError, match="rename them to RADIO247_"):
        Settings.from_env()


def test_env_overrides_and_validation(monkeypatch):
    monkeypatch.setenv("RADIO247_SAMPLE_RATE", "48000")
    monkeypatch.setenv("RADIO247_AUTOSTART", "false")
    monkeypatch.setenv("RADIO247_WIDTH", "640")
    monkeypatch.setenv("RADIO247_FPS", "1")
    monkeypatch.setenv("RADIO247_ZIP_MAX_FILES", "25")
    monkeypatch.setenv("RADIO247_ZIP_MAX_BYTES", "2048")
    monkeypatch.setenv("RADIO247_DEFAULT_IMAGE", "/images/default.png")
    monkeypatch.setenv("RADIO247_DEFAULT_IMAGE_SECONDS", "12.5")
    monkeypatch.setenv("RADIO247_DEFAULT_IMAGE_POSITION", "start")
    settings = Settings.from_env()
    assert settings.sample_rate == 48000
    assert settings.autostart is False
    assert settings.width == 640
    assert settings.fps == 1
    assert settings.zip_max_files == 25
    assert settings.zip_max_bytes == 2048
    assert str(settings.default_image) == "/images/default.png"
    assert settings.default_image_seconds == 12.5
    assert settings.default_image_position == "start"
    monkeypatch.setenv("RADIO247_WEB_PORT", "8123")
    monkeypatch.delenv("RADIO247_INTERNAL_API_URL", raising=False)
    assert Settings.from_env().internal_api_url == "http://127.0.0.1:8123"
    monkeypatch.setenv("RADIO247_WIDTH", "641")
    with pytest.raises(ValidationError, match="even"):
        Settings.from_env()


@pytest.mark.parametrize(
    "values",
    [
        {"sample_rate": 1},
        {"fps": 0},
        {"ingest_url": "https://example.invalid/live"},
        {"retry_initial": 30, "retry_max": 2},
        {"channels": 6},
        {"target_peak": 0},
        {"zip_max_files": 0},
        {"zip_max_bytes": -1},
        {"default_image_seconds": -1},
        {"default_image_seconds": float("nan")},
        {"default_image_seconds": float("inf")},
        {"default_image_position": "middle"},
    ],
)
def test_reject_invalid_settings(values):
    with pytest.raises(ValidationError):
        Settings(**values)


@pytest.mark.parametrize("name", ["RADIO247_TRACK_INTRO_SECONDS", "RADIO247_TRACK_OUTRO_SECONDS"])
def test_old_picture_timing_env_requires_migration(monkeypatch, name):
    monkeypatch.setenv(name, "10")
    with pytest.raises(ValueError, match="use RADIO247_DEFAULT_IMAGE_SECONDS"):
        Settings.from_env()


def test_default_youtube_and_explicit_local_mode(monkeypatch):
    monkeypatch.delenv("RADIO247_INGEST_URL", raising=False)
    monkeypatch.delenv("RADIO247_STREAM_KEY", raising=False)
    assert Settings.from_env().ingest_url == "rtmps://a.rtmps.youtube.com/live2"
    monkeypatch.setenv("RADIO247_INGEST_URL", "")
    assert Settings.from_env().ingest_url == ""


def test_stream_key_env_is_loaded_without_exposing_it(monkeypatch):
    key = "test-only-env-stream-key"
    monkeypatch.setenv("RADIO247_STREAM_KEY", key)
    settings = Settings.from_env()
    assert settings.stream_key.get_secret_value() == key
    assert key not in repr(settings)
    assert key not in str(settings.model_dump())
    assert key not in settings.model_dump_json()


@pytest.mark.parametrize("key", [None, "", "  ", "short"])
def test_invalid_stream_key_does_not_prevent_settings_loading(monkeypatch, tmp_path, key):
    key_file = tmp_path / "legacy-key"
    key_file.write_text("legacy-test-stream-key")
    monkeypatch.setenv("RADIO247_STREAM_KEY_FILE", str(key_file))
    if key is None:
        monkeypatch.delenv("RADIO247_STREAM_KEY", raising=False)
    else:
        monkeypatch.setenv("RADIO247_STREAM_KEY", key)
    settings = Settings.from_env()
    assert settings.stream_key.get_secret_value() == (key or "")
