"""Set up dashboards (Homarr, Homepage, Glance) to show Stowaway's status.

Nothing here runs on its own: each function is called when the user picks apps
in Integrations and clicks the button for that dashboard.
"""
from __future__ import annotations

import io
import posixpath
import re
import tarfile
import time
from urllib.parse import quote, urlsplit

import httpx
import yaml

BEGIN = "# >>> Stowaway: written by Stowaway (Integrations > Homepage). Changes inside this block are replaced. >>>"
END = "# <<< Stowaway <<<"
GLANCE_FILE = "stowaway.yml"


class SetupError(Exception):
    """A problem worth showing to the user as is."""


def check_url(url: str, what: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SetupError(f"{what} must be an http:// or https:// address")
    return url


def icon_slug(image: str, name: str) -> str:
    """Best guess at the dashboard-icons name: the image's repository name, e.g.
    lscr.io/linuxserver/jellyfin:latest -> jellyfin."""
    repo = (image or "").split("@")[0].rsplit("/", 1)[-1].split(":")[0]
    slug = re.sub(r"[^a-z0-9-]+", "-", (repo or name).lower()).strip("-")
    return slug or "docker"


def status_urls(base: str, name: str):
    st = f"{base}/status/{quote(name, safe='')}"
    return st, f"{st}?code=1"


# ---------------------------------------------------------------- Homepage --
def homepage_block(group: str, items: list[dict], base: str) -> str:
    services = []
    for it in items:
        st, dot = status_urls(base, it["name"])
        services.append({it["name"]: {
            "href": it["link"],
            "icon": f"{it['icon']}.png",
            "description": "Wakes when opened · managed by Stowaway",
            "siteMonitor": dot,
            "widget": {
                "type": "customapi",
                "url": st,
                "refreshInterval": 15000,
                "mappings": [
                    {"field": "state", "label": "Status",
                     "remap": [{"value": "running", "to": "Awake"},
                               {"value": "sleeping", "to": "Asleep"},
                               {"value": "starting", "to": "Starting"},
                               {"any": True, "to": "Working"}]},
                    {"field": "maintenance.next_restart_text", "label": "Restart"},
                ],
            },
        }})
    body = yaml.safe_dump([{group: services}], sort_keys=False, allow_unicode=True, width=1000)
    return f"{BEGIN}\n{body}{END}\n"


def merge_services(text: str, block: str) -> str:
    """Put the block in services.yaml: replace an earlier Stowaway block, or add
    it at the end. Everything outside the block stays exactly as it was."""
    if BEGIN in text and END in text:
        start = text.index(BEGIN)
        stop = text.index(END, start) + len(END)
        if stop < len(text) and text[stop] == "\n":
            stop += 1
        new = text[:start] + block + text[stop:]
    else:
        try:
            current = yaml.safe_load(text) if text.strip() else None
        except yaml.YAMLError as e:
            raise SetupError(f"services.yaml can't be read as YAML, so it was left alone ({e.__class__.__name__}).")
        if current is not None and not isinstance(current, list):
            raise SetupError("services.yaml isn't a list of groups, so it was left alone.")
        if text.strip() in ("[]", "null", "~"):
            text = ""
        new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    try:
        parsed = yaml.safe_load(new)
    except yaml.YAMLError as e:
        raise SetupError(f"The updated services.yaml wouldn't be valid YAML, so nothing was changed ({e}).")
    if not isinstance(parsed, list):
        raise SetupError("The updated services.yaml wouldn't be a list of groups, so nothing was changed.")
    return new


# ------------------------------------------------------------------ Glance --
GLANCE_TEMPLATE = """<ul class="list list-gap-10 collapsible-container" data-collapse-after="8">
{{ range .JSON.Array "apps" }}
  <li><span class="color-highlight">{{ .String "name" }}</span>
    <span class="color-subdue">{{ .String "summary" }}</span></li>
{{ end }}
</ul>
"""


def glance_file(items: list[dict], base: str, title: str) -> str:
    sites = []
    for it in items:
        _, dot = status_urls(base, it["name"])
        sites.append({"title": it["name"], "url": it["link"], "check-url": dot, "icon": f"di:{it['icon']}"})
    widgets = [
        {"type": "monitor", "title": title, "cache": "1m", "sites": sites},
        {"type": "custom-api", "title": "Stowaway", "url": f"{base}/status", "cache": "30s",
         "template": GLANCE_TEMPLATE},
    ]
    head = ("# Written by Stowaway (Integrations > Glance). Clicking the button there again replaces this file.\n"
            "# Use it in glance.yml with a line like:  - $include: stowaway.yml  (inside a column's widgets)\n")
    return head + yaml.safe_dump(widgets, sort_keys=False, allow_unicode=True, width=1000)


def glance_includes(text: str) -> bool:
    return bool(re.search(r"^\s*-?\s*\$include:\s*['\"]?(\./)?" + re.escape(GLANCE_FILE) + r"['\"]?\s*$", text, re.M))


# ------------------------------------------------------- files in containers --
def tar_one(path: str, data: bytes, like: tarfile.TarInfo | None) -> bytes:
    info = tarfile.TarInfo(posixpath.basename(path))
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = like.mode if like else 0o644
    info.uid, info.gid = (like.uid, like.gid) if like else (0, 0)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def write_file(dk, container: str, path: str, data: bytes, like=None):
    await dk.put_archive(container, posixpath.dirname(path), tar_one(path, data, like))


def on_volume(attrs: dict, path: str) -> bool:
    """Whether the path is inside one of the container's mounts (so it survives
    the container being recreated)."""
    for m in attrs.get("Mounts") or []:
        dest = (m.get("Destination") or "").rstrip("/")
        if dest and (path == dest or path.startswith(dest + "/")):
            return True
    return False


def config_path(attrs: dict, flag: str, default: str) -> str:
    """The config file a dashboard was started with (e.g. glance --config X)."""
    cfg = attrs.get("Config") or {}
    args = (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or [])
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return default


def env_of(attrs: dict) -> dict:
    return dict(e.split("=", 1) for e in (attrs.get("Config") or {}).get("Env") or [] if "=" in e)


# ------------------------------------------------------------------- icons --
ICON_SOURCES = ("https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons",
                "https://raw.githubusercontent.com/homarr-labs/dashboard-icons/main")


def _icon_ok(data: bytes, ext: str) -> bool:
    if not data or len(data) > 1_000_000:
        return False
    if ext == "png":
        return data.startswith(b"\x89PNG")
    low = data.lower()
    head = low[:512].lstrip()
    return (b"<svg" in head or head.startswith(b"<?xml")) and b"<script" not in low and b"javascript:" not in low


async def fetch_icon(slugs: list[str]):
    """The app's icon from the dashboard-icons collection: (bytes, extension, slug),
    or None if it isn't there or can't be downloaded."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        for base in ICON_SOURCES:
            reachable = False
            for slug in dict.fromkeys(s for s in slugs if s):
                for ext in ("svg", "png"):
                    try:
                        r = await c.get(f"{base}/{ext}/{slug}.{ext}")
                    except httpx.HTTPError:
                        break
                    reachable = True
                    if r.status_code == 200 and _icon_ok(r.content, ext):
                        return r.content, ext, slug
                else:
                    continue
                break
            if reachable:
                return None                 # the collection was reachable and doesn't have it
    return None


# ------------------------------------------------------------------ Homarr --
def check_homarr_key(key: str) -> str:
    key = "".join((key or "").split())          # copied keys sometimes pick up spaces or line breaks
    if key.count(".") != 1 or not all(key.split(".")):
        raise SetupError("That doesn't look like a Homarr API key: it should be two parts joined by a dot, "
                         "like 1a2b3c4d.Xy… Copy it again from Homarr (it's only shown once, right after creating it).")
    return key


async def homarr_call(method: str, url: str, key: str, path: str, body=None):
    key = check_homarr_key(key)
    headers = {"ApiKey": key, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as c:
            r = await c.request(method, url + path, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise SetupError(f"Couldn't reach Homarr at {url} ({e.__class__.__name__}). Check the address; "
                         "if Homarr is on a macvlan network, use its own address, e.g. http://192.168.1.6:7575.")
    if r.status_code == 401:
        raise SetupError("Homarr didn't accept the API key. Paste the whole key exactly as Homarr showed it "
                         "(two parts joined by a dot, like 1a2b3c4d.Xy…). Homarr's log says why it was "
                         "refused: run  docker logs homarr 2>&1 | grep -i api-key  on the server.")
    if r.status_code == 403:
        raise SetupError("Homarr accepted the API key, but its user isn't allowed to change apps. "
                         "Create the key while signed in as a Homarr admin.")
    if r.status_code == 404:
        raise SetupError("Homarr didn't recognise the request. This needs Homarr 1.0 or newer.")
    if r.status_code >= 400:
        try:
            detail = r.json().get("message") or r.text
        except ValueError:
            detail = r.text
        raise SetupError(f"Homarr answered {r.status_code}: {str(detail)[:200]}")
    if r.status_code >= 300:
        raise SetupError(f"Homarr answered {r.status_code} (a redirect). Use Homarr's address exactly as it "
                         "opens in your browser, including http:// or https://.")
    try:
        return r.json() if r.content else None
    except ValueError:
        raise SetupError("Homarr's answer wasn't what Stowaway expected. Check that the address is Homarr's.")


def homarr_match(apps: list[dict], name: str, link: str) -> str | None:
    """Find the Homarr app that is most likely this Stowaway app: same link, then
    same port on its address, then the same name."""
    lk = urlsplit(link)
    for a in apps:
        if (a.get("href") or "").rstrip("/") == link.rstrip("/"):
            return a["id"]
    for a in apps:
        h = urlsplit(a.get("href") or "")
        if h.port and h.port == lk.port and h.hostname == lk.hostname:
            return a["id"]
    for a in apps:
        if (a.get("name") or "").strip().lower() == name.lower():
            return a["id"]
    return None
