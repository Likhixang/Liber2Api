import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from string import Formatter
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from contextlib import asynccontextmanager
from typing import Any, Dict, Literal, Optional

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
import httpx
import anyio

import app.config as config
from app.database import (
    add_or_update_account,
    create_admin_session,
    get_admin_session,
    delete_admin_session,
    get_active_accounts,
    create_api_token,
    delete_account,
    delete_api_token,
    delete_mail_provider,
    reset_builtin_mail_providers,
    get_all_accounts,
    get_all_api_tokens,
    get_all_mail_providers,
    get_effective_proxy_settings,
    get_pool_summary,
    get_setting,
    init_db,
    set_setting,
    create_mail_provider,
    update_account_usage,
    update_api_token_status,
    update_mail_provider_status,
    validate_and_record_token,
)
from app.libertai_client import LibertAIClient, LibertAIException
from app.model_resolver import canonicalize_models_list, resolve_upstream_model
from app.pool_manager import pool_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("liber2api.server")

# Templates
templates_dir = config.BASE_DIR / "app" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


def _password_fingerprint() -> str:
    return hmac.new(config.SESSION_SECRET.encode(), config.ADMIN_PASSWORD.encode(), hashlib.sha256).hexdigest()


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# A bounded, process-local throttle; a reverse proxy can add shared rate limiting.
_login_failures: OrderedDict[str, list[float]] = OrderedDict()


def _check_login_throttle(host: str) -> None:
    now = time.monotonic()
    recent = [t for t in _login_failures.get(host, []) if now - t < 60]
    if len(recent) >= 5:
        raise HTTPException(status_code=429, detail="Too many login attempts; retry later", headers={"Retry-After": "60"})
    _login_failures[host] = recent
    _login_failures.move_to_end(host)
    while len(_login_failures) > 1024:
        _login_failures.popitem(last=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Liber2Api database and background workers...")
    await init_db()
    p_sets = await get_effective_proxy_settings()
    config.PROXY_URL = p_sets["proxy_url"]
    config.RESIN_URL = p_sets["resin_url"]
    config.RESIN_PLATFORM_NAME = p_sets["resin_platform_name"]
    await pool_manager.start_background_worker()
    yield
    # Shutdown
    logger.info("Shutting down Liber2Api...")
    await pool_manager.stop_background_worker()


app = FastAPI(
    title="Liber2Api",
    version="2.2.2",
    description="High-availability OpenAI-compatible 2API proxy with account pool management for LibertAI.",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "app" / "static")), name="static")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def protect_admin_origin(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin and origin != expected and origin not in config.ADMIN_ALLOWED_ORIGINS:
            return JSONResponse({"detail": "Untrusted admin origin"}, status_code=403)
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    return response


@app.get("/ready")
async def readiness_check():
    ready = bool(await get_active_accounts())
    return JSONResponse({"status": "ready" if ready else "not_ready"}, status_code=200 if ready else 503)


@app.get("/health")
async def health_check():
    """Unauthenticated health probe endpoint for Docker and load balancers."""
    return {"status": "ok", "service": "Liber2Api"}


# Security Dependency for 2API endpoints (Supports Multi-Token Pool)
async def verify_api_key(authorization: Optional[str] = Header(None)) -> bool:
    db_enabled = await get_setting("api_key_enabled")
    enabled = True
    if db_enabled is not None:
        enabled = db_enabled.lower() == "true"
    elif not config.API_KEY:
        enabled = False

    if not enabled:
        return True

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format",
        )
    token_val = parts[1].strip()

    # 1. Check multi-token pool in database
    if await validate_and_record_token(token_val):
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )


# Helper to check if current request has valid admin credentials
async def is_admin_authenticated(
    x_admin_password: Optional[str] = None,
    liber2api_session: Optional[str] = None,
    client_host: Optional[str] = None,
) -> bool:
    if not config.ADMIN_PASSWORD:
        return True
    if x_admin_password:
        host = client_host or "header-client"
        _check_login_throttle(host)
        if hmac.compare_digest(x_admin_password.encode(), config.ADMIN_PASSWORD.encode()):
            _login_failures.pop(host, None)
            return True
        _login_failures[host].append(time.monotonic())
        return False
    if liber2api_session:
        session = await get_admin_session(_session_hash(liber2api_session))
        if session and hmac.compare_digest(session["password_fingerprint"], _password_fingerprint()):
            try:
                return datetime.fromisoformat(session["expires_at"]) > datetime.now(timezone.utc)
            except (ValueError, TypeError):
                return False
    return False


# Security Dependency for WebUI Admin operations
async def verify_admin(
    request: Request,
    x_admin_password: Optional[str] = Header(None),
    liber2api_session: Optional[str] = Cookie(None),
) -> bool:
    host = request.client.host if request.client else "unknown"
    if not await is_admin_authenticated(x_admin_password, liber2api_session, host):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized admin access. Please login with admin password.",
        )
    return True


# ---------------------------------------------------------
# WebUI & Frontend Routes
# ---------------------------------------------------------

class AdminLoginRequest(BaseModel):
    password: str


@app.post("/api/admin/login")
async def admin_login(req: AdminLoginRequest, request: Request, response: Response):
    if not config.ADMIN_PASSWORD:
        return {"success": True, "message": "No admin password configured"}
    host = request.client.host if request.client else "unknown"
    _check_login_throttle(host)
    if not hmac.compare_digest(req.password.encode(), config.ADMIN_PASSWORD.encode()):
        _login_failures[host].append(time.monotonic())
        raise HTTPException(status_code=401, detail="密码错误，请重新输入")
    _login_failures.pop(host, None)
    session_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=config.ADMIN_SESSION_TTL)
    await create_admin_session(_session_hash(session_token), expires_at.isoformat(), _password_fingerprint())
    response.set_cookie(
        key="liber2api_session",
        value=session_token,
        path="/",
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=config.ADMIN_SESSION_TTL,
    )
    return {"success": True, "message": "登录成功"}


@app.post("/api/admin/logout")
async def admin_logout(response: Response, liber2api_session: Optional[str] = Cookie(None)):
    if liber2api_session:
        await delete_admin_session(_session_hash(liber2api_session))
    response.delete_cookie("liber2api_session", path="/")
    return {"success": True, "message": "已退出登录"}


@app.get("/api/admin/status")
async def admin_status(
    request: Request,
    x_admin_password: Optional[str] = Header(None),
    liber2api_session: Optional[str] = Cookie(None),
):
    host = request.client.host if request.client else "unknown"
    authenticated = await is_admin_authenticated(x_admin_password, liber2api_session, host)
    return {
        "has_admin_pass": bool(config.ADMIN_PASSWORD),
        "is_authenticated": authenticated,
    }


class CreateTokenRequest(BaseModel):
    name: str = Field(..., min_length=1)
    token: Optional[str] = None


class UpdateTokenStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(active|disabled)$")


class AuthPolicyRequest(BaseModel):
    enabled: bool


@app.get("/api/tokens")
async def api_list_tokens(_admin: bool = Depends(verify_admin)):
    tokens = await get_all_api_tokens()
    db_enabled = await get_setting("api_key_enabled")
    auth_enabled = (db_enabled.lower() == "true") if db_enabled is not None else bool(config.API_KEY)
    return {"tokens": tokens, "auth_enabled": auth_enabled}


@app.post("/api/tokens")
async def api_create_token(req: CreateTokenRequest, _admin: bool = Depends(verify_admin)):
    token_val = req.token.strip() if req.token and req.token.strip() else f"sk-liber2api-{secrets.token_hex(16)}"
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="Token name must not be blank")
    try:
        new_tok = await create_api_token(name=req.name.strip(), token=token_val)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Token already exists")
    return {"success": True, "message": "令牌创建成功", "token": new_tok}


@app.patch("/api/tokens/{token_id}/status")
async def api_update_token_status(token_id: int, req: UpdateTokenStatusRequest, _admin: bool = Depends(verify_admin)):
    success = await update_api_token_status(token_id, req.status)
    if not success:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"success": True, "message": "令牌状态已更新"}


@app.delete("/api/tokens/{token_id}")
async def api_delete_token(token_id: int, _admin: bool = Depends(verify_admin)):
    success = await delete_api_token(token_id)
    if not success:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"success": True, "message": "令牌已删除"}


# ---------------------------------------------------------
# Mail Provider Endpoints (Built-in & Custom Mail APIs)
# ---------------------------------------------------------

class CreateMailProviderRequest(BaseModel):
    name: str = Field(..., min_length=1)
    provider_type: Literal["custom_http", "neighbours", "tempmail_plus", "mtempmail", "tempmail_ing", "inbound_webhook", "tempmail_lol", "duckmail"] = "custom_http"
    domain: Optional[str] = None
    api_url: str = Field(default="")
    create_url: Optional[str] = None
    api_key: Optional[str] = None
    priority: int = Field(default=10, ge=0, le=100)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Provider name must not be blank")
        return value.strip()

    @field_validator("api_url", "create_url")
    @classmethod
    def validate_url(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return value
        value = value.strip()
        for _, field, spec, conversion in Formatter().parse(value):
            if field is not None and (field != "email" or spec or conversion):
                raise ValueError("Only the {email} URL placeholder is supported")
        parsed = urlsplit(value.replace("{email}", "test@example.invalid"))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Mail provider URL must be an absolute HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def validate_provider(self):
        if self.provider_type in ("custom_http", "inbound_webhook") and not self.api_url:
            raise ValueError("This mail provider requires an explicit polling URL")
        if self.provider_type == "inbound_webhook" and not self.domain:
            raise ValueError("Inbound webhook requires an explicit domain")
        if self.provider_type == "custom_http" and not self.domain and not self.create_url:
            raise ValueError("Custom HTTP requires a domain or mailbox creation URL")
        from app.auto_register import validate_mail_provider
        validate_mail_provider(self.model_dump())
        return self


class UpdateMailProviderStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(active|disabled)$")


@app.get("/api/mail-providers")
async def api_list_mail_providers(_admin: bool = Depends(verify_admin)):
    providers = await get_all_mail_providers()
    return {"providers": providers}


@app.post("/api/mail-providers")
async def api_create_mail_provider(req: CreateMailProviderRequest, _admin: bool = Depends(verify_admin)):
    provider = await create_mail_provider(
        name=req.name,
        provider_type=req.provider_type,
        domain=req.domain,
        api_url=req.api_url,
        create_url=req.create_url,
        api_key=req.api_key,
        priority=req.priority,
    )
    return {"success": True, "message": "邮箱 API 添加成功", "provider": provider}


@app.patch("/api/mail-providers/{provider_id}/status")
async def api_update_mail_provider_status(provider_id: int, req: UpdateMailProviderStatusRequest, _admin: bool = Depends(verify_admin)):
    success = await update_mail_provider_status(provider_id, req.status)
    if not success:
        raise HTTPException(status_code=404, detail="Provider not found")
    return {"success": True, "message": "邮箱 API 状态已更新"}


@app.delete("/api/mail-providers/{provider_id}")
async def api_delete_mail_provider(provider_id: int, _admin: bool = Depends(verify_admin)):
    success = await delete_mail_provider(provider_id)
    if not success:
        raise HTTPException(status_code=404, detail="Provider not found")
    return {"success": True, "message": "邮箱 API 已删除"}


@app.post("/api/mail-providers/reset-builtins")
async def api_reset_builtins(_admin: bool = Depends(verify_admin)):
    await reset_builtin_mail_providers()
    return {"success": True, "message": "已恢复全部官方内置接码源", "providers": await get_all_mail_providers()}


def _mail_poll_url(url: str, email: str) -> str:
    if "{email}" in url:
        return url.format(email=email)
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("email", email))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


async def _test_mail_provider(req: CreateMailProviderRequest) -> dict:
    from app.libertai_client import resolve_forward_proxy
    from app.auto_register import mail_auth_headers
    headers = mail_auth_headers(req.model_dump())
    base = req.api_url.rstrip("/")
    p_type = req.provider_type
    if p_type in ("custom_http", "inbound_webhook"):
        url = req.create_url or _mail_poll_url(req.api_url, "test@example.invalid")
    elif p_type == "neighbours":
        url = f"{base or 'https://neighbours.sh/api/v1'}/config/domains"
    elif p_type == "duckmail":
        url = f"{base or 'https://api.duckmail.sbs'}/domains"
    elif p_type == "mtempmail":
        key = req.api_key or "pub_nRn1hUwpdmZvxQNVWDfoXgKyF7dIm9nRIIIt1qDw"
        url = f"{base or 'https://mtempmail.com/api'}/domains/{key}/all"
    elif p_type == "tempmail_lol":
        return {"success": False, "message": "该供应商没有配置无副作用的连通性探针；未验证可用性"}
    elif p_type == "tempmail_plus":
        url = f"{base or 'https://tempmail.plus/api'}/mails?email=test@example.invalid&limit=1"
    elif p_type == "tempmail_ing":
        url = base or "https://tempmail.ing/"
    else:
        raise HTTPException(status_code=422, detail="Unsupported mail provider")
    try:
        async with httpx.AsyncClient(timeout=10.0, proxy=resolve_forward_proxy()) as client:
            response = await client.get(url, headers=headers)
        ok = 200 <= response.status_code < 300
        return {"success": ok, "message": f"连通性探针 HTTP {response.status_code}（不代表验证码投递或注册成功）"}
    except httpx.HTTPError:
        return {"success": False, "message": "连通性探针失败，请检查服务端日志与网络配置"}


@app.post("/api/mail-providers/test")
async def api_test_mail_provider(req: CreateMailProviderRequest, _admin: bool = Depends(verify_admin)):
    return await _test_mail_provider(req)


@app.post("/api/mail-providers/{provider_id}/test")
async def api_test_saved_mail_provider(provider_id: int, _admin: bool = Depends(verify_admin)):
    provider = next((p for p in await get_all_mail_providers() if p["id"] == provider_id), None)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        req = CreateMailProviderRequest(**provider)
    except ValueError:
        raise HTTPException(status_code=422, detail="Saved provider configuration is invalid")
    return await _test_mail_provider(req)

class ProxySettingsRequest(BaseModel):
    proxy_url: Optional[str] = ""
    resin_url: Optional[str] = ""
    resin_platform_name: Optional[str] = "Default"


@app.get("/api/settings/proxy")
async def api_get_proxy_settings(_admin: bool = Depends(verify_admin)):
    settings = await get_effective_proxy_settings()
    return settings


@app.post("/api/settings/proxy")
async def api_set_proxy_settings(req: ProxySettingsRequest, _admin: bool = Depends(verify_admin)):
    p_url = req.proxy_url.strip() if req.proxy_url else ""
    r_url = req.resin_url.strip() if req.resin_url else ""
    r_plat = req.resin_platform_name.strip() if req.resin_platform_name else "Default"

    await set_setting("proxy_url", p_url)
    await set_setting("resin_url", r_url)
    await set_setting("resin_platform_name", r_plat)

    # Hot update config module values
    config.PROXY_URL = p_url
    config.RESIN_URL = r_url.rstrip("/")
    config.RESIN_PLATFORM_NAME = r_plat

    return {"success": True, "message": "代理与 Resin 配置已更新并即时生效", "settings": await get_effective_proxy_settings()}


@app.get("/api/settings/auth-policy")
async def api_get_auth_policy(_admin: bool = Depends(verify_admin)):
    db_enabled = await get_setting("api_key_enabled")
    enabled = (db_enabled.lower() == "true") if db_enabled is not None else bool(config.API_KEY)
    return {"auth_enabled": enabled}


@app.post("/api/settings/auth-policy")
async def api_set_auth_policy(req: AuthPolicyRequest, _admin: bool = Depends(verify_admin)):
    await set_setting("api_key_enabled", "true" if req.enabled else "false")
    return {"success": True, "auth_enabled": req.enabled, "message": "全局鉴权策略已更新"}


@app.get("/", response_class=HTMLResponse)
async def serve_index(
    request: Request,
    liber2api_session: Optional[str] = Cookie(None),
):
    authenticated = await is_admin_authenticated(liber2api_session=liber2api_session)
    db_enabled = await get_setting("api_key_enabled")
    auth_enabled = (db_enabled.lower() == "true") if db_enabled is not None else bool(config.API_KEY)
    tokens = await get_all_api_tokens()
    active_tokens = [t["token"] for t in tokens if t["status"] == "active"]
    primary_token = active_tokens[0] if active_tokens and authenticated else ""

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "api_key": primary_token if auth_enabled else "",
            "api_key_enabled": auth_enabled,
            "has_admin_pass": bool(config.ADMIN_PASSWORD),
            "is_authenticated": authenticated,
        },
    )


class SendCodeRequest(BaseModel):
    email: str = Field(..., min_length=3)


class VerifyCodeRequest(BaseModel):
    email: str = Field(..., min_length=3)
    code: str = Field(..., min_length=6, max_length=6)


@app.post("/api/auth/send-code")
async def api_send_code(req: SendCodeRequest, _admin: bool = Depends(verify_admin)):
    client = LibertAIClient()
    try:
        await client.send_login_email(req.email)
        return {"success": True, "message": f"Verification code sent to {req.email}"}
    except LibertAIException as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error sending login code")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/auth/verify-code")
async def api_verify_code(req: VerifyCodeRequest, _admin: bool = Depends(verify_admin)):
    client = LibertAIClient()
    try:
        tokens = await client.verify_code(req.email, req.code)
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        chat_api_key = await client.fetch_chat_api_key(access_token, account_email=req.email)

        account = await add_or_update_account(
            email=req.email,
            access_token=access_token,
            refresh_token=refresh_token,
            chat_api_key=chat_api_key,
        )
        return {
            "success": True,
            "message": f"Account {req.email} successfully bound and added to pool",
            "account": {
                "id": account.get("id"),
                "email": account.get("email"),
                "status": account.get("status"),
            },
        }
    except LibertAIException as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error verifying code")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/accounts")
async def api_list_accounts(_admin: bool = Depends(verify_admin)):
    accounts = await get_all_accounts()
    summary = await get_pool_summary()

    # Redact sensitive tokens in listing
    clean_accounts = []
    for acc in accounts:
        item = dict(acc)
        item.pop("access_token", None)
        item.pop("refresh_token", None)
        item.pop("chat_api_key", None)
        clean_accounts.append(item)

    return {"accounts": clean_accounts, "summary": summary}


@app.post("/api/accounts/auto-register")
async def api_auto_register_account(_admin: bool = Depends(verify_admin)):
    from app.auto_register import registrar
    try:
        account = await registrar.register_account()
        clean_acc = dict(account)
        clean_acc.pop("access_token", None)
        clean_acc.pop("refresh_token", None)
        clean_acc.pop("chat_api_key", None)
        return {
            "success": True,
            "message": f"新账号 {account.get('email')} 自动注册绑定成功并加入号池！",
            "account": clean_acc,
        }
    except Exception as e:
        logger.exception("Auto registration failed: %s", e)
        raise HTTPException(status_code=500, detail=f"自动注册失败: {str(e)}")


@app.get("/api/accounts/{account_id}/usage")
async def api_get_account_usage(account_id: int, _admin: bool = Depends(verify_admin)):
    usage = await pool_manager.fetch_account_usage(account_id)
    return {"success": True, "usage": usage}


@app.post("/api/accounts/{account_id}/refresh")
async def api_refresh_account(account_id: int, _admin: bool = Depends(verify_admin)):
    success = await pool_manager.refresh_account(account_id)
    if not success:
        raise HTTPException(status_code=400, detail="Token refresh failed")
    return {"success": True, "message": "Tokens and chatApiKey successfully refreshed"}


@app.post("/api/accounts/{account_id}/test")
async def api_test_account(account_id: int, _admin: bool = Depends(verify_admin)):
    success, message = await pool_manager.test_account(account_id)
    return {"success": success, "message": message}


@app.post("/api/accounts/{account_id}/reset")
async def api_reset_account(account_id: int, _admin: bool = Depends(verify_admin)):
    success = await pool_manager.reset_cooldown(account_id)
    if not success:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"success": True, "message": "Account cooldown status reset to active"}


@app.delete("/api/accounts/{account_id}")
async def api_delete_account(account_id: int, _admin: bool = Depends(verify_admin)):
    deleted = await delete_account(account_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"success": True, "message": "Account removed from pool"}


# ---------------------------------------------------------
# OpenAI-Compatible 2API Endpoints
# ---------------------------------------------------------

@app.get("/v1/models")
async def get_models(_auth: bool = Depends(verify_api_key)):
    client = LibertAIClient()
    try:
        raw_data = await client.list_models()
        models = raw_data.get("data", [])
    except Exception as e:
        logger.warning("Failed to proxy models from upstream: %s", e)
        # Fallback list of known pure-blood models
        models = [
            {"id": "qwen3.6-35b-a3b", "object": "model", "owned_by": "Qwen"},
            {"id": "qwen3.6-35b-a3b-thinking", "object": "model", "owned_by": "Qwen"},
            {"id": "qwen3.8-27b", "object": "model", "owned_by": "Qwen"},
            {"id": "qwen3.8-27b-thinking", "object": "model", "owned_by": "Qwen"},
            {"id": "deepseek-v4.1-flash-1017", "object": "model", "owned_by": "deepseek-ai"},
            {"id": "deepseek-v4.1-flash-1017-thinking", "object": "model", "owned_by": "deepseek-ai"},
            {"id": "qwen3.5-122b-a10b", "object": "model", "owned_by": "Qwen"},
            {"id": "hermes-3-8b-tee", "object": "model", "owned_by": "NousResearch"},
        ]
    canonical_list = canonicalize_models_list(models)
    return {"object": "list", "data": canonical_list}


async def _close_stream(stream) -> None:
    close = getattr(stream, "aclose", None)
    if close:
        await close()


async def _accounted_stream(stream, account):
    completed = False
    try:
        async for chunk in stream:
            yield chunk
        completed = True
    except httpx.RequestError:
        logger.warning("Upstream stream failed after response started for account id=%s", account["id"])
        error = {"error": {"message": "Upstream stream interrupted", "type": "upstream_error"}}
        yield f"data: {json.dumps(error)}\n\ndata: [DONE]\n\n".encode()
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await _close_stream(stream)
            finally:
                await pool_manager.handle_response(account, 200 if completed else 502)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _auth: bool = Depends(verify_api_key)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")
    requested_model = body.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise HTTPException(status_code=400, detail="model must be a non-empty string")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) or not isinstance(m.get("role"), str) or not m["role"] for m in messages):
        raise HTTPException(status_code=400, detail="messages must be a non-empty list of message objects with roles")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise HTTPException(status_code=400, detail="stream must be a boolean")
    if "tee" in body and not isinstance(body["tee"], bool):
        raise HTTPException(status_code=400, detail="tee must be a boolean")
    try:
        body["model"] = resolve_upstream_model(requested_model, payload=body, headers=dict(request.headers))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    stream = body.get("stream", False)
    client = LibertAIClient()
    attempted_ids: set[int] = set()
    last_status = 503
    last_body = {"detail": "No active accounts available in pool."}
    retryable = {401, 403, 408, 429, 500, 502, 503, 504}
    for _ in range(max(1, config.MAX_FAILOVER_RETRIES)):
        account = await pool_manager.get_next_account(exclude_ids=set(attempted_ids))
        if not account or account["id"] in attempted_ids:
            break
        attempted_ids.add(account["id"])
        stream_gen = None
        try:
            if stream:
                status_code, stream_gen = await client.forward_completion_stream(account["chat_api_key"], body, account_email=account["email"])
                if status_code == 200:
                    # ClosingIterator is also closed by its response owner, including cancellation
                    # before the first upstream byte. Never retry once downstream starts.
                    from starlette.background import BackgroundTask
                    return StreamingResponse(
                        _accounted_stream(stream_gen, account),
                        media_type="text/event-stream",
                        background=BackgroundTask(_close_stream, stream_gen),
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )
                try:
                    error_bytes = b"".join([chunk async for chunk in stream_gen])
                finally:
                    await _close_stream(stream_gen)
                text = error_bytes.decode("utf-8", errors="replace")
                try:
                    response_body = json.loads(text)
                except ValueError:
                    response_body = {"detail": text}
            else:
                status_code, response_body = await client.forward_completion_json(account["chat_api_key"], body, account_email=account["email"])
        except httpx.RequestError as exc:
            if stream_gen is not None:
                await _close_stream(stream_gen)
            last_status = 504 if isinstance(exc, httpx.TimeoutException) else 502
            last_body = {"detail": "Upstream request timed out" if last_status == 504 else "Upstream connection failed"}
            await pool_manager.handle_response(account, last_status)
            continue
        if status_code == 200:
            await pool_manager.handle_response(account, 200)
            return JSONResponse(content=response_body, status_code=200)
        await pool_manager.handle_response(account, status_code, json.dumps(response_body))
        last_status, last_body = status_code, response_body
        if status_code not in retryable:
            break
    return JSONResponse(content=last_body, status_code=last_status)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=True)
