import os
import secrets
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Database
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", DATA_DIR / "liber2api.db"))

# Server Configuration
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Security & Authentication
# If set, WebUI management requires entering this password to view and manage pool
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))

# If set, downstream clients accessing /v1/chat/completions require Bearer <API_KEY>
API_KEY = os.getenv("API_KEY", "")

# Resin Sticky Proxy Gateway Integration
# e.g. "http://127.0.0.1:2260/my-token"
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
