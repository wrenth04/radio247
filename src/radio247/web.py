import io
import logging
import re
import secrets
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit, urlunsplit

import av
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, model_validator

from radio247.library import AUDIO_SUFFIXES, Library, Store, secret
from radio247.settings import ImagePosition, Settings
from radio247.uploads import import_zip

COVER_NAME = re.compile(r"[0-9a-f]{64}\.mp4")


class Action(BaseModel):
    action: Literal["start", "stop", "skip", "retry", "play"]
    track_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if (self.action == "play") != (self.track_id is not None):
            raise ValueError("track_id is required only for the play action")
        return self


class ImageMode(BaseModel):
    position: ImagePosition
    seconds: float = Field(ge=0, le=86400, allow_inf_nan=False)
    interval: float = Field(ge=0, le=86400, allow_inf_nan=False)


def image_url(video: str | None) -> str | None:
    name = Path(video).name if video else ""
    return f"/api/cover/{name}" if COVER_NAME.fullmatch(name) else None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    destination = None
    output_mode = "local"
    if settings.ingest_url:
        address = urlsplit(settings.ingest_url)
        output_mode = address.scheme
        destination = (
            urlunsplit(
                (
                    address.scheme,
                    address.netloc.rsplit("@", 1)[-1],
                    address.path.rstrip("/"),
                    "",
                    "",
                )
            )
            + "/<stream-key>"
        )
    basic = HTTPBasic()
    store = Store(settings)
    library = Library(settings, store)
    failures: dict[str, tuple[int, float]] = {}
    radio: dict = {}
    credentials: dict[str, str] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for directory in (settings.media_dir, settings.images_dir, settings.cache_dir):
            directory.mkdir(parents=True, exist_ok=True)
        credentials["admin"] = secret(settings.admin_password_file, create=True)
        credentials["internal"] = secret(settings.internal_token_file, create=True)
        uploads = settings.data_dir / "uploads"
        uploads.mkdir(mode=0o700, parents=True, exist_ok=True)
        previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(uploads)
        logging.getLogger(__name__).info(
            "Admin username: admin; password file: %s", settings.admin_password_file
        )
        library.start()
        try:
            yield
        finally:
            library.stop()
            tempfile.tempdir = previous_tempdir

    app = FastAPI(title="Radio247", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def limit_upload(request: Request, call_next):
        if request.url.path == "/api/upload" and request.method == "POST":
            try:
                supplied = await basic(request)
                if supplied is None:
                    raise HTTPException(401, "Credentials are required")
                admin(request, supplied)
            except HTTPException as error:
                return JSONResponse(
                    {"detail": error.detail}, status_code=error.status_code, headers=error.headers
                )
            length = request.headers.get("content-length")
            if length is None:
                return JSONResponse({"detail": "Content-Length is required"}, status_code=411)
            try:
                size = int(length)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
            if size < 0 or size > settings.upload_max_bytes + 65536:
                return JSONResponse({"detail": "Upload size limit exceeded"}, status_code=413)
        return await call_next(request)

    def admin(request: Request, supplied: Annotated[HTTPBasicCredentials, Depends(basic)]) -> None:
        host = request.client.host if request.client else "unknown"
        count, since = failures.get(host, (0, time.monotonic()))
        if time.monotonic() - since >= 60:
            count, since = 0, time.monotonic()
        if count >= 10:
            raise HTTPException(429, "Too many login attempts; retry in one minute")
        if supplied.username != "admin" or not secrets.compare_digest(
            supplied.password.encode(), credentials["admin"].encode()
        ):
            if len(failures) > 1024:
                failures.clear()
            failures[host] = count + 1, since
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})
        failures.pop(host, None)

    def mutation(
        request: Request,
        x_radio247_action: Annotated[str | None, Header()] = None,
        _: None = Depends(admin),
    ) -> None:
        if x_radio247_action != "1":
            raise HTTPException(403, "X-Radio247-Action: 1 is required")
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin control is not permitted")

    def internal(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {credentials['internal']}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(401, "Invalid internal token")

    @app.get("/healthz")
    def health():
        if not library.thread.is_alive():
            raise HTTPException(503, "Preparation worker is not running")
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def index():
        html = (Path(__file__).parent / "static" / "index.html").read_text()
        return html.replace("__POLL_MS__", str(int(settings.ui_poll_seconds * 1000)))

    @app.get("/api/status", dependencies=[Depends(admin)])
    def status():
        snapshot = store.snapshot(library.default_video)
        stale = time.time() - radio.get("reported_at", 0) > settings.poll_interval * 5
        cover = radio.get("cover")
        cover_url = None
        if (
            snapshot.enabled
            and radio.get("playing")
            and not stale
            and isinstance(cover, str)
            and COVER_NAME.fullmatch(cover)
        ):
            cover_url = image_url(cover)
        tracks = store.tracks()
        for track in tracks:
            video = track.pop("video")
            track["cover_url"] = image_url(video) if track["state"] == "ready" else None
        return {
            "enabled": snapshot.enabled,
            "skip": snapshot.skip,
            "tracks": tracks,
            "ready": len(snapshot.tracks),
            "preparing": library.current,
            "scanning": library.scanning,
            "error": library.error,
            "image": store.image() | {"available": settings.default_image is not None},
            "radio": radio,
            "radio_stale": stale,
            "cover_url": cover_url,
            "output": output_mode,
            "destination": destination,
        }

    @lru_cache(maxsize=1)
    def cover_png(name: str) -> bytes:
        path = settings.cache_dir / name
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Playback image cache not found")
        try:
            with av.open(str(path)) as media:
                image = next(media.decode(video=0)).to_image()
            image.thumbnail((640, 360))
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        except (av.FFmpegError, OSError, ValueError, StopIteration) as error:
            logging.getLogger(__name__).exception("Cannot decode playback image: %s", name)
            raise HTTPException(500, "Cannot decode playback image") from error

    @app.get("/api/cover/{name}", dependencies=[Depends(admin)])
    def cover(name: str):
        if not COVER_NAME.fullmatch(name):
            raise HTTPException(404, "Playback image cache not found")
        return Response(
            cover_png(name),
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=60"},
        )

    @app.post("/api/control", dependencies=[Depends(mutation)])
    def control(action: Action):
        try:
            store.control(action.action, action.track_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"status": "requested"}

    @app.post("/api/image", dependencies=[Depends(mutation)])
    def image(mode: ImageMode):
        try:
            store.set_image(mode.position, mode.seconds, mode.interval)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return store.image()

    @app.delete("/api/tracks/{track_id}", dependencies=[Depends(mutation)])
    def delete_track(track_id: str):
        if not re.fullmatch(r"[0-9a-f]{24}", track_id):
            raise HTTPException(404, "Track not found")
        if not library.delete(track_id):
            raise HTTPException(404, "Track not found")
        if radio.get("track_id") == track_id and radio.get("playing"):
            store.control("skip")
        return {"status": "deleted"}

    @app.get("/internal/snapshot", dependencies=[Depends(internal)])
    def snapshot():
        return store.snapshot(library.default_video)

    @app.post("/internal/status", dependencies=[Depends(internal)])
    def report(payload: dict):
        if type(payload.get("skip_ack")) is int:
            store.acknowledge(payload["skip_ack"])
        radio.clear()
        radio.update(payload)
        radio["reported_at"] = time.time()
        return {"status": "ok"}

    @app.post("/api/upload", dependencies=[Depends(mutation)])
    async def upload(file: Annotated[UploadFile, File()]):
        filename = file.filename or ""
        if not filename or Path(filename).name != filename or "\\" in filename:
            raise HTTPException(400, "A plain filename is required")
        suffix = Path(filename).suffix.lower()
        if suffix not in AUDIO_SUFFIXES | {".png", ".zip"}:
            raise HTTPException(400, "Supported files: MP3, MPA, M4A, WAV, PNG, ZIP")
        directory = settings.images_dir if suffix == ".png" else settings.media_dir
        if suffix == ".zip":
            directory = settings.data_dir / "uploads"
        temporary = directory / f".upload-{uuid.uuid4().hex}"
        size = 0
        try:
            with temporary.open("xb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.upload_max_bytes:
                        raise HTTPException(413, "Upload size limit exceeded")
                    handle.write(chunk)
            if size == 0:
                raise HTTPException(400, "Empty upload")
            if suffix == ".zip":
                result = await run_in_threadpool(import_zip, temporary, settings)
                return {"status": "uploaded", "filename": filename, **result}
            temporary.replace(directory / filename)
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()
        return {"status": "uploaded", "filename": filename}

    return app
