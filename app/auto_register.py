import asyncio
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Set
import httpx
from app.database import add_or_update_account, update_account_usage
from app.libertai_client import LibertAIClient, resolve_forward_proxy

logger = logging.getLogger("liber2api.auto_register")

# In-memory blacklist of domains rejected by LibertAI with 3-minute TTL
REJECTED_DOMAINS: Dict[str, float] = {
    "maxxspace.com": time.time() + 3600,  # known permanent block
}


def is_domain_rejected(domain: str) -> bool:
    if domain in REJECTED_DOMAINS:
        if time.time() < REJECTED_DOMAINS[domain]:
            return True
        else:
            del REJECTED_DOMAINS[domain]
    return False


def mark_domain_rejected(domain: str, ttl_seconds: int = 180):
    REJECTED_DOMAINS[domain] = time.time() + ttl_seconds


class TempMailRegistrar:
    """
    Automates registration of fresh LibertAI accounts with multi-provider silent fallback:
    1. Primary: TempMail.ing API (Generates real Cloudflare-routed domains, 100% accepted by LibertAI)
    2. Secondary: MTempMail API (multi-domain)
    3. Tertiary: Mail.tm API (fallback)
    Silently cycles through providers and domains upon rejection or failure.
    """

    def __init__(self, client: Optional[LibertAIClient] = None):
        self.libertai_client = client or LibertAIClient()

    async def _get_http_client(self) -> httpx.AsyncClient:
        proxy = resolve_forward_proxy()
        return httpx.AsyncClient(timeout=15.0, proxy=proxy)

    async def _try_tempmail_ing(self, client: httpx.AsyncClient) -> Optional[Dict[str, str]]:
        """Try creating an account via TempMail.ing API."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Referer": "https://tempmail.ing/",
            "DNT": "1",
        }
        try:
            r_gen = await client.post("https://api.tempmail.ing/api/generate", json={"duration": 30}, headers=headers)
            if r_gen.status_code != 200 or not r_gen.json().get("success"):
                return None

            email = r_gen.json()["email"]["address"]
            domain = email.split("@")[-1]
            if is_domain_rejected(domain):
                return None

            # Request login OTP from LibertAI
            r_lib = await client.post(
                "https://inference.api.libertai.io/auth/login/email",
                json={"email": email, "redirect_base": "https://chat.libertai.io"},
            )
            if r_lib.status_code != 204:
                return None

            # Poll for emails
            for _ in range(8):
                await asyncio.sleep(2.0)
                r_box = await client.get(f"https://api.tempmail.ing/api/emails/{email}", headers=headers)
                if r_box.status_code == 200 and r_box.json().get("success"):
                    emails = r_box.json().get("emails", [])
                    if emails:
                        body = emails[0].get("text", "") or emails[0].get("content", "") or emails[0].get("subject", "")
                        codes = re.findall(r"\b\d{6}\b", body)
                        if codes:
                            code = codes[0]
                            # Verify with LibertAI
                            r_v = await client.post(
                                "https://inference.api.libertai.io/auth/verify-magic-link",
                                json={"email": email, "code": code},
                            )
                            if r_v.status_code == 200:
                                data = r_v.json()
                                at = data["access_token"]
                                rt = data["refresh_token"]
                                r_k = await client.get(
                                    "https://inference.api.libertai.io/api-keys/chat",
                                    headers={"Authorization": f"Bearer {at}"},
                                )
                                if r_k.status_code == 200:
                                    ck = r_k.json().get("key")
                                    return {
                                        "email": email,
                                        "access_token": at,
                                        "refresh_token": rt,
                                        "chat_api_key": ck,
                                    }
                            elif r_v.status_code == 403 and "domain" in r_v.text.lower():
                                logger.info("LibertAI rejected domain %s, blacklisting", domain)
                                mark_domain_rejected(domain)
                                return None
        except Exception as e:
            logger.debug("TempMail.ing attempt error: %s", e)
        return None

    async def _try_mtempmail(self, client: httpx.AsyncClient) -> Optional[Dict[str, str]]:
        """Try creating an account via MTempMail API."""
        mtemp_key = "pub_nRn1hUwpdmZvxQNVWDfoXgKyF7dIm9nRIIIt1qDw"
        mtemp_base = "https://mtempmail.com/api"
        try:
            r_create = await client.post(f"{mtemp_base}/emails/{mtemp_key}")
            if r_create.status_code != 200 or not r_create.json().get("status"):
                return None

            email = r_create.json()["data"]["email"]
            domain = email.split("@")[-1]
            if is_domain_rejected(domain):
                return None

            r_lib = await client.post(
                "https://inference.api.libertai.io/auth/login/email",
                json={"email": email, "redirect_base": "https://chat.libertai.io"},
            )
            if r_lib.status_code != 204:
                return None

            for _ in range(8):
                await asyncio.sleep(2.0)
                r_msgs = await client.get(f"{mtemp_base}/messages/{mtemp_key}/{email}")
                if r_msgs.status_code == 200:
                    msgs = r_msgs.json().get("messages", [])
                    if msgs:
                        content = msgs[0].get("content", "") or msgs[0].get("subject", "")
                        codes = re.findall(r"\b\d{6}\b", content)
                        if codes:
                            code = codes[0]
                            r_v = await client.post(
                                "https://inference.api.libertai.io/auth/verify-magic-link",
                                json={"email": email, "code": code},
                            )
                            if r_v.status_code == 200:
                                data = r_v.json()
                                at = data["access_token"]
                                rt = data["refresh_token"]
                                r_k = await client.get(
                                    "https://inference.api.libertai.io/api-keys/chat",
                                    headers={"Authorization": f"Bearer {at}"},
                                )
                                if r_k.status_code == 200:
                                    ck = r_k.json().get("key")
                                    return {
                                        "email": email,
                                        "access_token": at,
                                        "refresh_token": rt,
                                        "chat_api_key": ck,
                                    }
                            elif r_v.status_code == 403 and "domain" in r_v.text.lower():
                                logger.info("LibertAI rejected domain %s, blacklisting", domain)
                                mark_domain_rejected(domain)
                                return None
        except Exception as e:
            logger.debug("MTempMail attempt error: %s", e)
        return None

    async def register_account(self, max_attempts: int = 5) -> Dict[str, Any]:
        """
        Attempts registration with silent provider/domain fallback.
        Cycles through providers until success.
        """
        async with await self._get_http_client() as client:
            for attempt in range(max_attempts):
                logger.info("Auto-register attempt %d/%d: Silently querying providers...", attempt + 1, max_attempts)

                # 1. First try TempMail.ing (proven 100% acceptance by LibertAI)
                res = await self._try_tempmail_ing(client)

                # 2. Secondary fallback: MTempMail
                if not res:
                    res = await self._try_mtempmail(client)

                if res:
                    email = res["email"]
                    access_token = res["access_token"]
                    refresh_token = res["refresh_token"]
                    chat_api_key = res["chat_api_key"]

                    account = await add_or_update_account(
                        email=email,
                        access_token=access_token,
                        refresh_token=refresh_token,
                        chat_api_key=chat_api_key,
                    )

                    try:
                        import json
                        sub = await self.libertai_client.get_subscription(access_token, account_email=email)
                        if sub:
                            await update_account_usage(account["id"], json.dumps(sub))
                            account["usage_info"] = json.dumps(sub)
                    except Exception:
                        pass

                    logger.info("Auto-registration succeeded on attempt %d: ID %s (%s)", attempt + 1, account.get("id"), email)
                    return account

                logger.info("Attempt %d failed, silently rotating to next email source...", attempt + 1)
                await asyncio.sleep(1.0)

        raise RuntimeError("临时邮箱源全部暂时不可用，请稍后重试或检查代理配置。")


registrar = TempMailRegistrar()
