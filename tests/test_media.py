from fractions import Fraction

import av
import numpy as np
import pytest
from PIL import Image, ImageDraw

from radio247.media import (
    BLOCK,
    AudioEncoder,
    CachedVideo,
    Muxer,
    VideoSpec,
    prepare_image,
    prepare_video,
)


@pytest.mark.parametrize("size", [(90, 180), (360, 90), (80, 45)])
def test_picture_fills_frame_without_distorting_center(tmp_path, settings, size):
    source, target = tmp_path / "source.png", tmp_path / "prepared.png"
    image = Image.new("RGB", size, "red")
    x, y = size[0] // 2, size[1] // 2
    ImageDraw.Draw(image).rectangle((x - 5, y - 5, x + 5, y + 5), fill="lime")
    image.save(source)

    prepare_image(source, target, settings)

    with Image.open(target) as prepared:
        assert prepared.size == (settings.width, settings.height)
        pixels = np.array(prepared)
    for y, x in ((0, 0), (0, -1), (-1, 0), (-1, -1)):
        assert tuple(pixels[y, x]) == (255, 0, 0)
    ys, xs = np.where((pixels[:, :, 1] > 128) & (pixels[:, :, 0] < 128))
    assert abs((xs.max() - xs.min()) - (ys.max() - ys.min())) <= 2
    assert abs((xs.max() + xs.min()) / 2 - settings.width / 2) <= 3
    assert abs((ys.max() + ys.min()) / 2 - settings.height / 2) <= 3


@pytest.mark.parametrize("rate,channels,fps", [(44100, 2, 30), (48000, 1, 25), (44100, 2, 1)])
@pytest.mark.parametrize("off_gop", [False, True])
def test_cached_picture_switch_preserves_audio_and_timestamps(
    tmp_path, settings, rate, channels, fps, off_gop
):
    settings = settings.model_copy(update={"sample_rate": rate, "channels": channels, "fps": fps})
    spec = VideoSpec.from_settings(settings)
    videos = []
    for color in ("red", "blue"):
        image, target = tmp_path / f"{color}.png", tmp_path / f"{color}.mp4"
        Image.new("RGB", (320, 180), color).save(image)
        prepare_video(image, target, spec, settings)
        videos.append(CachedVideo(target, spec))
    assert videos[0].extra == videos[1].extra
    audio = AudioEncoder(settings)
    output = tmp_path / "stream.flv"
    muxer = Muxer(str(output), spec, videos[0], audio.extra, settings)
    video_frame = 0
    switch_frames = spec.gop + int(off_gop)
    for pts in range(0, rate * 6, BLOCK):
        signal = np.sin(np.arange(pts, pts + BLOCK) * 2 * np.pi * 440 / rate) * 0.1
        samples = np.repeat(signal[:, None], channels, axis=1).astype("float32")
        for packet in audio.encode(samples, pts):
            while Fraction(video_frame, fps) <= packet.timestamp:
                picture = video_frame // switch_frames
                muxer.write(
                    videos[picture % 2].at(video_frame, start_frame=picture * switch_frames)
                )
                video_frame += 1
            muxer.write(packet)
    for packet in audio.flush():
        while Fraction(video_frame, fps) <= packet.timestamp:
            picture = video_frame // switch_frames
            muxer.write(videos[picture % 2].at(video_frame, start_frame=picture * switch_frames))
            video_frame += 1
        muxer.write(packet)
    muxer.close()
    with av.open(str(output)) as container:
        last: dict[int, int] = {}
        for demuxed in container.demux():
            if demuxed.dts is not None:
                assert demuxed.dts > last.get(demuxed.stream.index, -1)
                last[demuxed.stream.index] = demuxed.dts
    with av.open(str(output)) as container:
        frames = list(container.decode(video=0))
        assert len(frames) > fps * 5
        assert frames[0].to_ndarray(format="rgb24")[80, 80, 0] > 200
        assert frames[switch_frames - 1].to_ndarray(format="rgb24")[80, 80, 0] > 200
        assert frames[switch_frames].key_frame
        assert frames[switch_frames].to_ndarray(format="rgb24")[80, 80, 2] > 200
    with av.open(str(output)) as container:
        decoded = np.concatenate([f.to_ndarray() for f in container.decode(audio=0)], axis=1)
        assert container.streams.audio[0].codec_context.sample_rate == rate
        assert decoded.shape[0] == channels
        assert abs(decoded.shape[1] - len(range(0, rate * 6, BLOCK)) * BLOCK) <= BLOCK
        assert np.max(np.abs(decoded)) < 0.2
