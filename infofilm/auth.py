"""Local password hashing. Plaintext passwords and derived salts are never persisted."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import hashlib
import hmac
import json
import os
import secrets
import uuid
from pathlib import Path

from .core import ROOT

ITERATIONS = 600_000


class Auth:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "auth.json"
        self.key_path = self.directory / "server.key"

    def _key(self):
        return self.key_path.read_bytes()

    def _hash(self, password, username):
        # Domain separation keeps the derived per-user salt distinct from other
        # uses of the random installation key. The salt itself is never stored.
        salt = hmac.digest(self._key(), b"remotion-video/password-salt/v1\0" + username.encode(), "sha256")
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS).hex()

    def load(self):
        value = json.loads(self.path.read_text())
        if value.get("version") != 1 or len(self._key()) != 32:
            raise ValueError("Unsupported or damaged login configuration")
        return value

    def verify(self, username, password):
        value = self.load()
        candidate = self._hash(password, value["username"])
        if hmac.compare_digest(candidate, value["password_hash"]) and hmac.compare_digest(username.encode(), value["username"].encode()):
            return value["revision"]
        return None

    def set_password(self, username, password):
        if not username or len(username) > 80 or not username.isascii():
            raise ValueError("Use a short ASCII username")
        if len(password) < 8 or len(password) > 1024:
            raise ValueError("Password must contain between 8 and 1024 characters")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "auth.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.key_path.exists():
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secrets.token_bytes(32))
            os.chmod(self.key_path, 0o600)
            value = {"version": 1, "username": username, "password_hash": self._hash(password, username),
                     "revision": uuid.uuid4().hex}
            temporary = self.path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
            temporary.replace(self.path)
            os.chmod(self.path, 0o600)


def main():
    parser = argparse.ArgumentParser(description="Change the Remotion Video login password without storing plaintext or a salt.")
    parser.add_argument("command", choices=["set-password"])
    parser.add_argument("--username", default="nitin")
    parser.add_argument("--auth-dir", type=Path, default=ROOT / "artifacts/interface")
    args = parser.parse_args()
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords do not match")
    try:
        Auth(args.auth_dir).set_password(args.username, password)
    except ValueError as exc:
        parser.error(str(exc))
    print("Password updated. Existing browser sessions are now invalid.")


if __name__ == "__main__":
    main()
