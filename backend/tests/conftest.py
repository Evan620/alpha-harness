"""Offline safety rails shared by the backend test suite."""

from __future__ import annotations

import importlib.util
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("alpha_harness") is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(scope="session", autouse=True)
def _isolate_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep every implicit Settings instance away from the real data directory."""
    from alpha_harness.config import get_settings

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("AH_DATA_DIR", str(tmp_path_factory.mktemp("ah-session")))
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _real_data_dir_untouched(_isolate_data_dir: None) -> Iterator[None]:
    """Fail if a test reaches the developer's live SQLite or DuckDB files."""
    paths = (
        Path.home() / ".alpha-harness" / "harness.db",
        Path.home() / ".alpha-harness" / "catalog.duckdb",
    )
    before = {path: path.stat().st_mtime_ns if path.exists() else None for path in paths}

    yield

    for path, expected in before.items():
        actual = path.stat().st_mtime_ns if path.exists() else None
        assert actual == expected, (
            f"tests changed {path}: expected mtime {expected!r}, got {actual!r}. "
            "Tests must use a temporary data directory because DuckDB is single-writer "
            "(db/duck.py:398 CatalogLockedError)."
        )


@pytest.fixture(autouse=True)
def _no_inet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny real TCP connections while preserving local AF_UNIX socketpairs."""
    real_connect = socket.socket.connect

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"test opened a real TCP connection to {address!r}")
        real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
