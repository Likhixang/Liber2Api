import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from app.config import (
    COOLDOWN_HOURS,
    AUTO_REFRESH_INTERVAL,
)
from app.database import (
    delete_account,
    get_all_accounts,
    get_active_accounts,
    get_account_by_id,
    mark_account_cooldown,
    mark_account_invalid,
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
        self._last_replenish_fail_time: float = 0

    async def get_next_account(self) -> Optional[Dict[str, Any]]:
        """
        Picks the next available active account.
        Orders by last_used_at ascending (LRU).
        If pool is exhausted, attempts emergency auto-registration on the fly.
        """
        async with self._lock:
            accounts = await get_active_accounts()
            if not accounts:
                import time
                if time.time() - self._last_replenish_fail_time > 300:
                    logger.warning("No active accounts available in pool! Attempting emergency auto-registration...")
                    try:
                        from app.auto_register import registrar
                        new_acc = await registrar.register_account(max_wait_seconds=20)
                        if new_acc:
                            return new_acc
                    except Exception as e:
                        self._last_replenish_fail_time = time.time()
                        logger.error("Emergency auto-registration failed (%s), backing off for 5 min", e)
                return None
            return accounts[0]

    async def handle_response(
        self,
        account: Dict[str, Any],
        status_code: int,
        error_detail: str = "",
    ) -> bool:
        """
        Handles post-response state updates for an account.
        Returns True if the response was successful, False if account was marked cooldown/invalid.
        """
        account_id = account["id"]
        if 200 <= status_code < 300:
            await record_account_usage(account_id, success=True)
            return True

        if status_code in (401, 429):
            # 401/429 on LibertAI corresponds to quota limits or 5-hour rolling window exhaustion.
            # Attempt a single proactive token refresh. If still limited, put into 5-hour cooldown. Do NOT kick or delete!
            refreshed, _ = await self.refresh_account_with_error(account_id)
            if not refreshed:
                cooldown_until = datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)
                msg = f"HTTP {status_code}: 额度已达上限，进入5小时自动冷却"
                logger.warning("Account %s triggered %d (quota limit), cooling down until %s", account["email"], status_code, cooldown_until.isoformat())
                await mark_account_cooldown(account_id, cooldown_until.isoformat(), msg)
            return False

        # Other 4xx/5xx errors
        await record_account_usage(account_id, success=False)
        return False

    async def refresh_account(self, account_id: int) -> bool:
        ok, _ = await self.refresh_account_with_error(account_id)
        return ok

    async def refresh_account_with_error(self, account_id: int) -> Tuple[bool, str]:
        """
        Manually or automatically refreshes tokens and chatApiKey for an account.
        Returns (success: bool, error_message: str).
        """
        account = await get_account_by_id(account_id)
        if not account:
            return False, "账号在本地数据库中不存在"

        try:
            tokens = await self.client.refresh_tokens(account["refresh_token"], account_email=account["email"])
            new_access_token = tokens["access_token"]
            new_refresh_token = tokens["refresh_token"]

            # Try to fetch fresh chatApiKey with new access token
            try:
                new_chat_key = await self.client.fetch_chat_api_key(new_access_token, account_email=account["email"])
            except Exception as e:
                logger.warning("Failed to fetch fresh chatApiKey during refresh, keeping previous: %s", e)
                new_chat_key = account["chat_api_key"]

            await update_account_tokens(
                account_id=account_id,
                access_token=new_access_token,
                refresh_token=new_refresh_token,
                chat_api_key=new_chat_key,
            )
            # Update usage info
            try:
                import json
                sub = await self.client.get_subscription(new_access_token, account_email=account["email"])
                if sub:
                    await update_account_usage(account_id, json.dumps(sub))
            except Exception:
                pass

            logger.info("Successfully refreshed account %s", account["email"])
            return True, ""
        except Exception as e:
            logger.error("Failed to refresh account %s: %s", account["email"], e)
            return False, str(e)

    async def test_account(self, account_id: int) -> Tuple[bool, str]:
        """
        Tests if an account's chat_api_key is working by performing lightweight auth check.
        Uses GET https://api.libertai.io/libertai/auth/check.
        If auth check returns 401/429, marks cooldown instead of invalid or deleting.
        """
        account = await get_account_by_id(account_id)
        if not account:
            return False, "Account not found"

        try:
            status, resp_text = await self.client.check_chat_api_key_auth(
                account["chat_api_key"], account_email=account["email"]
            )
            if status == 200:
                await record_account_usage(account_id, success=True)
                return True, "账号状态活跃正常"
            elif status in (401, 429):
                refreshed, _ = await self.refresh_account_with_error(account_id)
                if refreshed:
                    acc_new = await get_account_by_id(account_id)
                    if acc_new:
                        s2, _ = await self.client.check_chat_api_key_auth(
                            acc_new["chat_api_key"], account_email=acc_new["email"]
                        )
                        if s2 == 200:
                            return True, "账号刷新后恢复正常"

                cooldown_until = datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)
                msg = f"HTTP {status} 额度已达上限，进入冷却中"
                await mark_account_cooldown(account_id, cooldown_until.isoformat(), msg)
                return False, msg
            else:
                await record_account_usage(account_id, success=False)
                return False, f"HTTP {status}: {resp_text[:100]}"
        except Exception as e:
            return False, f"Network error: {str(e)}"

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

                # 2. Every 5 minutes (300s): Health check & cooldown evaluation, plus auto replenishment
                if health_check_tick >= 300:
                    health_check_tick = 0
                    logger.info("Running 5-minute health probe across pool...")
                    current_accounts = await get_all_accounts()
                    for acc in current_accounts:
                        # Probe account
                        is_ok, msg = await self.test_account(acc["id"])
                        if not is_ok:
                            logger.info("Account %s in cooldown: %s", acc["email"], msg)
                        await asyncio.sleep(1)

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
                            await self.refresh_account(acc["id"])
                            await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in background worker loop: %s", e)


# Global singleton manager instance
pool_manager = PoolManager()
