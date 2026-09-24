"""Bounded-memory output delivery with atomic, integrity-checked publication."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping
from urllib.parse import quote

from .errors import NodusError, ValidationError


def portable_output_name(name: str) -> bool:
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None
            and name not in (".", "..") and not name.endswith(".")
            and name.split(".")[0].upper() not in reserved)


def output_destinations(root: Path, outputs: list) -> list[tuple[object, Path]]:
    """Plan portable file names without trusting remote paths or overwriting files."""
    seen = set()
    planned = []
    for output in outputs:
        for name in (output.stage_id, *output.name.split("/")):
            if not portable_output_name(name):
                raise ValidationError("Output stage and file names must be portable single path components.")
        directory = root / output.stage_id
        if output.member_id:
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", output.member_id) or output.member_id in (".", ".."):
                raise ValidationError("Invalid output member identity.")
            directory = directory / ("member-" + quote(output.member_id, safe=""))
        destination = directory / output.name
        key = str(destination).casefold()
        if key in seen:
            raise ValidationError("Output names collide on the local filesystem.")
        seen.add(key)
        for part in (destination, *destination.parents):
            if part.is_symlink():
                raise ValidationError("Download destinations cannot contain symbolic links.")
        if destination.exists():
            raise ValidationError("An output file already exists. Choose a new download directory.")
        planned.append((output, destination))
    return planned


def download_path(workload_id: str, name: str) -> str:
    if (not isinstance(name, str) or len(name) > 1024 or any(not portable_output_name(part) for part in name.split("/"))):
        raise ValidationError("Output name must contain portable relative path components.")
    return f"/v1/workloads/{workload_id}/outputs/{quote(name, safe='')}"


@contextmanager
def verified_file(destination: str | os.PathLike[str], headers: Mapping[str, str], *, overwrite: bool = True) -> Iterator[Callable[[bytes], None]]:
    digest = headers.get("X-Nodus-SHA256", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise NodusError("Output response has no valid SHA-256. Download was not saved.")
    length = headers.get("Content-Length")
    if length is not None and not re.fullmatch(r"[0-9]+", length):
        raise NodusError("Output response has an invalid Content-Length.")
    expected = int(length) if length is not None else None
    destination = Path(destination)
    parent = destination.parent
    if not overwrite:
        if any(p.is_symlink() for p in (parent, *parent.parents)):
            raise ValidationError("Download destinations cannot contain symbolic links.")
        if destination.exists() or destination.is_symlink():
            raise ValidationError("An output file already exists. Choose a new download directory.")
    identity = parent.stat()
    fd, temporary = tempfile.mkstemp(prefix=".nodus-download-", dir=destination.parent)
    hasher = hashlib.sha256()
    count = 0
    try:
        with os.fdopen(fd, "wb") as stream:
            def write(chunk: bytes) -> None:
                nonlocal count
                count += len(chunk)
                if expected is not None and count > expected:
                    raise NodusError("Output is larger than its declared Content-Length.")
                hasher.update(chunk)
                stream.write(chunk)
            yield write
            if expected is not None and count != expected:
                raise NodusError("Output length mismatch. Download was not saved.")
            if hasher.hexdigest() != digest.lower():
                raise NodusError("Output SHA-256 mismatch. Download was not saved.")
        if overwrite:
            os.replace(temporary, destination)
        else:
            current = parent.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino) or any(p.is_symlink() for p in (parent, *parent.parents)):
                raise ValidationError("Download directory changed during the transfer.")
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise ValidationError("An output file already exists. Choose a new download directory.") from None
    finally:
        current = parent.stat()
        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino) and os.path.exists(temporary):
            os.unlink(temporary)
