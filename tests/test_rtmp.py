import socket
import subprocess
import time
from fractions import Fraction

import av
import numpy as np
from pydantic import SecretStr

from radio247.library import Library, Store
from radio247.media import AudioEncoder, CachedVideo, VideoSpec
from radio247.publisher import Publisher


def test_actual_rtmp_output_to_local_receiver(settings, tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = settings.model_copy(
        update={
            "ingest_url": f"rtmp://127.0.0.1:{port}/live",
            "stream_key": SecretStr("local-test-stream-key"),
        }
    )
    library = Library(settings, Store(settings))
    picture = library.picture(None)
    video = CachedVideo(picture, VideoSpec.from_settings(settings))
    audio = AudioEncoder(settings)
    publisher = Publisher(settings)
    target = tmp_path / "received.flv"
    with (tmp_path / "receiver.log").open("w+") as log:
        receiver = subprocess.Popen(
            [
                settings.ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-listen",
                "1",
                "-timeout",
                "10",
                "-i",
                f"{settings.ingest_url}/local-test-stream-key",
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
        try:
            start = time.monotonic()
            video_frame = 0
            streamed = False
            for pts in range(0, settings.sample_rate * 5, 1024):
                publisher.tick(picture, audio.extra)
                samples = np.zeros((1024, settings.channels), dtype="float32")
                for packet in audio.encode(samples, pts):
                    while Fraction(video_frame, settings.fps) <= packet.timestamp:
                        publisher.send(video.at(video_frame))
                        video_frame += 1
                    publisher.send(packet)
                streamed = streamed or publisher.state == "streaming"
                time.sleep(max(0, start + (pts + 1024) / settings.sample_rate - time.monotonic()))
            for packet in audio.flush():
                publisher.send(packet)
            publisher.close(graceful=True)
            receiver.wait(timeout=12)
            assert streamed
            assert target.exists()
            with av.open(str(target)) as output:
                assert len(list(output.decode(video=0))) > settings.fps
            with av.open(str(target)) as output:
                assert len(list(output.decode(audio=0))) > 30
        except BaseException:
            log.flush()
            log.seek(0)
            print(log.read())
            raise
        finally:
            publisher.close()
            if receiver.poll() is None:
                receiver.terminate()
                try:
                    receiver.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    receiver.kill()
                    receiver.wait(timeout=5)
