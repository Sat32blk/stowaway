"""Set up dashboards (Homarr, Homepage, Glance) to show Stowaway's status.

Nothing here runs on its own: each function is called when the user picks apps
in an app's Settings → Dashboards and clicks the button for that dashboard.
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
STOWAWAY_ICON = "https://raw.githubusercontent.com/Sat32blk/Stowaway/main/docs/icon.png"
SUMMARY_MAPPINGS = {
    "awake": [{"field": "awake", "label": "Awake"}, {"field": "asleep", "label": "Asleep"}],
    "memory": [{"field": "memory_freed", "label": "Mem freed"}],
    "cpu": [{"field": "cpu_freed", "label": "CPU freed"}],
    "cpu_time": [{"field": "cpu_time_saved", "label": "Saved (7 days)"}],
    "updates": [{"field": "updates_waiting", "label": "Updates"}],
}


def homepage_self(base: str, link: str, fields: list[str]) -> dict:
    """Homepage service entry for Stowaway itself."""
    mappings = [m for f in fields for m in SUMMARY_MAPPINGS.get(f, [])][:4]   # Homepage shows up to 4
    return {"Stowaway": {
        "href": link,
        "icon": STOWAWAY_ICON,
        "description": "Wakes apps when opened",
        "siteMonitor": f"{base}/summary",
        "widget": {"type": "customapi", "url": f"{base}/summary", "refreshInterval": 30000, "mappings": mappings},
    }}


def homepage_block(group: str, items: list[dict], base: str, self_entry: dict | None = None) -> str:
    services = [self_entry] if self_entry else []
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
                "mappings": [{"field": "indicator", "label": "Status"}],
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
GLANCE_COLORS = {"green": "hsl(134, 50%, 60%)", "yellow": "hsl(45, 95%, 55%)", "orange": "hsl(27, 98%, 58%)",
                 "teal": "hsl(162, 75%, 45%)", "blue": "hsl(207, 90%, 63%)", "red": "hsl(0, 94%, 65%)"}
GLANCE_SUMMARY_TEMPLATE = """<div class="flex gap-20 items-end">
{{ if .JSON.Bool "show.awake" }}  <div><div class="color-highlight size-h1">{{ .JSON.Int "awake" }}</div><div class="size-h6 color-positive">AWAKE</div></div>
  <div><div class="color-highlight size-h1">{{ .JSON.Int "asleep" }}</div><div class="size-h6">ASLEEP</div></div>
{{ end }}{{ if .JSON.Bool "show.memory" }}  <div><div class="color-highlight size-h1">{{ .JSON.String "memory_freed" }}</div><div class="size-h6">FREED</div></div>
{{ end }}</div>
<ul class="list list-gap-10" style="margin-top: 12px">
{{ if .JSON.Bool "show.memory" }}  <li class="flex justify-between"><span>Memory freed</span><span class="color-highlight">{{ .JSON.String "memory_share_text" }}</span></li>
{{ end }}{{ if .JSON.Bool "show.cpu" }}  <li class="flex justify-between"><span>CPU freed</span><span class="color-highlight">{{ .JSON.String "cpu_freed" }}</span></li>
{{ end }}{{ if .JSON.Bool "show.cpu_time" }}  <li class="flex justify-between"><span>CPU time saved this week</span><span class="color-highlight">{{ .JSON.String "cpu_time_saved" }}</span></li>
{{ end }}{{ if .JSON.Bool "show.updates" }}  <li class="flex justify-between"><span>Updates waiting</span><span class="color-highlight">{{ .JSON.Int "updates_waiting" }}</span></li>
{{ end }}</ul>
"""

GLANCE_TEMPLATE = """<ul class="list list-gap-10 collapsible-container" data-collapse-after="8">
{{ range .JSON.Array "apps" }}
  <li class="flex justify-between items-center gap-10"><span class="color-highlight">{{ .String "name" }}</span>
""" + "".join(
    f'    {{{{ {"if" if i == 0 else "else if"} eq (.String "indicator_color") "{c}" }}}}'
    f'<span style="color: {v}; font-weight: 600">{{{{ .String "indicator" }}}}</span>\n'
    for i, (c, v) in enumerate(GLANCE_COLORS.items())) + """    {{ else }}<span class="color-subdue">{{ .String "indicator" }}</span>{{ end }}</li>
{{ end }}
</ul>
"""


def glance_file(items: list[dict], base: str, title: str, with_self: bool = False) -> str:
    sites = []
    for it in items:
        _, dot = status_urls(base, it["name"])
        sites.append({"title": it["name"], "url": it["link"], "check-url": dot, "icon": f"di:{it['icon']}"})
    widgets = []
    if with_self:
        widgets.append({"type": "custom-api", "title": "Stowaway", "url": f"{base}/summary", "cache": "1m",
                        "template": GLANCE_SUMMARY_TEMPLATE})
    if sites:
        widgets.append({"type": "monitor", "title": title, "cache": "1m", "sites": sites})
    widgets.append({"type": "custom-api", "title": "Apps" if with_self else "Stowaway", "url": f"{base}/status",
                    "cache": "30s", "template": GLANCE_TEMPLATE})
    head = ("# Written by Stowaway (an app's Settings > Dashboards > Glance). Clicking the button there again replaces this file.\n"
            "# Use it in glance.yml with a line like:  - $include: stowaway.yml  (inside a column's widgets)\n")
    return head + yaml.dump(widgets, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True, width=1000)


class _BlockDumper(yaml.SafeDumper):
    """Writes multi-line text (Glance templates) as readable | blocks."""


_BlockDumper.add_representer(str, lambda d, v: d.represent_scalar(
    "tag:yaml.org,2002:str", v, style="|" if "\n" in v else None))


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


# ------------------------------------------------------- Homarr status widget --
HOMARR_WIDGET_TEMPLATE = """<Stack gap={6} p="sm" h="100%" justify="center" style={{ minWidth: 0 }}>
  <Group justify="space-between" wrap="nowrap" gap="xs"><Text size="xs" c="dimmed" tt="uppercase" fw={700} truncate>{options.title || data.status?.name || options.app}</Text><RefreshButton requestId="status" label="Refresh status" size="xs" /></Group>
  {status.status?.loading && !data.status ? <Skeleton height={34} radius="md" /> : status.status?.ok === false ? <Text size="sm" c="red">{options.app ? "Can't reach Stowaway, or no app named " + options.app : "Choose the app in this widget's settings"}</Text> : <Group gap="sm" wrap="nowrap"><ThemeIcon color={data.status?.indicator_color ?? "gray"} variant="light" radius="xl" size="lg"><Icon name={data.status?.indicator_icon ?? "moon"} size={20} /></ThemeIcon><Text fw={700} size="lg" c={data.status?.indicator_color ?? "gray"} truncate>{data.status?.indicator ?? "Unknown"}</Text></Group>}
  {(data.status?.sleeps_in ?? 0) > 0 && (data.status?.idle_timeout ?? 0) > 0 ? <Progress value={Math.min(100, data.status.sleeps_in * 100 / data.status.idle_timeout)} color="yellow" size="sm" radius="xl" /> : null}
</Stack>"""


def homarr_widget(base: str) -> dict:
    """A Homarr custom widget ("Stowaway status") that shows one app's status.
    Import it once in Homarr, then pick the app in each placed widget's settings."""
    host = (urlsplit(base).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        scope = "loopback"
    else:
        try:
            import ipaddress
            scope = "private" if ipaddress.ip_address(host).is_private else "public"
        except ValueError:
            scope = "private" if host.endswith((".local", ".lan", ".home", ".internal", ".home.arpa")) or "." not in host else "public"
    return {
        "$schema": "homarr-custom-widget-v2",
        "name": "Stowaway status",
        "description": "Shows whether an app Stowaway manages is In Use, Sleeping in a few minutes, Idle or Sleeping. Checking never wakes the app.",
        "iconUrl": "https://raw.githubusercontent.com/Sat32blk/Stowaway/main/heimdall/Stowaway/stowaway.svg",
        "sources": {"default": {"name": "Stowaway", "baseUrl": base, "networkScope": scope, "auth": "none"}},
        "requests": {"status": {"path": "/status/{option:app}", "cacheSeconds": 10}},
        "options": {
            "app": {"label": "App name", "description": "The app's name in Stowaway, e.g. jellyfin",
                    "control": "text", "default": ""},
            "title": {"label": "Title", "description": "Shown above the status; leave empty to use the app's name",
                      "control": "text", "default": ""},
        },
        "template": HOMARR_WIDGET_TEMPLATE,
    }


HOMARR_SUMMARY_TEMPLATE = """<Stack gap="sm" p="sm" h="100%" style={{ minWidth: 0 }}>
  <Group justify="space-between" wrap="nowrap" gap="xs"><Text fw={700} truncate>{options.title || "Stowaway"}</Text><Group gap={4} wrap="nowrap">{data.summary && data.summary.show?.updates && data.summary.updates_waiting > 0 ? <Badge color="yellow" variant="light">{data.summary.updates_waiting + " updates"}</Badge> : null}{data.summary ? <Badge color={data.summary.awake > 0 ? "green" : "gray"} variant="light">{data.summary.awake + " awake"}</Badge> : null}<RefreshButton requestId="summary" label="Refresh" size="xs" /></Group></Group>
  {status.summary?.loading && !data.summary ? <Skeleton height={70} radius="md" /> : status.summary?.ok === false ? <Text size="sm" c="red">Can't reach Stowaway</Text> : options.layout === "wide" ? <Stack gap="sm"><SimpleGrid cols={4} spacing="sm">{data.summary.show?.awake ? <Paper withBorder p="xs" radius="md"><Text size="xs" c="dimmed">Awake</Text><Text fw={700} size="xl" c="green">{data.summary.awake}</Text><Text size="xs" c="dimmed">{"of " + data.summary.apps + " apps"}</Text></Paper> : null}{data.summary.show?.awake ? <Paper withBorder p="xs" radius="md"><Text size="xs" c="dimmed">Asleep</Text><Text fw={700} size="xl">{data.summary.asleep}</Text><Text size="xs" c="dimmed">{"of " + data.summary.apps + " apps"}</Text></Paper> : null}{data.summary.show?.memory ? <Paper withBorder p="xs" radius="md"><Text size="xs" c="dimmed">Memory freed</Text><Text fw={700} size="xl">{data.summary.memory_freed}</Text><Text size="xs" c="dimmed">right now</Text></Paper> : null}{data.summary.show?.cpu ? <Paper withBorder p="xs" radius="md"><Text size="xs" c="dimmed">CPU freed</Text><Text fw={700} size="xl">{data.summary.cpu_freed}</Text><Text size="xs" c="dimmed">of the whole CPU</Text></Paper> : null}{data.summary.show?.cpu_time ? <Paper withBorder p="xs" radius="md"><Text size="xs" c="dimmed">CPU time saved</Text><Text fw={700} size="xl">{data.summary.cpu_time_saved}</Text><Text size="xs" c="dimmed">this week</Text></Paper> : null}</SimpleGrid>{data.summary.show?.memory && data.summary.memory_share != null ? <Group gap="xs" wrap="nowrap"><Text size="xs" c="dimmed">Memory freed</Text><Progress value={Math.min(100, data.summary.memory_share * 100)} color="teal" size="sm" radius="xl" style={{ flex: 1 }} /><Text size="xs">{data.summary.memory_share_text}</Text></Group> : null}</Stack> : <Stack gap="xs">{data.summary.show?.awake ? <Stack gap={4}><Progress value={data.summary.apps ? data.summary.awake * 100 / data.summary.apps : 0} color="green" size="md" radius="xl" /><Group justify="space-between" wrap="nowrap"><Text size="xs" c="dimmed">{data.summary.awake + " awake"}</Text><Text size="xs" c="dimmed">{data.summary.asleep + " asleep"}</Text></Group></Stack> : null}<SimpleGrid cols={2} spacing="sm">{data.summary.show?.memory ? <Stack gap={0}><Text fw={700} size="lg">{data.summary.memory_freed}</Text><Text size="xs" c="dimmed">memory freed</Text></Stack> : null}{data.summary.show?.cpu ? <Stack gap={0}><Text fw={700} size="lg">{data.summary.cpu_freed}</Text><Text size="xs" c="dimmed">CPU freed</Text></Stack> : null}{data.summary.show?.cpu_time ? <Stack gap={0}><Text fw={700} size="lg">{data.summary.cpu_time_saved}</Text><Text size="xs" c="dimmed">saved this week</Text></Stack> : null}</SimpleGrid></Stack>}
</Stack>"""


def homarr_summary_widget(base: str) -> dict:
    """A Homarr custom widget ("Stowaway summary") for Stowaway itself: apps awake and
    asleep, and what sleeping them frees. Small or wide, picked in its settings."""
    w = homarr_widget(base)
    return {
        **w,
        "name": "Stowaway summary",
        "description": "How many apps Stowaway has awake and asleep, and how much memory and CPU sleeping them frees.",
        "requests": {"summary": {"path": "/summary", "cacheSeconds": 20}},
        "options": {
            "layout": {"label": "Layout", "description": "Small fits a 2×2 space, wide a 4×2 one",
                       "control": "select", "default": "small",
                       "choices": [{"label": "Small", "value": "small"}, {"label": "Wide", "value": "wide"}]},
            "title": {"label": "Title", "description": "Leave empty for \"Stowaway\"", "control": "text", "default": ""},
        },
        "template": HOMARR_SUMMARY_TEMPLATE,
    }


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
