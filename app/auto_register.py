import asyncio
import json
import logging
import re
import secrets
import time
from typing import Any, Dict, Optional
from string import Formatter
from urllib.parse import quote, urlencode, urlparse
import httpx
from app.database import add_or_update_account, get_active_mail_providers, update_account_usage
from app.libertai_client import LibertAIClient, LibertAIException, resolve_forward_proxy

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


def _validate_mail_url(url: str, *, template: bool = False) -> None:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("An explicit mail API URL is required")
    fields = list(Formatter().parse(url))
    for _, field, spec, conversion in fields:
        if field is not None and (not template or field != "email" or spec or conversion):
            raise ValueError("Only the {email} mail URL placeholder is supported")
    parsed = urlparse(url.replace("{email}", "mailbox"))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Mail API URL must be absolute HTTP(S)")
    _ = parsed.port  # Validate malformed/out-of-range ports before any network call.


def validate_mail_provider(provider: Dict[str, Any]) -> None:
    """Validate before requesting an OTP; shared by registration and admin routes."""
    kind = provider.get("provider_type")
    if kind in ("custom_http", "inbound_webhook"):
        _validate_mail_url(provider.get("api_url", ""), template=True)
        if provider.get("create_url"):
            _validate_mail_url(provider["create_url"])
        if kind == "inbound_webhook" and not (provider.get("domain") or "").strip():
            raise ValueError("Inbound mail requires an explicit domain")
    elif provider.get("api_url"):
        _validate_mail_url(provider["api_url"])


def mail_auth_headers(provider: Dict[str, Any]) -> Dict[str, str]:
    key = (provider.get("api_key") or "").strip()
    if not key:
        return {}
    if key.startswith(("Bearer ", "Basic ")):
        return {"Authorization": key}
    return {"Authorization": f"Bearer {key}", "X-API-Key": key}


def _poll_url(template: str, email: str) -> str:
    _validate_mail_url(template, template=True)
    if "{email}" in template:
        return template.format(email=quote(email, safe=""))
    return template + ("&" if "?" in template else "?") + urlencode({"email": email})


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
        headers = mail_auth_headers(provider)

        try:
            validate_mail_provider({**provider, "provider_type": "custom_http"})
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
            await self.libertai_client.send_login_email(email)

            api_url = provider.get("api_url", "")
            if not api_url:
                return None

            # Poll for verification code
            poll_url = _poll_url(api_url, email)

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

    async def _try_tempmail_lol(self, client: httpx.AsyncClient, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Try creating an account via TempMail.lol API."""
        base_url = (provider.get("api_url") or "https://api.tempmail.lol/v2").rstrip("/")
        try:
            r_c = await client.get(f"{base_url}/inbox/create")
            if r_c.status_code not in (200, 201):
                return None
            data = r_c.json()
            address = data.get("address")
            token = data.get("token")
            if not address or not token:
                return None
            domain = address.split("@")[-1]
            if is_domain_rejected(domain):
                return None

            await self.libertai_client.send_login_email(address)

            for _ in range(6):
                await asyncio.sleep(2.0)
                r_in = await client.get(f"{base_url}/inbox?token={token}")
                if r_in.status_code == 200:
                    emails = r_in.json().get("emails", [])
                    if emails:
                        body = emails[0].get("body", "") or emails[0].get("html", "") or str(emails[0])
                        codes = re.findall(r"\b\d{6}\b", body)
                        if codes:
                            code = codes[0]
                            return await self._verify_and_fetch_key(client, address, code, domain)
            mark_domain_rejected(domain, ttl_seconds=120)
        except Exception as e:
            logger.debug("TempMail.lol attempt error: %s", e)
        return None

    async def _try_duckmail(self, client: httpx.AsyncClient, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Try creating an account via DuckMail API."""
        base_url = (provider.get("api_url") or "https://api.duckmail.sbs").rstrip("/")
        try:
            r_dom = await client.get(f"{base_url}/domains")
            if r_dom.status_code != 200:
                return None
            members = r_dom.json().get("hydra:member", [])
            domains = [d["domain"] for d in members if not is_domain_rejected(d["domain"])]
            if not domains:
                return None
            domain = secrets.choice(domains)
            username = f"user_{secrets.token_hex(4)}"
            email = f"{username}@{domain}"
            password = f"P@{secrets.token_hex(6)}"

            r_acc = await client.post(f"{base_url}/accounts", json={"address": email, "password": password})
            if r_acc.status_code not in (200, 201):
                return None

            r_tok = await client.post(f"{base_url}/token", json={"address": email, "password": password})
            if r_tok.status_code != 200:
                return None
            mail_token = r_tok.json().get("token")

            await self.libertai_client.send_login_email(email)

            for _ in range(6):
                await asyncio.sleep(2.0)
                r_msgs = await client.get(f"{base_url}/messages", headers={"Authorization": f"Bearer {mail_token}"})
                if r_msgs.status_code == 200:
                    items = r_msgs.json().get("hydra:member", [])
                    if items:
                        mid = items[0]["id"]
                        r_det = await client.get(f"{base_url}/messages/{mid}", headers={"Authorization": f"Bearer {mail_token}"})
                        text = r_det.json().get("text", "") or r_det.json().get("intro", "") or str(r_det.json().get("html", ""))
                        codes = re.findall(r"\b\d{6}\b", text)
                        if codes:
                            code = codes[0]
                            return await self._verify_and_fetch_key(client, email, code, domain)
            mark_domain_rejected(domain, ttl_seconds=120)
        except Exception as e:
            logger.debug("DuckMail attempt error: %s", e)
        return None

    async def _try_inbound_webhook(self, client: httpx.AsyncClient, provider: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Try creating an account via local/configured Inbound Webhook buffer."""
        try:
            validate_mail_provider({**provider, "provider_type": "inbound_webhook"})
            domain = provider["domain"].strip()
            email = f"user_{secrets.token_hex(4)}@{domain}"
            poll_url = _poll_url(provider["api_url"], email)
            await self.libertai_client.send_login_email(email)

            for _ in range(8):
                await asyncio.sleep(2.0)
                r_poll = await client.get(poll_url, headers=mail_auth_headers(provider))
                if r_poll.status_code == 200:
                    data = r_poll.json()
                    if data.get("success") and data.get("code"):
                        code = data["code"]
                        return await self._verify_and_fetch_key(client, email, code, domain)
        except Exception as e:
            logger.debug("Inbound webhook attempt error: %s", e)
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

            await self.libertai_client.send_login_email(email)

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

            await self.libertai_client.send_login_email(email)

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

            await self.libertai_client.send_login_email(email)

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
            await self.libertai_client.send_login_email(email)

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
        """Use the configured LibertAI transport for OTP and key retrieval."""
        try:
            tokens = await self.libertai_client.verify_code(email, str(code))
            access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
            if not isinstance(access, str) or not access.strip() or not isinstance(refresh, str) or not refresh.strip():
                return None
            key = await self.libertai_client.fetch_chat_api_key(access, account_email=email)
            if not isinstance(key, str) or not key.strip():
                return None
            return {"email": email, "access_token": access, "refresh_token": refresh, "chat_api_key": key}
        except LibertAIException as exc:
            if exc.status_code == 403 and "domain" in str(exc).lower():
                mark_domain_rejected(domain)
            return None

    async def register_account(self, max_attempts: int = 2, max_wait_seconds: float = 20) -> Dict[str, Any]:
        """Bound the entire operation (DB, providers, polling, retries) by a deadline."""
        if max_attempts < 1 or max_wait_seconds <= 0:
            raise ValueError("Registration attempts and time budget must be positive")
        deadline = asyncio.get_running_loop().time() + max_wait_seconds
        try:
            return await asyncio.wait_for(self._register_with_deadline(max_attempts, deadline), timeout=max_wait_seconds)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Automatic registration exceeded its total time budget") from exc

    async def _register_with_deadline(self, max_attempts: int, deadline: float) -> Dict[str, Any]:
        handlers = {
            "custom_http": self._try_custom_http,
            "inbound_webhook": self._try_inbound_webhook,
            "tempmail_lol": self._try_tempmail_lol,
            "duckmail": self._try_duckmail,
            "neighbours": self._try_neighbours,
            "tempmail_ing": self._try_tempmail_ing,
            "mtempmail": self._try_mtempmail,
            "tempmail_plus": self._try_tempmail_plus,
        }
        for attempt in range(max_attempts):
            providers = await get_active_mail_providers()
            if not providers:
                raise RuntimeError("No active mail providers; automatic registration is disabled")
            async with await self._get_http_client() as client:
                for index, provider in enumerate(providers):
                    handler = handlers.get(provider.get("provider_type", ""))
                    if handler is None:
                        logger.warning("Unsupported mail provider type: %s", provider.get("provider_type"))
                        continue
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    # A slow source cannot consume the entire fallback budget. Keep
                    # time for each remaining source and subsequent configured round.
                    slots = (len(providers) - index) + (max_attempts - attempt - 1) * len(providers)
                    try:
                        validate_mail_provider(provider)
                        result = await asyncio.wait_for(handler(client, provider), timeout=remaining / slots)
                        if not result or not all(isinstance(result.get(field), str) and result[field].strip()
                                                 for field in ("email", "access_token", "refresh_token", "chat_api_key")):
                            continue
                    except asyncio.TimeoutError:
                        logger.info("Mail provider %s exhausted its time slice", provider.get("name"))
                        continue
                    except Exception as exc:
                        logger.warning("Mail provider %s failed: %s", provider.get("name"), type(exc).__name__)
                        continue
                    account = await add_or_update_account(
                        email=result["email"], access_token=result["access_token"],
                        refresh_token=result["refresh_token"], chat_api_key=result["chat_api_key"],
                    )
                    # Usage is optional; it must not spend the registration budget
                    # after credentials have already been successfully persisted.
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining > 0:
                        try:
                            subscription = await asyncio.wait_for(
                                self.libertai_client.get_subscription(result["access_token"], account_email=result["email"]),
                                timeout=min(1.0, remaining / 2),
                            )
                            if subscription:
                                encoded = json.dumps(subscription)
                                await update_account_usage(account["id"], encoded)
                                account["usage_info"] = encoded
                        except Exception:
                            pass
                    return account
        raise RuntimeError("所有可用邮箱源暂时无法收信或域名被风控，请检查邮箱配置或稍后重试。")


registrar = TempMailRegistrar()
