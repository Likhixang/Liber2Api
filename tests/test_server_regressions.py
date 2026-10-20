"""Regression coverage for authentication, request validation and failover."""
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

import app.config as config
import app.database as db
import app.main as main


@pytest_asyncio.fixture(autouse=True)
async def isolated_app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "API_KEY", "")
    await db.init_db()
    yield
    main.app.dependency_overrides.clear()


def api_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False), base_url="http://test")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], "hello", 42, {"model": 123}, {"model": []}, {"model": {}}, {"model": ""}, {"model": "qwen3.8-27b", "messages": []}, {"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "hi"}], "stream": "false"}])
async def test_invalid_payload_is_client_error(payload):
    async with api_client() as client:
        response = await client.post("/v1/chat/completions", json=payload)
    assert response.status_code in (400, 422)


@pytest.mark.asyncio
async def test_disabled_bootstrap_key_cannot_bypass(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "audit-bootstrap")
    token = await db.create_api_token("bootstrap mirror", "audit-bootstrap")
    await db.update_api_token_status(token["id"], "disabled")
    async with api_client() as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer audit-bootstrap"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_legacy_key_cannot_bypass_disabled_state():
    await db.set_setting("api_key_enabled", "true")
    await db.set_setting("api_key", "audit-legacy")
    token = await db.create_api_token("legacy mirror", "audit-legacy")
    await db.update_api_token_status(token["id"], "disabled")
    async with api_client() as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer audit-legacy"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_copied_session_and_login_is_random(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "audit-password")
    async with api_client() as client:
        first = await client.post("/api/admin/login", json={"password": "audit-password"})
        first_cookie = first.cookies["liber2api_session"]
        second = await client.post("/api/admin/login", json={"password": "audit-password"})
        second_cookie = second.cookies["liber2api_session"]
        assert first_cookie != second_cookie
        assert "token" not in second.json()
        await client.post("/api/admin/logout")
    async with api_client() as client:
        response = await client.get("/api/tokens", headers={"Cookie": f"liber2api_session={second_cookie}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_session_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "audit-password")
    async with api_client() as client:
        login = await client.post("/api/admin/login", json={"password": "audit-password"})
        token_hash = hashlib.sha256(login.cookies["liber2api_session"].encode()).hexdigest()
        async with db.aiosqlite.connect(db.DATABASE_PATH) as conn:
            await conn.execute("UPDATE admin_sessions SET expires_at = ? WHERE token_hash = ?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), token_hash))
            await conn.commit()
        response = await client.get("/api/tokens")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_upstream_timeout_switches_account():
    accounts = [{"id": 1, "email": "a@example.invalid", "chat_api_key": "a"}, {"id": 2, "email": "b@example.invalid", "chat_api_key": "b"}]
    forward = AsyncMock(side_effect=[httpx.ReadTimeout("simulated timeout"), (200, {"choices": []})])
    with patch.object(main.pool_manager, "get_next_account", AsyncMock(side_effect=accounts)) as select, patch.object(main.pool_manager, "handle_response", AsyncMock()), patch.object(main.LibertAIClient, "forward_completion_json", forward):
        async with api_client() as client:
            response = await client.post("/v1/chat/completions", json={"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert forward.await_count == 2
    assert select.call_args_list[1].kwargs["exclude_ids"] == {1}


@pytest.mark.asyncio
async def test_invalid_upstream_request_not_retried_across_accounts():
    with patch.object(main.pool_manager, "get_next_account", AsyncMock(return_value={"id": 1, "email": "a@example.invalid", "chat_api_key": "a"})), patch.object(main.pool_manager, "handle_response", AsyncMock()), patch.object(main.LibertAIClient, "forward_completion_json", AsyncMock(return_value=(400, {"error": "bad request"}))) as forward:
        async with api_client() as client:
            response = await client.post("/v1/chat/completions", json={"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 400
    assert forward.await_count == 1


@pytest.mark.asyncio
async def test_duplicate_token_returns_conflict():
    async with api_client() as client:
        assert (await client.post("/api/tokens", json={"name": "one", "token": "audit-duplicate"})).status_code == 200
        response = await client.post("/api/tokens", json={"name": "two", "token": "audit-duplicate"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_unknown_provider_rejected_and_no_fake_test_success():
    async with api_client() as client:
        response = await client.post("/api/mail-providers/test", json={"name": "unknown", "provider_type": "not-a-provider"})
    assert response.status_code in (400, 422)


@pytest.mark.asyncio
async def test_saved_provider_test_uses_saved_credentials():
    provider = await db.create_mail_provider("private", "custom_http", domain="example.invalid", api_url="https://mail.example.invalid/code?email={email}", api_key="audit-mail-key")
    mocked = httpx.Response(200, json={"code": None})
    with patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=mocked)) as get:
        async with api_client() as client:
            response = await client.post(f"/api/mail-providers/{provider['id']}/test")
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer audit-mail-key"


@pytest.mark.asyncio
async def test_health_separate_from_readiness():
    async with api_client() as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 503
