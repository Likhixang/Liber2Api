"""Offline regressions: real isolated SQLite, fake upstream/mail transports only."""
import asyncio
import hashlib
import stat
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

import app.auto_register as registration
import app.database as db
from app.auto_register import TempMailRegistrar
from app.libertai_client import LibertAIClient, LibertAIException
from app.pool_manager import PoolManager


@pytest_asyncio.fixture(autouse=True)
async def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pool.db")
    await db.init_db()


async def add_account(name="one"):
    return await db.add_or_update_account(f"{name}@example.test", "access", "refresh", f"key-{name}")


def fake_upstream():
    return AsyncMock(spec=LibertAIClient)


@pytest.mark.asyncio
async def test_parallel_selection_reserves_lru_without_counting_requests():
    one, two = await add_account("one"), await add_account("two")
    managers = [PoolManager(fake_upstream()), PoolManager(fake_upstream())]
    chosen = await asyncio.gather(*(managers[i % 2].get_next_account() for i in range(8)))
    ids = [a["id"] for a in chosen]
    assert ids.count(one["id"]) == ids.count(two["id"]) == 4
    assert all(a["last_used_at"] for a in await db.get_all_accounts())
    assert all(a["total_requests"] == 0 for a in await db.get_all_accounts())


@pytest.mark.asyncio
async def test_request_exclusions_do_not_trigger_registration(monkeypatch):
    one, two = await add_account("one"), await add_account("two")
    register = AsyncMock(side_effect=AssertionError("must not replenish on request exclusion"))
    monkeypatch.setattr(registration.registrar, "register_account", register)
    manager = PoolManager(fake_upstream())
    assert (await manager.get_next_account(exclude_ids={one["id"]}))["id"] == two["id"]
    assert await manager.get_next_account(exclude_ids={one["id"], two["id"]}) is None
    register.assert_not_called()


@pytest.mark.asyncio
async def test_emergency_registration_budget_and_none_backoff(monkeypatch):
    register = AsyncMock(return_value=None)
    monkeypatch.setattr(registration.registrar, "register_account", register)
    manager = PoolManager(fake_upstream())
    assert await manager.get_next_account() is None
    assert await manager.get_next_account() is None
    register.assert_awaited_once_with(max_wait_seconds=20)


@pytest.mark.asyncio
async def test_replenishment_does_not_hold_selection_lock(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def register(**kwargs):
        started.set()
        await release.wait()
        return None
    monkeypatch.setattr(registration.registrar, "register_account", register)
    manager = PoolManager(fake_upstream())
    replenishing = asyncio.create_task(manager.get_next_account())
    await started.wait()
    account = await add_account()
    try:
        selected = await asyncio.wait_for(manager.get_next_account(), timeout=0.2)
        assert selected["id"] == account["id"]
    finally:
        release.set()
        await replenishing


@pytest.mark.asyncio
async def test_429_cools_down_and_counts_failure_without_identity_refresh():
    account = await add_account()
    client = fake_upstream()
    manager = PoolManager(client)
    assert await manager.handle_response(account, 429, "quota exceeded") is False
    current = await db.get_account_by_id(account["id"])
    assert current["status"] == "cooldown"
    assert datetime.fromisoformat(current["cooldown_until"]) > datetime.now(timezone.utc)
    assert current["failed_requests"] == current["total_requests"] == 1
    client.refresh_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_confirmed_invalid_key_preserves_auto_remove_business_policy():
    account = await add_account()
    client = fake_upstream()
    client.refresh_tokens.side_effect = httpx.ReadTimeout("temporary timeout")
    await PoolManager(client).handle_response(account, 401, "invalid api key")
    current = await db.get_account_by_id(account["id"])
    assert current is None


@pytest.mark.asyncio
async def test_explicit_revocation_preserves_auto_remove_policy():
    account = await add_account()
    client = fake_upstream()
    client.refresh_tokens.side_effect = LibertAIException("Session expired", 401)
    manager = PoolManager(client)
    await manager.handle_response(account, 401)
    assert await db.get_account_by_id(account["id"]) is None


@pytest.mark.asyncio
async def test_untyped_permanent_sounding_network_error_is_not_invalid():
    account = await add_account()
    client = fake_upstream()
    client.refresh_tokens.side_effect = RuntimeError("session expired while temporary proxy failed")
    await PoolManager(client).handle_response(account, 401)
    assert (await db.get_account_by_id(account["id"]))["status"] == "cooldown"


@pytest.mark.asyncio
async def test_refresh_singleflight_and_new_tokens_persist_when_chat_key_fails():
    account = await add_account()
    client = fake_upstream()
    async def rotate(*args, **kwargs):
        await asyncio.sleep(0.02)
        return {"access_token": "new-access", "refresh_token": "new-refresh"}
    client.refresh_tokens.side_effect = rotate
    client.fetch_chat_api_key.side_effect = httpx.ReadTimeout("key unavailable")
    manager = PoolManager(client)
    results = await asyncio.gather(*(manager.refresh_account_with_error(account["id"]) for _ in range(6)))
    client.refresh_tokens.assert_awaited_once()
    assert all(not ok for ok, _ in results)
    current = await db.get_account_by_id(account["id"])
    assert current["refresh_token"] == "new-refresh"
    assert current["chat_api_key"] == account["chat_api_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_stale_refresh_cannot_overwrite_or_delete_new_credentials(failure):
    account = await add_account()
    client = fake_upstream()
    entered, release = asyncio.Event(), asyncio.Event()
    async def rotate(*args, **kwargs):
        entered.set()
        await release.wait()
        if failure:
            raise LibertAIException("session expired", 401)
        return {"access_token": "stale-access", "refresh_token": "stale-refresh"}
    client.refresh_tokens.side_effect = rotate
    client.fetch_chat_api_key.return_value = "stale-key"
    task = asyncio.create_task(PoolManager(client).refresh_account_with_error(account["id"]))
    await entered.wait()
    await db.add_or_update_account(account["email"], "manual-access", "manual-refresh", "manual-key")
    release.set()
    assert (await task)[0] is False
    current = await db.get_account_by_id(account["id"])
    assert current["access_token"] == "manual-access"
    assert current["status"] == "active"


@pytest.mark.asyncio
async def test_stale_401_does_not_delete_new_key():
    account = await add_account()
    await db.add_or_update_account(account["email"], "new-a", "new-r", "new-k")
    client = fake_upstream()
    await PoolManager(client).handle_response(account, 401, "invalid api key")
    current = await db.get_account_by_id(account["id"])
    assert current["status"] == "active" and current["chat_api_key"] == "new-k"
    assert current["failed_requests"] == 1
    client.refresh_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_probe_network_error_counts_failure_and_preserves_account():
    account = await add_account()
    client = fake_upstream()
    client.forward_completion_json.side_effect = httpx.ReadTimeout("probe timeout")
    assert not (await PoolManager(client).test_account(account["id"]))[0]
    current = await db.get_account_by_id(account["id"])
    assert current is not None and current["failed_requests"] == 1


@pytest.mark.asyncio
async def test_disabled_providers_never_fall_back(monkeypatch):
    for provider in await db.get_all_mail_providers():
        await db.update_mail_provider_status(provider["id"], "disabled")
    registrar = TempMailRegistrar(fake_upstream())
    fallback = AsyncMock(return_value=None)
    monkeypatch.setattr(registrar, "_try_neighbours", fallback)
    with pytest.raises(RuntimeError):
        await registrar.register_account(max_attempts=1)
    fallback.assert_not_called()


def mock_mail_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))


@pytest.mark.asyncio
async def test_registration_total_deadline_cancels_slow_provider(monkeypatch):
    registrar = TempMailRegistrar(fake_upstream())
    cancelled = asyncio.Event()
    async def slow(*args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    monkeypatch.setattr(registrar, "_get_http_client", AsyncMock(side_effect=mock_mail_client))
    monkeypatch.setattr(registrar, "_try_neighbours", slow)
    start = time.monotonic()
    with pytest.raises((RuntimeError, TimeoutError)):
        await registrar.register_account(max_attempts=2, max_wait_seconds=0.05)
    assert time.monotonic() - start < 0.3
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_provider_exception_isolated_and_max_attempts_preserved(monkeypatch):
    providers = [{"provider_type": "inbound_webhook", "api_url": "https://mail.test/{email}", "domain": "mail.test"},
                 {"provider_type": "neighbours"}]
    monkeypatch.setattr(registration, "get_active_mail_providers", AsyncMock(return_value=providers))
    registrar = TempMailRegistrar(fake_upstream())
    monkeypatch.setattr(registrar, "_get_http_client", AsyncMock(side_effect=mock_mail_client))
    broken = AsyncMock(side_effect=ValueError("malformed provider"))
    fallback = AsyncMock(return_value=None)
    monkeypatch.setattr(registrar, "_try_inbound_webhook", broken)
    monkeypatch.setattr(registrar, "_try_neighbours", fallback)
    with pytest.raises(RuntimeError):
        await registrar.register_account(max_attempts=2)
    assert fallback.await_count == broken.await_count == 2


@pytest.mark.asyncio
async def test_slow_provider_gets_slice_not_entire_fallback_budget(monkeypatch):
    providers = [{"provider_type": "inbound_webhook", "api_url": "https://mail.test/{email}", "domain": "mail.test"},
                 {"provider_type": "neighbours"}]
    monkeypatch.setattr(registration, "get_active_mail_providers", AsyncMock(return_value=providers))
    registrar = TempMailRegistrar(fake_upstream())
    monkeypatch.setattr(registrar, "_get_http_client", AsyncMock(side_effect=mock_mail_client))
    async def slow(*args):
        await asyncio.sleep(10)
    monkeypatch.setattr(registrar, "_try_inbound_webhook", slow)
    fallback = AsyncMock(return_value={"email": "ok@example.test", "access_token": "a", "refresh_token": "r", "chat_api_key": "k"})
    monkeypatch.setattr(registrar, "_try_neighbours", fallback)
    registrar.libertai_client.get_subscription.return_value = {}
    result = await registrar.register_account(max_attempts=1, max_wait_seconds=0.3)
    assert result["chat_api_key"] == "k"
    fallback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["", "https://mail.test/{other}", "https://mail.test/{email.__class__}", "file:///mail/{email}"])
async def test_invalid_inbound_config_rejected_before_login(url):
    client = fake_upstream()
    registrar = TempMailRegistrar(client)
    requests = []
    def capture(request):
        requests.append(request)
        return httpx.Response(204)
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as mail:
        result = await registrar._try_inbound_webhook(mail, {"api_url": url, "domain": "example.test"})
    assert result is None
    assert requests == []
    client.send_login_email.assert_not_called()


@pytest.mark.asyncio
async def test_verify_uses_shared_client_and_rejects_empty_key():
    client = fake_upstream()
    client.verify_code.return_value = {"access_token": "a", "refresh_token": "r"}
    client.fetch_chat_api_key.return_value = ""
    registrar = TempMailRegistrar(client)
    async with mock_mail_client() as mail:
        assert await registrar._verify_and_fetch_key(mail, "a@example.test", "123456", "example.test") is None
    client.verify_code.assert_awaited_once_with("a@example.test", "123456")
    client.fetch_chat_api_key.assert_awaited_once_with("a", account_email="a@example.test")


@pytest.mark.asyncio
async def test_empty_provider_key_never_persisted(monkeypatch):
    registrar = TempMailRegistrar(fake_upstream())
    monkeypatch.setattr(registrar, "_get_http_client", AsyncMock(side_effect=mock_mail_client))
    result = {"email": "bad@example.test", "access_token": "a", "refresh_token": "r", "chat_api_key": " "}
    monkeypatch.setattr(registrar, "_try_neighbours", AsyncMock(return_value=result))
    with pytest.raises(RuntimeError):
        await registrar.register_account(max_attempts=1)
    assert await db.get_all_accounts() == []


@pytest.mark.asyncio
async def test_custom_provider_validates_template_before_requesting_login():
    client = fake_upstream()
    registrar = TempMailRegistrar(client)
    async with mock_mail_client() as mail:
        assert await registrar._try_custom_http(mail, {"domain": "example.test", "api_url": "https://mail.test/{unknown}"}) is None
    client.send_login_email.assert_not_called()


@pytest.mark.asyncio
async def test_auth_bootstrap_never_revives_disabled_or_deleted_tokens(monkeypatch):
    monkeypatch.setattr(db.config, "API_KEY", "environment-test-key")
    await db.set_setting("api_key", "legacy-test-key")
    await db.bootstrap_auth_tokens()
    environment = await db.get_api_token_by_value("environment-test-key")
    legacy = await db.get_api_token_by_value("legacy-test-key")
    assert environment and legacy
    await db.update_api_token_status(environment["id"], "disabled")
    await db.delete_api_token(legacy["id"])
    await db.init_db()
    await db.bootstrap_auth_tokens()
    assert not await db.validate_and_record_token("environment-test-key")
    assert await db.get_api_token_by_value("legacy-test-key") is None
    monkeypatch.setattr(db.config, "API_KEY", "rotated-test-key")
    await db.bootstrap_auth_tokens()
    assert await db.validate_and_record_token("rotated-test-key")


@pytest.mark.asyncio
async def test_admin_session_storage_and_revocation():
    token_hash = hashlib.sha256(b"offline-session").hexdigest()
    expires = "2099-01-01T00:00:00+00:00"
    await db.create_admin_session(token_hash, expires, "password-fingerprint")
    session = await db.get_admin_session(token_hash)
    assert session["expires_at"] == expires
    assert session["password_fingerprint"] == "password-fingerprint"
    await db.delete_admin_session(token_hash)
    assert await db.get_admin_session(token_hash) is None


@pytest.mark.asyncio
async def test_new_database_and_directory_private_without_chmod_existing(tmp_path, monkeypatch):
    path = tmp_path / "new-private" / "accounts.db"
    monkeypatch.setattr(db, "DATABASE_PATH", path)
    await db.init_db()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.chmod(0o640)
    path.parent.chmod(0o750)
    await db.init_db()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o750


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tempmail_lol", "duckmail", "inbound_webhook"])
async def test_user_added_providers_protocol_closed_loop_without_network(kind, monkeypatch):
    """Preserve draft provider protocols; exercise actual parsing with fake HTTP."""
    upstream = fake_upstream()
    upstream.verify_code.return_value = {"access_token": "a", "refresh_token": "r"}
    upstream.fetch_chat_api_key.return_value = "generated-key"
    registrar = TempMailRegistrar(upstream)
    requests = []
    def respond(request):
        requests.append(request)
        path = request.url.path
        if kind == "tempmail_lol":
            if path == "/inbox/create":
                return httpx.Response(200, json={"address": "new@example.test", "token": "mail-token"})
            assert path == "/inbox" and request.url.params["token"] == "mail-token"
            return httpx.Response(200, json={"emails": [{"body": "Your code is 123456"}]})
        if kind == "duckmail":
            if path == "/domains":
                return httpx.Response(200, json={"hydra:member": [{"domain": "example.test"}]})
            if path == "/accounts":
                assert request.method == "POST"
                return httpx.Response(201, json={})
            if path == "/token":
                return httpx.Response(200, json={"token": "mail-token"})
            assert request.headers["Authorization"] == "Bearer mail-token"
            if path == "/messages":
                return httpx.Response(200, json={"hydra:member": [{"id": "message-one"}]})
            assert path == "/messages/message-one"
            return httpx.Response(200, json={"text": "Your code is 123456"})
        assert path == "/code" and request.url.params["email"].endswith("@example.test")
        return httpx.Response(200, json={"success": True, "code": "123456"})
    monkeypatch.setattr(registration.asyncio, "sleep", AsyncMock())
    provider = {"provider_type": kind, "domain": "example.test", "api_url": "https://mail.test"}
    if kind == "inbound_webhook":
        provider["api_url"] += "/code?email={email}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await getattr(registrar, f"_try_{kind}")(client, provider)
    assert result and result["chat_api_key"] == "generated-key"
    upstream.send_login_email.assert_awaited_once_with(result["email"])
    upstream.verify_code.assert_awaited_once_with(result["email"], "123456")
    upstream.fetch_chat_api_key.assert_awaited_once_with("a", account_email=result["email"])
    assert all(request.url.host == "mail.test" for request in requests)


@pytest.mark.asyncio
async def test_401_refresh_success_still_counts_failure_and_backs_off():
    account = await add_account()
    client = fake_upstream()
    client.refresh_tokens.return_value = {"access_token": "rotated-a", "refresh_token": "rotated-r"}
    client.fetch_chat_api_key.return_value = "rotated-k"
    assert await PoolManager(client).handle_response(account, 401, "invalid api key") is False
    current = await db.get_account_by_id(account["id"])
    assert current and current["status"] == "cooldown"
    assert current["failed_requests"] == current["total_requests"] == 1
    assert current["chat_api_key"] == "rotated-k"


@pytest.mark.asyncio
async def test_delete_compare_and_swap_rejects_old_version():
    account = await add_account()
    await db.add_or_update_account(account["email"], "new-a", "new-r", "new-k")
    assert not await db.delete_account(account["id"], expected_version=account["credentials_version"])
    assert await db.get_account_by_id(account["id"]) is not None
