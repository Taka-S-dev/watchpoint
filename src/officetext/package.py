"""Checks on an Office file as a zip package, made before any library unpacks it."""

from __future__ import annotations

import zipfile
from pathlib import Path

MAX_UNPACKED_BYTES = 256 * 1024 * 1024  # what the parts add up to once unpacked


class TooLarge(ValueError):
    """The package would unpack to more than is read."""


def check_unpacked_size(path: Path, limit: int | None = None) -> None:
    """Refuse a package whose parts add up to more than limit once unpacked.

    A few kilobytes of zip can declare gigabytes of XML (a decompression bomb); python-docx and
    openpyxl would unpack all of it into memory. The sizes are those the zip declares for each part,
    which zipfile also holds a part to when it is read.
    """
    limit = MAX_UNPACKED_BYTES if limit is None else limit
    try:
        with zipfile.ZipFile(path) as z:
            total = sum(info.file_size for info in z.infolist())
    except zipfile.BadZipFile:
        return  # not a zip: the library reading it says what is wrong
    if total > limit:
        raise TooLarge(
            f"展開すると {total / 1024 / 1024:.0f} MB になるので読みません（{limit // 1024 // 1024} MB まで）: {path.name}"
        )
