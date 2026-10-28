from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import av
from pydantic import BaseModel

from radio247.audio import AudioSource
from radio247.media import (
    CachedVideo,
    VideoSpec,
    analyze,
    command,
    prepare_image,
    prepare_video,
    probe,
)
from radio247.settings import Settings

LOG = logging.getLogger(__name__)
AUDIO_SUFFIXES = {".mp3", ".mpa", ".m4a", ".wav"}
MEDIA_ERRORS = (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, av.FFmpegError)


class Track(BaseModel):
    id: str
    title: str
    audio: str
    video: str
    samples: int


class Snapshot(BaseModel):
    enabled: bool
    skip: int
    track_id: str | None = None
    default_video: str | None
    tracks: list[Track]


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_dir / "radio.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS control (
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
                    skip INTEGER NOT NULL DEFAULT 0, track_id TEXT
                );
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, error TEXT, audio TEXT, video TEXT,
                    samples INTEGER, active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tracks)")}
            if "audio" not in columns:
                db.execute("ALTER TABLE tracks ADD COLUMN audio TEXT")
            controls = {row["name"] for row in db.execute("PRAGMA table_info(control)")}
            if "track_id" not in controls:
                db.execute("ALTER TABLE control ADD COLUMN track_id TEXT")
            db.execute(
                "INSERT OR IGNORE INTO control(id,enabled) VALUES(1,?)",
                (settings.autostart,),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def control(self, action: str, track_id: str | None = None) -> None:
        with self.connect() as db:
            if action == "play":
                track = db.execute(
                    "SELECT audio,video FROM tracks WHERE id=? AND active=1 AND state='ready'",
                    (track_id,),
                ).fetchone()
                if not track or not all(
                    track[key] and Path(track[key]).is_file() for key in ("audio", "video")
                ):
                    raise ValueError("Selected track is unavailable or not ready")
                db.execute(
                    "UPDATE control SET enabled=1,skip=skip+1,track_id=? WHERE id=1", (track_id,)
                )
            elif action == "skip":
                db.execute("UPDATE control SET skip=skip+1,track_id=NULL WHERE id=1")
            elif action in {"start", "stop"}:
                db.execute(
                    "UPDATE control SET enabled=?,track_id=NULL WHERE id=1", (action == "start",)
                )
            elif action == "retry":
                db.execute("UPDATE tracks SET state='uploaded',error=NULL WHERE state='failed'")
            else:
                raise ValueError("Unknown action")

    def acknowledge(self, sequence: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE control SET track_id=NULL WHERE id=1 AND skip=? AND track_id IS NOT NULL",
                (sequence,),
            )

    def tracks(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,state,error,samples,video FROM tracks "
                    "WHERE active=1 ORDER BY title"
                )
            ]

    def snapshot(self, default_video: Path | None) -> Snapshot:
        with self.connect() as db:
            control = db.execute("SELECT enabled,skip,track_id FROM control WHERE id=1").fetchone()
            rows = db.execute(
                "SELECT id,title,audio,video,samples FROM tracks "
                "WHERE state='ready' AND active=1 AND audio IS NOT NULL"
            ).fetchall()
        return Snapshot(
            enabled=bool(control["enabled"]),
            skip=control["skip"],
            track_id=control["track_id"],
            default_video=str(default_video) if default_video else None,
            tracks=[Track.model_validate(dict(row)) for row in rows],
        )


class Library:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.spec = VideoSpec.from_settings(settings)
        self.default_video: Path | None = None
        self.error: str | None = None
        self.current: str | None = None
        self.scanning = False
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="preparation", daemon=True)
        self.observed: dict[Path, tuple[tuple[int, int], float]] = {}
        self.digests: dict[Path, tuple[tuple[int, int], str]] = {}
        processing = settings.model_dump(
            mode="json",
            include={
                "sample_rate",
                "channels",
                "target_lufs",
                "target_peak",
                "target_lra",
                "width",
                "height",
                "fps",
                "gop_seconds",
                "video_bitrate",
                "video_buffer_size",
                "video_preset",
            },
        )
        tools = command([settings.ffmpeg, "-version"], settings.probe_timeout)
        self.version = (
            json.dumps(processing, sort_keys=True).encode()
            + tools.encode()
            + av.__version__.encode()
            + b":cache-v1"
        )
        self.picture_version = self.version + b":image-cover-v1"
        version = hashlib.sha256(self.picture_version + b":streamed-audio-v1").hexdigest()
        with store.connect() as db:
            previous = db.execute("SELECT value FROM metadata WHERE key='processing'").fetchone()
            if previous is None or previous["value"] != version:
                db.execute("UPDATE tracks SET state='uploaded',error=NULL")
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES('processing',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (version,),
                )

    def start(self) -> None:
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=2)

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.default_video = self.picture(self.settings.default_image)
                self.scan()
                self.error = None
            except MEDIA_ERRORS as error:
                self.error = str(error)
                LOG.error("Library scan failed: %s", error)
            self.stopping.wait(self.settings.scan_interval)

    def digest(self, path: Path) -> str:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        previous = self.digests.get(path)
        if previous and previous[0] == stamp:
            return previous[1]
        with path.open("rb") as source:
            value = hashlib.file_digest(source, "sha256").hexdigest()
        if path.stat().st_mtime_ns != stamp[0] or path.stat().st_size != stamp[1]:
            raise ValueError(f"File changed during hashing: {path.name}")
        self.digests[path] = stamp, value
        return value

    def usage(self) -> int:
        return sum(p.stat().st_size for p in self.settings.cache_dir.glob("*") if p.is_file())

    def require_space(self, needed: int) -> None:
        if self.usage() + needed > self.settings.cache_max_bytes:
            raise ValueError("Cache quota exceeded; stop radio before removing unused cache files")

    def picture(self, image: Path | None) -> Path:
        digest = self.digest(image) if image else "black"
        key = hashlib.sha256(self.picture_version + digest.encode()).hexdigest()
        target = self.settings.cache_dir / f"{key}.mp4"
        if target.exists():
            CachedVideo(target, self.spec)
            return target
        self.require_space(
            self.spec.buffer_size // 8 + self.spec.bitrate * self.settings.gop_seconds // 8
        )
        png = target.with_suffix(".preparing.png")
        temporary = target.with_suffix(".preparing.mp4")
        try:
            prepare_image(image, png, self.settings)
            prepare_video(png, temporary, self.spec, self.settings)
            CachedVideo(temporary, self.spec)
            self.require_space(0)
            temporary.replace(target)
        finally:
            png.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
        return target

    def scan(self) -> None:
        self.scanning = True
        active = set()
        try:
            for source in self.settings.media_dir.iterdir():
                if self.stopping.is_set():
                    return
                if (
                    source.is_symlink()
                    or not source.is_file()
                    or source.suffix.lower() not in AUDIO_SUFFIXES
                ):
                    continue
                track_id = hashlib.sha256(source.name.encode()).hexdigest()[:24]
                active.add(track_id)
                stat = source.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                observation = self.observed.get(source)
                if observation is None or observation[0] != stamp:
                    self.observed[source] = stamp, time.monotonic()
                    continue
                if time.monotonic() - observation[1] < self.settings.file_stable_seconds:
                    continue
                image = self.settings.images_dir / f"{source.stem}.png"
                if image.is_symlink():
                    raise ValueError("Symlink images are not supported")
                self.prepare(track_id, source, image if image.is_file() else None)
            with self.store.connect() as db:
                rows = db.execute("SELECT id FROM tracks").fetchall()
                for row in rows:
                    db.execute(
                        "UPDATE tracks SET active=? WHERE id=?",
                        (row["id"] in active, row["id"]),
                    )
        finally:
            self.scanning = False

    def prepare(self, track_id: str, source: Path, image: Path | None) -> None:
        fingerprint = ""
        try:
            original = self.digest(source)
            stamp = source.stat()
            audio_key = hashlib.sha256(
                self.version
                + original.encode()
                + str(source).encode()
                + str(stamp.st_mtime_ns).encode()
                + b":analysis-v1"
            ).hexdigest()
            picture_version = (
                self.digest(image)
                if image
                else str(self.default_video.name if self.default_video else "pending")
            )
            fingerprint = hashlib.sha256(audio_key.encode() + picture_version.encode()).hexdigest()
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
                if row and row["fingerprint"] == fingerprint:
                    if row["state"] == "failed":
                        return
                    if (
                        row["state"] == "ready"
                        and row["audio"]
                        and Path(row["audio"]).is_file()
                        and Path(row["video"]).is_file()
                    ):
                        return
                db.execute(
                    "INSERT INTO tracks(id,title,fingerprint,state) VALUES(?,?,?,'preparing') "
                    "ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                    "fingerprint=excluded.fingerprint,state='preparing',error=NULL,active=1",
                    (track_id, source.stem, fingerprint),
                )
            self.current = source.name
            audio = self.settings.cache_dir / f"{audio_key}.json"
            if not audio.exists():
                duration = probe(source, self.settings)
                metadata = AudioSource(
                    source=source,
                    filters=analyze(source, self.settings),
                    samples=max(1, round(duration * self.settings.sample_rate)),
                    size=stamp.st_size,
                    mtime_ns=stamp.st_mtime_ns,
                )
                if (
                    self.digest(source) != original
                    or source.stat().st_mtime_ns != stamp.st_mtime_ns
                ):
                    raise ValueError("Source changed during analysis")
                content = metadata.model_dump_json()
                self.require_space(len(content.encode()))
                temporary = audio.with_suffix(".preparing.json")
                try:
                    temporary.write_text(content)
                    temporary.replace(audio)
                finally:
                    temporary.unlink(missing_ok=True)
            metadata = AudioSource.model_validate_json(audio.read_text())
            video = self.picture(image) if image else self.default_video
            if video is None:
                raise ValueError("Default picture is not ready")
            with self.store.connect() as db:
                db.execute(
                    "UPDATE tracks SET state='ready',audio=?,video=?,samples=?,error=NULL "
                    "WHERE id=?",
                    (str(audio), str(video), metadata.samples, track_id),
                )
            LOG.info("Ready: %s", source.name)
        except MEDIA_ERRORS as error:
            LOG.warning("Preparation failed for %s: %s", source.name, error)
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO tracks(id,title,fingerprint,state,error) VALUES(?,?,?,'failed',?) "
                    "ON CONFLICT(id) DO UPDATE SET state='failed',error=excluded.error,"
                    "fingerprint=excluded.fingerprint,active=1",
                    (track_id, source.stem, fingerprint, str(error)),
                )
        finally:
            self.current = None


def secret(path: Path, *, create: bool = False) -> str:
    if create:
        import secrets

        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(secrets.token_urlsafe(32) + "\n")
    value = path.read_text().strip()
    if len(value) < 12:
        raise ValueError(f"Secret file must contain at least 12 characters: {path}")
    return value
