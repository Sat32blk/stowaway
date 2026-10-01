"""Dashboard accounts for Stowaway.

One account (username + password), created on first visit. Passwords are
stored as scrypt hashes in config/auth.json (readable by root only). Signed-in
browsers get an HttpOnly session cookie signed with a secret kept in the same
file; changing the password bumps a generation number, which signs out every
other session.

Reset a forgotten password from the server:
    docker exec stowaway python -m app.auth reset
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

COOKIE = "stowaway_session"
SESSION_DAYS = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,32}$")


def password_problems(pw: str) -> list[str]:
    """What's missing from a password; empty means it's acceptable."""
    missing = []
    if len(pw) < 8:
        missing.append("at least 8 characters")
    if not re.search(r"[A-Za-z]", pw):
        missing.append("a letter")
    if not re.search(r"[0-9]", pw):
        missing.append("a number")
    if not re.search(r"[^A-Za-z0-9]", pw):
        missing.append("a special character such as ! @ # $ %")
    if len(pw) > 128:
        missing.append("no more than 128 characters")
    return missing


def username_problem(name: str) -> str | None:
    if not USERNAME_RE.match(name or ""):
        return "Username must be 3–32 characters: letters, numbers, and . _ - @"
    return None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(h)}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, _unb64(h))
    except Exception:
        return False


# A hash of a random password, so failed logins for unknown usernames take as
# long as real ones and don't reveal which usernames exist.
_DUMMY = hash_password(secrets.token_urlsafe(16))


class AuthStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = None
        self._mtime = "unset"
        self.refresh()

    def load(self):
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.data = None

    def refresh(self):
        """Pick up changes made outside (e.g. the reset command) without a restart."""
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            m = None
        if m != self._mtime:
            self._mtime = m
            self.load()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    @property
    def needs_setup(self) -> bool:
        return not (self.data and self.data.get("username") and self.data.get("password"))

    @property
    def username(self):
        return self.data.get("username") if self.data else None

    def create(self, username: str, password: str):
        self.data = {
            "username": username,
            "password": hash_password(password),
            "secret": _b64(secrets.token_bytes(32)),
            "generation": 1,
            "created": int(time.time()),
        }
        self.save()

    def check(self, username: str, password: str) -> bool:
        if self.needs_setup:
            verify_password(password, _DUMMY)
            return False
        name_ok = hmac.compare_digest(username.encode(), self.data["username"].encode())
        pw_ok = verify_password(password, self.data["password"] if name_ok else _DUMMY)
        return name_ok and pw_ok

    def change(self, username: str | None = None, password: str | None = None):
        if username:
            self.data["username"] = username
        if password:
            self.data["password"] = hash_password(password)
            self.data["generation"] = self.data.get("generation", 1) + 1   # signs out other sessions
        self.save()

    # ---- sessions ----
    def _sign(self, payload: str) -> str:
        key = _unb64(self.data["secret"])
        return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())

    def issue(self) -> str:
        body = _b64(json.dumps({
            "u": self.data["username"],
            "g": self.data.get("generation", 1),
            "exp": int(time.time()) + SESSION_DAYS * 86400,
        }).encode())
        return f"{body}.{self._sign(body)}"

    def session_user(self, token: str | None):
        if not token or self.needs_setup or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(body)):
            return None
        try:
            claims = json.loads(_unb64(body))
        except Exception:
            return None
        if claims.get("exp", 0) < time.time():
            return None
        if claims.get("g") != self.data.get("generation", 1) or claims.get("u") != self.data["username"]:
            return None
        return claims["u"]


def _cli():
    """docker exec stowaway python -m app.auth reset"""
    cfg = Path(os.environ.get("STOWAWAY_CONFIG", "/config/config.yaml")).parent / "auth.json"
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        if cfg.exists():
            cfg.unlink()
        print("Sign-in reset. Open the Stowaway dashboard from your home network to create a new account.")
    else:
        print("usage: python -m app.auth reset")


if __name__ == "__main__":
    _cli()
