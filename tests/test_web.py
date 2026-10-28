import io
import stat
import time
import zipfile
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from test_library import make_song

from radio247.library import Library, Store
from radio247.web import create_app


def test_admin_controls_and_upload_validation(settings):
    with TestClient(create_app(settings)) as client:
        password = settings.admin_password_file.read_text().strip()
        assert client.get("/").status_code == 401
        client.auth = ("admin", password)
        assert client.get("/").status_code == 200
        assert client.post("/api/control", json={"action": "stop"}).status_code == 403
        headers = {"X-Radio247-Action": "1"}
        assert (
            client.post("/api/control", json={"action": "stop"}, headers=headers).status_code == 200
        )
        assert client.get("/api/status").json()["enabled"] is False
        assert (
            client.post(
                "/api/control",
                json={"action": "start"},
                headers={**headers, "Origin": "https://untrusted.invalid"},
            ).status_code
            == 403
        )
        assert client.get("/internal/snapshot").status_code == 401
        assert (
            client.post(
                "/api/upload",
                files={"file": ("../escape.wav", b"data")},
                headers=headers,
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/upload",
                files={"file": ("track.exe", b"data")},
                headers=headers,
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/upload",
                files={"file": ("song.wav", b"placeholder")},
                headers=headers,
            ).status_code
            == 200
        )
        assert (settings.media_dir / "song.wav").read_bytes() == b"placeholder"
        assert not list(settings.media_dir.glob(".upload-*"))


@pytest.fixture
def web_client(settings, monkeypatch):
    monkeypatch.setattr(Library, "start", lambda self: None)
    monkeypatch.setattr(Library, "stop", lambda self: None)

    @contextmanager
    def open_client(**overrides):
        with TestClient(create_app(settings.model_copy(update=overrides))) as client:
            client.auth = ("admin", settings.admin_password_file.read_text().strip())
            client.headers["X-Radio247-Action"] = "1"
            yield client

    return open_client


def zip_bytes(entries, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def test_zip_imports_nested_media_and_reports_skipped_files(web_client, settings):
    (settings.media_dir / "song.m4a").write_bytes(b"old")
    archive = zip_bytes(
        [
            ("album/", b""),
            ("album/song.M4A", b"new audio"),
            ("covers/song.PNG", b"new image"),
            ("README.txt", b"notes"),
            ("__MACOSX/._song.m4a", b"metadata"),
        ]
    )
    with web_client() as client:
        response = client.post("/api/upload", files={"file": ("album.zip", archive)})
        assert response.status_code == 200
        assert response.json()["imported"] == 2
        assert response.json()["skipped"] == 2
        assert (settings.media_dir / "song.m4a").read_bytes() == b"new audio"
        assert (settings.images_dir / "song.png").read_bytes() == b"new image"
        assert not list(settings.data_dir.joinpath("uploads").iterdir())
        assert not list(settings.media_dir.glob(".upload-*"))
        assert not list(settings.images_dir.glob(".upload-*"))


@pytest.mark.parametrize(
    "name",
    ["../escape.png", "/escape.png", r"folder\escape.png", "C:/escape.png", "a/../escape.png"],
)
def test_zip_rejects_unsafe_paths_before_import(web_client, settings, name):
    with web_client() as client:
        response = client.post(
            "/api/upload",
            files={"file": ("album.zip", zip_bytes([("song.m4a", b"audio"), (name, b"image")]))},
        )
        assert response.status_code == 400
        assert not list(settings.media_dir.iterdir())
        assert not list(settings.images_dir.iterdir())
        assert not list(settings.data_dir.joinpath("uploads").iterdir())


@pytest.mark.parametrize(
    ("overrides", "entries"),
    [
        ({"zip_max_files": 1}, [("song.m4a", b"a"), ("song.png", b"b")]),
        ({"zip_max_bytes": 1024}, [("song.m4a", b"a" * 2048)]),
        ({"upload_max_bytes": 1024}, [("song.m4a", b"a" * 2048)]),
    ],
)
def test_zip_enforces_limits(web_client, settings, overrides, entries):
    with web_client(**overrides) as client:
        response = client.post("/api/upload", files={"file": ("album.zip", zip_bytes(entries))})
        assert response.status_code == 413
        assert not list(settings.media_dir.iterdir())
        assert not list(settings.data_dir.joinpath("uploads").iterdir())


def test_zip_rejects_duplicates_links_and_non_media_archives(web_client, settings):
    link = zipfile.ZipInfo("link.png")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archives = [
        zip_bytes([("a/song.png", b"first"), ("b/song.PNG", b"second")]),
        zip_bytes([(link, b"target.png")]),
        zip_bytes([("song.m4a", b"")]),
        zip_bytes([("README.txt", b"no media")]),
        zip_bytes([("song.m4a", b"audio")], zipfile.ZIP_BZIP2),
        b"not a zip",
    ]
    with web_client() as client:
        for archive in archives:
            response = client.post("/api/upload", files={"file": ("album.zip", archive)})
            assert response.status_code == 400
            assert not list(settings.media_dir.iterdir())
            assert not list(settings.images_dir.iterdir())
            assert not list(settings.data_dir.joinpath("uploads").iterdir())


def test_zip_crc_failure_keeps_existing_files_and_cleans_staging(web_client, settings):
    (settings.media_dir / "song.m4a").write_bytes(b"original")
    archive = zip_bytes(
        [("song.m4a", b"replacement"), ("song.png", b"bad-crc-png")], zipfile.ZIP_STORED
    )
    offset = archive.index(b"bad-crc-png")
    damaged = archive[:offset] + b"X" + archive[offset + 1 :]
    with web_client() as client:
        response = client.post("/api/upload", files={"file": ("album.zip", damaged)})
        assert response.status_code == 400
        assert (settings.media_dir / "song.m4a").read_bytes() == b"original"
        assert not list(settings.images_dir.iterdir())
        assert not list(settings.media_dir.glob(".upload-*"))
        assert not list(settings.data_dir.joinpath("uploads").iterdir())


def test_playback_cover_auth_changes_and_stale_state(web_client, settings, monkeypatch):
    library = Library(settings, Store(settings))
    black = library.picture(None)
    image = settings.images_dir / "blue.png"
    Image.new("RGB", (320, 180), "blue").save(image)
    blue = library.picture(image)
    with web_client() as client:
        assert client.get(f"/api/cover/{black.name}", auth=("wrong", "wrong")).status_code == 401
        assert client.get("/api/cover/settings.py").status_code == 404
        assert client.get(f"/api/cover/{'f' * 64}.mp4").status_code == 404
        assert client.get("/api/status").json()["cover_url"] is None
        headers = {"Authorization": f"Bearer {settings.internal_token_file.read_text().strip()}"}
        for path, color in [(black, "black"), (blue, "blue")]:
            client.post(
                "/internal/status",
                json={"playing": True, "cover": path.name},
                headers=headers,
                auth=None,
            ).raise_for_status()
            url = client.get("/api/status").json()["cover_url"]
            assert url == f"/api/cover/{path.name}"
            response = client.get(url)
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            with Image.open(io.BytesIO(response.content)) as preview:
                pixel = preview.convert("RGB").getpixel((80, 80))
                assert isinstance(pixel, tuple)
                red, green, blue_channel = pixel
                assert red < 20 and green < 20
                assert blue_channel > 150 if color == "blue" else blue_channel < 20
        future = time.time() + 60
        with monkeypatch.context() as patch:
            patch.setattr("radio247.web.time.time", lambda: future)
            assert client.get("/api/status").json()["cover_url"] is None
        client.post("/api/control", json={"action": "stop"}).raise_for_status()
        assert client.get("/api/status").json()["cover_url"] is None


@pytest.mark.parametrize("scheme", ["rtmp", "rtmps"])
def test_status_shows_destination_without_credentials(web_client, settings, scheme):
    with web_client(
        ingest_url=f"{scheme}://user:password@example.test:443/live2?token=private#fragment",
        stream_key=SecretStr("test-only-stream-secret"),
    ) as client:
        response = client.get("/api/status")
        status = response.json()
        assert status["output"] == scheme
        assert status["destination"] == f"{scheme}://example.test:443/live2/<stream-key>"
        for secret in ("user:password", "private", "fragment", "test-only-stream-secret"):
            assert secret not in response.text


@pytest.mark.parametrize("key", ["", "short"])
def test_invalid_stream_key_keeps_management_available(web_client, key):
    with web_client(ingest_url="rtmps://example.invalid/live", stream_key=SecretStr(key)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/status").status_code == 200
        assert client.post("/api/control", json={"action": "stop"}).status_code == 200


def test_local_status_does_not_claim_rtmp_output(web_client):
    with web_client() as client:
        status = client.get("/api/status").json()
        assert status["output"] == "local"
        assert status["destination"] is None


def test_track_thumbnails_and_specific_play_commands(web_client, settings):
    store = Store(settings)
    library = Library(settings, store)
    library.default_video = library.picture(None)
    for name in ("first", "selected"):
        make_song(settings.media_dir / f"{name}.wav")
    Image.new("RGB", (320, 180), "red").save(settings.images_dir / "selected.png")
    (settings.media_dir / "broken.mp3").write_bytes(b"not audio")
    library.scan()
    library.scan()
    with web_client() as client:
        tracks = {track["title"]: track for track in client.get("/api/status").json()["tracks"]}
        first, selected, broken = (tracks[name] for name in ("first", "selected", "broken"))
        assert first["cover_url"] == f"/api/cover/{library.default_video.name}"
        assert selected["cover_url"] != first["cover_url"]
        assert client.get(selected["cover_url"]).headers["content-type"] == "image/png"
        assert broken["cover_url"] is None
        client.post("/api/control", json={"action": "stop"}).raise_for_status()
        for payload, code in [
            ({"action": "play"}, 422),
            ({"action": "play", "track_id": "missing"}, 409),
            ({"action": "play", "track_id": broken["id"]}, 409),
            ({"action": "start", "track_id": selected["id"]}, 422),
        ]:
            assert client.post("/api/control", json=payload).status_code == code
            assert not store.snapshot(None).enabled
        client.post(
            "/api/control", json={"action": "play", "track_id": selected["id"]}
        ).raise_for_status()
        snapshot = store.snapshot(None)
        assert snapshot.enabled and snapshot.track_id == selected["id"]
        first_sequence = snapshot.skip
        client.post(
            "/api/control", json={"action": "play", "track_id": first["id"]}
        ).raise_for_status()
        headers = {"Authorization": f"Bearer {settings.internal_token_file.read_text().strip()}"}
        client.post(
            "/internal/status", json={"skip_ack": first_sequence}, headers=headers, auth=None
        ).raise_for_status()
        snapshot = store.snapshot(None)
        assert snapshot.track_id == first["id"]
        client.post(
            "/internal/status", json={"skip_ack": snapshot.skip}, headers=headers, auth=None
        ).raise_for_status()
        assert store.snapshot(None).track_id is None
        assert store.snapshot(None).enabled
        client.post(
            "/api/control", json={"action": "play", "track_id": selected["id"]}
        ).raise_for_status()
        client.post("/api/control", json={"action": "skip"}).raise_for_status()
        assert store.snapshot(None).track_id is None
