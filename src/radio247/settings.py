from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ImagePosition = Literal["start", "end", "both", "middle", "interval", "always", "random", "none"]


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    media_dir: Path = Path("/media")
    images_dir: Path = Path("/images")
    cache_dir: Path = Path("/cache")
    data_dir: Path = Path("/data")
    output_dir: Path = Path("/output")
    runtime_dir: Path = Path("/run/radio247")
    default_image: Path | None = None
    default_image_position: ImagePosition = "end"
    default_image_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)
    default_image_interval: float = Field(default=120, ge=0, allow_inf_nan=False)
    admin_password_file: Path = Path("/data/secrets/admin-password")
    internal_token_file: Path = Path("/data/secrets/internal-token")
    stream_key: SecretStr = SecretStr("")
    ingest_url: str = "rtmps://a.rtmps.youtube.com/live2"
    internal_api_url: str = "http://127.0.0.1:8000"
    bind_host: str = "0.0.0.0"
    web_port: int = Field(default=8000, ge=1, le=65535)
    autostart: bool = True
    sample_rate: Literal[44100, 48000] = 44100
    channels: Literal[1, 2] = 2
    audio_bitrate: int = Field(default=192000, ge=64000, le=320000)
    target_lufs: float = Field(default=-16, ge=-36, le=-5)
    target_peak: float = Field(default=-2, ge=-12, le=-1)
    target_lra: float = Field(default=11, ge=1, le=20)
    fade_ms: int = Field(default=30, ge=1, le=2000)
    width: int = Field(default=1920, ge=160, le=3840)
    height: int = Field(default=1080, ge=90, le=2160)
    fps: Literal[1, 24, 25, 30, 50, 60] = 30
    gop_seconds: int = Field(default=2, ge=1, le=4)
    video_bitrate: int = Field(default=5000000, ge=100000, le=30000000)
    video_buffer_size: int = Field(default=10000000, ge=100000, le=60000000)
    video_preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"] = (
        "veryfast"
    )
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    threads: int = Field(default=1, ge=1, le=16)
    prepare_timeout: float = Field(default=3600, gt=0, le=86400)
    probe_timeout: float = Field(default=30, gt=0, le=300)
    scan_interval: float = Field(default=5, ge=0.1, le=3600)
    file_stable_seconds: float = Field(default=2, ge=0, le=3600)
    cache_max_bytes: int = Field(default=5000000000, ge=1000000)
    upload_max_bytes: int = Field(default=536870912, ge=1024)
    zip_max_bytes: int = Field(default=1073741824, ge=1024)
    zip_max_files: int = Field(default=1000, ge=1, le=100000)
    image_max_pixels: int = Field(default=20000000, ge=10000, le=100000000)
    poll_interval: float = Field(default=1, ge=0.1, le=30)
    request_timeout: float = Field(default=5, gt=0, le=60)
    connect_timeout: float = Field(default=15, ge=1, le=120)
    stall_timeout: float = Field(default=10, ge=1, le=120)
    retry_initial: float = Field(default=1, ge=0.1, le=60)
    retry_max: float = Field(default=30, ge=1, le=300)
    retry_reset_seconds: float = Field(default=60, ge=1, le=3600)
    retry_jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    queue_seconds: float = Field(default=3, ge=0.5, le=10)
    decode_buffer_seconds: float = Field(default=2, ge=0.1, le=10)
    decode_timeout: float = Field(default=15, ge=1, le=120)
    local_segment_seconds: int = Field(default=300, ge=5, le=3600)
    local_keep_segments: int = Field(default=3, ge=1, le=100)
    ui_poll_seconds: float = Field(default=2, ge=0.5, le=60)
    service_start_timeout: float = Field(default=60, ge=5, le=600)
    service_stall_timeout: float = Field(default=30, ge=5, le=600)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("sample_rate", "channels", "fps", mode="before")
    @classmethod
    def parse_integer_choice(cls, value: object) -> object:
        if isinstance(value, str):
            return int(value)
        return value

    @model_validator(mode="after")
    def validate_combination(self) -> Self:
        if self.width % 2 or self.height % 2:
            raise ValueError("Video dimensions must be even for yuv420p")
        if (
            self.default_image_position == "interval"
            and self.default_image_interval <= self.default_image_seconds
        ):
            raise ValueError("DEFAULT_IMAGE_INTERVAL must exceed DEFAULT_IMAGE_SECONDS")
        if self.retry_initial > self.retry_max:
            raise ValueError("RETRY_INITIAL must not exceed RETRY_MAX")
        if self.ingest_url and not self.ingest_url.startswith(("rtmp://", "rtmps://")):
            raise ValueError("INGEST_URL must use rtmp:// or rtmps://")
        if not self.internal_api_url.startswith(("http://", "https://")):
            raise ValueError("INTERNAL_API_URL must use HTTP or HTTPS")
        for path in (
            self.media_dir,
            self.images_dir,
            self.cache_dir,
            self.data_dir,
            self.output_dir,
            self.runtime_dir,
        ):
            if not path.is_absolute():
                raise ValueError("Storage paths must be absolute")
        return self

    @classmethod
    def from_env(cls) -> Self:
        if any(name.startswith("AIRADIO_") for name in os.environ):
            raise ValueError("Legacy AIRADIO_ settings detected; rename them to RADIO247_")
        for name in ("RADIO247_TRACK_INTRO_SECONDS", "RADIO247_TRACK_OUTRO_SECONDS"):
            if name in os.environ:
                raise ValueError(
                    f"{name} is no longer supported; use RADIO247_DEFAULT_IMAGE_SECONDS "
                    "and RADIO247_DEFAULT_IMAGE_POSITION"
                )
        values = {}
        for field in cls.model_fields:
            value = os.environ.get(f"RADIO247_{field.upper()}")
            if value is not None and (value != "" or field == "ingest_url"):
                values[field] = value
        if "internal_api_url" not in values:
            port = values.get("web_port", str(cls.model_fields["web_port"].default))
            values["internal_api_url"] = f"http://127.0.0.1:{port}"
        return cls.model_validate(values)

    @property
    def layout(self) -> str:
        return "stereo" if self.channels == 2 else "mono"
