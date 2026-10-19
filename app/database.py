import aiosqlite
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from app.config import DATABASE_PATH


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        await db.commit()


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
            WHERE status != 'invalid'
              AND (cooldown_until IS NULL OR cooldown_until <= ?)
            ORDER BY COALESCE(last_used_at, '1970-01-01T00:00:00') ASC, id ASC
            """,
            (now,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_account_tokens(
    account_id: int,
    access_token: str,
    refresh_token: str,
    chat_api_key: Optional[str] = None,
) -> None:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        if chat_api_key:
            await db.execute(
                """
                UPDATE accounts
                SET access_token = ?, refresh_token = ?, chat_api_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (access_token, refresh_token, chat_api_key, now, account_id),
            )
        else:
            await db.execute(
                """
                UPDATE accounts
                SET access_token = ?, refresh_token = ?, updated_at = ?
                WHERE id = ?
                """,
                (access_token, refresh_token, now, account_id),
            )
        await db.commit()


async def mark_account_cooldown(account_id: int, cooldown_until_iso: str, error_msg: str) -> None:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE accounts
            SET status = 'cooldown',
                cooldown_until = ?,
                last_error = ?,
                updated_at = ?,
                failed_requests = failed_requests + 1
            WHERE id = ?
            """,
            (cooldown_until_iso, error_msg, now, account_id),
        )
        await db.commit()


async def mark_account_invalid(account_id: int, error_msg: str) -> None:
    now = _utc_now_str()
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            UPDATE accounts
            SET status = 'invalid',
                last_error = ?,
                updated_at = ?,
                failed_requests = failed_requests + 1
            WHERE id = ?
            """,
            (error_msg, now, account_id),
        )
        await db.commit()


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


async def delete_account(account_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
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

        success_rate = 100.0 if total_reqs == 0 else round((total_reqs - failed_reqs) / total_reqs * 100, 1)

        return {
            "total_accounts": total,
            "active_accounts": active,
            "cooldown_accounts": cooldown,
            "invalid_accounts": invalid,
            "total_requests": total_reqs,
            "failed_requests": failed_reqs,
            "success_rate": success_rate,
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
