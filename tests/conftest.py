from pathlib import Path

import pytest

from radio247.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    values = {}
    for name in ("media", "images", "cache", "data", "output", "runtime"):
        directory = tmp_path / name
        directory.mkdir()
        values[f"{name}_dir"] = directory
    return Settings(
        ingest_url="",
        media_dir=values["media_dir"],
        images_dir=values["images_dir"],
        cache_dir=values["cache_dir"],
        data_dir=values["data_dir"],
        output_dir=values["output_dir"],
        runtime_dir=values["runtime_dir"],
        admin_password_file=tmp_path / "data" / "admin-password",
        internal_token_file=tmp_path / "data" / "internal-token",
        width=320,
        height=180,
        video_bitrate=300000,
        video_buffer_size=600000,
        scan_interval=0.1,
        file_stable_seconds=0,
        poll_interval=0.2,
        prepare_timeout=30,
    )
