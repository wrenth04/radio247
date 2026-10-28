import io
import os
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from PIL import Image
from test_library import make_song
from test_runtime import wait_for

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(
        http_credentials={"username": "admin", "password": "ui-test-password"},
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    yield page
    context.close()
    assert not errors, errors


@pytest.fixture
def station(page):
    state = {
        "enabled": False,
        "radio_stale": False,
        "ready": 1,
        "skip": 0,
        "preparing": None,
        "scanning": False,
        "error": None,
        "output": "local",
        "destination": None,
        "cover_url": None,
        "radio": {"playing": False, "publisher": "stopped"},
        "tracks": [
            {
                "id": "ready",
                "title": "Evening music",
                "state": "ready",
                "cover_url": "/api/cover/ready.mp4",
                "error": None,
            },
            {
                "id": "waiting",
                "title": "Waiting music",
                "state": "preparing",
                "cover_url": None,
                "error": None,
            },
            {
                "id": "broken",
                "title": "失敗的音樂" * 30,
                "state": "failed",
                "cover_url": None,
                "error": "FFmpeg raw diagnostic: " + "long-error/" * 50,
            },
        ],
    }
    config = {"offline": False, "control_status": 200, "upload_status": 200}
    commands = []
    html = Path("src/radio247/static/index.html").read_text().replace("__POLL_MS__", "100")
    image = io.BytesIO()
    Image.new("RGB", (320, 180), "#66884b").save(image, format="PNG")

    def route(request):
        path = request.request.url.removeprefix("http://radio.test")
        if path == "/":
            request.fulfill(content_type="text/html", body=html)
        elif path == "/api/status":
            if config["offline"]:
                request.abort("connectionrefused")
            else:
                request.fulfill(json=state)
        elif path.startswith("/api/cover/"):
            request.fulfill(content_type="image/png", body=image.getvalue())
        elif path == "/api/control":
            assert request.request.headers["x-radio247-action"] == "1"
            commands.append(request.request.post_data_json)
            request.fulfill(status=config["control_status"], json={"detail": "Raw control failure"})
        elif path == "/api/upload":
            assert request.request.headers["x-radio247-action"] == "1"
            request.fulfill(
                status=config["upload_status"],
                json={"imported": 2, "skipped": 1, "detail": "ZIP raw failure"},
            )
        else:
            request.fulfill(status=404)

    page.route("**/*", route)
    return state, config, commands


@pytest.mark.parametrize("width", [1280, 390, 320])
def test_music_layout_selection_focus_and_diagnostics(page, station, width):
    state, _, commands = station
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("http://radio.test")
    art = page.locator(".track-art[data-track-id=ready]")
    expect(art).to_be_enabled()
    expect(page.locator("#destination")).not_to_be_visible()
    expect(page.locator("#status")).not_to_be_visible()
    expect(page.get_by_text("FFmpeg raw diagnostic:", exact=False)).not_to_be_visible()
    expect(page.locator(".track-art[data-track-id=waiting]")).to_be_disabled()
    expect(page.locator(".track-play[data-track-id=broken]")).to_be_disabled()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    art.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#message-text")).to_contain_text("已要求播放")
    assert commands == [{"action": "play", "track_id": "ready"}]
    expect(page.locator("#current-track")).to_have_text("讓音樂開始吧")
    state["enabled"] = True
    state["cover_url"] = "/api/cover/ready.mp4"
    state["radio"] = {
        "playing": True,
        "publisher": "streaming",
        "track_id": "ready",
        "track": "Evening music",
        "position": 12,
    }
    expect(page.locator("#current-track")).to_have_text("Evening music")
    expect(page.locator("#cover")).to_be_visible()
    expect(art).to_be_focused()
    expect(page.locator("#transport-toggle")).to_have_text("停止播送")
    expect(page.locator("#output-summary")).to_contain_text("僅在本機錄製")
    art.press("Enter")
    expect(page.locator("#message-text")).to_contain_text("已要求播放")
    assert commands[-1] == {"action": "play", "track_id": "ready"}

    page.locator("#search").fill("Evening")
    expect(page.locator(".track-row")).to_have_count(1)
    state["radio"]["position"] = 30
    expect(page.locator("#elapsed")).to_have_text("已播放 0:30")
    expect(page.locator("#search")).to_be_focused()
    page.locator("#search").fill("no matching song")
    expect(page.locator("#empty-title")).to_have_text("沒有符合的曲目")
    page.locator("#clear-filters").click()
    expect(page.locator(".track-row")).to_have_count(3)
    page.locator("[data-filter=failed]").click()
    expect(page.locator(".track-row")).to_have_count(1)
    page.locator(".track-errors summary").click()
    expect(page.get_by_text("FFmpeg raw diagnostic:", exact=False)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_reconnection_errors_and_stale_playback_are_distinct(page, station):
    state, config, commands = station
    state.update(enabled=True, output="rtmps", destination="rtmps://example.test/<stream-key>")
    state["radio"] = {
        "playing": True,
        "publisher": "reconnecting",
        "track": "Evening music",
        "track_id": "ready",
        "error": "Raw publisher failure",
    }
    page.goto("http://radio.test")
    expect(page.locator("#station-status")).to_have_text("電台播放中")
    expect(page.locator("#output-summary")).to_contain_text("正在自動重連")
    expect(page.locator("#operation-summary")).to_contain_text("播送遇到問題")
    expect(page.get_by_text("Raw publisher failure", exact=False)).not_to_be_visible()

    config["control_status"] = 409
    page.locator(".track-play[data-track-id=ready]").click()
    expect(page.locator("#message-text")).to_contain_text("目前無法播放")
    expect(page.locator(".track-play[data-track-id=ready]")).to_be_enabled()
    page.locator("#message-details").click()
    expect(page.locator("#request-error")).to_contain_text("Raw control failure")
    page.locator("#diagnostics>summary").click()

    state["radio_stale"] = True
    expect(page.locator("#station-status")).to_have_text("等待電台回應")
    expect(page.locator("#cover")).not_to_be_visible()
    expect(page.locator("[data-action=skip]")).to_be_disabled()
    config["offline"] = True
    expect(page.locator("#station-status")).to_have_text("連線中斷")
    expect(page.locator("#transport-toggle")).to_be_disabled()
    expect(page.locator(".track-play[data-track-id=ready]")).to_be_disabled()
    expect(page.locator("#results-count")).to_contain_text("上次同步資料")
    config["offline"] = False
    state["radio_stale"] = False
    state["radio"]["publisher"] = "streaming"
    state["radio"]["error"] = None
    expect(page.locator("#station-status")).to_have_text("電台播放中")
    expect(page.locator("#operation-alert")).not_to_be_visible()
    assert len(commands) == 1


def test_empty_library_upload_feedback_and_cover_failure(page, station):
    state, config, _ = station
    state["tracks"] = []
    state["ready"] = 0
    page.goto("http://radio.test")
    expect(page.locator("#empty-title")).to_have_text("你的電台，從第一首歌開始")
    page.locator("#empty-upload").click()
    expect(page.locator("#file")).to_be_focused()
    page.locator("#file").set_input_files(
        {"name": "music.zip", "mimeType": "application/zip", "buffer": b"mock upload"}
    )
    config["upload_status"] = 413
    page.locator("#upload-button").click()
    expect(page.locator("#upload-status")).to_contain_text("檔案超過上傳限制")
    expect(page.locator("#upload-button")).to_be_enabled()
    config["upload_status"] = 200
    page.locator("#upload-button").click()
    expect(page.locator("#upload-status")).to_contain_text("已匯入 2 個檔案，略過 1")
    expect(page.locator("#upload-status")).to_contain_text("等待背景準備")
    expect(page.locator("#file-selection")).not_to_be_visible()

    page.route("**/api/upload", lambda route: route.fulfill(json={"status": "uploaded"}))
    page.locator("#file").set_input_files(
        {"name": "single.wav", "mimeType": "audio/wav", "buffer": b"mock audio"}
    )
    page.locator("#upload-button").click()
    expect(page.locator("#upload-status")).to_have_text("上傳完成，背景準備就緒後即可播放。")

    page.route("**/api/cover/**", lambda route: route.fulfill(status=404))
    state["tracks"] = [
        {
            "id": "ready",
            "title": "No artwork",
            "state": "ready",
            "cover_url": "/api/cover/missing.mp4",
            "error": None,
        }
    ]
    state["ready"] = 1
    expect(page.locator(".track-detail")).to_have_text("封面無法載入")
    expect(page.locator(".track-art")).to_be_enabled()


def test_pending_controls_wait_for_radio_confirmation(page, station):
    state, _, _ = station
    pending = []
    page.route("**/api/control", lambda route: pending.append(route))
    page.goto("http://radio.test")
    toggle = page.locator("#transport-toggle")
    expect(toggle).to_be_enabled()
    toggle.click()
    expect(toggle).to_be_disabled()
    expect(page.locator(".track-art[data-track-id=ready]")).to_be_disabled()
    toggle.dispatch_event("click")
    assert len(pending) == 1
    assert pending[0].request.post_data_json == {"action": "start"}
    pending[0].fulfill(json={"status": "requested"})
    expect(toggle).to_be_enabled()
    expect(page.locator("#station-status")).to_have_text("已停止")
    state["enabled"] = True
    expect(page.locator("#station-status")).to_have_text("啟動中")
    state["radio"]["playing"] = True
    expect(page.locator("#station-status")).to_have_text("電台播放中")
    expect(page.locator("#current-track")).to_have_text("下一首，等你加入")
    toggle.click()
    expect(toggle).to_be_disabled()
    assert pending[1].request.post_data_json == {"action": "stop"}
    state["enabled"] = False
    pending[1].fulfill(json={"status": "requested"})
    expect(page.locator("#station-status")).to_have_text("正在停止")
    state["radio_stale"] = True
    expect(page.locator("#station-status")).to_have_text("等待電台回應")
    state["radio"].update(playing=False, publisher="stopped")
    state["radio_stale"] = False
    expect(page.locator("#station-status")).to_have_text("已停止")


def test_real_browser_import_select_and_stop(page, settings, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = settings.model_copy(
        update={
            "autostart": False,
            "bind_host": "127.0.0.1",
            "web_port": port,
            "internal_api_url": f"http://127.0.0.1:{port}",
            "ui_poll_seconds": 0.5,
        }
    )
    settings.admin_password_file.write_text("ui-test-password")
    song, cover = tmp_path / "Evening.wav", tmp_path / "Evening.png"
    make_song(song, seconds=6)
    Image.new("RGB", (320, 180), "#66884b").save(cover)
    archive = tmp_path / "music.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(song, song.name)
        bundle.write(cover, cover.name)
    env = dict(os.environ)
    for name, value in settings.model_dump(mode="json").items():
        env[f"RADIO247_{name.upper()}"] = "" if value is None else str(value)
    with (tmp_path / "service.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "radio247", "serve"], env=env, stdout=log, stderr=log
        )
        try:
            with httpx.Client(base_url=settings.internal_api_url, trust_env=False) as client:

                def healthy():
                    try:
                        return client.get("/healthz").status_code == 200
                    except httpx.TransportError:
                        return False

                wait_for(healthy, [process])
                page.goto(settings.internal_api_url)
                page.locator("#open-upload").click()
                page.locator("#file").set_input_files(archive)
                page.locator("#upload-button").click()
                expect(page.locator("#upload-status")).to_contain_text("已匯入 2", timeout=15000)
                art = page.locator(".track-art")
                expect(art).to_be_enabled(timeout=30000)
                art.click()
                expect(page.locator("#current-track")).to_have_text("Evening", timeout=15000)
                expect(page.locator("#cover")).to_be_visible(timeout=15000)
                expect(page.locator("#publisher-status")).to_have_text(
                    "本機檔案寫入中", timeout=15000
                )
                page.locator("#transport-toggle").click()
                expect(page.locator("#station-status")).to_have_text("已停止")
                expect(page.locator("#cover")).not_to_be_visible()
                assert list(settings.output_dir.glob("radio-*.flv"))
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
