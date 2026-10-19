import os
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
# If set, WebUI management API requires x-admin-password header or session cookie
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# If set, downstream clients accessing /v1/chat/completions require Bearer <API_KEY>
API_KEY = os.getenv("API_KEY", "")

# LibertAI Endpoints
LIBERTAI_INFERENCE_URL = os.getenv("LIBERTAI_INFERENCE_URL", "https://inference.api.libertai.io").rstrip("/")
LIBERTAI_API_URL = os.getenv("LIBERTAI_API_URL", "https://api.libertai.io/v1").rstrip("/")

# Pool & Cooldown Settings
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "5"))
AUTO_REFRESH_INTERVAL = int(os.getenv("AUTO_REFRESH_INTERVAL", "43200"))  # 12 hours in seconds
MAX_FAILOVER_RETRIES = int(os.getenv("MAX_FAILOVER_RETRIES", "3"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "120.0"))
