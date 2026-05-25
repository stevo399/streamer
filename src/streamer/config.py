import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

MEDIA_ROOTS = [
    Path(p.strip())
    for p in os.environ.get("MEDIA_ROOTS", "").split(",")
    if p.strip()
]

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

TTS_ENGINE = os.environ.get("TTS_ENGINE", "cloud").lower()
TTS_VOICE = os.environ.get("TTS_VOICE", "Kore")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8054"))

AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH", "")

_notes_dir = os.environ.get("NOTES_DIR", "")
NOTES_DIR = Path(_notes_dir) if _notes_dir.strip() else None

OLLAMA_URL = os.environ.get("OLLAMA_URL", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
