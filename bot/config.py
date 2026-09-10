import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = BASE_DIR / "downloads"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

_raw_ids = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS: set[int] = {
    int(part.strip())
    for part in _raw_ids.split(",")
    if part.strip().isdigit()
}

# Empty = official api.telegram.org (~50 MB). Local Bot API, e.g. http://127.0.0.1:8081
TELEGRAM_API_URL = os.getenv("TELEGRAM_API_URL", "").strip().rstrip("/")
USING_LOCAL_API = bool(TELEGRAM_API_URL)
# When bot and Bot API share the same filesystem (Docker), send file:// paths.
LOCAL_API_FILE_MODE = os.getenv("LOCAL_API_FILE_MODE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

CLOUD_MAX_FILE_MB = 49
LOCAL_MAX_FILE_MB = 1990

_raw_max = os.getenv("MAX_FILE_MB", "").strip()
_requested_max = int(_raw_max) if _raw_max else None
IGNORED_CLOUD_MAX = (
    not USING_LOCAL_API
    and _requested_max is not None
    and _requested_max > CLOUD_MAX_FILE_MB
)

if USING_LOCAL_API:
    MAX_FILE_MB = min(_requested_max or 1900, LOCAL_MAX_FILE_MB)
else:
    MAX_FILE_MB = CLOUD_MAX_FILE_MB

MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

# How long to wait while Telegram (or local Bot API) accepts a large upload.
UPLOAD_TIMEOUT = int(os.getenv("UPLOAD_TIMEOUT", "3600"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
LOG_DIR = os.getenv("LOG_DIR", str(BASE_DIR / "logs")).strip() or str(BASE_DIR / "logs")
