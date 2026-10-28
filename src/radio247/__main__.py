from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import threading
import time

import httpx

from radio247.settings import Settings

LOG = logging.getLogger(__name__)

# Child exit is polled every second; the heavier HTTP/heartbeat probe runs less often.
PROBE_EVERY = 5


def check_web(settings: Settings) -> None:
    response = httpx.get(
        f"http://127.0.0.1:{settings.web_port}/healthz",
        timeout=settings.request_timeout,
        trust_env=False,
    )
    response.raise_for_status()


def check_radio(settings: Settings) -> None:
    report = json.loads((settings.runtime_dir / "health.json").read_text())
    if time.time() - report["heartbeat"] > max(10, settings.poll_interval * 5):
        raise ValueError("Radio heartbeat is stale")


def serve(settings: Settings) -> None:
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopping.set())
    children: list[subprocess.Popen] = []
    last_healthy = time.monotonic()
    started = last_healthy
    became_healthy = False
    tick = 0
    try:
        for role in ("web", "radio"):
            children.append(subprocess.Popen([sys.executable, "-m", "radio247", role]))
        while not stopping.wait(1):
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A managed service exited; restarting the container is required")
            tick += 1
            if tick % PROBE_EVERY:
                continue
            try:
                check_web(settings)
                check_radio(settings)
            except (OSError, ValueError, httpx.HTTPError) as error:
                limit = (
                    settings.service_stall_timeout
                    if became_healthy
                    else settings.service_start_timeout
                )
                reference = last_healthy if became_healthy else started
                if time.monotonic() - reference > limit:
                    raise RuntimeError("Managed services are unhealthy") from error
            else:
                last_healthy = time.monotonic()
                became_healthy = True
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)


def main() -> None:
    roles = {"serve", "web", "radio", "health", "health-web", "health-radio"}
    if len(sys.argv) != 2 or sys.argv[1] not in roles:
        raise SystemExit("Usage: python -m radio247 {serve|web|radio|health}")
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    role = sys.argv[1]
    if role == "serve":
        serve(settings)
    elif role == "web":
        import uvicorn

        from radio247.web import create_app

        uvicorn.run(
            create_app(settings),
            host=settings.bind_host,
            port=settings.web_port,
            log_level=settings.log_level.lower(),
            proxy_headers=False,
        )
    elif role == "radio":
        from radio247.radio import run

        run(settings)
    else:
        if role in {"health", "health-web"}:
            check_web(settings)
        if role in {"health", "health-radio"}:
            check_radio(settings)


if __name__ == "__main__":
    main()
