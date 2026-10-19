import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.config import (
    ADMIN_PASSWORD,
    API_KEY,
    BASE_DIR,
    HOST,
    MAX_FAILOVER_RETRIES,
    PORT,
)
from app.database import (
    add_or_update_account,
    delete_account,
    get_all_accounts,
    get_pool_summary,
    init_db,
)
from app.libertai_client import LibertAIClient, LibertAIException
from app.pool_manager import pool_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("liber2api.server")

# Templates
templates_dir = BASE_DIR / "app" / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


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
    version="1.0.0",
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


# Security Dependency for API endpoints
def verify_api_key(authorization: Optional[str] = Header(None)) -> bool:
    if not API_KEY:
        return True
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return True


# Security Dependency for WebUI Admin operations (optional)
def verify_admin(x_admin_password: Optional[str] = Header(None)) -> bool:
    if not ADMIN_PASSWORD:
        return True
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized admin access",
        )
    return True


# ---------------------------------------------------------
# WebUI & Frontend Routes
# ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "api_key": API_KEY,
            "has_admin_pass": bool(ADMIN_PASSWORD),
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
        data = await client.list_models()
        return data
    except Exception as e:
        logger.warning("Failed to proxy models from upstream: %s", e)
        # Fallback list of known models
        return {
            "object": "list",
            "data": [
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
            ],
        }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _auth: bool = Depends(verify_api_key)):
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    stream = bool(body.get("stream", False))
    client = LibertAIClient()

    # Retry loop across account pool
    attempts = 0
    last_error_status = 503
    last_error_body = {"detail": "No active accounts available in pool."}

    while attempts < MAX_FAILOVER_RETRIES:
        attempts += 1
        account = await pool_manager.get_next_account()
        if not account:
            logger.warning("No active accounts available in pool (attempt %d/%d)", attempts, MAX_FAILOVER_RETRIES)
            break

        chat_key = account["chat_api_key"]
        logger.info("Routing request to account %s (%s)", account["email"], "streaming" if stream else "sync")

        if stream:
            status_code, stream_gen = await client.forward_completion_stream(chat_key, body)
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
            status_code, resp_body = await client.forward_completion_json(chat_key, body)
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
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
