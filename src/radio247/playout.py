from __future__ import annotations

import logging
import math
import queue
import random
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from radio247.audio import AudioSource, decode_source
from radio247.library import MEDIA_ERRORS, Track
from radio247.settings import Settings

LOG = logging.getLogger(__name__)


@dataclass
class AudioBlock:
    generation: int
    track: Track
    position: int
    samples: np.ndarray


class Playout:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.current: Track | None = None
        self.position = 0
        self.error: str | None = None
        self.underruns = 0
        self.bag: list[str] = []
        self.known: set[str] = set()
        self.failed: set[tuple[str, str]] = set()
        self.last: str | None = None
        self.decoding: Track | None = None
        self.preferred: str | None = None
        self.tracks: list[Track] = []
        self.generation = 0
        self.stopping = threading.Event()
        self.changed = threading.Event()
        self.lock = threading.Lock()
        self.queue: queue.Queue[AudioBlock] = queue.Queue(
            maxsize=max(1, math.ceil(settings.decode_buffer_seconds * settings.sample_rate / 1024))
        )
        self.pending: AudioBlock | None = None
        self.offset = 0
        self.discard = 0
        self.skip_remaining = 0
        self.requested: str | None = None
        self.starved = False
        self.thread = threading.Thread(target=self.produce, name="audio-decoder", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stopping.set()
        self.changed.set()
        self.thread.join(timeout=self.settings.decode_timeout + 2)
        if self.thread.is_alive():
            raise RuntimeError("Audio decoder worker did not stop")

    def picture(self, default_video: str) -> Path:
        if self.current is None:
            return Path(default_video)
        window = self.settings.default_image_seconds * self.settings.sample_rate
        in_window = (
            self.position < window
            if self.settings.default_image_position == "start"
            else self.current.samples - self.position <= window
        )
        if self.settings.default_image is not None and window > 0 and in_window:
            return Path(default_video)
        return Path(self.current.video)

    def choose(self) -> Track | None:
        available = {
            track.id: track for track in self.tracks if (track.id, track.audio) not in self.failed
        }
        self.bag = [key for key in self.bag if key in available]
        new = list(available.keys() - self.known)
        random.shuffle(new)
        self.bag.extend(new)
        self.known = set(available)
        if self.preferred is not None and self.preferred in available:
            track = available[self.preferred]
            self.bag = [key for key in self.bag if key != track.id]
            self.preferred = None
            self.last = track.id
            return track
        self.preferred = None
        if not self.bag:
            self.bag = list(available)
            random.shuffle(self.bag)
        if len(self.bag) > 1 and self.bag[-1] == self.last:
            self.bag[0], self.bag[-1] = self.bag[-1], self.bag[0]
        if not self.bag:
            return None
        track = available[self.bag.pop()]
        self.last = track.id
        return track

    def enqueue(self, block: AudioBlock) -> bool:
        while not self.stopping.is_set() and block.generation == self.generation:
            try:
                self.queue.put(block, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def produce(self) -> None:
        fade = max(1, self.settings.sample_rate * self.settings.fade_ms // 1000)
        peak = 10 ** (self.settings.target_peak / 20)
        while not self.stopping.is_set():
            with self.lock:
                generation = self.generation
                track = self.choose()
                self.decoding = track
            if track is None:
                self.changed.wait(0.1)
                self.changed.clear()
                continue

            def cancelled(generation: int = generation) -> bool:
                return self.stopping.is_set() or generation != self.generation

            pending = np.empty((0, self.settings.channels), dtype="float32")
            position = 0
            try:
                source = AudioSource.model_validate_json(Path(track.audio).read_text())
                for samples in decode_source(source, self.settings, cancelled):
                    if not np.isfinite(samples).all():
                        raise ValueError("Audio decoder returned non-finite samples")
                    pending = np.concatenate((pending, samples))
                    length = len(pending) - fade
                    if length <= 0:
                        continue
                    chunk = pending[:length].copy()
                    pending = pending[length:].copy()
                    ramp = np.minimum((position + np.arange(length)) / fade, 1)
                    chunk *= ramp[:, None]
                    np.clip(chunk, -peak, peak, out=chunk)
                    if not self.enqueue(AudioBlock(generation, track, position, chunk)):
                        break
                    position += length
                if not cancelled() and len(pending):
                    indices = np.arange(len(pending))
                    ramp = np.minimum(
                        np.minimum(
                            (position + indices) / fade, (len(pending) - 1 - indices) / fade
                        ),
                        1,
                    )
                    pending *= ramp[:, None]
                    np.clip(pending, -peak, peak, out=pending)
                    # Tail size follows FADE_MS; split it so queue capacity remains bounded.
                    for start in range(0, len(pending), 1024):
                        if not self.enqueue(
                            AudioBlock(
                                generation, track, position + start, pending[start : start + 1024]
                            )
                        ):
                            break
            except MEDIA_ERRORS as error:
                self.failed.add((track.id, track.audio))
                self.error = f"Cannot decode {track.title}: {error}"
                LOG.error("%s", self.error)

    def skip(self) -> None:
        self.requested = None
        self.skip_remaining = max(1, self.settings.sample_rate * self.settings.fade_ms // 1000)

    def play(self, track_id: str, tracks: list[Track]) -> bool:
        selected = next((track for track in tracks if track.id == track_id), None)
        if selected is None:
            self.error = "Selected track is no longer ready"
            LOG.warning("%s: %s", self.error, track_id)
            return False
        with self.lock:
            self.tracks = list(tracks)
            self.failed.discard((selected.id, selected.audio))
        self.error = None
        self.requested = track_id
        if self.current is None:
            self.restart()
        else:
            self.skip_remaining = max(1, self.settings.sample_rate * self.settings.fade_ms // 1000)
        return True

    def restart(self) -> None:
        with self.lock:
            self.generation += 1
            audible = self.current.id if self.current else None
            preferred = None
            while True:
                try:
                    queued = self.queue.get_nowait()
                    if preferred is None and queued.track.id != audible:
                        preferred = queued.track.id
                except queue.Empty:
                    break
            if preferred is None and self.decoding and self.decoding.id != audible:
                preferred = self.decoding.id
            self.preferred = self.requested or preferred
            self.last = audible
        self.requested = None
        self.pending = None
        self.offset = 0
        self.current = None
        self.discard = 0
        self.skip_remaining = 0
        self.changed.set()

    def read(self, count: int, tracks: list[Track]) -> np.ndarray:
        with self.lock:
            self.tracks = list(tracks)
        self.changed.set()
        output = np.zeros((count, self.settings.channels), dtype="float32")
        written = 0
        while written < count:
            if self.pending is None or self.offset >= len(self.pending.samples):
                try:
                    self.pending = self.queue.get_nowait()
                    self.offset = 0
                except queue.Empty:
                    if self.skip_remaining:
                        self.restart()
                    elif self.decoding is None:
                        self.current = None
                        self.position = 0
                    elif self.current is not None and not self.starved:
                        self.underruns += 1
                        LOG.warning("Audio decoder underrun; inserting silence")
                    self.starved = True
                    break
            block = self.pending
            if block.generation != self.generation:
                self.pending = None
                continue
            available = len(block.samples) - self.offset
            if self.discard:
                length = min(available, self.discard)
                self.offset += length
                self.discard -= length
                continue
            length = min(count - written, available)
            if self.skip_remaining:
                length = min(length, self.skip_remaining)
            chunk = block.samples[self.offset : self.offset + length]
            if self.skip_remaining:
                fade = max(1, self.settings.sample_rate * self.settings.fade_ms // 1000)
                chunk = chunk * ((self.skip_remaining - 1 - np.arange(length)) / fade)[:, None]
                self.skip_remaining -= length
                restart = self.skip_remaining == 0
            else:
                restart = False
            output[written : written + length] = chunk
            self.current = block.track
            self.position = block.position + self.offset + length
            self.offset += length
            written += length
            self.starved = False
            if restart:
                self.restart()
        return output

    def advance(self, count: int) -> None:
        self.discard += count
