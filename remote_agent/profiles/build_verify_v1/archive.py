from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import struct
import unicodedata
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path

MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 1024
_CHUNK_BYTES = 64 * 1024
_DEVICE_NAME = re.compile(r"con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³]", re.IGNORECASE)
_REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT
_INVALID_ARCHIVE = "build.invalid_archive"


def _regular_snapshot(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and not (getattr(info, "st_file_attributes", 0) & _REPARSE)
        and 0 < info.st_size <= MAX_ZIP_BYTES
    )


def read_snapshot(path: Path | None, workdir: Path) -> tuple[bytes, str]:
    if path is None:
        raise ValueError(_INVALID_ARCHIVE)
    try:
        if not _regular_snapshot(path.lstat()):
            raise ValueError(_INVALID_ARCHIVE)
        resolved = path.resolve(strict=True)
        if resolved.is_relative_to(workdir.resolve()):
            raise ValueError(_INVALID_ARCHIVE)
        flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(resolved, os.O_RDONLY | flags), "rb") as source:
            if not _regular_snapshot(os.fstat(source.fileno())):
                raise ValueError(_INVALID_ARCHIVE)
            data = source.read(MAX_ZIP_BYTES + 1)
        if not 0 < len(data) <= MAX_ZIP_BYTES:
            raise ValueError(_INVALID_ARCHIVE)
    except (OSError, RuntimeError, ValueError):
        raise ValueError(_INVALID_ARCHIVE) from None
    return data, hashlib.sha256(data).hexdigest()


def _parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.orig_filename
    parts = tuple(name.removesuffix("/").split("/"))
    if len(name.encode("utf-8")) > 240 or len(parts) > 16 or any(
        not part
        or part.endswith((" ", "."))
        or not part.isprintable()
        or any(char in '<>:"\\|?*~' for char in part)
        or _DEVICE_NAME.fullmatch(part.partition(".")[0].rstrip(" "))
        for part in parts
    ):
        raise ValueError(_INVALID_ARCHIVE)
    return parts


def _members(archive: zipfile.ZipFile, zip_bytes: int) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    entries = archive.infolist()
    if not 0 < len(entries) <= MAX_ENTRIES:
        raise ValueError(_INVALID_ARCHIVE)
    layout: dict[tuple[str, ...], tuple[tuple[str, ...], bool]] = {}
    named: set[tuple[str, ...]] = set()
    compressed = expanded = 0
    members = []
    for info in entries:
        parts = _parts(info)
        directory = info.is_dir()
        mode = stat.S_IFMT(info.external_attr >> 16)
        dos_attributes = info.external_attr & 0xFFFF
        compressed += info.compress_size
        expanded += info.file_size
        extra = info.extra
        while extra:
            field, size = struct.unpack_from("<HH", extra)
            if field in {0x000D, 0x756E} or size + 4 > len(extra):
                raise ValueError(_INVALID_ARCHIVE)
            extra = extra[4 + size:]
        if (
            mode not in (0, stat.S_IFDIR if directory else stat.S_IFREG)
            or dos_attributes & (_REPARSE | 0x08)
            or (dos_attributes & stat.FILE_ATTRIBUTE_DIRECTORY and not directory)
            or (directory and info.file_size)
            or info.flag_bits & 0x61
            or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or (info.compress_type == zipfile.ZIP_STORED and info.compress_size != info.file_size)
            or compressed > zip_bytes
            or expanded > MAX_EXPANDED_BYTES
        ):
            raise ValueError(_INVALID_ARCHIVE)
        key = tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)
        if key in named:
            raise ValueError(_INVALID_ARCHIVE)
        named.add(key)
        for depth in range(1, len(parts) + 1):
            node = (parts[:depth], directory or depth < len(parts))
            if layout.setdefault(key[:depth], node) != node or len(layout) > MAX_ENTRIES:
                raise ValueError(_INVALID_ARCHIVE)
        members.append((info, parts))
    if layout.get(("tests",)) != (("tests",), True):
        raise ValueError(_INVALID_ARCHIVE)
    return members


def inspect_zip(
    data: bytes,
    destination: Path | None = None,
    check_active: Callable[[], None] | None = None,
) -> None:
    """Validate the ZIP layout, and check payloads when extracting into a fresh directory."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = _members(archive, len(data))
            if destination is None:
                return
            expanded = 0
            for info, parts in members:
                if check_active is not None:
                    check_active()
                target = destination.joinpath(*parts)
                with archive.open(info) as source:
                    if info.is_dir():
                        if source.read(1):
                            raise ValueError(_INVALID_ARCHIVE)
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with target.open("xb") as output:
                        while chunk := source.read(_CHUNK_BYTES):
                            if check_active is not None:
                                check_active()
                            written += len(chunk)
                            expanded += len(chunk)
                            if written > info.file_size or expanded > MAX_EXPANDED_BYTES:
                                raise ValueError(_INVALID_ARCHIVE)
                            output.write(chunk)
                    if written != info.file_size:
                        raise ValueError(_INVALID_ARCHIVE)
    except (OSError, EOFError, RuntimeError, ValueError, zipfile.BadZipFile, zlib.error, struct.error):
        raise ValueError(_INVALID_ARCHIVE) from None
