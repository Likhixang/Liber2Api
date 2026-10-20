"""Offline defaults: tests must never reach real providers or persistent data."""
import os
import socket
import tempfile
from pathlib import Path

import pytest

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="liber2api-pytest-")
os.environ["DATA_DIR"] = str(Path(_TEST_ROOT.name) / "data")
os.environ["DATABASE_PATH"] = str(Path(_TEST_ROOT.name) / "data/default.db")
os.environ["SESSION_SECRET"] = "test-only-isolated-session-secret"
os.environ["ADMIN_PASSWORD"] = ""
os.environ["API_KEY"] = ""
os.environ["PROXY_URL"] = ""
os.environ["RESIN_URL"] = ""


@pytest.fixture(autouse=True)
def block_external_sockets(monkeypatch):
    def blocked(*args, **kwargs):
        raise OSError("Tests cannot connect to external services; use MockTransport")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def pytest_sessionfinish(session, exitstatus):
    _TEST_ROOT.cleanup()
