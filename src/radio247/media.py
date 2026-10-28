from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageOps

from radio247.settings import Settings

BLOCK = 1024


@dataclass(frozen=True)
class VideoSpec:
    width: int = 1920
    height: int = 1080
    bitrate: int = 5000000
    fps: int = 30
    gop: int = 60
    buffer_size: int = 10000000
    preset: str = "veryfast"

    @classmethod
    def from_settings(cls, settings: Settings) -> VideoSpec:
        return cls(
            settings.width,
            settings.height,
            settings.video_bitrate,
            settings.fps,
            settings.fps * settings.gop_seconds,
            settings.video_buffer_size,
            settings.video_preset,
        )


@dataclass(frozen=True)
class WirePacket:
    kind: str
    data: bytes
    pts: int
    dts: int
    duration: int
    time_base: Fraction
    keyframe: bool = False

    def packet(self, stream: av.stream.Stream, epoch: Fraction) -> av.Packet:
        packet = av.Packet(self.data)
        offset = round(epoch / self.time_base)
        packet.pts = self.pts - offset
        packet.dts = self.dts - offset
        packet.duration = self.duration
        packet.time_base = self.time_base
        packet.is_keyframe = self.keyframe
        packet.stream = stream
        return packet

    @property
    def timestamp(self) -> Fraction:
        return self.dts * self.time_base


def command(args: list[str], timeout: float = 3600) -> str:
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} exited with {result.returncode}: {result.stderr[-3000:]}")
    return result.stdout + result.stderr


def probe(path: Path, settings: Settings) -> float:
    metadata = json.loads(
        command(
            [
                settings.ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            settings.probe_timeout,
        )
    )
    if not any(stream["codec_type"] == "audio" for stream in metadata["streams"]):
        raise ValueError("No audio stream")
    duration = float(metadata["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid duration")
    return duration


def analyze(path: Path, settings: Settings) -> str:
    target = f"I={settings.target_lufs}:TP={settings.target_peak}:LRA={settings.target_lra}"
    limiter = f"alimiter=limit={10 ** (settings.target_peak / 20)}:level=false:latency=true"
    output = command(
        [
            settings.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-threads",
            str(settings.threads),
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            f"loudnorm={target}:print_format=json",
            "-f",
            "null",
            "-",
        ],
        settings.prepare_timeout,
    )
    values = json.loads(output[output.rfind("{") :])
    if values["input_i"] == "-inf":
        return limiter
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if not math.isfinite(float(values[key])):
            raise ValueError(f"Invalid loudness result: {key}")
    return (
        f"loudnorm={target}:linear=true"
        f":measured_I={values['input_i']}:measured_TP={values['input_tp']}"
        f":measured_LRA={values['input_lra']}:measured_thresh={values['input_thresh']}"
        f":offset={values['target_offset']},{limiter}"
    )


def prepare_image(source: Path | None, target: Path, settings: Settings) -> None:
    background = Image.new("RGB", (settings.width, settings.height), "black")
    if source:
        with Image.open(source) as original:
            if original.width * original.height > settings.image_max_pixels:
                raise ValueError("Image pixel limit exceeded")
            original.verify()
        with Image.open(source) as original:
            image = ImageOps.exif_transpose(original).convert("RGBA")
            image = ImageOps.fit(image, background.size, method=Image.Resampling.LANCZOS)
            background.paste(image, (0, 0), image)
    background.save(target, format="PNG")


def prepare_video(
    image: Path, target: Path, spec: VideoSpec, settings: Settings | None = None
) -> None:
    settings = settings or Settings()
    command(
        [
            settings.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(spec.fps),
            "-i",
            str(image),
            "-frames:v",
            str(spec.gop),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            spec.preset,
            "-threads",
            str(settings.threads),
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(spec.gop),
            "-keyint_min",
            str(spec.gop),
            "-bf",
            "0",
            "-b:v",
            str(spec.bitrate),
            "-minrate",
            str(spec.bitrate),
            "-maxrate",
            str(spec.bitrate),
            "-bufsize",
            str(spec.buffer_size),
            "-x264-params",
            "nal-hrd=cbr:force-cfr=1:scenecut=0:open-gop=0",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-movflags",
            "+faststart",
            "-y",
            str(target),
        ],
        settings.prepare_timeout,
    )


class CachedVideo:
    def __init__(self, path: Path, spec: VideoSpec | None = None):
        self.path = path
        self.spec = spec or VideoSpec()
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            self.extra = stream.codec_context.extradata
            self.packets = [
                (bytes(packet), packet.is_keyframe)
                for packet in container.demux(stream)
                if packet.size
            ]
        if not self.extra or len(self.packets) != self.spec.gop or not self.packets[0][1]:
            raise ValueError("Invalid closed GOP cache")

    def at(self, frame: int, *, start_frame: int = 0) -> WirePacket:
        data, key = self.packets[(frame - start_frame) % self.spec.gop]
        return WirePacket("video", data, frame, frame, 1, Fraction(1, self.spec.fps), key)


class AudioEncoder:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.codec = av.CodecContext.create("aac", "w")
        self.codec.sample_rate = self.settings.sample_rate
        self.codec.layout = self.settings.layout
        self.codec.format = "fltp"
        self.codec.bit_rate = self.settings.audio_bitrate
        self.codec.time_base = Fraction(1, self.settings.sample_rate)
        self.codec.flags |= av.codec.context.Flags.global_header
        self.codec.open()

    @property
    def extra(self) -> bytes:
        extra = self.codec.extradata
        if extra is None:
            raise ValueError("AAC encoder did not produce extradata")
        return extra

    def encode(self, samples: np.ndarray, pts: int) -> list[WirePacket]:
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(samples.T), format="fltp", layout=self.settings.layout
        )
        frame.sample_rate = self.settings.sample_rate
        frame.time_base = Fraction(1, self.settings.sample_rate)
        frame.pts = pts
        return self._packets(self.codec.encode(frame))

    def flush(self) -> list[WirePacket]:
        return self._packets(self.codec.encode(None))

    def _packets(self, packets: list[av.Packet]) -> list[WirePacket]:
        result = []
        for packet in packets:
            pts, dts, duration = packet.pts, packet.dts, packet.duration
            if pts is None or dts is None or duration is None:
                raise ValueError("AAC encoder returned a packet without timing")
            if pts >= 0:
                result.append(
                    WirePacket(
                        "audio",
                        bytes(packet),
                        pts,
                        dts,
                        duration,
                        Fraction(1, self.settings.sample_rate),
                    )
                )
        return result


class Muxer:
    def __init__(
        self,
        url: str,
        spec: VideoSpec,
        video: CachedVideo,
        audio_extra: bytes,
        settings: Settings | None = None,
    ):
        settings = settings or Settings()
        self.container = av.open(
            url,
            "w",
            format="flv",
            options={"flvflags": "no_duration_filesize"},
            timeout=(settings.connect_timeout, settings.stall_timeout),
        )
        # The source container must remain open while copying the remux template.
        with av.open(str(video.path)) as template:
            video_stream = self.container.add_stream_from_template(template.streams.video[0])
        audio = self.container.add_stream("aac", rate=settings.sample_rate)
        audio.layout = settings.layout
        audio.codec_context.bit_rate = settings.audio_bitrate
        audio.codec_context.extradata = audio_extra
        self.streams = {"video": video_stream, "audio": audio}
        self.epoch: Fraction | None = None

    def write(self, packet: WirePacket) -> bool:
        if self.epoch is None:
            if packet.kind != "video" or not packet.keyframe:
                return False
            self.epoch = packet.timestamp
        if packet.timestamp < self.epoch:
            return False
        self.container.mux(packet.packet(self.streams[packet.kind], self.epoch))
        return True

    def close(self) -> None:
        self.container.close()
