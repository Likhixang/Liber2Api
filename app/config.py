import os
import secrets
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

# Database
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", DATA_DIR / "liber2api.db"))

# Server Configuration
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

def _load_or_create_session_secret() -> str:
    env_secret = os.getenv("SESSION_SECRET")
    if env_secret:
        return env_secret
    secret_file = DATA_DIR / "session_secret"
    if secret_file.exists():
        val = secret_file.read_text(encoding="utf-8").strip()
        if not val:
            raise RuntimeError("Existing session_secret is empty; supply SESSION_SECRET or repair the file")
        return val
    new_secret = secrets.token_hex(32)
    try:
        fd = os.open(secret_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Another process won creation; use its secret rather than diverging.
        val = secret_file.read_text(encoding="utf-8").strip()
        if not val:
            raise RuntimeError("session_secret initialization is incomplete")
        return val
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(new_secret)
    return new_secret


# Security & Authentication
# If set, WebUI management requires entering this password to view and manage pool
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = _load_or_create_session_secret()
ADMIN_SESSION_TTL = max(60, int(os.getenv("ADMIN_SESSION_TTL", "2592000")))
ADMIN_ALLOWED_ORIGINS = tuple(origin.strip().rstrip("/") for origin in os.getenv("ADMIN_ALLOWED_ORIGINS", "").split(",") if origin.strip())

# If set, downstream clients accessing /v1/chat/completions require Bearer <API_KEY>
API_KEY = os.getenv("API_KEY", "")

# Forward Proxy (Duck2api style: e.g. "http://Default:TOKEN@resin:2260" or "http://127.0.0.1:7890")
PROXY_URL = os.getenv("PROXY_URL", os.getenv("HTTP_PROXY", os.getenv("http_proxy", ""))).strip()

# Resin Sticky Proxy Gateway Integration (Reverse proxy style: e.g. "http://127.0.0.1:2260/my-token")
RESIN_URL = os.getenv("RESIN_URL", "").rstrip("/")
RESIN_PLATFORM_NAME = os.getenv("RESIN_PLATFORM_NAME", "Default").strip()

# LibertAI Endpoints
LIBERTAI_INFERENCE_URL = os.getenv("LIBERTAI_INFERENCE_URL", "https://inference.api.libertai.io").rstrip("/")
LIBERTAI_API_URL = os.getenv("LIBERTAI_API_URL", "https://api.libertai.io/v1").rstrip("/")

# Pool & Cooldown Settings
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "5"))
AUTO_REFRESH_INTERVAL = int(os.getenv("AUTO_REFRESH_INTERVAL", "43200"))  # 12 hours in seconds
MAX_FAILOVER_RETRIES = int(os.getenv("MAX_FAILOVER_RETRIES", "3"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "120.0"))
