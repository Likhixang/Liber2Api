from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import app.config as config
import app.database as db
import app.main as main


@pytest_asyncio.fixture(autouse=True)
async def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "db.sqlite")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "audit-password")
    monkeypatch.setattr(config, "API_KEY", "")
    main._login_failures.clear()
    await db.init_db()


def client(base="http://test"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url=base)


@pytest.mark.asyncio
async def test_admin_rejects_foreign_origin_even_with_valid_cookie():
    async with client() as c:
        await c.post("/api/admin/login", json={"password": "audit-password"})
        r = await c.get("/api/tokens", headers={"Origin": "https://untrusted.invalid"})
        assert r.status_code == 403
        r = await c.get("/api/tokens", headers={"Origin": "http://test"})
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_https_cookie_secure_and_password_change_invalidates_session(monkeypatch):
    async with client("https://test") as c:
        r = await c.post("/api/admin/login", json={"password": "audit-password"})
        assert "; secure" in r.headers["set-cookie"].lower()
        monkeypatch.setattr(config, "ADMIN_PASSWORD", "audit-new-password")
        assert (await c.get("/api/tokens")).status_code == 401


@pytest.mark.asyncio
async def test_login_attempts_are_throttled():
    async with client() as c:
        for _ in range(5):
            assert (await c.post("/api/admin/login", json={"password": "wrong"})).status_code == 401
        assert (await c.post("/api/admin/login", json={"password": "wrong"})).status_code == 429


@pytest.mark.asyncio
async def test_stream_interruption_not_counted_as_success(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    account = {"id": 1, "email": "a@example.invalid", "chat_api_key": "k"}
    async def broken():
        yield b'data: {"choices":[]}\n\n'
        raise httpx.ReadError("simulated read interruption")
    with patch.object(main.pool_manager, "get_next_account", AsyncMock(return_value=account)), patch.object(main.pool_manager, "handle_response", AsyncMock()) as record, patch.object(main.LibertAIClient, "forward_completion_stream", AsyncMock(return_value=(200, broken()))):
        async with client() as c:
            r = await c.post("/v1/chat/completions", json={"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 200  # HTTP headers already sent; error is signalled in SSE.
    assert "upstream_error" in r.text
    record.assert_awaited_once_with(account, 502)
