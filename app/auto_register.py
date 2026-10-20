import asyncio
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse
import httpx
from app.database import add_or_update_account, get_active_mail_providers, update_account_usage
from app.libertai_client import LibertAIClient, resolve_forward_proxy

logger = logging.getLogger("liber2api.auto_register")

# In-memory blacklist of domains rejected by LibertAI with 3-minute TTL
REJECTED_DOMAINS: Dict[str, float] = {
    "maxxspace.com": time.time() + 3600,  # known permanent block
    "fraudfindez.com": time.time() + 3600,
    "zub.edu.pl": time.time() + 3600,
}


def is_domain_rejected(domain: str) -> bool:
    if domain in REJECTED_DOMAINS:
        if time.time() < REJECTED_DOMAINS[domain]:
            return True
        else:
            del REJECTED_DOMAINS[domain]
    return False


def mark_domain_rejected(domain: str, ttl_seconds: int = 300):
    REJECTED_DOMAINS[domain] = time.time() + ttl_seconds


class TempMailRegistrar:
    """
    Automates registration of fresh LibertAI accounts with multi-provider silent fallback.
    Supports user-defined custom mail APIs (Cloudflare Workers, webhooks, private mail services)
    and built-in public providers (neighbours.sh, tempmail.ing, mtempmail, tempmail.plus).
    Silently cycles through providers and domains upon rejection or failure.
    """

    def __init__(self, client: Optional[LibertAIClient] = None):
        self.libertai_client = client or LibertAIClient()

    async def _get_http_client(self) -> httpx.AsyncClient:
        proxy = resolve_forward_proxy()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://chat.libertai.io",
            "Referer": "https://chat.libertai.io/",
            "Sec-Ch-Ua": '"Google Chrome";v="143", "Chromium";v="143", "Not?A_Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        return httpx.AsyncClient(timeout=15.0, proxy=proxy, headers=headers)

    async def _try_custom_http(self, client: httpx.AsyncClient, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """
        Attempts registration using a user-defined custom HTTP/Webhook mail API.
        Example endpoints:
        - create_url (optional): Returns {"email": "..."} or raw email string
        - api_url (required): GET URL to poll for verification code, supports {email} placeholder.
          Response can be JSON with {"code": "123456"} or text containing the 6-digit code.
        """
        headers = {}
        if provider.get("api_key"):
            key = provider["api_key"].strip()
            if key.startswith("Bearer ") or key.startswith("Basic "):
                headers["Authorization"] = key
            else:
                headers["Authorization"] = f"Bearer {key}"
                headers["X-API-Key"] = key

        try:
            email = None
            create_url = provider.get("create_url")
            domain = (provider.get("domain") or "").strip()

            if create_url:
                r_c = await client.get(create_url, headers=headers)
                if r_c.status_code == 200:
                    try:
                        email = r_c.json().get("email")
                    except Exception:
                        email = r_c.text.strip()

            if not email and domain:
                email = f"liber_{secrets.token_hex(4)}@{domain}"

            if not email:
                logger.warning("Custom provider %s missing both valid create_url response and domain", provider.get("name"))
                return None

            dom = email.split("@")[-1]
            if is_domain_rejected(dom):
                return None

            # Request login OTP from LibertAI
            r_lib = await client.post(
                "https://inference.api.libertai.io/auth/login/email",
                json={"email": email, "redirect_base": "https://chat.libertai.io"},
            )
            if r_lib.status_code != 204:
                return None

            api_url = provider.get("api_url", "")
            if not api_url:
                return None

            # Poll for verification code
            poll_url = api_url.format(email=email) if "{email}" in api_url else f"{api_url}?email={email}"

            for _ in range(12):
                await asyncio.sleep(2.5)
                r_poll = await client.get(poll_url, headers=headers)
                if r_poll.status_code == 200:
                    code = None
                    try:
                        data = r_poll.json()
                        if isinstance(data, dict):
                            code = data.get("code")
                            if not code and "messages" in data:
                                for m in data["messages"]:
                                    text = str(m.get("content") or m.get("text") or m.get("subject") or "")
                                    c = re.findall(r"\b\d{6}\b", text)
                                    if c:
                                        code = c[0]
                                        break
                    except Exception:
                        pass

                    if not code:
                        c = re.findall(r"\b\d{6}\b", r_poll.text)
                        if c:
                            code = c[0]

                    if code:
                        return await self._verify_and_fetch_key(client, email, code, dom)
        except Exception as e:
            logger.debug("Custom provider %s error: %s", provider.get("name"), e)
        return None

    async def _try_neighbours(self, client: httpx.AsyncClient, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Try creating an account via neighbours.sh API with pristine fast-delivering domains."""
        base_url = (provider.get("api_url") or "https://neighbours.sh/api/v1").rstrip("/")
        try:
            r_cfg = await client.get(f"{base_url}/config/domains")
            if r_cfg.status_code != 200:
                return None
            domains = r_cfg.json().get("data", {}).get("domains", [])
            valid_domains = [d for d in domains if not is_domain_rejected(d)]
            if not valid_domains:
                return None

            # Prioritize fast-delivering pristine domains verified working with LibertAI
            verified_priority = ["vanish.wtf", "femboy.foundation", "diddyblud.shop", "iqpill.online", "edging.life"]
            candidate_domains = [d for d in verified_priority if d in valid_domains]
            domain = secrets.choice(candidate_domains) if candidate_domains else secrets.choice(valid_domains)

            username = f"user_{secrets.token_hex(4)}"
            email = f"{username}@{domain}"

            r_lib = await client.post(
                "https://inference.api.libertai.io/auth/login/email",
                json={"email": email, "redirect_base": "https://chat.libertai.io"},
            )
            if r_lib.status_code != 204:
                return None

            # Fast polling: max 4 checks (8 seconds total). Fast failover if mail delayed.
            for _ in range(4):
                await asyncio.sleep(2.0)
                r_inbox = await client.get(f"{base_url}/inbox/{email}")
                if r_inbox.status_code == 200:
                    items = r_inbox.json().get("data", [])
                    if items:
                        uid = items[0]["uid"]
                        r_msg = await client.get(f"{base_url}/inbox/{email}/{uid}")
                        if r_msg.status_code == 200:
                            msg_data = r_msg.json().get("data", {})
                            text = msg_data.get("text", "") or msg_data.get("html", "")
                            codes = re.findall(r"\b\d{6}\b", text)
                            if codes:
                                code = codes[0]
                                return await self._verify_and_fetch_key(client, email, code, domain)
            # Mark domain rejected temporarily if not delivered in 8s
            mark_domain_rejected(domain, ttl_seconds=120)
        except Exception as e:
            logger.debug("neighbours.sh attempt error: %s", e)
        return None

    async def _try_tempmail_ing(self, client: httpx.AsyncClient, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Try creating an account via TempMail.ing API."""
        base_url = (provider.get("api_url") or "https://api.tempmail.ing/api").rstrip("/")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Referer": "https://tempmail.ing/",
            "DNT": "1",
        }
        try:
            r_gen = await client.post(f"{base_url}/generate", json={"duration": 30}, headers=headers)
            if r_gen.status_code != 200 or not r_gen.json().get("success"):
                return None

            email = r_gen.json()["email"]["address"]
            domain = email.split("@")[-1]
            if is_domain_rejected(domain):
                return None

            r_lib = await client.post(
                "https://inference.api.libertai.io/auth/login/email",
                json={"email": email, "redirect_base": "https://chat.libertai.io"},
            )
            if r_lib.status_code != 204:
                return None

            for _ in range(10):
                await asyncio.sleep(2.5)
                r_box = await client.get(f"{base_url}/emails/{email}", headers=headers)
                if r_box.status_code == 200 and r_box.json().get("success"):
                    emails = r_box.json().get("emails", [])
                    if emails:
                        body = emails[0].get("text", "") or emails[0].get("content", "") or emails[0].get("subject", "")
                        codes = re.findall(r"\b\d{6}\b", body)
                        if codes:
                            code = codes[0]
                            return await self._verify_and_fetch_key(client, email, code, domain)
        except Exception as e:
            logger.debug("TempMail.ing attempt error: %s", e)
        return None

    async def _try_mtempmail(self, client: httpx.AsyncClient, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Try creating an account via MTempMail API."""
        mtemp_key = provider.get("api_key") or "pub_nRn1hUwpdmZvxQNVWDfoXgKyF7dIm9nRIIIt1qDw"
        mtemp_base = (provider.get("api_url") or "https://mtempmail.com/api").rstrip("/")
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
                            return await self._verify_and_fetch_key(client, email, code, domain)
        except Exception as e:
            logger.debug("MTempMail attempt error: %s", e)
        return None

    async def _try_tempmail_plus(self, client: httpx.AsyncClient, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Try creating an account via TempMail.plus API."""
        base_url = (provider.get("api_url") or "https://tempmail.plus/api").rstrip("/")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
            "referer": "https://tempmail.plus/",
        }
        domains = ["fexpost.com", "fexbox.org", "mailbox.in.ua", "rover.info", "chitthi.in", "fextemp.com", "any.pink"]
        valid_domains = [d for d in domains if not is_domain_rejected(d)]
        if not valid_domains:
            return None

        domain = secrets.choice(valid_domains)
        username = f"user_{secrets.token_hex(4)}"
        email = f"{username}@{domain}"
        try:
            r_lib = await client.post(
                "https://inference.api.libertai.io/auth/login/email",
                json={"email": email, "redirect_base": "https://chat.libertai.io"},
            )
            if r_lib.status_code != 204:
                return None

            for _ in range(8):
                await asyncio.sleep(2.0)
                r_check = await client.get(f"{base_url}/mails", params={"email": email, "limit": 10, "epin": ""}, headers=headers)
                if r_check.status_code == 200:
                    data = r_check.json()
                    if data.get("result") and data.get("mail_list"):
                        mid = data["mail_list"][0].get("mail_id")
                        r_detail = await client.get(f"{base_url}/mails/{mid}", params={"email": email, "epin": ""}, headers=headers)
                        text = r_detail.json().get("text", "")
                        codes = re.findall(r"\b\d{6}\b", text)
                        if codes:
                            code = codes[0]
                            return await self._verify_and_fetch_key(client, email, code, domain)
        except Exception as e:
            logger.debug("TempMail.plus attempt error: %s", e)
        return None

    async def _verify_and_fetch_key(
        self, client: httpx.AsyncClient, email: str, code: str, domain: str
    ) -> Optional[Dict[str, str]]:
        """Verifies OTP code with LibertAI and fetches API Key."""
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

    async def register_account(self, max_attempts: int = 2) -> Dict[str, Any]:
        """
        Attempts registration with silent provider/domain fallback.
        Dynamically queries active providers from database (supporting user-defined custom APIs).
        Fast failover within ~20 seconds to prevent reverse proxy gateway 504 timeouts.
        """
        for attempt in range(max_attempts):
            logger.info("Auto-register attempt %d/%d: Querying active providers...", attempt + 1, max_attempts)
            providers = await get_active_mail_providers()
            if not providers:
                # Default fallback
                providers = [{"name": "neighbours.sh", "provider_type": "neighbours", "api_url": "https://neighbours.sh/api/v1"}]

            async with await self._get_http_client() as client:
                res = None
                for p in providers:
                    p_type = p.get("provider_type", "")
                    if p_type == "custom_http":
                        res = await self._try_custom_http(client, p)
                    elif p_type == "neighbours":
                        res = await self._try_neighbours(client, p)
                    elif p_type == "tempmail_ing":
                        res = await self._try_tempmail_ing(client, p)
                    elif p_type == "mtempmail":
                        res = await self._try_mtempmail(client, p)
                    elif p_type == "tempmail_plus":
                        res = await self._try_tempmail_plus(client, p)

                    if res:
                        break

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

        raise RuntimeError("所有可用邮箱源（包括自定义接口与内置源）暂时无法收信或域名被风控，请检查邮箱配置或稍后重试。")


registrar = TempMailRegistrar()
