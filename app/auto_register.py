import asyncio
import logging
import re
import secrets
from typing import Any, Dict, Optional
import httpx
import app.config as config
from app.database import add_or_update_account, update_account_usage
from app.libertai_client import LibertAIClient, resolve_forward_proxy

logger = logging.getLogger("liber2api.auto_register")


class TempMailRegistrar:
    """
    Automates registration of fresh LibertAI accounts using mail.tm free disposable email API.
    1. Fetches active domain from mail.tm.
    2. Registers mailbox on mail.tm.
    3. Requests 6-digit login OTP code from LibertAI.
    4. Polls inbox on mail.tm for OTP code.
    5. Verifies code with LibertAI to obtain access_token & refresh_token.
    6. Fetches chatApiKey and stores account into SQLite pool.
    """

    def __init__(self, client: Optional[LibertAIClient] = None):
        self.libertai_client = client or LibertAIClient()
        self.mail_api_base = "https://api.mail.tm"

    async def _get_mail_client(self) -> httpx.AsyncClient:
        proxy = resolve_forward_proxy()
        return httpx.AsyncClient(timeout=15.0, proxy=proxy)

    async def register_account(self, max_wait_seconds: int = 30) -> Dict[str, Any]:
        username = f"liber_{secrets.token_hex(4)}"
        password = f"P_{secrets.token_hex(8)}"

        async with await self._get_mail_client() as m_client:
            # 1. Fetch active domain
            try:
                d_resp = await m_client.get(f"{self.mail_api_base}/domains")
                domains_data = d_resp.json().get("hydra:member", [])
                if not domains_data:
                    raise RuntimeError("No available domains from mail.tm")
                domain = domains_data[0]["domain"]
            except Exception as e:
                logger.error("Failed to query mail.tm domains: %s", e)
                domain = "maxxspace.com"

            email = f"{username}@{domain}"
            logger.info("Starting automatic registration for %s", email)

            # 2. Create mailbox
            acc_resp = await m_client.post(
                f"{self.mail_api_base}/accounts",
                json={"address": email, "password": password},
            )
            if acc_resp.status_code != 201:
                raise RuntimeError(f"Failed to create mail.tm account ({acc_resp.status_code}): {acc_resp.text}")

            # 3. Obtain mail.tm auth token
            tok_resp = await m_client.post(
                f"{self.mail_api_base}/token",
                json={"address": email, "password": password},
            )
            if tok_resp.status_code != 200:
                raise RuntimeError("Failed to obtain mail.tm auth token")
            mail_token = tok_resp.json().get("token")
            mail_headers = {"Authorization": f"Bearer {mail_token}"}

            # 4. Request login OTP code from LibertAI
            await self.libertai_client.send_login_email(email)
            logger.info("Sent login OTP request to LibertAI for %s, waiting for verification code...", email)

            # 5. Poll mail.tm inbox
            code: Optional[str] = None
            elapsed = 0
            poll_interval = 2.0

            while elapsed < max_wait_seconds:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                try:
                    msgs_resp = await m_client.get(
                        f"{self.mail_api_base}/messages", headers=mail_headers
                    )
                    if msgs_resp.status_code != 200:
                        continue
                    msgs = msgs_resp.json().get("hydra:member", [])
                    if msgs:
                        msg_id = msgs[0]["id"]
                        det_resp = await m_client.get(
                            f"{self.mail_api_base}/messages/{msg_id}", headers=mail_headers
                        )
                        body_text = (
                            det_resp.json().get("text", "")
                            or det_resp.json().get("intro", "")
                            or ""
                        )
                        # Extract 6-digit code
                        codes = re.findall(r"\b\d{6}\b", body_text)
                        if codes:
                            code = codes[0]
                            logger.info("Found verification code %s for %s", code, email)
                            break
                except Exception as e:
                    logger.debug("Polling mail.tm error: %s", e)

            if not code:
                raise TimeoutError(f"Timed out waiting for verification code for {email}")

            # 6. Verify with LibertAI
            tokens = await self.libertai_client.verify_code(email, code)
            access_token = tokens["access_token"]
            refresh_token = tokens["refresh_token"]

            # 7. Fetch chatApiKey
            chat_api_key = await self.libertai_client.fetch_chat_api_key(
                access_token, account_email=email
            )

            # 8. Store account into SQLite
            account = await add_or_update_account(
                email=email,
                access_token=access_token,
                refresh_token=refresh_token,
                chat_api_key=chat_api_key,
            )

            # 9. Query initial subscription usage
            try:
                import json
                sub = await self.libertai_client.get_subscription(access_token, account_email=email)
                if sub:
                    await update_account_usage(account["id"], json.dumps(sub))
                    account["usage_info"] = json.dumps(sub)
            except Exception:
                pass

            logger.info("Auto-registration succeeded for account ID %s (%s)", account.get("id"), email)
            return account


registrar = TempMailRegistrar()
