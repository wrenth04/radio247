from __future__ import annotations

import logging
import stat
import uuid
import zipfile
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath

from fastapi import HTTPException

from radio247.library import AUDIO_SUFFIXES, IMAGE_SUFFIXES
from radio247.settings import Settings


def import_zip(path: Path, settings: Settings) -> dict[str, int]:
    staged: list[tuple[Path, Path]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > settings.zip_max_files:
                raise HTTPException(413, "ZIP entry count limit exceeded")
            if sum(entry.file_size for entry in entries) > settings.zip_max_bytes:
                raise HTTPException(413, "ZIP uncompressed size limit exceeded")
            selected: list[tuple[zipfile.ZipInfo, Path]] = []
            destinations: set[str] = set()
            skipped = 0
            for entry in entries:
                name = entry.orig_filename
                parts = name.rstrip("/").split("/")
                if (
                    not name
                    or "\0" in name
                    or "\\" in name
                    or name.startswith("/")
                    or PureWindowsPath(name).drive
                    or any(part in ("", ".", "..") for part in parts)
                ):
                    raise HTTPException(400, "ZIP contains an unsafe path")
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG):
                    raise HTTPException(400, "ZIP links and special files are not supported")
                if entry.flag_bits & 1:
                    raise HTTPException(400, "Encrypted ZIP files are not supported")
                if entry.is_dir():
                    continue
                member = PurePosixPath(name)
                suffix = member.suffix.lower()
                if any(
                    part.startswith(".") or part == "__MACOSX" for part in parts
                ) or suffix not in AUDIO_SUFFIXES | set(IMAGE_SUFFIXES):
                    skipped += 1
                    continue
                if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise HTTPException(400, "ZIP supports only stored or Deflate compression")
                if entry.file_size == 0:
                    raise HTTPException(400, "ZIP contains an empty media file")
                if entry.file_size > settings.upload_max_bytes:
                    raise HTTPException(413, "ZIP media file size limit exceeded")
                directory = settings.images_dir if suffix in IMAGE_SUFFIXES else settings.media_dir
                target = directory / (member.stem + suffix)
                key = str(target).casefold()
                if key in destinations:
                    raise HTTPException(400, "ZIP contains duplicate destination filenames")
                if target.is_dir():
                    raise HTTPException(400, "ZIP destination conflicts with an existing directory")
                destinations.add(key)
                selected.append((entry, target))
            if not selected:
                raise HTTPException(400, "ZIP contains no supported audio, PNG or MP4 files")

            total = 0
            for entry, target in selected:
                temporary = target.parent / f".upload-{uuid.uuid4().hex}"
                staged.append((temporary, target))
                size = 0
                with archive.open(entry) as source, temporary.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        size += len(chunk)
                        if total > settings.zip_max_bytes or size > settings.upload_max_bytes:
                            raise HTTPException(413, "ZIP extracted size limit exceeded")
                        output.write(chunk)
                if size != entry.file_size:
                    raise HTTPException(400, "ZIP media size does not match its metadata")

        # Validate every member before publication; pictures precede new audio files.
        for temporary, target in sorted(staged, key=lambda item: item[1].suffix not in IMAGE_SUFFIXES):
            temporary.replace(target)
        return {"imported": len(staged), "skipped": skipped}
    except (zipfile.BadZipFile, EOFError, zlib.error, NotImplementedError) as error:
        raise HTTPException(400, "Invalid, damaged or unsupported ZIP archive") from error
    except OSError as error:
        logging.getLogger(__name__).exception("ZIP import storage failure")
        raise HTTPException(
            507, "ZIP storage error; some files may already have been imported. Check disk space."
        ) from error
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
