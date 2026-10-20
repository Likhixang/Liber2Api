import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from app.config import (
    COOLDOWN_HOURS,
    AUTO_REFRESH_INTERVAL,
)
from app.database import (
    delete_account,
    claim_next_account,
    get_all_accounts,
    get_active_accounts,
    get_account_by_id,
    mark_account_cooldown,
    update_account_tokens,
    update_account_usage,
    record_account_usage,
    reset_account_status,
)
from app.libertai_client import LibertAIClient, LibertAIException

logger = logging.getLogger("liber2api.pool")


class PoolManager:
    def __init__(self, client: Optional[LibertAIClient] = None):
        self.client = client or LibertAIClient()
        self._lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_replenish_fail_time: float = float("-inf")
        self._replenishing = False
        self._refresh_tasks: Dict[int, asyncio.Task] = {}

    async def get_next_account(self, exclude_ids=None) -> Optional[Dict[str, Any]]:
        """Atomically reserve an LRU turn; exclusions are request-local.

        Reservation advances scheduling time, not request counters; it balances
        concurrent work rather than granting an exclusive account lease.
        """
        account = await claim_next_account(exclude_ids)
        if account:
            return account
        if exclude_ids or await get_active_accounts():
            return None
        async with self._lock:
            if self._replenishing or time.monotonic() - self._last_replenish_fail_time < 300:
                return None
            self._replenishing = True
        try:
            from app.auto_register import registrar
            new_account = await registrar.register_account(max_wait_seconds=20)
            if new_account:
                return await claim_next_account(exclude_ids)
            self._last_replenish_fail_time = time.monotonic()
        except Exception as exc:
            self._last_replenish_fail_time = time.monotonic()
            logger.warning("Emergency auto-registration failed: %s", exc)
        finally:
            self._replenishing = False
        return None

    @staticmethod
    def _permanent_error(message: str) -> bool:
        return any(marker in message.lower() for marker in (
            "session expired", "invalid api key", "invalid refresh token",
            "refresh token expired", "refresh token revoked", "user not found",
            "account disabled", "account banned",
        ))

    async def handle_response(
        self, account: Dict[str, Any], status_code: int, error_detail: str = "",
    ) -> bool:
        """Count one attempt; True means HTTP success, not identity refresh success.

        429 always cools down without identity refresh. Callers exclude all tried
        IDs for their request. Confirmed permanent invalidity retains the existing
        auto-removal policy, guarded by the exact credentials version.
        """
        account_id = account["id"]
        success = 200 <= status_code < 300
        await record_account_usage(account_id, success=success)
        if success:
            return True
        if status_code not in (401, 429):
            return False
        current = await get_account_by_id(account_id)
        version = account.get("credentials_version", 0)
        if not current or current["credentials_version"] != version:
            return False
        seconds = max(60, COOLDOWN_HOURS * 3600) if status_code == 429 else 60
        until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await mark_account_cooldown(account_id, until.isoformat(), f"HTTP {status_code}: temporary backoff",
                                    count_failure=False, expected_version=version)
        if status_code == 429:
            return False
        refreshed, _ = await self.refresh_account_with_error(account_id, expected_version=version)
        if not refreshed and self._permanent_error(error_detail):
            # Old chat-key evidence must never delete credentials rotated meanwhile.
            await delete_account(account_id, expected_version=version)
        return False

    async def refresh_account(self, account_id: int) -> bool:
        ok, _ = await self.refresh_account_with_error(account_id)
        return ok

    async def refresh_account_with_error(self, account_id: int, *, expected_version: Optional[int] = None) -> Tuple[bool, str]:
        """Share one in-flight rotation per account; guard writes/deletes by version."""
        task = self._refresh_tasks.get(account_id)
        if task is None:
            task = asyncio.create_task(self._refresh_account_once(account_id, expected_version=expected_version))
            self._refresh_tasks[account_id] = task
            def forget(done):
                if self._refresh_tasks.get(account_id) is done:
                    self._refresh_tasks.pop(account_id, None)
            task.add_done_callback(forget)
        return await asyncio.shield(task)

    async def _refresh_account_once(self, account_id: int, *, expected_version: Optional[int] = None) -> Tuple[bool, str]:
        account = await get_account_by_id(account_id)
        if not account:
            return False, "Account not found"
        version = account["credentials_version"]
        if expected_version is not None and version != expected_version:
            return False, "Credentials changed before refresh; stale response ignored"
        try:
            tokens = await self.client.refresh_tokens(account["refresh_token"], account_email=account["email"])
            access = tokens.get("access_token")
            refresh = tokens.get("refresh_token")
            if not isinstance(access, str) or not access.strip() or not isinstance(refresh, str) or not refresh.strip():
                return False, "Refresh returned empty credentials"
        except Exception as exc:
            # Network errors alone are not revocation evidence, even when a proxy's
            # error text happens to contain one of the permanent-error phrases.
            if isinstance(exc, LibertAIException) and exc.status_code in (401, 403) and self._permanent_error(str(exc)):
                await delete_account(account_id, expected_version=version)
            return False, str(exc)
        # Save the rotated refresh token before attempting the separate key fetch.
        if not await update_account_tokens(account_id, access, refresh, expected_version=version):
            return False, "Credentials changed during refresh; stale result ignored"
        version += 1
        try:
            key = await self.client.fetch_chat_api_key(access, account_email=account["email"])
            if not isinstance(key, str) or not key.strip():
                return False, "Chat key refresh returned an empty key"
        except Exception as exc:
            return False, f"Tokens rotated but chat key unavailable: {exc}"
        if not await update_account_tokens(account_id, access, refresh, key, expected_version=version):
            return False, "Credentials changed during key refresh; stale result ignored"
        try:
            import json
            subscription = await asyncio.wait_for(
                self.client.get_subscription(access, account_email=account["email"]), timeout=1.0,
            )
            if subscription:
                await update_account_usage(account_id, json.dumps(subscription))
        except Exception:
            pass
        return True, ""

    async def test_account(self, account_id: int) -> Tuple[bool, str]:
        """One probe, with the same response policy and accounting as forwarding."""
        account = await get_account_by_id(account_id)
        if not account:
            return False, "Account not found"
        try:
            status, body = await self.client.forward_completion_json(
                account["chat_api_key"],
                {"model": "qwen3.6-35b-a3b", "messages": [{"role": "user", "content": "1"}], "max_tokens": 1},
                account_email=account["email"],
            )
            ok = await self.handle_response(account, status, str(body))
            return ok, "账号状态活跃正常" if ok else f"HTTP {status}"
        except Exception as exc:
            await record_account_usage(account_id, success=False)
            return False, f"Network error: {exc}"

    async def reset_cooldown(self, account_id: int) -> bool:
        account = await get_account_by_id(account_id)
        if not account:
            return False
        await reset_account_status(account_id)
        return True

    async def fetch_account_usage(self, account_id: int) -> Dict[str, Any]:
        account = await get_account_by_id(account_id)
        if not account:
            return {}
        try:
            sub = await self.client.get_subscription(account["access_token"], account_email=account["email"])
            if sub:
                import json
                await update_account_usage(account_id, json.dumps(sub))
                return sub
        except Exception as e:
            logger.warning("Failed to fetch usage for %s: %s", account["email"], e)
        return {}

    async def start_background_worker(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("Background pool maintenance worker started")

    async def stop_background_worker(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Background pool maintenance worker stopped")

    async def _worker_loop(self) -> None:
        """
        Periodic worker:
        1. Checks for accounts with expired cooldowns and restores them to active.
        2. Every 5 minutes (300s): Performs health probe on all accounts; purges failed accounts.
        3. Every 5 minutes (300s): Auto-replenishes 3 accounts if active count < 100.
        4. Proactively refreshes tokens for accounts that haven't been updated recently.
        """
        # Initial startup replenishment if active count < 3
        try:
            initial_active = await get_active_accounts()
            if len(initial_active) < 3:
                logger.info("Initial startup: active accounts (%d < 3), creating batch to ensure minimum 3 accounts...", len(initial_active))
                from app.auto_register import registrar
                for i in range(3 - len(initial_active)):
                    try:
                        await registrar.register_account()
                        await asyncio.sleep(2)
                    except Exception as e:
                        logger.error("Initial registration failed: %s", e)
        except Exception as e:
            logger.error("Startup replenishment error: %s", e)

        check_interval = 60  # Cooldown check loop: every 60s
        health_check_tick = 0  # Health check & replenishment interval: every 300s (5 min)
        refresh_tick = 0

        while self._running:
            try:
                await asyncio.sleep(check_interval)
                refresh_tick += check_interval
                health_check_tick += check_interval

                # 1. Restore cooldowns
                now = datetime.now(timezone.utc).isoformat()
                all_accounts = await get_all_accounts()
                for acc in all_accounts:
                    if acc["status"] == "cooldown":
                        cd = acc.get("cooldown_until")
                        if cd and cd <= now:
                            logger.info("Cooldown ended for account %s, reactivating", acc["email"])
                            await reset_account_status(acc["id"])

                # 2. Every 5 minutes (300s): Auto replenishment if active count < 100
                if health_check_tick >= 300:
                    health_check_tick = 0

                    # Auto replenishment: create 3 new accounts if active count < 100
                    active_now = await get_active_accounts()
                    if len(active_now) < 100:
                        logger.info("Active accounts (%d < 100), running scheduled 5-min batch creation of 3 accounts...", len(active_now))
                        from app.auto_register import registrar
                        for i in range(3):
                            try:
                                await registrar.register_account()
                                logger.info("Batch replenish: created account %d/3", i + 1)
                                await asyncio.sleep(2)
                            except Exception as e:
                                logger.error("Batch replenish account %d failed: %s", i + 1, e)
                    else:
                        logger.info("Active accounts count (%d >= 100), skipping auto-replenishment", len(active_now))

                # 3. Proactive token rotation
                if refresh_tick >= AUTO_REFRESH_INTERVAL:
                    refresh_tick = 0
                    logger.info("Running scheduled token rotation across account pool")
                    for acc in await get_all_accounts():
                        if acc["status"] != "invalid":
                            await self.refresh_account_with_error(acc["id"])
                            await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in background worker loop: %s", e)


# Global singleton manager instance
pool_manager = PoolManager()
