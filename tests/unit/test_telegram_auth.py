import stat

import pytest

from tg_jobs_searcher.telegram.auth import (
    SessionFileError,
    read_string_session,
    write_string_session,
)


def test_writes_string_session_with_private_permissions(tmp_path) -> None:
    path = tmp_path / "telethon.session"

    write_string_session(path, "session-value")

    assert read_string_session(path) == "session-value"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rejects_group_readable_session(tmp_path) -> None:
    path = tmp_path / "telethon.session"
    path.write_text("session-value", encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(SessionFileError, match="permissions must be 0600"):
        read_string_session(path)
