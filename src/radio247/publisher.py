from __future__ import annotations

import logging
import math
import multiprocessing as mp
import queue
import random
import time
import uuid
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue
from pathlib import Path

import av

from radio247.media import CachedVideo, Muxer, VideoSpec, WirePacket
from radio247.settings import Settings

LOG = logging.getLogger(__name__)


class OutputConfigurationError(ValueError):
    pass


def publish(
    settings: Settings,
    picture: str,
    extra: bytes,
    packets: Queue,
    events: Connection,
    destination: str,
) -> None:
    av.logging.set_level(-8)
    muxer: Muxer | None = None
    try:
        spec = VideoSpec.from_settings(settings)
        muxer = Muxer(destination, spec, CachedVideo(Path(picture), spec), extra, settings)
        last_report = 0.0
        while True:
            packet = packets.get()
            if packet is None:
                break
            if muxer.write(packet) and time.monotonic() - last_report >= 0.25:
                last_report = time.monotonic()
                events.send(("progress", last_report))
    except (OSError, ValueError, av.FFmpegError) as error:
        # Exception messages from FFmpeg can contain the secret RTMPS URL.
        events.send(("error", type(error).__name__))
    finally:
        if muxer is not None:
            try:
                muxer.close()
            except (OSError, ValueError, av.FFmpegError) as error:
                events.send(("error", type(error).__name__))
        events.close()


class Publisher:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.context = mp.get_context("spawn")
        self.process: BaseProcess | None = None
        self.packets: Queue | None = None
        self.events: Connection | None = None
        self.started = 0.0
        self.progress = 0.0
        self.next_attempt = 0.0
        self.delay = settings.retry_initial
        self.reconnects = 0
        self.error: str | None = None
        self.state = "stopped"
        self.local_file: str | None = None

    def close(self, *, graceful: bool = False) -> None:
        if self.process is not None:
            if graceful and self.packets is not None:
                try:
                    self.packets.put_nowait(None)
                except queue.Full:
                    pass
                self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1)
            if self.process.is_alive():
                raise RuntimeError("Publisher could not be terminated")
            self.process.close()
        if self.packets is not None:
            self.packets.cancel_join_thread()
            self.packets.close()
        if self.events is not None:
            self.events.close()
        self.process = None
        self.packets = None
        self.events = None
        self.state = "stopped"

    def failed(self, reason: str) -> None:
        self.close()
        self.error = reason
        self.reconnects += 1
        self.next_attempt = time.monotonic() + random.uniform(
            self.delay * (1 - self.settings.retry_jitter_ratio), self.delay
        )
        self.delay = min(self.delay * 2, self.settings.retry_max)
        self.state = "reconnecting"
        LOG.warning("Publisher reconnecting: %s", reason)

    def tick(self, picture: Path, extra: bytes) -> None:
        now = time.monotonic()
        if self.process is not None:
            if self.events is not None:
                try:
                    while self.events.poll():
                        kind, value = self.events.recv()
                        if kind == "error":
                            self.failed(value)
                            return
                        self.progress = value
                        self.state = "streaming"
                        self.error = None
                except EOFError:
                    self.failed("Publisher pipe closed")
                    return
            if not self.process.is_alive():
                self.failed("Publisher exited")
                return
            if self.progress:
                if now - self.progress > self.settings.stall_timeout:
                    self.failed("No output progress")
                    return
                if now - self.started >= self.settings.retry_reset_seconds:
                    self.delay = self.settings.retry_initial
            elif now - self.started > self.settings.connect_timeout + self.settings.gop_seconds:
                self.failed("Connection timed out")
                return
            if (
                not self.settings.ingest_url
                and now - self.started >= self.settings.local_segment_seconds
            ):
                self.close(graceful=True)
            else:
                return
        if now < self.next_attempt:
            return
        try:
            destination = self.destination()
        except (OSError, ValueError) as error:
            message = (
                str(error)
                if isinstance(error, OutputConfigurationError)
                else f"Output configuration: {type(error).__name__}"
            )
            self.failed(message)
            return
        packet_count = math.ceil(
            (self.settings.sample_rate / 1024 + self.settings.fps) * self.settings.queue_seconds
        )
        self.packets = self.context.Queue(maxsize=packet_count)
        receiver, sender = self.context.Pipe(duplex=False)
        self.events = receiver
        self.process = self.context.Process(
            target=publish,
            args=(self.settings, str(picture), extra, self.packets, sender, destination),
            name="publisher",
            daemon=True,
        )
        self.process.start()
        sender.close()
        self.started = now
        self.progress = 0
        self.state = "connecting"

    def destination(self) -> str:
        if self.settings.ingest_url:
            key = self.settings.stream_key.get_secret_value().strip()
            if not key:
                raise OutputConfigurationError("RADIO247_STREAM_KEY is missing")
            if len(key) < 12:
                raise OutputConfigurationError(
                    "RADIO247_STREAM_KEY must contain at least 12 characters"
                )
            return self.settings.ingest_url.rstrip("/") + "/" + key
        directory = self.settings.output_dir
        directory.mkdir(parents=True, exist_ok=True)
        existing = sorted(directory.glob("radio-*.flv"))
        for path in existing[: max(0, len(existing) - self.settings.local_keep_segments + 1)]:
            path.unlink()
        self.local_file = f"radio-{time.time_ns()}-{uuid.uuid4().hex[:6]}.flv"
        return str(directory / self.local_file)

    def send(self, packet: WirePacket) -> None:
        if self.packets is None:
            return
        try:
            self.packets.put_nowait(packet)
        except queue.Full:
            self.failed("Output queue is full")
