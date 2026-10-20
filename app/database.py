import hashlib
import os

import aiosqlite
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
import app.config as config
from app.config import DATABASE_PATH


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    # Restrict newly created paths only; existing deployment permissions are untouched.
    missing = []
    parent = DATABASE_PATH.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    try:
        fd = os.open(DATABASE_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                chat_api_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                cooldown_until TEXT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT NULL,
                total_requests INTEGER NOT NULL DEFAULT 0,
                failed_requests INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS system_stats (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                last_used_at TEXT NULL,
                total_requests INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await db.commit()

        await db.execute(
            """CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                expires_at TEXT NOT NULL,
                password_fingerprint TEXT NOT NULL
            )"""
        )
        await db.commit()

        # Check if usage_info column exists in accounts table
        c_info = await db.execute("PRAGMA table_info(accounts)")
        columns = [r[1] for r in await c_info.fetchall()]
        if "credentials_version" not in columns:
            await db.execute("ALTER TABLE accounts ADD COLUMN credentials_version INTEGER NOT NULL DEFAULT 0")
            await db.commit()
        if "usage_info" not in columns:
            await db.execute("ALTER TABLE accounts ADD COLUMN usage_info TEXT NULL")
            await db.commit()

        # Create mail_providers table for dynamic and custom email API management
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                domain TEXT NULL,
                api_url TEXT NOT NULL,
                create_url TEXT NULL,
                api_key TEXT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                priority INTEGER NOT NULL DEFAULT 10,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()

        # Seed default built-in provider only if table is completely empty
        cursor = await db.execute("SELECT COUNT(*) FROM mail_providers")
        cnt_row = await cursor.fetchone()
        if cnt_row and cnt_row[0] == 0:
            now = _utc_now_str()
            # Only include proven working channels with unblocked domains (Neighbours)
            defaults = [
                ("Neighbours 集群", "neighbours", None, "https://neighbours.sh/api/v1", None, None, "active", 1),
            ]
            for name, p_type, domain, api_url, create_url, api_key, status, priority in defaults:
                await db.execute(
                    """
                    INSERT INTO mail_providers (name, provider_type, domain, api_url, create_url, api_key, status, priority, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, p_type, domain, api_url, create_url, api_key, status, priority, now),
                )
            await db.commit()

    await bootstrap_auth_tokens()


async def add_or_update_account(
    email: str,
    access_token: str,
    refresh_token: str,
    chat_api_key: str,
) -> Dict[str, Any]:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO accounts (
                email, access_token, refresh_token, chat_api_key,
                status, cooldown_until, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', NULL, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                chat_api_key = excluded.chat_api_key,
                credentials_version = accounts.credentials_version + 1,
                status = 'active',
                cooldown_until = NULL,
                updated_at = excluded.updated_at,
                last_error = NULL
            """,
            (email, access_token, refresh_token, chat_api_key, now, now),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM accounts WHERE email = ?", (email,))
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def get_all_accounts() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM accounts ORDER BY id ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_account_by_id(account_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_accounts() -> List[Dict[str, Any]]:
    """
    Returns accounts eligible for request forwarding:
    status == 'active' AND (cooldown_until IS NULL OR cooldown_until <= now).
    Ordered by last_used_at ascending (least recently used first).
    """
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM accounts
            WHERE status IN ('active', 'cooldown')
              AND trim(chat_api_key) != ''
              AND (cooldown_until IS NULL OR cooldown_until <= ?)
            ORDER BY COALESCE(last_used_at, '1970-01-01T00:00:00') ASC, id ASC
            """,
            (now,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def claim_next_account(exclude_ids: Optional[Iterable[int]] = None) -> Optional[Dict[str, Any]]:
    """Atomically reserve LRU scheduling time, without counting a completed request."""
    excluded = tuple(set(exclude_ids or ()))
    exclusion = " AND id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        now = _utc_now_str()
        cursor = await db.execute(
            """SELECT * FROM accounts
               WHERE status IN ('active', 'cooldown')
                 AND trim(chat_api_key) != ''
                 AND (cooldown_until IS NULL OR cooldown_until <= ?)""" + exclusion +
            " ORDER BY COALESCE(last_used_at, '1970-01-01T00:00:00') ASC, id ASC LIMIT 1",
            (now, *excluded),
        )
        row = await cursor.fetchone()
        if not row:
            await db.commit()
            return None
        await db.execute("UPDATE accounts SET last_used_at = ? WHERE id = ?", (now, row["id"]))
        await db.commit()
        return {**dict(row), "last_used_at": now}


async def update_account_tokens(
    account_id: int,
    access_token: str,
    refresh_token: str,
    chat_api_key: Optional[str] = None,
    *,
    expected_version: Optional[int] = None,
) -> bool:
    """Compare-and-swap credentials; reject results from an obsolete refresh."""
    if not access_token or not refresh_token:
        raise ValueError("Access and refresh tokens must be nonempty")
    condition = " AND credentials_version = ?" if expected_version is not None else ""
    params = [access_token, refresh_token, chat_api_key, _utc_now_str(), account_id]
    if expected_version is not None:
        params.append(expected_version)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """UPDATE accounts
               SET access_token = ?, refresh_token = ?, chat_api_key = COALESCE(?, chat_api_key),
                   updated_at = ?, credentials_version = credentials_version + 1
               WHERE id = ?""" + condition,
            params,
        )
        await db.commit()
        return cursor.rowcount > 0


async def _mark_account_state(
    account_id: int, status: str, cooldown_until: Optional[str], error_msg: str,
    *, count_failure: bool, expected_version: Optional[int],
) -> bool:
    condition = " AND credentials_version = ?" if expected_version is not None else ""
    # An unrelated later failure must not remove a reversible invalid quarantine.
    if status == "cooldown":
        condition += " AND status != 'invalid'"
    params = [status, cooldown_until, error_msg, _utc_now_str(), int(count_failure), int(count_failure), account_id]
    if expected_version is not None:
        params.append(expected_version)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """UPDATE accounts
               SET status = ?, cooldown_until = ?, last_error = ?, updated_at = ?,
                   total_requests = total_requests + ?, failed_requests = failed_requests + ?
               WHERE id = ?""" + condition,
            params,
        )
        await db.commit()
        return cursor.rowcount > 0


async def mark_account_cooldown(
    account_id: int, cooldown_until_iso: str, error_msg: str,
    *, count_failure: bool = True, expected_version: Optional[int] = None,
) -> bool:
    """Reversible cooldown; count_failure=False when the caller already recorded usage."""
    return await _mark_account_state(account_id, "cooldown", cooldown_until_iso, error_msg,
                                     count_failure=count_failure, expected_version=expected_version)


async def mark_account_invalid(
    account_id: int, error_msg: str,
    *, count_failure: bool = True, expected_version: Optional[int] = None,
) -> bool:
    """Quarantine, never delete; expected_version guards stale upstream results."""
    return await _mark_account_state(account_id, "invalid", None, error_msg,
                                     count_failure=count_failure, expected_version=expected_version)


async def reset_account_status(account_id: int) -> None:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE accounts
            SET status = 'active',
                cooldown_until = NULL,
                last_error = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, account_id),
        )
        await db.commit()


async def record_account_usage(account_id: int, success: bool) -> None:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if success:
            await db.execute(
                """
                UPDATE accounts
                SET total_requests = total_requests + 1,
                    last_used_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, account_id),
            )
        else:
            await db.execute(
                """
                UPDATE accounts
                SET total_requests = total_requests + 1,
                    failed_requests = failed_requests + 1,
                    last_used_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, account_id),
            )
        await db.commit()


async def delete_account(account_id: int, *, expected_version: Optional[int] = None) -> bool:
    condition = " AND credentials_version = ?" if expected_version is not None else ""
    params = (account_id, expected_version) if expected_version is not None else (account_id,)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("DELETE FROM accounts WHERE id = ?" + condition, params)
        await db.commit()
        return cursor.rowcount > 0


async def get_pool_summary() -> Dict[str, Any]:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM accounts")
        rows = await cursor.fetchall()
        accounts = [dict(r) for r in rows]

        total = len(accounts)
        active = 0
        cooldown = 0
        invalid = 0
        total_reqs = 0
        failed_reqs = 0

        for acc in accounts:
            total_reqs += acc["total_requests"]
            failed_reqs += acc["failed_requests"]
            if acc["status"] == "invalid":
                invalid += 1
            elif acc["status"] == "cooldown":
                cd = acc.get("cooldown_until")
                if cd and cd > now:
                    cooldown += 1
                else:
                    active += 1
            else:
                active += 1

        if total_reqs == 0:
            success_rate = None
        else:
            effective_success = max(0, total_reqs - failed_reqs)
            success_rate = round(min(100.0, max(0.0, (effective_success / total_reqs) * 100)), 1)

        return {
            "total_accounts": total,
            "active_accounts": active,
            "cooldown_accounts": cooldown,
            "invalid_accounts": invalid,
            "total_requests": total_reqs,
            "failed_requests": failed_reqs,
            "success_rate": success_rate,
        }


async def update_account_usage(account_id: int, usage_info: str) -> None:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE accounts
            SET usage_info = ?, updated_at = ?
            WHERE id = ?
            """,
            (usage_info, now, account_id),
        )
        await db.commit()


async def get_effective_proxy_settings() -> Dict[str, str]:
    """
    Returns effective proxy settings, prioritizing SQLite 'settings' table over environment variables.
    """
    db_proxy = await get_setting("proxy_url")
    db_resin = await get_setting("resin_url")
    db_platform = await get_setting("resin_platform_name")

    return {
        "proxy_url": db_proxy if db_proxy is not None else config.PROXY_URL,
        "resin_url": db_resin if db_resin is not None else config.RESIN_URL,
        "resin_platform_name": db_platform if db_platform is not None else config.RESIN_PLATFORM_NAME,
    }


async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()


# ---------------------------------------------------------
# Multi-Token Management
# ---------------------------------------------------------

async def bootstrap_auth_tokens() -> None:
    """Import each configured key once; a durable tombstone preserves revocations."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'api_key'")
        row = await cursor.fetchone()
        candidates = (("environment", config.API_KEY), ("legacy", row[0] if row else ""))
        for source, value in candidates:
            value = (value or "").strip()
            if not value:
                continue
            marker = f"auth_bootstrap:{source}:{hashlib.sha256(value.encode()).hexdigest()}"
            cursor = await db.execute("SELECT 1 FROM settings WHERE key = ?", (marker,))
            if await cursor.fetchone():
                continue
            await db.execute(
                "INSERT OR IGNORE INTO api_tokens (name, token, status, created_at) VALUES (?, ?, 'active', ?)",
                ("环境令牌" if source == "environment" else "默认令牌", value, _utc_now_str()),
            )
            await db.execute("INSERT INTO settings (key, value) VALUES (?, '1')", (marker,))
        await db.commit()


async def get_api_token_by_value(token: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM api_tokens WHERE token = ?", (token,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def create_admin_session(token_hash: str, expires_at: str, password_fingerprint: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO admin_sessions (token_hash, expires_at, password_fingerprint) VALUES (?, ?, ?)",
            (token_hash, expires_at, password_fingerprint),
        )
        await db.commit()


async def get_admin_session(token_hash: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM admin_sessions WHERE token_hash = ?", (token_hash,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_admin_session(token_hash: str) -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))
        await db.commit()

async def create_api_token(name: str, token: str) -> Dict[str, Any]:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO api_tokens (name, token, status, created_at)
            VALUES (?, ?, 'active', ?)
            """,
            (name.strip(), token.strip(), now),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM api_tokens WHERE token = ?", (token.strip(),))
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def get_all_api_tokens() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM api_tokens ORDER BY id DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_api_token_status(token_id: int, status: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("UPDATE api_tokens SET status = ? WHERE id = ?", (status, token_id))
        await db.commit()
        return cursor.rowcount > 0


async def delete_api_token(token_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        await db.commit()
        return cursor.rowcount > 0


async def validate_and_record_token(token_str: str) -> bool:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM api_tokens WHERE token = ? AND status = 'active'",
            (token_str.strip(),),
        )
        row = await cursor.fetchone()
        if not row:
            return False
        token_id = row[0]
        await db.execute(
            """
            UPDATE api_tokens
            SET total_requests = total_requests + 1, last_used_at = ?
            WHERE id = ?
            """,
            (now, token_id),
        )
        await db.commit()
        return True


# ---------------------------------------------------------
# Dynamic & Custom Mail Provider Management
# ---------------------------------------------------------

async def get_all_mail_providers() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM mail_providers ORDER BY priority ASC, id ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_active_mail_providers() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM mail_providers WHERE status = 'active' ORDER BY priority ASC, id ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def create_mail_provider(
    name: str,
    provider_type: str,
    domain: Optional[str] = None,
    api_url: str = "",
    create_url: Optional[str] = None,
    api_key: Optional[str] = None,
    priority: int = 10,
) -> Dict[str, Any]:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            INSERT INTO mail_providers (name, provider_type, domain, api_url, create_url, api_key, status, priority, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                name.strip(),
                provider_type.strip(),
                domain.strip() if domain else None,
                api_url.strip(),
                create_url.strip() if create_url else None,
                api_key.strip() if api_key else None,
                priority,
                now,
            ),
        )
        await db.commit()
        new_id = cursor.lastrowid
        c2 = await db.execute("SELECT * FROM mail_providers WHERE id = ?", (new_id,))
        row = await c2.fetchone()
        return dict(row) if row else {}


async def update_mail_provider_status(provider_id: int, status: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("UPDATE mail_providers SET status = ? WHERE id = ?", (status, provider_id))
        await db.commit()
        return cursor.rowcount > 0


async def delete_mail_provider(provider_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("DELETE FROM mail_providers WHERE id = ?", (provider_id,))
        await db.commit()
        return cursor.rowcount > 0


async def reset_builtin_mail_providers() -> None:
    """Restores official proven working built-in providers to active state and default URLs."""
    defaults = [
        ("Neighbours 集群", "neighbours", None, "https://neighbours.sh/api/v1", None, None, "active", 1),
    ]
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for name, p_type, domain, api_url, create_url, api_key, status, priority in defaults:
            cursor = await db.execute("SELECT id FROM mail_providers WHERE provider_type = ?", (p_type,))
            row = await cursor.fetchone()
            if row:
                await db.execute(
                    """
                    UPDATE mail_providers
                    SET name = ?, domain = ?, api_url = ?, create_url = ?, api_key = ?, status = 'active', priority = ?
                    WHERE id = ?
                    """,
                    (name, domain, api_url, create_url, api_key, priority, row[0]),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO mail_providers (name, provider_type, domain, api_url, create_url, api_key, status, priority, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (name, p_type, domain, api_url, create_url, api_key, priority, now),
                )
        await db.commit()
