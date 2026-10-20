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
async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


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


@pytest.mark.asyncio
async def test_multi_token_management():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Enable auth policy
        resp = await client.post("/api/settings/auth-policy", json={"enabled": True})
        assert resp.status_code == 200

        # 2. Create Token A and Token B
        resp_a = await client.post("/api/tokens", json={"name": "Client A", "token": "sk-token-a-1111"})
        assert resp_a.status_code == 200
        tok_a_id = resp_a.json()["token"]["id"]

        resp_b = await client.post("/api/tokens", json={"name": "Client B", "token": "sk-token-b-2222"})
        assert resp_b.status_code == 200
        tok_b_id = resp_b.json()["token"]["id"]

        # 3. Request with Token A should pass auth check (will return 503 if pool is empty, but not 401)
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-token-a-1111"},
            json={"model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code != 401

        # 4. Disable Token A
        resp = await client.patch(f"/api/tokens/{tok_a_id}/status", json={"status": "disabled"})
        assert resp.status_code == 200

        # Now Token A should get 401, while Token B still works
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-token-a-1111"},
            json={"model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 401

        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-token-b-2222"},
            json={"model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code != 401

        # 5. Delete Token B
        resp = await client.delete(f"/api/tokens/{tok_b_id}")
        assert resp.status_code == 200

        # Now Token B should also get 401
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-token-b-2222"},
            json={"model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 401

        # 6. Disable auth policy -> Anyone can call
        resp = await client.post("/api/settings/auth-policy", json={"enabled": False})
        assert resp.status_code == 200

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code != 401


def test_model_resolver_and_canonicalization():
    from app.model_resolver import canonicalize_models_list, resolve_upstream_model

    # 1. Test canonicalize list merges -thinking and -tee and filters adulterated models
    raw_models = [
        {"id": "qwen3.8-27b"},
        {"id": "qwen3.8-27b-thinking"},
        {"id": "qwen3.8-27b-tee"},
        {"id": "qwen3.8-27b-tee-thinking"},
        {"id": "deepseek-v4.1-flash-1017"},
        {"id": "deepseek-v4.1-flash-1017-thinking"},
        {"id": "glm-5.3"},
        {"id": "glm-5.3-flash"},
        {"id": "hermes-3-8b-tee"},
    ]
    canonical = canonicalize_models_list(raw_models)
    canonical_ids = [m["id"] for m in canonical]
    # GLM is blocked, only pure-blood models are exposed
    assert canonical_ids == ["qwen3.8-27b", "deepseek-v4.1-flash-1017", "hermes-3-8b"]

    # 2. Test resolve_upstream_model
    # Case A: Blocked adulterated models raise ValueError
    with pytest.raises(ValueError):
        resolve_upstream_model("glm-5.3")
    with pytest.raises(ValueError):
        resolve_upstream_model("glm-5.3-flash")

    # Case B: Alias resolution (deepseek-v4.1-flash upgrades to official deepseek-v4.1-flash-1017)
    assert resolve_upstream_model("deepseek-v4.1-flash", {"reasoning_effort": "none"}) == "deepseek-v4.1-flash-1017"
    assert resolve_upstream_model("deepseek-v4.1-flash") == "deepseek-v4.1-flash-1017-thinking"

    # Case C: Model with tee=true (defaults to thinking enabled)
    assert resolve_upstream_model("qwen3.8-27b", {"tee": True}) == "qwen3.8-27b-tee-thinking"
    assert resolve_upstream_model("qwen3.8-27b", {"tee": True, "reasoning_effort": "none"}) == "qwen3.8-27b-tee"

    # Case D: Default without reasoning_effort defaults to thinking enabled
    assert resolve_upstream_model("deepseek-v4.1-flash-1017") == "deepseek-v4.1-flash-1017-thinking"

    # Case D: Model with tee=true and reasoning_effort=max -> FIRST tee THEN thinking
    assert resolve_upstream_model("qwen3.8-27b", {"tee": True, "reasoning_effort": "max"}) == "qwen3.8-27b-tee-thinking"

    # Case E: Model that is only TEE upstream (hermes-3-8b)
    assert resolve_upstream_model("hermes-3-8b") == "hermes-3-8b-tee"


def test_duck2api_proxy_url_resolution():
    from app.libertai_client import resolve_forward_proxy

    orig = config.PROXY_URL
    try:
        # Resin proxy style with auth credentials
        config.PROXY_URL = "http://Default:my_secret_token@resin:2260"
        proxy = resolve_forward_proxy("user_tom@example.com")
        assert proxy == "http://Default.user_tom%40example.com:my_secret_token@resin:2260"

        # General proxy without credentials
        config.PROXY_URL = "http://127.0.0.1:7890"
        proxy_gen = resolve_forward_proxy("user_tom@example.com")
        assert proxy_gen == "http://127.0.0.1:7890"
    finally:
        config.PROXY_URL = orig


@pytest.mark.asyncio
async def test_stream_reasoning_normalizer():
    from app.stream_normalizer import StreamReasoningNormalizer, normalize_sse_stream

    normalizer = StreamReasoningNormalizer()

    # 1. Test dual-write on pure thinking chunk
    chunk1 = {"choices": [{"delta": {"reasoning": "Thinking step 1"}}]}
    res1 = normalizer.process_chunk_dict(chunk1)
    assert len(res1) == 1
    assert res1[0]["choices"][0]["delta"]["reasoning"] == "Thinking step 1"
    assert res1[0]["choices"][0]["delta"]["reasoning_content"] == "Thinking step 1"

    # 2. Test initial empty content chunk does not prematurely trigger content mode
    chunk_empty = {"choices": [{"delta": {"role": "assistant", "content": ""}}]}
    res_empty = normalizer.process_chunk_dict(chunk_empty)
    assert len(res_empty) == 1
    assert not normalizer.has_started_content

    # 3. Test mixed chunk splitting (e.g. reasoning '.' and content '9.')
    chunk_mixed = {"choices": [{"delta": {"reasoning": ".", "content": "9."}}]}
    res_mixed = normalizer.process_chunk_dict(chunk_mixed)
    assert len(res_mixed) == 2
    # Frame 1: Flush thinking
    assert res_mixed[0]["choices"][0]["delta"]["reasoning"] == "."
    assert res_mixed[0]["choices"][0]["delta"]["reasoning_content"] == "."
    assert "content" not in res_mixed[0]["choices"][0]["delta"]
    # Frame 2: Start content
    assert res_mixed[1]["choices"][0]["delta"]["content"] == "9."
    assert "reasoning" not in res_mixed[1]["choices"][0]["delta"]
    assert normalizer.has_started_content is True

    # 4. Test monotonic state guard: jitter reasoning after content started
    # It must be smoothed into content and never jump back to thinking mode
    chunk_jitter = {"choices": [{"delta": {"reasoning": " jitter CoT"}}]}
    res_jitter = normalizer.process_chunk_dict(chunk_jitter)
    assert len(res_jitter) == 1
    assert res_jitter[0]["choices"][0]["delta"]["content"] == " jitter CoT"
    assert "reasoning" not in res_jitter[0]["choices"][0]["delta"]
    assert "reasoning_content" not in res_jitter[0]["choices"][0]["delta"]

    # 5. End-to-end SSE byte stream normalization
    raw_sse = (
        b'data: {"choices":[{"delta":{"reasoning":"Thought"}}]}\n\n'
        b'data: {"choices":[{"delta":{"reasoning":".","content":"Ans"}}]}\n\n'
        b'data: [DONE]\n\n'
    )

    async def fake_stream():
        yield raw_sse

    out_bytes = b""
    async for b_chunk in normalize_sse_stream(fake_stream()):
        out_bytes += b_chunk

    out_text = out_bytes.decode("utf-8")
    assert "Thought" in out_text
    assert "reasoning_content" in out_text
    assert "data: [DONE]" in out_text

    # 6. Test embedded <think>...</think> extraction from content
    normalizer2 = StreamReasoningNormalizer()
    chunk_think = {"choices": [{"delta": {"content": "<think>planning steps</think>final answer"}}]}
    res_think = normalizer2.process_chunk_dict(chunk_think)
    assert len(res_think) == 2
    assert res_think[0]["choices"][0]["delta"]["reasoning"] == "planning steps"
    assert res_think[0]["choices"][0]["delta"]["reasoning_content"] == "planning steps"
    assert res_think[1]["choices"][0]["delta"]["content"] == "final answer"


@pytest.mark.asyncio
async def test_proxy_settings_api():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET proxy settings
        resp = await client.get("/api/settings/proxy")
        assert resp.status_code == 200
        data = resp.json()
        assert "proxy_url" in data
        assert "resin_url" in data

        # POST proxy settings
        resp = await client.post(
            "/api/settings/proxy",
            json={
                "proxy_url": "http://Default:secret@resin:2260",
                "resin_url": "http://127.0.0.1:2260/test",
                "resin_platform_name": "TestPlatform",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify updated values
        resp2 = await client.get("/api/settings/proxy")
        data2 = resp2.json()
        assert data2["proxy_url"] == "http://Default:secret@resin:2260"
        assert data2["resin_url"] == "http://127.0.0.1:2260/test"
        assert data2["resin_platform_name"] == "TestPlatform"


@pytest.mark.asyncio
async def test_mail_providers_api():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. List mail providers (default seeded should exist)
        resp = await client.get("/api/mail-providers")
        assert resp.status_code == 200
        providers = resp.json()["providers"]
        assert len(providers) >= 1

        # 2. Add custom mail provider
        resp = await client.post(
            "/api/mail-providers",
            json={
                "name": "My Custom Worker",
                "provider_type": "custom_http",
                "domain": "my-domain.org",
                "api_url": "https://worker.dev/inbox?email={email}",
                "priority": 5,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        p_id = data["provider"]["id"]

        # 3. Toggle status
        resp = await client.patch(f"/api/mail-providers/{p_id}/status", json={"status": "disabled"})
        assert resp.status_code == 200

        # 4. Test provider endpoint
        resp = await client.post(
            "/api/mail-providers/test",
            json={
                "name": "Test",
                "provider_type": "custom_http",
                "domain": "test.org",
                "api_url": "https://example.com/test",
            },
        )
        assert resp.status_code == 200

        # 5. Delete provider
        resp = await client.delete(f"/api/mail-providers/{p_id}")
        assert resp.status_code == 200

