import io
import os
import signal
import socket
import subprocess
import sys
import time
import zipfile

import av
import httpx
import numpy as np
import psutil
import pytest
from PIL import Image
from test_library import make_song

from radio247.media import command


def wait_for(check, processes, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in processes:
            assert process.poll() is None, f"Service exited with {process.returncode}"
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Service readiness timed out")


@pytest.mark.parametrize("fps", [30, 1])
@pytest.mark.parametrize("position", ["start", "end"])
def test_m4a_timed_picture_in_output_and_on_replay(settings, tmp_path, fps, position):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    default = settings.images_dir / "default.png"
    settings = settings.model_copy(
        update={
            "bind_host": "127.0.0.1",
            "web_port": port,
            "internal_api_url": f"http://127.0.0.1:{port}",
            "autostart": False,
            "default_image": default,
            "default_image_seconds": 1.5,
            "default_image_position": position,
            "fps": fps,
            "queue_seconds": 10,
            "local_segment_seconds": 60,
        }
    )
    source = tmp_path / "tone.wav"
    make_song(source, seconds=4)
    command(
        [
            settings.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-c:a",
            "aac",
            str(settings.media_dir / "tone.m4a"),
        ],
        settings.prepare_timeout,
    )
    Image.new("RGB", (320, 180), "red").save(default)
    Image.new("RGB", (320, 180), "blue").save(settings.images_dir / "tone.png")
    env = dict(os.environ)
    for name, value in settings.model_dump(mode="json").items():
        env[f"RADIO247_{name.upper()}"] = "" if value is None else str(value)
    with (tmp_path / "services.log").open("w+") as log:
        service = subprocess.Popen(
            [sys.executable, "-m", "radio247", "serve"], env=env, stdout=log, stderr=log
        )
        try:
            with httpx.Client(
                base_url=settings.internal_api_url, timeout=2, trust_env=False
            ) as client:

                def healthy():
                    try:
                        return client.get("/healthz").status_code == 200
                    except httpx.TransportError:
                        return False

                wait_for(healthy, [service])
                client.auth = ("admin", settings.admin_password_file.read_text().strip())
                wait_for(lambda: client.get("/api/status").json()["ready"] == 1, [service])
                track = client.get("/api/status").json()["tracks"][0]
                starts_with_default = position == "start"
                switch_at = (
                    settings.default_image_seconds
                    if starts_with_default
                    else track["samples"] / settings.sample_rate - settings.default_image_seconds
                )
                headers = {"X-Radio247-Action": "1"}

                def control(action):
                    payload = {"action": action}
                    if action == "play":
                        payload["track_id"] = track["id"]
                    client.post("/api/control", json=payload, headers=headers).raise_for_status()

                def wait_picture(use_default):
                    def matches():
                        status = client.get("/api/status").json()
                        radio = status["radio"]
                        if (
                            radio.get("track_id") == track["id"]
                            and status["cover_url"]
                            and (status["cover_url"] != track["cover_url"]) == use_default
                            and (
                                radio["position"] < switch_at
                                if starts_with_default
                                else radio["position"] >= switch_at
                            )
                            == use_default
                        ):
                            return status
                        return None

                    status = wait_for(matches, [service], timeout=12)
                    with Image.open(io.BytesIO(client.get(status["cover_url"]).content)) as image:
                        pixel = image.convert("RGB").getpixel((80, 80))
                        assert isinstance(pixel, tuple) and pixel[0 if use_default else 2] > 150
                    return status

                control("start")
                wait_picture(starts_with_default)
                switched = wait_picture(not starts_with_default)
                assert switched["radio"]["position"] < switch_at + 1 / fps + 0.75
                wait_for(
                    lambda: (
                        client.get("/api/status").json()["radio"].get("publisher") == "streaming"
                    ),
                    [service],
                )
                wait_picture(starts_with_default)  # The single M4A loops naturally.
                wait_picture(not starts_with_default)
                control("play")
                wait_picture(starts_with_default)
                wait_picture(not starts_with_default)
                control("skip")
                wait_picture(starts_with_default)
                control("stop")
                wait_for(
                    lambda: client.get("/api/status").json()["radio"].get("playing") is False,
                    [service],
                )
            outputs = list(settings.output_dir.glob("radio-*.flv"))
            assert len(outputs) == 1
            with av.open(str(outputs[0])) as media:
                frames = list(media.decode(video=0))
                colors = []
                for frame in frames:
                    pixel = frame.to_ndarray(format="rgb24")[80, 80]
                    color = "red" if pixel[0] > pixel[2] else "blue"
                    if not colors or colors[-1] != color:
                        colors.append(color)
                if not starts_with_default and colors[0] == "red":
                    colors.pop(0)  # Idle default picture before audio becomes available.
                first, second = ("red", "blue") if starts_with_default else ("blue", "red")
                assert colors[:7] == [first, second, first, second, first, second, first]
            with av.open(str(outputs[0])) as media:
                samples = np.concatenate(
                    [frame.to_ndarray() for frame in media.decode(audio=0)], axis=1
                )
                assert np.max(np.abs(samples)) > 0.01
                assert abs(samples.shape[1] / settings.sample_rate - len(frames) / fps) < 1.1
        except BaseException:
            log.flush()
            log.seek(0)
            print(log.read())
            raise
        finally:
            if service.poll() is None:
                service.terminate()
                try:
                    service.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=5)


def test_real_service_streams_with_env_key_without_key_file(settings, tmp_path):
    with socket.socket() as web_probe, socket.socket() as rtmp_probe:
        web_probe.bind(("127.0.0.1", 0))
        rtmp_probe.bind(("127.0.0.1", 0))
        web_port = web_probe.getsockname()[1]
        rtmp_port = rtmp_probe.getsockname()[1]
    settings = settings.model_copy(
        update={
            "bind_host": "127.0.0.1",
            "web_port": web_port,
            "internal_api_url": f"http://127.0.0.1:{web_port}",
            "ingest_url": f"rtmp://127.0.0.1:{rtmp_port}/live",
            "autostart": False,
        }
    )
    key = "runtime-test-only-stream-key"
    env = dict(os.environ)
    for name, value in settings.model_dump(mode="json").items():
        env[f"RADIO247_{name.upper()}"] = "" if value is None else str(value)
    # JSON serialization deliberately masks SecretStr; inject only this test credential.
    env["RADIO247_STREAM_KEY"] = key
    env.pop("RADIO247_STREAM_KEY_FILE", None)
    target = tmp_path / "received.flv"
    processes = []
    with (tmp_path / "services.log").open("w+") as log:
        try:
            receiver = subprocess.Popen(
                [
                    settings.ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-listen",
                    "1",
                    "-timeout",
                    "30",
                    "-i",
                    f"{settings.ingest_url}/{key}",
                    "-c",
                    "copy",
                    "-f",
                    "flv",
                    "-y",
                    str(target),
                ],
                stdout=log,
                stderr=log,
            )
            processes.append(receiver)
            service = subprocess.Popen(
                [sys.executable, "-m", "radio247", "serve"],
                env=env,
                stdout=log,
                stderr=log,
            )
            processes.append(service)
            with httpx.Client(
                base_url=settings.internal_api_url, timeout=2, trust_env=False
            ) as client:

                def healthy():
                    try:
                        return client.get("/healthz").status_code == 200
                    except httpx.TransportError:
                        return False

                wait_for(healthy, processes)
                client.auth = ("admin", settings.admin_password_file.read_text().strip())
                headers = {"X-Radio247-Action": "1"}
                client.post(
                    "/api/control", json={"action": "start"}, headers=headers
                ).raise_for_status()
                wait_for(
                    lambda: (
                        client.get("/api/status").json()["radio"].get("publisher") == "streaming"
                    ),
                    processes,
                )
                time.sleep(2)
                status = client.get("/api/status")
                assert status.json()["output"] == "rtmp"
                assert status.json()["destination"] == f"{settings.ingest_url}/<stream-key>"
                assert key not in status.text
                assert key not in (settings.runtime_dir / "health.json").read_text()
                assert not (settings.data_dir / "secrets" / "youtube-key").exists()
                assert not list(settings.output_dir.iterdir())
                client.post(
                    "/api/control", json={"action": "stop"}, headers=headers
                ).raise_for_status()
                wait_for(
                    lambda: client.get("/api/status").json()["radio"].get("playing") is False,
                    [service],
                )
                assert receiver.wait(timeout=12) == 0
            with av.open(str(target)) as output:
                assert len(list(output.decode(video=0))) > settings.fps
            with av.open(str(target)) as output:
                assert len(list(output.decode(audio=0))) > 30
            log.flush()
            log.seek(0)
            assert key not in log.read()
        except BaseException:
            log.flush()
            log.seek(0)
            print(log.read())
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


@pytest.mark.parametrize("fps", [30, 1])
def test_real_web_radio_local_output_and_controls(settings, tmp_path, fps):
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    settings = settings.model_copy(
        update={
            "bind_host": "127.0.0.1",
            "web_port": port,
            "internal_api_url": f"http://127.0.0.1:{port}",
            "local_segment_seconds": 30,
            "fps": fps,
        }
    )
    song = tmp_path / "tone.wav"
    picture = tmp_path / "tone.png"
    make_song(song)
    Image.new("RGB", (320, 180), "blue").save(picture)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(song, "music/tone.wav")
        bundle.write(picture, "pictures/tone.png")
    env = dict(os.environ)
    for name, value in settings.model_dump(mode="json").items():
        env[f"RADIO247_{name.upper()}"] = "" if value is None else str(value)
    processes = []
    with (tmp_path / "services.log").open("w+") as log:
        try:
            web = subprocess.Popen(
                [sys.executable, "-m", "radio247", "serve"],
                env=env,
                stdout=log,
                stderr=log,
            )
            processes.append(web)
            with httpx.Client(
                base_url=settings.internal_api_url, timeout=2, trust_env=False
            ) as client:

                def healthy():
                    try:
                        return client.get("/healthz").status_code == 200
                    except httpx.TransportError:
                        return False

                wait_for(healthy, processes)
                client.auth = ("admin", settings.admin_password_file.read_text().strip())
                client.post(
                    "/api/upload",
                    files={"file": ("music.zip", archive.getvalue())},
                    headers={"X-Radio247-Action": "1"},
                ).raise_for_status()

                def streaming():
                    status = client.get("/api/status").json()
                    return (
                        status["ready"] == 1
                        and status["radio"].get("publisher") == "streaming"
                        and status["radio"].get("track") == "tone"
                    )

                wait_for(streaming, processes)
                time.sleep(3)
                cover_url = client.get("/api/status").json()["cover_url"]
                assert cover_url
                with Image.open(io.BytesIO(client.get(cover_url).content)) as preview:
                    pixel = preview.convert("RGB").getpixel((80, 80))
                    assert isinstance(pixel, tuple) and pixel[2] > 150
                headers = {"X-Radio247-Action": "1"}
                client.post(
                    "/api/control", json={"action": "skip"}, headers=headers
                ).raise_for_status()
                wait_for(
                    lambda: client.get("/api/status").json()["radio"].get("skip_ack") == 1,
                    processes,
                )
                client.post(
                    "/api/control", json={"action": "stop"}, headers=headers
                ).raise_for_status()
                wait_for(
                    lambda: client.get("/api/status").json()["radio"].get("playing") is False,
                    processes,
                )
                assert client.get("/api/status").json()["cover_url"] is None
                outputs = list(settings.output_dir.glob("radio-*.flv"))
                assert outputs
                with av.open(str(outputs[-1])) as media:
                    frames = list(media.decode(video=0))
                    assert len(frames) > settings.fps
                    assert any(
                        frame.to_ndarray(format="rgb24")[80, 80, 2] > 150 for frame in frames
                    )
                with av.open(str(outputs[-1])) as media:
                    audio = np.concatenate(
                        [frame.to_ndarray() for frame in media.decode(audio=0)], axis=1
                    )
                    assert audio.shape[1] > settings.sample_rate
                    assert np.max(np.abs(audio)) > 0.01
                    assert np.max(np.abs(audio)) < 1
                assert client.get("/api/status").json()["radio"]["underruns"] == 0
                selected_song, selected_picture = (
                    tmp_path / "selected.wav",
                    tmp_path / "selected.png",
                )
                make_song(selected_song, seconds=6)
                Image.new("RGB", (320, 180), "red").save(selected_picture)
                selection = io.BytesIO()
                with zipfile.ZipFile(selection, "w") as bundle:
                    bundle.write(selected_song, "selected.wav")
                    bundle.write(selected_picture, "selected.png")
                client.post(
                    "/api/upload",
                    files={"file": ("selection.zip", selection.getvalue())},
                    headers=headers,
                ).raise_for_status()
                wait_for(lambda: client.get("/api/status").json()["ready"] == 2, processes)
                tracks = {
                    track["title"]: track for track in client.get("/api/status").json()["tracks"]
                }
                client.post(
                    "/api/control",
                    json={"action": "play", "track_id": tracks["selected"]["id"]},
                    headers=headers,
                ).raise_for_status()

                def selected_is_playing():
                    status = client.get("/api/status").json()
                    return (
                        status["radio"].get("track_id") == tracks["selected"]["id"]
                        and status["cover_url"] == tracks["selected"]["cover_url"]
                        and status["radio"].get("publisher") == "streaming"
                    )

                wait_for(selected_is_playing, processes)
                active_file = client.get("/api/status").json()["radio"]["output_file"]
                client.post(
                    "/api/control",
                    json={"action": "play", "track_id": tracks["tone"]["id"]},
                    headers=headers,
                ).raise_for_status()
                wait_for(
                    lambda: (
                        client.get("/api/status").json()["radio"].get("track_id")
                        == tracks["tone"]["id"]
                    ),
                    processes,
                )
                assert client.get("/api/status").json()["radio"]["output_file"] == active_file
                child = next(
                    process
                    for process in psutil.Process(web.pid).children()
                    if process.cmdline()[-1] == "radio"
                )
                child.terminate()
                assert web.wait(timeout=15) != 0
        except BaseException:
            log.flush()
            log.seek(0)
            print(log.read())
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
