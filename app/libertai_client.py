from urllib.parse import quote_plus, urlparse, urlunparse
import httpx
from typing import Any, AsyncGenerator, AsyncIterator, Dict, Optional, Tuple
import anyio
import app.config as config
from app.stream_normalizer import StreamReasoningNormalizer, normalize_sse_stream


class LibertAIException(Exception):
    def __init__(self, message: str, status_code: int = 500, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


def resolve_forward_proxy(account_email: Optional[str] = None) -> Optional[str]:
    """
    Resolves forward proxy URL (e.g. PROXY_URL=http://Default:TOKEN@resin:2260).
    If pointing to Resin with auth credentials (user:pass@host:port), dynamically injects
    account email or a unique session tag to satisfy Resin sticky proxy format: Platform.Account:TOKEN.
    """
    if not config.PROXY_URL:
        return None

    try:
        parsed = urlparse(config.PROXY_URL)
        if parsed.username and parsed.password:
            # If username doesn't already have an account attached (e.g. "Default")
            if "." not in parsed.username:
                import secrets
                account_tag = quote_plus(account_email.strip()) if account_email else f"session_{secrets.token_hex(4)}"
                new_user = f"{parsed.username}.{account_tag}"
                netloc = f"{new_user}:{parsed.password}@{parsed.hostname}"
                if parsed.port:
                    netloc += f":{parsed.port}"
                return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        pass

    return config.PROXY_URL


def resolve_request_endpoint(
    target_url: str,
    account_email: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, str]]:
    """
    Resolves target URL and request headers according to Resin sticky proxy specification.

    If config.RESIN_URL is configured:
      Format: <resin_url>/<Platform>/<protocol>/<host><path_and_query>
      Header: X-Resin-Account: <account_email> (anchors business account to stable outbound node)
    Otherwise:
      Returns target_url directly with standard headers.
    """
    headers = dict(extra_headers or {})

    if not config.RESIN_URL:
        return target_url, headers

    parsed = urlparse(target_url)
    protocol = parsed.scheme or "https"
    host = parsed.netloc

    path = parsed.path
    if not path.startswith("/"):
        path = "/" + path
    if parsed.query:
        path = f"{path}?{parsed.query}"

    resin_base = config.RESIN_URL.rstrip("/")
    platform = config.RESIN_PLATFORM_NAME or "Default"
    proxy_url = f"{resin_base}/{platform}/{protocol}/{host}{path}"

    if account_email:
        headers["X-Resin-Account"] = account_email.strip()

    return proxy_url, headers


async def _close_upstream(client: httpx.AsyncClient, response: Optional[httpx.Response] = None):
    # Starlette cancels stream tasks inside an AnyIO cancellation scope.
    with anyio.CancelScope(shield=True):
        try:
            if response is not None:
                await response.aclose()
        finally:
            await client.aclose()


class _CompletionStream:
    """Own a response even before iteration starts; callers must close discarded streams."""

    def __init__(self, client, response):
        self._client = client
        self._response = response
        self._stream = normalize_sse_stream(response.aiter_bytes())
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        try:
            return await anext(self._stream)
        except BaseException:
            try:
                await self.aclose()
            except Exception:
                # Preserve transport errors/cancellation for the caller's policy.
                pass
            raise

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            try:
                await self._stream.aclose()
            finally:
                await _close_upstream(self._client, self._response)


class LibertAIClient:
    """Async client interacting with LibertAI API and Inference backend, with Resin sticky support."""

    def __init__(self, inference_base: Optional[str] = None, api_base: Optional[str] = None):
        self.inference_base = (inference_base or config.LIBERTAI_INFERENCE_URL).rstrip("/")
        self.api_base = (api_base or config.LIBERTAI_API_URL).rstrip("/")
        self.timeout = config.REQUEST_TIMEOUT

    def _get_client(self, account_email: Optional[str] = None) -> httpx.AsyncClient:
        proxy = resolve_forward_proxy(account_email)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://chat.libertai.io",
            "Referer": "https://chat.libertai.io/",
            "Sec-Ch-Ua": '"Google Chrome";v="143", "Chromium";v="143", "Not?A_Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        }
        return httpx.AsyncClient(timeout=self.timeout, proxy=proxy, headers=headers)

    async def send_login_email(self, email: str) -> bool:
        """
        Trigger LibertAI to send a 6-digit verification code to the given email.
        Calls POST /auth/login/email on inference backend.
        """
        target_url = f"{self.inference_base}/auth/login/email"
        url, headers = resolve_request_endpoint(target_url, account_email=email)
        headers["Content-Type"] = "application/json"
        payload = {
            "email": email.strip(),
            "redirect_base": "https://chat.libertai.io",
        }
        async with self._get_client(account_email=email) as client:
            resp = await client.post(url, json=payload, headers=headers)
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
        """
        target_url = f"{self.inference_base}/auth/verify-magic-link"
        url, headers = resolve_request_endpoint(target_url, account_email=email)
        headers["Content-Type"] = "application/json"
        payload = {
            "email": email.strip(),
            "code": code.strip(),
        }
        async with self._get_client(account_email=email) as client:
            resp = await client.post(url, json=payload, headers=headers)
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

    async def fetch_chat_api_key(self, access_token: str, account_email: Optional[str] = None) -> str:
        """
        Fetch the user's chatApiKey using their access_token.
        Calls GET /api-keys/chat.
        """
        target_url = f"{self.inference_base}/api-keys/chat"
        headers_in = {"Authorization": f"Bearer {access_token}"}
        url, headers = resolve_request_endpoint(target_url, account_email=account_email, extra_headers=headers_in)
        async with self._get_client(account_email=account_email) as client:
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

    async def refresh_tokens(self, refresh_token: str, account_email: Optional[str] = None) -> Dict[str, str]:
        """
        Rotate refresh token and receive a fresh token pair.
        Calls POST /auth/refresh with body {"refresh_token": "..."}.
        """
        target_url = f"{self.inference_base}/auth/refresh"
        headers_in = {"Content-Type": "application/json"}
        url, headers = resolve_request_endpoint(target_url, account_email=account_email, extra_headers=headers_in)
        payload = {"refresh_token": refresh_token}
        async with self._get_client(account_email=account_email) as client:
            resp = await client.post(url, json=payload, headers=headers)
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

    async def get_subscription(self, access_token: str, account_email: Optional[str] = None) -> Dict[str, Any]:
        """
        Get current subscription and usage limits.
        Calls GET /payments/subscription.
        """
        target_url = f"{self.inference_base}/payments/subscription"
        headers_in = {"Authorization": f"Bearer {access_token}"}
        url, headers = resolve_request_endpoint(target_url, account_email=account_email, extra_headers=headers_in)
        async with self._get_client(account_email=account_email) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return {}
            return resp.json()

    async def list_models(self) -> Dict[str, Any]:
        """
        Query public available models from LibertAI API.
        Calls GET /v1/models (no auth required).
        """
        target_url = f"{self.api_base}/models"
        url, headers = resolve_request_endpoint(target_url)
        async with self._get_client() as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise LibertAIException(
                    f"Failed to list models: {resp.text}",
                    status_code=resp.status_code,
                )
            return resp.json()

    async def check_chat_api_key_auth(self, chat_api_key: str, account_email: Optional[str] = None) -> Tuple[int, str]:
        """
        Lightweight check of chat_api_key validity using GET https://api.libertai.io/libertai/auth/check.
        Returns (status_code, response_text).
        """
        target_url = "https://api.libertai.io/libertai/auth/check"
        headers_in = {
            "Authorization": f"Bearer {chat_api_key}",
        }
        url, headers = resolve_request_endpoint(target_url, account_email=account_email, extra_headers=headers_in)
        async with self._get_client(account_email=account_email) as client:
            resp = await client.get(url, headers=headers)
            return resp.status_code, resp.text

    async def forward_completion_json(
        self, chat_api_key: str, payload: dict, account_email: Optional[str] = None
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Forward non-streaming chat completion to LibertAI.
        Returns (status_code, json_body).
        """
        target_url = f"{self.api_base}/chat/completions"
        headers_in = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {chat_api_key}",
        }
        url, headers = resolve_request_endpoint(target_url, account_email=account_email, extra_headers=headers_in)
        async with self._get_client(account_email=account_email) as client:
            resp = await client.post(url, json=payload, headers=headers)
            try:
                body = resp.json()
                if resp.status_code == 200 and isinstance(body, dict):
                    body = StreamReasoningNormalizer().normalize_json_response(body)
            except Exception:
                body = {"detail": resp.text}
            return resp.status_code, body

    async def forward_completion_stream(
        self, chat_api_key: str, payload: dict, account_email: Optional[str] = None
    ) -> Tuple[int, AsyncIterator[bytes]]:
        """
        Forward streaming completion. Returns (status_code, closeable async iterator).

        The caller owns the returned iterator: close discarded streams, and attach
        its aclose to the downstream response's cleanup/background task. Upstream
        iteration errors propagate; already-emitted output must never be replayed.
        """
        target_url = f"{self.api_base}/chat/completions"
        headers_in = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {chat_api_key}",
        }
        url, headers = resolve_request_endpoint(target_url, account_email=account_email, extra_headers=headers_in)
        client = self._get_client(account_email=account_email)
        resp = None
        try:
            req = client.build_request("POST", url, json=payload, headers=headers)
            resp = await client.send(req, stream=True)
            if resp.status_code == 200:
                return resp.status_code, _CompletionStream(client, resp)
            error_bytes = await resp.aread()
        except BaseException:
            try:
                await _close_upstream(client, resp)
            except Exception:
                pass
            raise
        await _close_upstream(client, resp)

        async def error_gen() -> AsyncGenerator[bytes, None]:
            yield error_bytes

        return resp.status_code, error_gen()
