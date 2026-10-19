import asyncio
import logging
import re
import secrets
from typing import Any, Dict, List, Optional, Set
import httpx
from app.database import add_or_update_account, update_account_usage
from app.libertai_client import LibertAIClient, resolve_forward_proxy

logger = logging.getLogger("liber2api.auto_register")

# In-memory blacklist of email domains rejected by LibertAI
REJECTED_DOMAINS: Set[str] = {
    "maxxspace.com",  # Known blacklisted by LibertAI
}


class TempMailRegistrar:
    """
    Automates registration of fresh LibertAI accounts with multi-provider fallback:
    1. Provider 1: MTempMail API (multi-domain: zub.edu.pl, mtempmail.com, fraudfindez.com, etc.)
    2. Provider 2: Mail.tm API (fallback)
    Silently cycles through domains and providers upon rejection or timeout.
    """

    def __init__(self, client: Optional[LibertAIClient] = None):
        self.libertai_client = client or LibertAIClient()
        self.mtemp_key = "pub_nRn1hUwpdmZvxQNVWDfoXgKyF7dIm9nRIIIt1qDw"
        self.mtemp_base = "https://mtempmail.com/api"
        self.mailtm_base = "https://api.mail.tm"

    async def _get_http_client(self) -> httpx.AsyncClient:
        proxy = resolve_forward_proxy()
        return httpx.AsyncClient(timeout=15.0, proxy=proxy)

    async def _try_mtempmail(self, client: httpx.AsyncClient, domain: Optional[str] = None) -> Optional[Dict[str, str]]:
        """Try creating an account via MTempMail API."""
        try:
            # Fetch active domain if not specified
            if not domain:
                r_dom = await client.get(f"{self.mtemp_base}/domains/{self.mtemp_key}/all")
                all_doms = r_dom.json().get("data", {}).get("domains", [])
                candidates = [d["domain"] for d in all_doms if d["domain"] not in REJECTED_DOMAINS]
                domain = candidates[0] if candidates else None

            url = f"{self.mtemp_base}/emails/{self.mtemp_key}"
            if domain:
                url += f"?domain={domain}"

            r_create = await client.post(url)
            c_data = r_create.json()
            if not c_data.get("status"):
                return None

            email = c_data["data"]["email"]
            actual_domain = email.split("@")[-1]
            if actual_domain in REJECTED_DOMAINS:
                return None

            # Request login OTP from LibertAI
            r_lib = await client.post(
                "https://inference.api.libertai.io/auth/login/email",
                json={"email": email, "redirect_base": "https://chat.libertai.io"},
            )
            if r_lib.status_code != 204:
                return None

            # Poll for verification code
            for _ in range(8):
                await asyncio.sleep(2)
                r_msgs = await client.get(f"{self.mtemp_base}/messages/{self.mtemp_key}/{email}")
                if r_msgs.status_code == 200:
                    msgs = r_msgs.json().get("messages", [])
                    if msgs:
                        content = msgs[0].get("content", "") or msgs[0].get("subject", "")
                        codes = re.findall(r"\b\d{6}\b", content)
                        if codes:
                            code = codes[0]
                            # Verify code with LibertAI
                            r_v = await client.post(
                                "https://inference.api.libertai.io/auth/verify-magic-link",
                                json={"email": email, "code": code},
                            )
                            if r_v.status_code == 200:
                                data = r_v.json()
                                at = data["access_token"]
                                rt = data["refresh_token"]
                                # Fetch chatApiKey
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
                                logger.info("LibertAI rejected domain %s, blacklisting for subsequent attempts", actual_domain)
                                REJECTED_DOMAINS.add(actual_domain)
                                return None
        except Exception as e:
            logger.debug("MTempMail attempt error: %s", e)
        return None

    async def _try_mailtm(self, client: httpx.AsyncClient) -> Optional[Dict[str, str]]:
        """Try creating an account via Mail.tm API."""
        try:
            r_dom = await client.get(f"{self.mailtm_base}/domains")
            doms = [d["domain"] for d in r_dom.json().get("hydra:member", []) if d["domain"] not in REJECTED_DOMAINS]
            if not doms:
                return None

            domain = doms[0]
            username = f"liber_{secrets.token_hex(4)}"
            password = f"P_{secrets.token_hex(8)}"
            email = f"{username}@{domain}"

            acc_resp = await client.post(
                f"{self.mailtm_base}/accounts",
                json={"address": email, "password": password},
            )
            if acc_resp.status_code != 201:
                return None

            tok_resp = await client.post(
                f"{self.mailtm_base}/token",
                json={"address": email, "password": password},
            )
            if tok_resp.status_code != 200:
                return None

            mail_token = tok_resp.json().get("token")
            mail_headers = {"Authorization": f"Bearer {mail_token}"}

            r_lib = await client.post(
                "https://inference.api.libertai.io/auth/login/email",
                json={"email": email, "redirect_base": "https://chat.libertai.io"},
            )
            if r_lib.status_code != 204:
                return None

            for _ in range(8):
                await asyncio.sleep(2)
                r_msgs = await client.get(f"{self.mailtm_base}/messages", headers=mail_headers)
                if r_msgs.status_code == 200:
                    msgs = r_msgs.json().get("hydra:member", [])
                    if msgs:
                        msg_id = msgs[0]["id"]
                        det_resp = await client.get(f"{self.mailtm_base}/messages/{msg_id}", headers=mail_headers)
                        body_text = det_resp.json().get("text", "") or det_resp.json().get("intro", "") or ""
                        codes = re.findall(r"\b\d{6}\b", body_text)
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
                                logger.info("LibertAI rejected domain %s, blacklisting for subsequent attempts", domain)
                                REJECTED_DOMAINS.add(domain)
                                return None
        except Exception as e:
            logger.debug("Mail.tm attempt error: %s", e)
        return None

    async def register_account(self, max_attempts: int = 5) -> Dict[str, Any]:
        """
        Attempts registration with silent domain rotation across multiple providers.
        Retries up to max_attempts times until success.
        """
        async with await self._get_http_client() as client:
            # Query mtempmail domains first
            mtemp_domains: List[str] = []
            try:
                r_dom = await client.get(f"{self.mtemp_base}/domains/{self.mtemp_key}/all")
                mtemp_domains = [d["domain"] for d in r_dom.json().get("data", {}).get("domains", [])]
            except Exception:
                pass

            candidate_domains = [d for d in mtemp_domains if d not in REJECTED_DOMAINS]
            if not candidate_domains:
                candidate_domains = [None]  # Let provider pick default

            for attempt in range(max_attempts):
                target_domain = candidate_domains[attempt % len(candidate_domains)]
                logger.info("Attempt %d/%d: Silently trying registration (domain: %s)...", attempt + 1, max_attempts, target_domain or "default")

                # 1. Try MTempMail
                res = await self._try_mtempmail(client, domain=target_domain)
                if not res and attempt % 2 == 1:
                    # 2. Try Mail.tm as secondary fallback
                    res = await self._try_mailtm(client)

                if res:
                    email = res["email"]
                    access_token = res["access_token"]
                    refresh_token = res["refresh_token"]
                    chat_api_key = res["chat_api_key"]

                    # Store in database
                    account = await add_or_update_account(
                        email=email,
                        access_token=access_token,
                        refresh_token=refresh_token,
                        chat_api_key=chat_api_key,
                    )

                    # Initial usage
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

                logger.debug("Attempt %d failed, silently switching to next domain...", attempt + 1)
                await asyncio.sleep(1)

        raise RuntimeError("所有备用邮箱源及域名均无法完成注册，请稍后重试或检查上游网络。")


registrar = TempMailRegistrar()
