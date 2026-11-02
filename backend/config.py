import os
from pathlib import Path

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR / ".env", override=False)

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]

LEADERBOARD_PATH = Path(os.getenv("LEADERBOARD_PATH", "leaderboard.sqlite3"))
if not LEADERBOARD_PATH.is_absolute():
    LEADERBOARD_PATH = BACKEND_DIR / LEADERBOARD_PATH

ARTWORK_SHARE_URL = os.getenv("ARTWORK_SHARE_URL", "").strip().rstrip("/")
ARTWORK_SHARE_UPLOAD_KEY = os.getenv("ARTWORK_SHARE_UPLOAD_KEY", "").strip()

# Server-side only. Empty/short passwords disable Admin access, not authentication.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
