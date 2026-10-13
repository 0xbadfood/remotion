"""Copy the Quiz Video Studio's YouTube connection (test channel) into this app, once.

Run with the quiz environment, which has its decryption key and library:
    ~/quiz_generator/.venv/bin/python scripts/import_youtube_credentials.py
Secrets are written to artifacts/interface/youtube-secrets.json (0600) and never printed.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

QUIZ = Path(os.environ.get("QUIZ_ROOT", Path.home() / "quiz_generator"))
sys.path.insert(0, str(QUIZ))
from quiz_harness.secure_store import SecretStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
database = Path(os.environ.get("QUIZ_DATABASE_PATH", QUIZ / "data" / "quiz_harness.db"))
key_file = Path(os.environ.get("QUIZ_SECRET_KEY_FILE", QUIZ / "data" / ".provider_secret_key"))
row = sqlite3.connect(database).execute(
    "SELECT client_id, client_secret_ciphertext, refresh_token_ciphertext FROM youtube_connections WHERE id='default'").fetchone()
if not row or not all(row):
    sys.exit("The quiz app has no connected YouTube channel")
store = SecretStore(key=os.environ.get("QUIZ_SECRET_KEY"), key_file=key_file)
target = ROOT / "artifacts" / "interface" / "youtube-secrets.json"
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as handle:
    json.dump({"client_id": row[0], "client_secret": store.decrypt(row[1]), "refresh_token": store.decrypt(row[2])}, handle)
os.chmod(target, 0o600)
print(f"Imported the YouTube connection into {target}")
