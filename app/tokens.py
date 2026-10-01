"""API tokens for Home Assistant and other automation.

A token is shown once when it's created; only a SHA-256 hash is stored, in
config/tokens.json (readable only by root). Tokens work only for the /api/v1
endpoints: they can see and control apps, but can't change settings, the
account, or create more tokens.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

PREFIX = "stw_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self.tokens: list[dict] = []
        self._dirty_used = 0.0
        try:
            self.tokens = json.loads(path.read_text()).get("tokens", [])
        except (OSError, ValueError):
            self.tokens = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"tokens": self.tokens}, f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def create(self, name: str) -> tuple[dict, str]:
        token = PREFIX + secrets.token_urlsafe(32)
        entry = {"id": secrets.token_hex(6), "name": name, "hash": _hash(token),
                 "hint": token[:8] + "…" + token[-4:], "created": time.time(), "last_used": None}
        self.tokens.append(entry)
        self._save()
        return self.public(entry), token

    def revoke(self, token_id: str) -> bool:
        before = len(self.tokens)
        self.tokens = [t for t in self.tokens if t["id"] != token_id]
        if len(self.tokens) != before:
            self._save()
            return True
        return False

    def check(self, token: str):
        """The matching entry, or None. Constant-time comparison."""
        if not token or not token.startswith(PREFIX):
            return None
        h = _hash(token)
        found = None
        for t in self.tokens:
            if hmac.compare_digest(t["hash"], h):
                found = t
        if found:
            now = time.time()
            found["last_used"] = now
            if now - self._dirty_used > 300:        # don't rewrite the file on every request
                self._dirty_used = now
                self._save()
        return found

    @staticmethod
    def public(t: dict) -> dict:
        return {k: t[k] for k in ("id", "name", "hint", "created", "last_used")}

    def list(self):
        return [self.public(t) for t in self.tokens]
