import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from app.config import (
    COOLDOWN_HOURS,
    AUTO_REFRESH_INTERVAL,
)
from app.database import (
    get_all_accounts,
    get_active_accounts,
    get_account_by_id,
    mark_account_cooldown,
    mark_account_invalid,
    update_account_tokens,
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

    async def get_next_account(self) -> Optional[Dict[str, Any]]:
        """
        Picks the next available active account.
        Orders by last_used_at ascending (LRU).
        """
        async with self._lock:
            accounts = await get_active_accounts()
            if not accounts:
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

        if status_code == 429:
            # Plan allowance or rate limit reached
            cooldown_until = datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)
            error_msg = f"Rate limited (429): {error_detail[:200]}"
            logger.warning("Account %s triggered 429, cooling down until %s", account["email"], cooldown_until.isoformat())
            await mark_account_cooldown(account_id, cooldown_until.isoformat(), error_msg)
            return False

        if status_code == 401:
            # Token might be expired, attempt immediate refresh
            logger.info("Account %s got 401, attempting token refresh", account["email"])
            refreshed = await self.refresh_account(account_id)
            if not refreshed:
                logger.error("Account %s token refresh failed, marking invalid", account["email"])
                await mark_account_invalid(account_id, f"401 Unauthorized, refresh failed: {error_detail[:200]}")
            return False

        # Other 4xx/5xx errors
        await record_account_usage(account_id, success=False)
        return False

    async def refresh_account(self, account_id: int) -> bool:
        """
        Manually or automatically refreshes tokens and chatApiKey for an account.
        """
        account = await get_account_by_id(account_id)
        if not account:
            return False

        try:
            tokens = await self.client.refresh_tokens(account["refresh_token"])
            new_access_token = tokens["access_token"]
            new_refresh_token = tokens["refresh_token"]

            # Try to fetch fresh chatApiKey with new access token
            try:
                new_chat_key = await self.client.fetch_chat_api_key(new_access_token)
            except Exception as e:
                logger.warning("Failed to fetch fresh chatApiKey during refresh, keeping previous: %s", e)
                new_chat_key = account["chat_api_key"]

            await update_account_tokens(
                account_id=account_id,
                access_token=new_access_token,
                refresh_token=new_refresh_token,
                chat_api_key=new_chat_key,
            )
            logger.info("Successfully refreshed account %s", account["email"])
            return True
        except Exception as e:
            logger.error("Failed to refresh account %s: %s", account["email"], e)
            return False

    async def test_account(self, account_id: int) -> Tuple[bool, str]:
        """
        Tests if an account's chat_api_key is working by performing a minimal completion.
        """
        account = await get_account_by_id(account_id)
        if not account:
            return False, "Account not found"

        test_payload = {
            "model": "qwen3.6-35b-a3b",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }
        try:
            status, resp = await self.client.forward_completion_json(account["chat_api_key"], test_payload)
            if status == 200:
                await record_account_usage(account_id, success=True)
                return True, "Account is active and functional."
            else:
                detail = resp.get("detail", str(resp))
                await self.handle_response(account, status, str(detail))
                return False, f"HTTP {status}: {detail}"
        except Exception as e:
            return False, f"Network error: {str(e)}"

    async def reset_cooldown(self, account_id: int) -> bool:
        account = await get_account_by_id(account_id)
        if not account:
            return False
        await reset_account_status(account_id)
        return True

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
        2. Proactively refreshes tokens for accounts that haven't been updated recently.
        """
        check_interval = 60  # Check cooldowns every minute
        refresh_tick = 0

        while self._running:
            try:
                await asyncio.sleep(check_interval)
                refresh_tick += check_interval

                # 1. Restore cooldowns
                now = datetime.now(timezone.utc).isoformat()
                all_accounts = await get_all_accounts()
                for acc in all_accounts:
                    if acc["status"] == "cooldown":
                        cd = acc.get("cooldown_until")
                        if cd and cd <= now:
                            logger.info("Cooldown ended for account %s, reactivating", acc["email"])
                            await reset_account_status(acc["id"])

                # 2. Proactive token rotation
                if refresh_tick >= AUTO_REFRESH_INTERVAL:
                    refresh_tick = 0
                    logger.info("Running scheduled token rotation across account pool")
                    for acc in all_accounts:
                        if acc["status"] != "invalid":
                            await self.refresh_account(acc["id"])
                            await asyncio.sleep(2)  # Avoid bursting the auth endpoint

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in background worker loop: %s", e)


# Global singleton manager instance
pool_manager = PoolManager()
