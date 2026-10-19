import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.config as config
import app.database as db
from app.main import app


@pytest_asyncio.fixture(autouse=True)
async def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = Path(f.name)

    orig_path = config.DATABASE_PATH
    config.DATABASE_PATH = temp_path
    db.DATABASE_PATH = temp_path

    await db.init_db()
    yield
    config.DATABASE_PATH = orig_path
    db.DATABASE_PATH = orig_path
    if temp_path.exists():
        temp_path.unlink()


@pytest.mark.asyncio
async def test_webui_index():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Liber2Api" in resp.text
        assert "添加新账号" in resp.text


@pytest.mark.asyncio
async def test_get_models_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert any(m["id"] == "qwen3.6-35b-a3b" for m in data["data"])


@pytest.mark.asyncio
async def test_send_and_verify_code_mocked():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Mock send email
        with patch("app.libertai_client.LibertAIClient.send_login_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            resp = await client.post("/api/auth/send-code", json={"email": "tester@example.com"})
            assert resp.status_code == 200
            assert resp.json()["success"] is True

        # Mock verify code and fetch chat key
        with patch("app.libertai_client.LibertAIClient.verify_code", new_callable=AsyncMock) as mock_verify, \
             patch("app.libertai_client.LibertAIClient.fetch_chat_api_key", new_callable=AsyncMock) as mock_key:
            mock_verify.return_value = {
                "access_token": "acc_mock",
                "refresh_token": "ref_mock",
                "token_type": "bearer",
            }
            mock_key.return_value = "chat_key_mock"

            resp = await client.post(
                "/api/auth/verify-code",
                json={"email": "tester@example.com", "code": "123456"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True

        # Check account in accounts list
        resp = await client.get("/api/accounts")
        assert resp.status_code == 200
        accounts = resp.json()["accounts"]
        assert len(accounts) == 1
        assert accounts[0]["email"] == "tester@example.com"


@pytest.mark.asyncio
async def test_chat_completions_proxy_mocked():
    # Insert account directly into db
    await db.add_or_update_account(
        email="active_user@example.com",
        access_token="tok",
        refresh_token="ref",
        chat_api_key="chat_key_123",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Mock forward_completion_json
        with patch("app.libertai_client.LibertAIClient.forward_completion_json", new_callable=AsyncMock) as mock_fwd:
            mock_fwd.return_value = (
                200,
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                },
            )

            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "qwen3.6-35b-a3b",
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == "chatcmpl-test"
            assert data["choices"][0]["message"]["content"] == "pong"


@pytest.mark.asyncio
async def test_admin_auth_flow():
    orig_pass = config.ADMIN_PASSWORD
    config.ADMIN_PASSWORD = "my_admin_secret"

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Accessing index without auth should show login gate
            resp = await client.get("/")
            assert resp.status_code == 200
            assert "管理员认证" in resp.text
            assert "ADMIN_PASSWORD" in resp.text

            # 2. Accessing protected API without auth should fail
            resp = await client.get("/api/accounts")
            assert resp.status_code == 401

            # 3. Wrong password should fail
            resp = await client.post("/api/admin/login", json={"password": "wrong_password"})
            assert resp.status_code == 401

            # 4. Correct password should succeed and set cookie
            resp = await client.post("/api/admin/login", json={"password": "my_admin_secret"})
            assert resp.status_code == 200
            assert "liber2api_session" in resp.cookies

            # 5. Accessing protected API with cookie should succeed
            resp = await client.get("/api/accounts")
            assert resp.status_code == 200

            # 6. Logout
            resp = await client.post("/api/admin/logout")
            assert resp.status_code == 200

            # 7. Accessing again should fail
            resp = await client.get("/api/accounts")
            assert resp.status_code == 401
    finally:
        config.ADMIN_PASSWORD = orig_pass


def test_resin_sticky_resolution():
    from app.libertai_client import resolve_request_endpoint

    # 1. Direct mode without Resin
    orig_resin = config.RESIN_URL
    config.RESIN_URL = ""
    try:
        url, headers = resolve_request_endpoint(
            "https://api.libertai.io/v1/chat/completions",
            account_email="tom@example.com",
        )
        assert url == "https://api.libertai.io/v1/chat/completions"
        assert "X-Resin-Account" not in headers

        # 2. Resin sticky mode
        config.RESIN_URL = "http://127.0.0.1:2260/my-proxy-token"
        config.RESIN_PLATFORM_NAME = "Default"
        url, headers = resolve_request_endpoint(
            "https://api.libertai.io/v1/chat/completions?stream=true",
            account_email="tom@example.com",
            extra_headers={"Content-Type": "application/json"},
        )
        expected_url = "http://127.0.0.1:2260/my-proxy-token/Default/https/api.libertai.io/v1/chat/completions?stream=true"
        assert url == expected_url
        assert headers["X-Resin-Account"] == "tom@example.com"
        assert headers["Content-Type"] == "application/json"
    finally:
        config.RESIN_URL = orig_resin
