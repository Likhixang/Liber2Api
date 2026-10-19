import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import pytest_asyncio

import app.config as config
import app.database as db


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
async def test_add_and_get_account():
    acc = await db.add_or_update_account(
        email="user1@example.com",
        access_token="acc_tok_1",
        refresh_token="ref_tok_1",
        chat_api_key="chat_key_1",
    )
    assert acc["email"] == "user1@example.com"
    assert acc["status"] == "active"
    assert acc["chat_api_key"] == "chat_key_1"

    all_accs = await db.get_all_accounts()
    assert len(all_accs) == 1
    assert all_accs[0]["email"] == "user1@example.com"


@pytest.mark.asyncio
async def test_active_accounts_and_cooldown():
    # Add two accounts
    acc1 = await db.add_or_update_account("u1@test.com", "a1", "r1", "k1")
    acc2 = await db.add_or_update_account("u2@test.com", "a2", "r2", "k2")

    active = await db.get_active_accounts()
    assert len(active) == 2

    # Put u1 into cooldown for 2 hours
    cooldown_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    await db.mark_account_cooldown(acc1["id"], cooldown_time, "Rate limited 429")

    active_after = await db.get_active_accounts()
    assert len(active_after) == 1
    assert active_after[0]["email"] == "u2@test.com"

    # Reset u1 cooldown
    await db.reset_account_status(acc1["id"])
    active_reset = await db.get_active_accounts()
    assert len(active_reset) == 2


@pytest.mark.asyncio
async def test_usage_and_summary():
    # Empty pool summary has None success_rate
    s_empty = await db.get_pool_summary()
    assert s_empty["success_rate"] is None

    acc = await db.add_or_update_account("user_stat@test.com", "a", "r", "k")
    await db.record_account_usage(acc["id"], success=True)
    await db.record_account_usage(acc["id"], success=False)

    summary = await db.get_pool_summary()
    assert summary["total_accounts"] == 1
    assert summary["total_requests"] == 2
    assert summary["failed_requests"] == 1
    assert summary["success_rate"] == 50.0
