from __future__ import annotations

import json
import logging
import math
import signal
import threading
import time
from fractions import Fraction
from pathlib import Path

import httpx

from radio247.library import Snapshot, secret
from radio247.media import AudioEncoder, CachedVideo, VideoSpec
from radio247.playout import Playout
from radio247.publisher import Publisher
from radio247.settings import Settings

LOG = logging.getLogger(__name__)


class Control:
    def __init__(self, settings: Settings, stop: threading.Event):
        self.settings = settings
        self.stop = stop
        self.lock = threading.Lock()
        self.snapshot: Snapshot | None = None
        self.report: dict = {}
        self.error: str | None = None
        self.thread = threading.Thread(target=self.run, name="control", daemon=True)

    def run(self) -> None:
        with httpx.Client(
            base_url=self.settings.internal_api_url,
            timeout=self.settings.request_timeout,
            trust_env=False,
        ) as client:
            while not self.stop.is_set():
                try:
                    token = secret(self.settings.internal_token_file)
                    headers = {"Authorization": f"Bearer {token}"}
                    response = client.get("/internal/snapshot", headers=headers)
                    response.raise_for_status()
                    snapshot = Snapshot.model_validate(response.json())
                    with self.lock:
                        self.snapshot = snapshot
                        report = dict(self.report)
                    result = client.post("/internal/status", json=report, headers=headers)
                    result.raise_for_status()
                    self.error = None
                except (OSError, ValueError, httpx.HTTPError) as error:
                    self.error = type(error).__name__
                    LOG.warning("Control API unavailable: %s; retaining snapshot", self.error)
                self.stop.wait(self.settings.poll_interval)


def run(settings: Settings) -> None:
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = settings.runtime_dir / "health.json"
    control = Control(settings, stopping)
    control.thread.start()
    publisher = Publisher(settings)
    player = Playout(settings)
    audio: AudioEncoder | None = None
    video: CachedVideo | None = None
    default: CachedVideo | None = None
    spec = VideoSpec.from_settings(settings)
    video_frame = 0
    video_start_frame = 0
    pts = 0
    start = time.monotonic()
    last_report = 0.0
    skip: int | None = None
    underruns = 0
    rejected: set[Path] = set()
    try:
        while not stopping.is_set():
            with control.lock:
                snapshot = control.snapshot
            enabled = (
                snapshot is not None and snapshot.enabled and snapshot.default_video is not None
            )
            if not enabled:
                if audio is not None:
                    for packet in audio.flush():
                        publisher.send(packet)
                    player.close()
                    player = Playout(settings)
                publisher.close(graceful=True)
                audio = None
                if snapshot and snapshot.track_id is None:
                    skip = snapshot.skip
                stopping.wait(0.1)
            else:
                assert snapshot is not None and snapshot.default_video is not None
                if audio is None:
                    audio = AudioEncoder(settings)
                    default = CachedVideo(Path(snapshot.default_video), spec)
                    video = default
                    pts = 0
                    video_frame = 0
                    video_start_frame = 0
                    start = time.monotonic()
                    skip = None
                assert video is not None and default is not None
                if snapshot.skip != skip:
                    if snapshot.track_id is not None:
                        player.play(snapshot.track_id, snapshot.tracks)
                    elif skip is not None:
                        player.skip()
                    skip = snapshot.skip
                due = start + pts / settings.sample_rate
                lag = time.monotonic() - due
                if lag > settings.queue_seconds:
                    lost = int(lag * settings.sample_rate)
                    player.advance(lost)
                    pts += lost
                    video_frame = math.ceil(pts * settings.fps / settings.sample_rate)
                    publisher.failed("Playback scheduling underrun")
                    audio = AudioEncoder(settings)
                    underruns += 1
                elif lag < 0:
                    stopping.wait(-lag)
                    if stopping.is_set():
                        break
                count = audio.codec.frame_size
                samples = player.read(count, snapshot.tracks)
                picture = player.picture(
                    snapshot.default_video,
                    snapshot.image_position,
                    snapshot.image_seconds,
                    snapshot.image_interval,
                )
                if picture in rejected:
                    picture = default.path
                publisher.tick(video.path, audio.extra)
                for packet in audio.encode(samples, pts):
                    while Fraction(video_frame, settings.fps) <= packet.timestamp:
                        if picture != video.path:
                            candidate = CachedVideo(picture, spec)
                            if candidate.extra != default.extra:
                                LOG.error(
                                    "Picture codec configuration does not match; "
                                    "using the default picture instead: %s",
                                    picture,
                                )
                                rejected.add(picture)
                                picture = default.path
                                candidate = default
                            video = candidate
                            video_start_frame = video_frame
                        publisher.send(video.at(video_frame, start_frame=video_start_frame))
                        video_frame += 1
                    publisher.send(packet)
                pts += count
            now = time.monotonic()
            if now - last_report >= settings.poll_interval:
                last_report = now
                report = {
                    "playing": bool(enabled),
                    "publisher": publisher.state,
                    "track": player.current.title if player.current else None,
                    "track_id": player.current.id if player.current else None,
                    "cover": video.path.name if enabled and video is not None else None,
                    "position": player.position / settings.sample_rate,
                    "skip_ack": skip,
                    "underruns": underruns,
                    "audio_underruns": player.underruns,
                    "reconnects": publisher.reconnects,
                    "output_file": publisher.local_file,
                    "error": publisher.error or player.error,
                    "control_error": control.error,
                    "heartbeat": time.time(),
                }
                with control.lock:
                    control.report = report
                temporary = heartbeat.with_suffix(".tmp")
                temporary.write_text(json.dumps(report))
                temporary.replace(heartbeat)
    finally:
        stopping.set()
        player.close()
        publisher.close(graceful=True)
        control.thread.join(timeout=settings.request_timeout * 2 + 1)
        heartbeat.unlink(missing_ok=True)
