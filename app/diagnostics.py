"""Logs and diagnostic reports, for when something isn't working.

- Every log line also goes to a small in-memory buffer and to config/logs/stowaway.log
  (rotated at 1 MB, three files kept, readable only by root), so the report can include
  what happened before a restart.
- Secrets are removed from every log line, wherever it goes: API keys and tokens in URLs,
  passwords, Authorization headers, and the DuckDNS/Cloudflare tokens from Settings.
- Detailed (debug) logging can be switched on from Settings; it switches itself off
  after 24 hours so it can't be forgotten.
- Nothing is ever sent anywhere: the report is shown or downloaded, and you decide
  whether to attach it to a GitHub issue.
"""
import collections
import ipaddress
import logging
import logging.handlers
import os
import re
import time
from pathlib import Path

log = logging.getLogger("stowaway")

DEBUG_HOURS = 24
FORMAT = "%(asctime)s %(levelname)s %(message)s"

_ring: collections.deque = collections.deque(maxlen=4000)        # recent formatted lines
_problems: collections.deque = collections.deque(maxlen=50)      # (time, level, message)
_secrets = lambda: []                                             # noqa: E731  set by setup()
log_dir: Path | None = None
debug_until = 0.0
_forced_level = None

SECRET_KV = re.compile(
    r"(?i)\b((?:x-)?api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|password|passwd|pwd|"
    r"secret|client[_-]?secret|auth|sig|signature|session|X-Plex-Token)([=:]\s*)(\"?)([^&\s\"',;]+)")
BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
COOKIE = re.compile(r"(?i)(stowaway_session=)[^;\s\"']+")


def redact(text: str) -> str:
    """Remove secrets. Applied to every log line and to the whole report."""
    if not text:
        return text
    for s in _secrets():
        if s and len(s) >= 6:
            text = text.replace(s, "<hidden>")
    text = SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<hidden>", text)
    text = BEARER.sub(lambda m: f"{m.group(1)} <hidden>", text)
    return COOKIE.sub(r"\1<hidden>", text)


IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def hide_personal(text: str, words: list[str]) -> str:
    """For sharing: replace the domain, email, username and public IP addresses.
    Home-network addresses stay, since they're needed to diagnose network problems."""
    for w, label in words:
        if w and len(w) >= 3:
            text = re.sub(re.escape(w), label, text, flags=re.I)

    def ip(m):
        try:
            a = ipaddress.ip_address(m.group(1))
        except ValueError:
            return m.group(1)
        return m.group(1) if (a.is_private or a.is_loopback or a.is_link_local or a.is_unspecified
                              or a.is_multicast or a.is_reserved) else "<public-ip>"
    return IPV4.sub(ip, text)


class _Formatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            line = self.format(record)
            _ring.append(line)
            if record.levelno >= logging.WARNING:
                _problems.append((record.created, record.levelname, line.split(" ", 3)[-1][:400]))
        except Exception:
            pass


class _PrivateRotatingFile(logging.handlers.RotatingFileHandler):
    def _open(self):
        f = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return f


def setup(config_dir: Path, secrets):
    """Configure logging: console (docker logs), memory buffer and a private log file."""
    global _secrets, log_dir, _forced_level
    _secrets = secrets
    env_level = os.environ.get("LOG_LEVEL", "").upper()
    _forced_level = env_level if env_level in ("DEBUG", "INFO", "WARNING", "ERROR") else None
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.INFO)
    fmt = _Formatter(FORMAT)
    handlers = [logging.StreamHandler(), _RingHandler()]
    try:
        log_dir = config_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        handlers.append(_PrivateRotatingFile(log_dir / "stowaway.log", maxBytes=1_000_000, backupCount=2,
                                             encoding="utf-8"))
    except OSError as e:
        log_dir = None
        print(f"can't write log files in {config_dir}: {e}")
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _apply_level()


def _apply_level():
    level = _forced_level or ("DEBUG" if debug_until > time.time() else "INFO")
    log.setLevel(level)


def set_debug(on: bool, until: float | None = None):
    global debug_until
    debug_until = (until or time.time() + DEBUG_HOURS * 3600) if on else 0.0
    _apply_level()
    log.info("detailed logging %s", f"on until {time.strftime('%Y-%m-%d %H:%M', time.localtime(debug_until))}"
             if on else "off")


def tick():
    """Called regularly: switches detailed logging off when its time is up."""
    global debug_until
    if debug_until and debug_until <= time.time():
        debug_until = 0.0
        _apply_level()
        log.info("detailed logging switched itself off after %d hours", DEBUG_HOURS)
        return True
    return False


def debug_on() -> bool:
    return log.isEnabledFor(logging.DEBUG)


def recent_problems(n=8):
    return [{"at": t, "level": lvl, "message": msg} for t, lvl, msg in list(_problems)[-n:]][::-1]


def recent_lines(n=400):
    return list(_ring)[-n:]


def log_files():
    if not log_dir:
        return []
    files = [log_dir / "stowaway.log.2", log_dir / "stowaway.log.1", log_dir / "stowaway.log"]
    return [f for f in files if f.exists()]
