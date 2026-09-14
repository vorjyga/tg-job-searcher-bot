"""Secure creation and loading of Telethon StringSession files."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from tg_jobs_searcher.config import ConfigurationError


class SessionFileError(ConfigurationError):
    """Raised when a session file is missing or has unsafe permissions."""


def read_string_session(path: Path) -> str:
    """Load a StringSession only when no other local user can read it."""
    if path.is_symlink():
        raise SessionFileError("TELEGRAM_SESSION_PATH must not be a symbolic link")
    try:
        file_stat = path.stat()
    except FileNotFoundError as exc:
        raise SessionFileError(
            "Telethon session file does not exist; run `tg-jobs-searcher auth` first"
        ) from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise SessionFileError("TELEGRAM_SESSION_PATH must point to a regular file")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise SessionFileError("Telethon session file permissions must be 0600")

    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SessionFileError("Telethon session file is empty")
    return value


def write_string_session(path: Path, value: str) -> None:
    """Atomically store a StringSession with mode 0600."""
    if not value:
        raise SessionFileError("Refusing to save an empty Telethon session")
    if path.is_symlink():
        raise SessionFileError("TELEGRAM_SESSION_PATH must not be a symbolic link")
    parent = path.parent
    if not parent.is_dir():
        raise SessionFileError(f"Session directory does not exist: {parent}")
    if path.exists() and not path.is_file():
        raise SessionFileError("TELEGRAM_SESSION_PATH must point to a regular file")

    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write(value)
            file_handle.write("\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
