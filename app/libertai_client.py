import httpx
from typing import Any, AsyncGenerator, Dict, Optional, Tuple
from app.config import (
    LIBERTAI_INFERENCE_URL,
    LIBERTAI_API_URL,
    REQUEST_TIMEOUT,
)


class LibertAIException(Exception):
    def __init__(self, message: str, status_code: int = 500, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class LibertAIClient:
    """Async client interacting with LibertAI API and Inference backend."""

    def __init__(self, inference_base: str = LIBERTAI_INFERENCE_URL, api_base: str = LIBERTAI_API_URL):
        self.inference_base = inference_base.rstrip("/")
        self.api_base = api_base.rstrip("/")
        self.timeout = REQUEST_TIMEOUT

    async def send_login_email(self, email: str) -> bool:
        """
        Trigger LibertAI to send a 6-digit verification code to the given email.
        Calls POST /auth/login/email on inference backend.
        """
        url = f"{self.inference_base}/auth/login/email"
        payload = {
            "email": email.strip(),
            "redirect_base": "https://chat.libertai.io",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code not in (200, 204):
                error_msg = resp.text
                try:
                    data = resp.json()
                    error_msg = data.get("detail", error_msg)
                except Exception:
                    pass
                raise LibertAIException(
                    f"Failed to send verification email: {error_msg}",
                    status_code=resp.status_code,
                    detail=error_msg,
                )
            return True

    async def verify_code(self, email: str, code: str) -> Dict[str, str]:
        """
        Verify the 6-digit code sent to the email.
        Calls POST /auth/verify-magic-link.
        Returns dict containing access_token, refresh_token, token_type.
        """
        url = f"{self.inference_base}/auth/verify-magic-link"
        payload = {
            "email": email.strip(),
            "code": code.strip(),
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                error_msg = resp.text
                try:
                    data = resp.json()
                    error_msg = data.get("detail", error_msg)
                except Exception:
                    pass
                raise LibertAIException(
                    f"Verification failed: {error_msg}",
                    status_code=resp.status_code,
                    detail=error_msg,
                )
            data = resp.json()
            return {
                "access_token": data.get("access_token", ""),
                "refresh_token": data.get("refresh_token", ""),
                "token_type": data.get("token_type", "bearer"),
            }

    async def fetch_chat_api_key(self, access_token: str) -> str:
        """
        Fetch the user's chatApiKey using their access_token.
        Calls GET /api-keys/chat.
        """
        url = f"{self.inference_base}/api-keys/chat"
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                error_msg = resp.text
                try:
                    data = resp.json()
                    error_msg = data.get("detail", error_msg)
                except Exception:
                    pass
                raise LibertAIException(
                    f"Failed to get chatApiKey: {error_msg}",
                    status_code=resp.status_code,
                    detail=error_msg,
                )
            data = resp.json()
            key = data.get("key") or data.get("api_key") or data.get("chat_api_key")
            if not key:
                raise LibertAIException("No key returned in /api-keys/chat response", status_code=500, detail=data)
            return key

    async def refresh_tokens(self, refresh_token: str) -> Dict[str, str]:
        """
        Rotate refresh token and receive a fresh token pair.
        Calls POST /auth/refresh with body {"refresh_token": "..."}.
        """
        url = f"{self.inference_base}/auth/refresh"
        payload = {"refresh_token": refresh_token}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                error_msg = resp.text
                try:
                    data = resp.json()
                    error_msg = data.get("detail", error_msg)
                except Exception:
                    pass
                raise LibertAIException(
                    f"Failed to refresh tokens: {error_msg}",
                    status_code=resp.status_code,
                    detail=error_msg,
                )
            data = resp.json()
            return {
                "access_token": data.get("access_token", ""),
                "refresh_token": data.get("refresh_token", ""),
                "token_type": data.get("token_type", "bearer"),
            }

    async def get_subscription(self, access_token: str) -> Dict[str, Any]:
        """
        Get current subscription and usage limits.
        Calls GET /payments/subscription.
        """
        url = f"{self.inference_base}/payments/subscription"
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return {}
            return resp.json()

    async def list_models(self) -> Dict[str, Any]:
        """
        Query public available models from LibertAI API.
        Calls GET /v1/models (no auth required).
        """
        url = f"{self.api_base}/models"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise LibertAIException(
                    f"Failed to list models: {resp.text}",
                    status_code=resp.status_code,
                )
            return resp.json()

    async def forward_completion_json(
        self, chat_api_key: str, payload: dict
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Forward non-streaming chat completion to LibertAI.
        Returns (status_code, json_body).
        """
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {chat_api_key}",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            try:
                body = resp.json()
            except Exception:
                body = {"detail": resp.text}
            return resp.status_code, body

    async def forward_completion_stream(
        self, chat_api_key: str, payload: dict
    ) -> Tuple[int, AsyncGenerator[bytes, None]]:
        """
        Forward streaming chat completion to LibertAI.
        Returns (status_code, stream_generator).
        """
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {chat_api_key}",
        }
        client = httpx.AsyncClient(timeout=self.timeout)
        req = client.build_request("POST", url, json=payload, headers=headers)
        resp = await client.send(req, stream=True)

        if resp.status_code != 200:
            error_bytes = await resp.aread()
            await resp.aclose()
            await client.aclose()

            async def error_gen() -> AsyncGenerator[bytes, None]:
                yield error_bytes

            return resp.status_code, error_gen()

        async def stream_gen() -> AsyncGenerator[bytes, None]:
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return resp.status_code, stream_gen()
