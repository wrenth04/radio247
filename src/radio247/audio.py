from __future__ import annotations

import os
import selectors
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from radio247.settings import Settings


class AudioSource(BaseModel):
    source: Path
    filters: str
    samples: int = Field(ge=1)
    size: int = Field(gt=0)
    mtime_ns: int


def decode_source(
    source: AudioSource, settings: Settings, cancelled: Callable[[], bool]
) -> Iterator[np.ndarray]:
    # Pin the checked inode so an atomic upload cannot swap it before FFmpeg opens it.
    with os.fdopen(os.open(source.source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as input_file:
        stamp = os.fstat(input_file.fileno())
        if (stamp.st_size, stamp.st_mtime_ns) != (source.size, source.mtime_ns):
            raise ValueError("Audio source changed; waiting for library preparation")
        process = subprocess.Popen(
            [
                settings.ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-threads",
                str(settings.threads),
                "-i",
                f"/proc/self/fd/{input_file.fileno()}",
                "-map",
                "0:a:0",
                "-vn",
                "-filter_threads",
                str(settings.threads),
                "-af",
                source.filters,
                "-ar",
                str(settings.sample_rate),
                "-ac",
                str(settings.channels),
                "-c:a",
                "pcm_f32le",
                "-f",
                "f32le",
                "pipe:1",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(input_file.fileno(),),
        )
    assert process.stdout is not None
    frame_bytes = settings.channels * 4
    block_bytes = 1024 * frame_bytes
    pending = bytearray()
    decoded = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + settings.decode_timeout
            while not cancelled():
                if not selector.select(timeout=0.1):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Audio decoder made no progress")
                    continue
                chunk = os.read(process.stdout.fileno(), block_bytes)
                if not chunk:
                    break
                pending.extend(chunk)
                if len(pending) >= block_bytes:
                    samples = np.frombuffer(bytes(pending[:block_bytes]), dtype="<f4")
                    del pending[:block_bytes]
                    decoded += len(samples)
                    yield samples.reshape(-1, settings.channels)
                # Time spent blocked by the bounded playback queue is not a decoder stall.
                deadline = time.monotonic() + settings.decode_timeout
        if cancelled():
            return
        deadline = time.monotonic() + settings.decode_timeout
        while process.poll() is None:
            if cancelled():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Audio decoder did not exit")
            time.sleep(0.01)
        code = process.returncode
        if code:
            raise RuntimeError(f"Audio decoder exited with code {code}")
        if len(pending) % frame_bytes:
            raise ValueError("Audio decoder returned an incomplete sample frame")
        if pending:
            decoded += len(pending) // 4
            yield np.frombuffer(bytes(pending), dtype="<f4").reshape(-1, settings.channels)
        if not decoded:
            raise ValueError("Audio decoder returned no samples")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
