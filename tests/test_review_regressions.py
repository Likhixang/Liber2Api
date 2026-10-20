"""Independent-review regressions for mail authentication and shared validation."""
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

import app.auto_register as registration
import app.config as config
import app.database as db
import app.main as main
from app.libertai_client import LibertAIClient


@pytest_asyncio.fixture(autouse=True)
async def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    await db.init_db()
    main._login_failures.clear()


@pytest.mark.asyncio
async def test_private_inbound_poll_uses_credentials(monkeypatch):
    upstream = AsyncMock(spec=LibertAIClient)
    upstream.verify_code.return_value = {"access_token": "a", "refresh_token": "r"}
    upstream.fetch_chat_api_key.return_value = "key"
    registrar = registration.TempMailRegistrar(upstream)
    def handler(request):
        assert request.headers.get("Authorization") == "Bearer test-private-key"
        return httpx.Response(200, json={"success": True, "code": "123456"})
    monkeypatch.setattr(registration.asyncio, "sleep", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await registrar._try_inbound_webhook(client, {"api_url": "https://mail.test/code?email={email}", "domain": "example.test", "api_key": "test-private-key"})
    assert result and result["chat_api_key"] == "key"


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"provider_type": "custom_http", "domain": "example.test", "api_url": "https://mail.test/code?email={email}", "create_url": "https://mail.test/new/{email}"},
    {"provider_type": "neighbours", "api_url": "https://mail.test/{email}"},
    {"provider_type": "custom_http", "domain": "example.test", "api_url": "https://mail.test:bad/code?email={email}"},
])
async def test_saved_mail_config_matches_registration_validation(fields):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/api/mail-providers", json={"name": "invalid", **fields})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_password_header_uses_the_login_throttle(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "audit-password")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        for _ in range(5):
            assert (await client.post("/api/admin/login", json={"password": "wrong"})).status_code == 401
        response = await client.get("/api/admin/status", headers={"X-Admin-Password": "wrong"})
        assert response.status_code == 429


@pytest.mark.asyncio
async def test_trusted_proxy_preserves_https_origin_and_cookie(monkeypatch):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "audit-password")
    app = ProxyHeadersMiddleware(main.app, trusted_hosts=["172.18.0.2"])
    transport = httpx.ASGITransport(app=app, client=("172.18.0.2", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://admin.example.invalid") as client:
        response = await client.post("/api/admin/login", headers={"Origin": "https://admin.example.invalid", "X-Forwarded-Proto": "https"}, json={"password": "audit-password"})
    assert response.status_code == 200
    assert "; secure" in response.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_cancelled_stream_final_accounting_is_shielded():
    import anyio
    from unittest.mock import patch
    account = {"id": 1}
    recorded = []
    started = anyio.Event()
    async def source():
        started.set()
        await anyio.sleep_forever()
        yield b"unreachable"
    async def record(*args):
        await anyio.sleep(0)
        recorded.append(args)
    with patch.object(main.pool_manager, "handle_response", record):
        async with anyio.create_task_group() as tasks:
            async def consume():
                async for _ in main._accounted_stream(source(), account):
                    pass
            tasks.start_soon(consume)
            await started.wait()
            tasks.cancel_scope.cancel()
    assert recorded == [(account, 502)]
