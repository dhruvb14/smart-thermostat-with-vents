"""
Tests for the one-shot flair.db → app.db rename helper in backend.main.
"""

from __future__ import annotations

from pathlib import Path

from backend.main import _migrate_db_filename


def test_fresh_dir_is_noop(tmp_path: Path) -> None:
    _migrate_db_filename(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_rename_when_only_old_present(tmp_path: Path) -> None:
    (tmp_path / "flair.db").write_bytes(b"sqlite-data")

    _migrate_db_filename(str(tmp_path))

    assert not (tmp_path / "flair.db").exists()
    assert (tmp_path / "app.db").read_bytes() == b"sqlite-data"


def test_noop_when_both_present(tmp_path: Path) -> None:
    (tmp_path / "flair.db").write_bytes(b"old-data")
    (tmp_path / "app.db").write_bytes(b"new-data")

    _migrate_db_filename(str(tmp_path))

    assert (tmp_path / "flair.db").read_bytes() == b"old-data"
    assert (tmp_path / "app.db").read_bytes() == b"new-data"


def test_rename_sidecars(tmp_path: Path) -> None:
    (tmp_path / "flair.db").write_bytes(b"main")
    (tmp_path / "flair.db-wal").write_bytes(b"wal")
    (tmp_path / "flair.db-shm").write_bytes(b"shm")

    _migrate_db_filename(str(tmp_path))

    assert not (tmp_path / "flair.db").exists()
    assert not (tmp_path / "flair.db-wal").exists()
    assert not (tmp_path / "flair.db-shm").exists()
    assert (tmp_path / "app.db").read_bytes() == b"main"
    assert (tmp_path / "app.db-wal").read_bytes() == b"wal"
    assert (tmp_path / "app.db-shm").read_bytes() == b"shm"


def test_idempotent_second_call(tmp_path: Path) -> None:
    (tmp_path / "flair.db").write_bytes(b"data")

    _migrate_db_filename(str(tmp_path))
    _migrate_db_filename(str(tmp_path))

    assert not (tmp_path / "flair.db").exists()
    assert (tmp_path / "app.db").read_bytes() == b"data"
