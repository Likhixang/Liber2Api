import hashlib
import hmac
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import app.config as config
from app.database import (
    add_or_update_account,
    create_api_token,
    delete_account,
    delete_api_token,
    get_all_accounts,
    get_all_api_tokens,
    get_pool_summary,
    get_setting,
    init_db,
    set_setting,
    update_api_token_status,
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


def compute_session_token(password: str) -> str:
    """Computes a deterministic HMAC-SHA256 session token for admin authentication."""
    return hmac.new(config.SESSION_SECRET.encode(), password.encode(), hashlib.sha256).hexdigest()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Liber2Api database and background workers...")
    await init_db()
    await pool_manager.start_background_worker()
    yield
    # Shutdown
    logger.info("Shutting down Liber2Api...")
    await pool_manager.stop_background_worker()


app = FastAPI(
    title="Liber2Api",
    version="1.3.0",
    description="High-availability OpenAI-compatible 2API proxy with account pool management for LibertAI.",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

    # 2. Check fallback environment key
    if config.API_KEY and hmac.compare_digest(token_val, config.API_KEY):
        return True

    # 3. Check legacy single key in settings table
    db_key = await get_setting("api_key")
    if db_key and hmac.compare_digest(token_val, db_key):
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )


# Helper to check if current request has valid admin credentials
def is_admin_authenticated(
    x_admin_password: Optional[str] = None,
    liber2api_session: Optional[str] = None,
) -> bool:
    if not config.ADMIN_PASSWORD:
        return True
    if x_admin_password and x_admin_password == config.ADMIN_PASSWORD:
        return True
    if liber2api_session:
        expected = compute_session_token(config.ADMIN_PASSWORD)
        if hmac.compare_digest(liber2api_session, expected):
            return True
    return False


# Security Dependency for WebUI Admin operations
def verify_admin(
    x_admin_password: Optional[str] = Header(None),
    liber2api_session: Optional[str] = Cookie(None),
) -> bool:
    if not is_admin_authenticated(x_admin_password, liber2api_session):
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
async def admin_login(req: AdminLoginRequest, response: Response):
    if not config.ADMIN_PASSWORD:
        return {"success": True, "message": "No admin password configured"}

    if req.password != config.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误，请重新输入")

    session_token = compute_session_token(config.ADMIN_PASSWORD)
    response.set_cookie(
        key="liber2api_session",
        value=session_token,
        path="/",
        httponly=True,
        samesite="lax",
        max_age=86400 * 30,  # 30 days
    )
    return {"success": True, "message": "登录成功", "token": session_token}


@app.post("/api/admin/logout")
async def admin_logout(response: Response):
    response.delete_cookie("liber2api_session", path="/")
    return {"success": True, "message": "已退出登录"}


@app.get("/api/admin/status")
async def admin_status(
    x_admin_password: Optional[str] = Header(None),
    liber2api_session: Optional[str] = Cookie(None),
):
    authenticated = is_admin_authenticated(x_admin_password, liber2api_session)
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
    new_tok = await create_api_token(name=req.name.strip(), token=token_val)
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
    authenticated = is_admin_authenticated(liber2api_session=liber2api_session)
    db_enabled = await get_setting("api_key_enabled")
    auth_enabled = (db_enabled.lower() == "true") if db_enabled is not None else bool(config.API_KEY)
    tokens = await get_all_api_tokens()
    active_tokens = [t["token"] for t in tokens if t["status"] == "active"]
    primary_token = active_tokens[0] if active_tokens else (config.API_KEY or "")

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

        chat_api_key = await client.fetch_chat_api_key(access_token)

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
        del item["access_token"]
        del item["refresh_token"]
        del item["chat_api_key"]
        clean_accounts.append(item)

    return {"accounts": clean_accounts, "summary": summary}


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
        # Fallback list of known models
        models = [
            {"id": "qwen3.6-35b-a3b", "object": "model", "owned_by": "libertai"},
            {"id": "qwen3.6-35b-a3b-thinking", "object": "model", "owned_by": "libertai"},
            {"id": "qwen3.8-27b", "object": "model", "owned_by": "libertai"},
            {"id": "qwen3.8-27b-thinking", "object": "model", "owned_by": "libertai"},
            {"id": "glm-5.3", "object": "model", "owned_by": "libertai"},
            {"id": "glm-5.3-thinking", "object": "model", "owned_by": "libertai"},
            {"id": "glm-5.3-flash", "object": "model", "owned_by": "libertai"},
            {"id": "glm-5.3-flash-thinking", "object": "model", "owned_by": "libertai"},
            {"id": "deepseek-v4.1-flash", "object": "model", "owned_by": "libertai"},
            {"id": "deepseek-v4.1-flash-thinking", "object": "model", "owned_by": "libertai"},
            {"id": "deepseek-v4.1-flash-1017-thinking", "object": "model", "owned_by": "libertai"},
            {"id": "qwen3.5-122b-a10b", "object": "model", "owned_by": "libertai"},
            {"id": "hermes-3-8b-tee", "object": "model", "owned_by": "libertai"},
        ]
    canonical_list = canonicalize_models_list(models)
    return {"object": "list", "data": canonical_list}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _auth: bool = Depends(verify_api_key)):
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Intelligently resolve model with reasoning_effort and tee options
    requested_model = body.get("model", "")
    headers_dict = {k.lower(): v for k, v in request.headers.items()}
    upstream_model = resolve_upstream_model(requested_model, payload=body, headers=headers_dict)
    body["model"] = upstream_model
    logger.info("Routing completion: client model '%s' -> upstream model '%s'", requested_model, upstream_model)

    stream = bool(body.get("stream", False))
    client = LibertAIClient()

    # Retry loop across account pool
    attempts = 0
    last_error_status = 503
    last_error_body = {"detail": "No active accounts available in pool."}

    while attempts < config.MAX_FAILOVER_RETRIES:
        attempts += 1
        account = await pool_manager.get_next_account()
        if not account:
            logger.warning("No active accounts available in pool (attempt %d/%d)", attempts, config.MAX_FAILOVER_RETRIES)
            break

        chat_key = account["chat_api_key"]
        email = account["email"]
        logger.info("Routing request to account %s (%s)", email, "streaming" if stream else "sync")

        if stream:
            status_code, stream_gen = await client.forward_completion_stream(
                chat_key, body, account_email=email
            )
            if status_code == 200:
                await pool_manager.handle_response(account, 200)
                return StreamingResponse(
                    stream_gen,
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            else:
                # Read error body from stream_gen
                chunks = []
                async for ch in stream_gen:
                    chunks.append(ch)
                err_text = b"".join(chunks).decode("utf-8", errors="ignore")
                await pool_manager.handle_response(account, status_code, err_text)
                last_error_status = status_code
                try:
                    last_error_body = json.loads(err_text)
                except Exception:
                    last_error_body = {"detail": err_text}
                # Continue loop to next account
                continue

        else:
            status_code, resp_body = await client.forward_completion_json(
                chat_key, body, account_email=email
            )
            if status_code == 200:
                await pool_manager.handle_response(account, 200)
                return JSONResponse(content=resp_body, status_code=200)
            else:
                await pool_manager.handle_response(account, status_code, str(resp_body))
                last_error_status = status_code
                last_error_body = resp_body
                # Continue loop to next account
                continue

    # All accounts failed or none available
    return JSONResponse(content=last_error_body, status_code=last_error_status)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=True)
