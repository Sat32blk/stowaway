"""On-demand Docker containers.

Starts a container when someone opens its link and stops it again after it
has been idle for a configurable time. The dashboard lists every container on
the server; tick "Enable Stowaway" to put one under its control.

stowaway runs on the host's network so it can open a new link port for each
controlled container on the fly, and reach containers through their published
ports or container IPs without changing them.
"""
import asyncio
import base64
import html
import io
import json
import contextlib
import ipaddress
import re
import logging
import os
import posixpath
import secrets
import signal
import socket
import sys
import tarfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlsplit

import httpx
import uvicorn
import yaml
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from app.web import App, HTTPException, Model as BaseModel, Request, Router
from starlette.background import BackgroundTask
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from app.auth import COOKIE, SESSION_DAYS, AuthStore, password_problems, username_problem
from app import dashsetup, diagnostics, dockerapi, maintenance as maint, netconf, updates
from app.version import REPO_URL, VERSION
from app.tokens import TokenStore
from app.savings import LEARN_AFTER_START, Savings, host_info
from app.certs import SOURCES as CERT_SOURCES, DOMAIN_RE, CertManager, dns_label, local_ips

CONFIG_PATH = Path(os.environ.get("STOWAWAY_CONFIG", "config.yaml"))
SECRET_SETTINGS = ("duckdns_token", "cloudflare_token", "homarr_key")   # never sent to the browser or into reports
diagnostics.setup(CONFIG_PATH.parent, lambda: [reg.settings.get(k) for k in SECRET_SETTINGS]
                  if "reg" in globals() else [])
log = logging.getLogger("stowaway")
DEMO = os.environ.get("STOWAWAY_DEMO") == "1"
def write_private(path: Path, text: str):
    """Write a file only root can read (settings hold API tokens)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


AUTH_REQUIRED = not DEMO or os.environ.get("STOWAWAY_REQUIRE_LOGIN") == "1"
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
REAP_INTERVAL = int(os.environ.get("REAP_INTERVAL", "5"))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8880"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
SELF_NAME = os.environ.get("STOWAWAY_SELF", "stowaway")
ADMIN = "/_stowaway"
STATIC = Path(__file__).parent / "static"

SPOOFABLE = {"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port",
             "x-forwarded-server", "x-real-ip", "forwarded", "x-client-ip", "true-client-ip",
             "cf-connecting-ip", "x-original-forwarded-for"}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


# --------------------------------------------------------------------------
# Docker access
# --------------------------------------------------------------------------
def describe(attrs: dict) -> dict:
    """Pull out what stowaway needs from `docker inspect` output."""
    cfg = attrs.get("Config") or {}
    host = attrs.get("HostConfig") or {}
    nets = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
    netlist = []
    for net_name, n in nets.items():
        ip = n.get("IPAddress") or ((n.get("IPAMConfig") or {}).get("IPv4Address")) or ""
        netlist.append({"name": net_name, "ip": ip})
    published = {}
    for key, binds in (host.get("PortBindings") or {}).items():
        cport, _, proto = key.partition("/")
        if proto == "tcp" and binds:
            hp = binds[0].get("HostPort")
            if hp and hp.isdigit() and cport.isdigit():
                published[int(cport)] = int(hp)
    exposed = sorted({
        int(k.split("/")[0]) for k in (cfg.get("ExposedPorts") or {})
        if k.endswith("/tcp") and k.split("/")[0].isdigit()
    } | set(published))
    labels = cfg.get("Labels") or {}
    return {
        "name": attrs.get("Name", "").lstrip("/"),
        "image": cfg.get("Image", ""),
        "status": (attrs.get("State") or {}).get("Status", "unknown"),
        "network_mode": host.get("NetworkMode", ""),
        "published": published,
        "exposed": exposed,
        "nets": netlist,
        "project": labels.get("com.docker.compose.project", ""),
        "managed_by": "truenas" if updates.managed_elsewhere(attrs) else None,
    }


def counters(s: dict):
    cpu = (s.get("cpu_stats") or {})
    usage = (cpu.get("cpu_usage") or {})
    nets = s.get("networks")
    mem = s.get("memory_stats") or {}
    detail = mem.get("stats") or {}
    cache = detail.get("inactive_file", detail.get("total_inactive_file", 0)) or 0
    return {
        "mem": max(0, (mem.get("usage") or 0) - cache) if mem.get("usage") else None,
        "cpu": usage.get("total_usage") or 0,
        "system": cpu.get("system_cpu_usage") or 0,
        "ncpu": cpu.get("online_cpus") or len(usage.get("percpu_usage") or []) or 1,
        "net": sum(n.get("rx_bytes", 0) + n.get("tx_bytes", 0) for n in nets.values()) if nets else None,
    }


class DockerDriver:
    """Talks to the real Docker daemon (via /var/run/docker.sock).

    Container state comes from Docker's event stream, so the idle checks don't
    have to ask Docker about every app every few seconds."""

    EVENTS = ["create", "start", "restart", "die", "stop", "destroy", "rename", "pause", "unpause"]
    STATE_AFTER = {"create": "created", "start": "running", "restart": "running", "die": "exited",
                   "stop": "exited", "pause": "paused", "unpause": "running"}

    def __init__(self):
        self.dk = dockerapi.Docker()
        self._mv, self._mv_time = {}, 0.0
        self.states: dict[str, str] = {}      # container name -> status, kept current by events
        self.events_live = False
        self._described: dict[str, tuple] = {}  # id -> (state, describe()) for the container list
        self._describe_errors = (dockerapi.DockerError, httpx.HTTPError)
        self.version_info, self.system_info = {}, {}

    async def connect(self):
        try:
            v = await self.dk.negotiate()
            info = await self.dk.info()
            self.version_info, self.system_info = v, info
            log.info("Docker %s (API %s) on %s", v.get("Version"), v.get("ApiVersion"),
                     info.get("OperatingSystem", "unknown system"))
        except Exception as e:
            log.error("can't talk to Docker: %s. Is /var/run/docker.sock mounted?", e)

    async def watch(self):
        """Follow Docker's container events; reconnects if the stream drops."""
        backoff = 1
        while True:
            try:
                since = int(time.time()) - 1     # replay anything that happens during the resync
                await self._resync()
                self.events_live = True
                backoff = 1
                async for ev in self.dk.events({"type": ["container"], "event": self.EVENTS}, since=since):
                    attrs = (ev.get("Actor") or {}).get("Attributes") or {}
                    name = attrs.get("name")
                    if not name:
                        continue
                    self._described.pop((ev.get("Actor") or {}).get("ID", ev.get("id")), None)
                    action = ev.get("Action", "")
                    log.debug("docker event: %s %s", action, name)
                    if action == "rename" and attrs.get("oldName"):
                        old = attrs["oldName"].lstrip("/")
                        self.states[name] = self.states.pop(old, "unknown")
                    elif action == "destroy":
                        self.states.pop(name, None)
                    elif action in self.STATE_AFTER:
                        # Taken from the event itself, in order: asking Docker here could
                        # return an answer that's already out of date by the time it arrives.
                        self.states[name] = self.STATE_AFTER[action]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("lost Docker's event stream (%s); retrying in %ds", e or type(e).__name__, backoff)
            self.events_live = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _resync(self):
        cs = await self.dk.list()
        self.states = {n.lstrip("/"): c.get("State", "unknown") for c in cs for n in (c.get("Names") or [])[:1]}

    async def status(self, name: str) -> str:
        try:
            return ((await self.dk.inspect(name)).get("State") or {}).get("Status", "unknown")
        except dockerapi.NotFound:
            return "missing"

    def cached_status(self, name: str):
        """Status without asking Docker, or None if it has to be looked up."""
        if not self.events_live:
            return None
        return self.states.get(name, "missing")

    async def inspect(self, name: str):
        try:
            return describe(await self.dk.inspect(name))
        except dockerapi.NotFound:
            return None

    async def start(self, name: str):
        try:
            await self.dk.start(name)
        except dockerapi.NotFound:
            raise RuntimeError(f"container '{name}' does not exist") from None
        except dockerapi.DockerError as e:
            raise RuntimeError(updates.friendly(e)) from None

    async def stop(self, name: str, timeout: int = 15):
        try:
            await self.dk.stop(name, timeout)
        except dockerapi.NotFound:
            pass

    async def restart(self, name: str, timeout: int = 30):
        try:
            await self.dk.restart(name, timeout)
        except dockerapi.NotFound:
            raise RuntimeError(f"container '{name}' does not exist") from None
        except dockerapi.DockerError as e:
            raise RuntimeError(updates.friendly(e)) from None

    async def health(self, name: str):
        """Run state, for checking that a container stays up after a restart."""
        try:
            st = (await self.dk.inspect(name)).get("State") or {}
        except dockerapi.NotFound:
            return None
        return {"status": st.get("Status"), "started_at": st.get("StartedAt"),
                "restarting": bool(st.get("Restarting")), "exit_code": st.get("ExitCode"),
                "health": (st.get("Health") or {}).get("Status"), "error": st.get("Error") or ""}

    async def list(self):
        """All containers, described. Only containers that changed since the last
        call are inspected again, so an open dashboard costs one call per refresh."""
        summary = await self.dk.list()
        out, seen = [], set()

        async def one(c):
            key = (c.get("State"), c.get("Created"), tuple(c.get("Names") or []))
            hit = self._described.get(c["Id"])
            if hit and hit[0] == key:
                return hit[1]
            try:
                d = describe(await self.dk.inspect(c["Id"]))
            except dockerapi.NotFound:
                return None
            self._described[c["Id"]] = (key, d)
            return d
        results = await asyncio.gather(*(one(c) for c in summary))
        for c, d in zip(summary, results):
            seen.add(c["Id"])
            if d:
                out.append(dict(d))
        for gone in set(self._described) - seen:
            self._described.pop(gone, None)
        return out

    async def check_update(self, name):
        return await updates.check(self.dk, name)

    async def pull(self, ref, on_progress):
        return await updates.pull(self.dk, ref, on_progress)

    async def recreate(self, name, ref):
        return await updates.recreate(self.dk, name, ref)

    async def commit(self, old_id, remove_old_image=True):
        return await updates.commit(self.dk, old_id, remove_old_image)

    async def rollback(self, name, new_id, old_id):
        return await updates.rollback(self.dk, name, new_id, old_id)

    async def image_of(self, name):
        return await updates.image_of(self.dk, name)

    async def restore_tag(self, ref, good_image, bad_image=None):
        return await updates.restore_tag(self.dk, ref, good_image, bad_image)

    async def stats(self, name: str):
        """Cumulative CPU and network counters for a running container."""
        try:
            return counters(await self.dk.stats(name))
        except Exception:
            return None

    async def macvlans(self):
        """Docker networks the host can't reach directly: {name: {driver, parent, subnet}}."""
        now = time.time()
        if now - self._mv_time > 60:
            nets = await self.dk.networks()
            self._mv = {
                n["Name"]: {
                    "driver": n.get("Driver"),
                    "parent": (n.get("Options") or {}).get("parent", ""),
                    "subnet": next(iter((n.get("IPAM") or {}).get("Config") or [{}]), {}).get("Subnet", ""),
                }
                for n in nets if n.get("Driver") in ("macvlan", "ipvlan")
            }
            self._mv_time = now
        return self._mv


class DemoDriver:
    """In-memory stand-in so the dashboard can be tried without Docker."""

    def __init__(self):
        def c(name, image, status, project, published=None, exposed=None, mode="bridge", macvlan_ip=None):
            published = published or {}
            nets = ([{"name": "lan_macvlan", "ip": macvlan_ip}] if macvlan_ip
                    else [{"name": f"{project or name}_default", "ip": "172.18.0.%d" % (len(name) + 1)}])
            return {"name": name, "image": image, "status": status, "network_mode": mode,
                    "published": published, "exposed": sorted(set(exposed or []) | set(published)),
                    "nets": nets, "project": project}
        self.state = {x["name"]: x for x in [
            c("whoami", "traefik/whoami", "exited", "stowaway", exposed=[80]),
            c("jellyfin", "jellyfin/jellyfin", "running", "jellyfin", {8096: 8096}, [8096, 8920]),
            c("code-server", "lscr.io/linuxserver/code-server", "exited", "code", exposed=[8443], macvlan_ip="192.168.1.60"),
            c("grafana", "grafana/grafana", "exited", "monitoring", {3000: 3000}),
            c("homarr", "ghcr.io/homarr-labs/homarr", "running", "homarr", {7575: 7575}),
            c("pihole", "pihole/pihole", "running", "", exposed=[53, 80], mode="host"),
            c("stowaway", "stowaway-stowaway", "running", "stowaway", mode="host"),
            c("broken-app", "example/broken", "exited", "", {8080: 8080}),
            c("tdarr", "ghcr.io/haveagitgat/tdarr", "exited", "tdarr", {8265: 8265}, [8265, 8266]),
            c("tdarr-node-cpu", "ghcr.io/haveagitgat/tdarr_node", "exited", "tdarr"),
            c("tdarr-node-intel", "ghcr.io/haveagitgat/tdarr_node", "exited", "tdarr"),
            c("tdarr-node-nvidia", "ghcr.io/haveagitgat/tdarr_node", "exited", "tdarr"),
        ]}

    async def status(self, name):
        return self.state[name]["status"] if name in self.state else "missing"

    def cached_status(self, name):
        return self.state[name]["status"] if name in self.state else "missing"

    async def connect(self):
        pass

    async def watch(self):
        pass

    async def inspect(self, name):
        return dict(self.state[name]) if name in self.state else None

    async def start(self, name):
        if name not in self.state:
            raise RuntimeError(f"container '{name}' does not exist")
        await asyncio.sleep(2)
        if name == "broken-app":
            raise RuntimeError("container exited right after starting (exit code 1)")
        self.state[name]["status"] = "running"
        self.state[name]["started_at"] = str(time.time())

    async def stop(self, name, timeout=15):
        await asyncio.sleep(1)
        if name in self.state:
            self.state[name]["status"] = "exited"

    async def restart(self, name, timeout=30):
        await self.stop(name)
        await self.start(name)

    async def health(self, name):
        if name not in self.state:
            return None
        st = self.state[name]
        if st["status"] == "running":
            st.setdefault("started_at", str(time.time()))
        return {"status": st["status"], "started_at": st.get("started_at"), "restarting": False,
                "exit_code": 1 if name == "broken-app" else 0, "health": None, "error": ""}

    async def list(self):
        return [dict(v) for v in self.state.values()]

    async def macvlans(self):
        return {"lan_macvlan": {"driver": "macvlan", "parent": "enp3s0", "subnet": "192.168.1.0/24"}}

    # pretend newer versions exist for these
    updatable = {"grafana", "broken-app", "whoami"}

    async def check_update(self, name):
        await asyncio.sleep(0.3)
        ref = self.state[name]["image"] + ":latest"
        if name in self.updatable:
            return {"status": "update", "ref": ref, "remote": "sha256:demo-new-" + name}
        return {"status": "current", "ref": ref, "remote": "sha256:demo"}

    async def pull(self, ref, on_progress):
        total = 180 * 1024 * 1024
        for i in range(1, 31):
            await asyncio.sleep(0.15)
            on_progress({"phase": "downloading", "pct": i * 3, "done_bytes": total * i // 30, "total_bytes": total})
        for i in range(1, 6):
            await asyncio.sleep(0.15)
            on_progress({"phase": "unpacking", "pct": 90 + i * 2, "done_bytes": total, "total_bytes": total})
        on_progress({"phase": "installing", "pct": 100, "done_bytes": total, "total_bytes": total})

    async def recreate(self, name, ref):
        await asyncio.sleep(0.5)
        self.updatable.discard(name)
        return "new-" + name, "old-" + name

    async def commit(self, old_id, remove_old_image=True):
        pass

    async def rollback(self, name, new_id, old_id):
        self.updatable.add(name)

    async def image_of(self, name):
        return "img-" + name

    async def restore_tag(self, ref, good_image, bad_image=None):
        pass

    # fraction of one core, bytes per second
    rates = {"jellyfin": (0.34, 1.2 * 1024 * 1024)}
    _ctr: dict = {}

    async def stats(self, name):
        if self.state.get(name, {}).get("status") != "running":
            return None
        now = time.time()
        cpu, sysc, net, last = self._ctr.get(name, (0.0, 0.0, 0.0, now))
        dt = now - last
        rc, rn = self.rates.get(name, (0.008, 400.0))
        cpu += rc * dt * 1e9
        sysc += dt * 1e9 * 4
        net += rn * dt
        self._ctr[name] = (cpu, sysc, net, now)
        mem_mb = {"jellyfin": 780, "grafana": 190, "code-server": 410, "whoami": 12}.get(name, 150)
        return {"cpu": cpu, "system": sysc, "ncpu": 4, "mem": mem_mb * 1024 * 1024,
                "net": None if self.state[name]["network_mode"] == "host" else net}


driver = DemoDriver() if DEMO else DockerDriver()


def direct_target(info: dict, port: int, macvlans: dict):
    """Where a browser on the LAN can reach the app itself (for "go to the app's
    own address"). host=None means "the server, by whatever name the browser used"."""
    if info["network_mode"] == "host":
        return {"host": None, "port": port}
    ip = next((n["ip"] for n in info["nets"] if n["name"] in macvlans and n["ip"]), None)
    if ip:
        return {"host": ip, "port": port}
    if port in info["published"]:
        return {"host": None, "port": info["published"][port]}
    return {"reason": f"port {port} isn't published on the server and the app isn't on a macvlan network, "
                      "so browsers have no address to reach it directly"}


def upstream_for(info: dict, port: int, macvlans: dict):
    """Where to reach a container's app port from the host network.

    Returns (url, shim) where shim is (parent, ip, driver) when the only way in
    is a macvlan/ipvlan address, which the host needs a helper interface for.
    """
    if info["network_mode"] == "host":
        return f"http://127.0.0.1:{port}", None
    normal = [n for n in info["nets"] if n["name"] not in macvlans]
    if normal and port in info["published"]:
        return f"http://127.0.0.1:{info['published'][port]}", None
    for n in normal:
        if n["ip"]:
            return f"http://{n['ip']}:{port}", None
    for n in info["nets"]:
        if n["name"] in macvlans and n["ip"]:
            mv = macvlans[n["name"]]
            return f"http://{n['ip']}:{port}", (mv["parent"], n["ip"], mv["driver"])
    return None, None


def macvlan_ip(info: dict, macvlans: dict):
    return next((n["ip"] for n in info["nets"] if n["name"] in macvlans and n["ip"]), None)


class Shim:
    """Helper interface so the host can reach macvlan/ipvlan containers.

    Linux blocks traffic between a host and its own macvlan children. A small
    macvlan interface on the host (with a free LAN IP) plus a /32 route per
    container IP gets around that. Rebuilt on demand, gone after a reboot.
    """
    PREFIX = "sw-"

    @staticmethod
    async def ip(*args):
        proc = await asyncio.create_subprocess_exec(
            "ip", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        result = (out + err).decode().strip()
        log.debug("ip %s -> %d %s", " ".join(args), proc.returncode, result[:300].replace("\n", " | "))
        return proc.returncode, result

    def ifname(self, parent: str) -> str:
        return (self.PREFIX + parent)[:15]

    async def existing_path(self, target: str):
        """A helper interface someone else already made (Unraid's "Host access to
        custom networks", or a hand-made macvlan shim from a guide): if the host's
        route to the container already goes through one, nothing needs adding."""
        rc, out = await self.ip("-o", "route", "get", target)
        if rc != 0 or " dev " not in out:
            return None
        dev = out.split(" dev ", 1)[1].split()[0]
        if dev.startswith(self.PREFIX):
            return None
        rc, detail = await self.ip("-d", "-o", "link", "show", "dev", dev)
        return dev if rc == 0 and any(k in detail.split() for k in ("macvlan", "ipvlan")) else None

    async def ensure(self, parent: str, target: str, kind: str):
        if not DEMO:
            other = await self.existing_path(target)
            if other:
                if other not in self.reported:
                    self.reported.add(other)
                    log.info("the server already reaches macvlan containers through %s; using it", other)
                return
        shim_ip = reg.settings.get("macvlan_shim_ip")
        if not shim_ip:
            raise RuntimeError("This container is on a macvlan network. Set a helper IP address "
                               "in Settings so Stowaway can reach it.")
        if not parent:
            raise RuntimeError("Can't tell which network card this macvlan network uses.")
        if DEMO:
            return
        name = self.ifname(parent)
        rc, _ = await self.ip("link", "show", name)
        if rc != 0:
            mode = ["type", "macvlan", "mode", "bridge"] if kind == "macvlan" else ["type", "ipvlan", "mode", "l2"]
            rc, out = await self.ip("link", "add", name, "link", parent, *mode)
            if rc != 0:
                hint = (" Uncomment 'cap_add: - NET_ADMIN' for stowaway in docker-compose.yml, then run 'docker compose up -d'."
                        if "not permitted" in out.lower() else "")
                raise RuntimeError(f"Couldn't create the macvlan helper: {out.rstrip('.')}.{hint}")
            self.quiet(name, parent)          # before it goes live, so it never answers for the server
            log.info("created macvlan helper %s on %s", name, parent)
        elif name not in self.quieted:
            self.quiet(name, parent)          # a helper made by an older version, or before a restart
        await self.set_address(name, shim_ip)
        rc, out = await self.ip("route", "replace", f"{target}/32", "dev", name, "src", shim_ip)
        if rc != 0:
            raise RuntimeError(f"Couldn't add a route to {target}: {out}")

    async def set_address(self, name: str, shim_ip: str):
        """Make sure the helper has exactly the helper IP and is up. A helper left
        over from an earlier run (it survives until the server reboots) may still
        carry an old address, which makes adding routes fail with
        "Invalid prefsrc address"."""
        rc, out = await self.ip("-o", "-4", "addr", "show", "dev", name)
        have = [w.split("/")[0] for line in out.splitlines() for i, w in enumerate(line.split())
                if i and line.split()[i - 1] == "inet"]
        if have != [shim_ip]:
            if have:
                log.info("macvlan helper %s had %s; changing it to %s", name, ", ".join(have), shim_ip)
            await self.ip("addr", "flush", "dev", name)
            rc, out = await self.ip("addr", "add", f"{shim_ip}/32", "dev", name)
            if rc != 0:
                raise RuntimeError(f"Couldn't give the macvlan helper the address {shim_ip}: {out}")
        await self.ip("link", "set", name, "up")

    quieted: set = set()
    reported: set = set()

    def quiet(self, name: str, parent: str):
        """Make the helper (and the card it sits on) answer ARP only for their own
        addresses. Otherwise both claim each other's IPs, and security software on
        the network reports it as ARP spoofing."""
        for ifname in (name, parent):
            try:
                netconf.quiet_arp(ifname)
            except OSError as e:
                log.error("couldn't set ARP behaviour on %s: %s", ifname, e)
                if ifname == name:
                    raise RuntimeError(f"Couldn't configure the macvlan helper safely ({e}).")
        self.quieted.add(name)
        log.info("ARP answers limited to own addresses on %s and %s", name, parent)

    generation = 0

    async def reset(self):
        """Remove helper interfaces (e.g. after the helper IP changed)."""
        self.quieted.clear()
        self.generation += 1
        if DEMO:
            return
        rc, out = await self.ip("-o", "link", "show")
        for line in out.splitlines():
            parts = line.split(": ")
            if len(parts) > 1:
                name = parts[1].split("@")[0]
                if name.startswith(self.PREFIX):
                    await self.ip("link", "del", name)
                    log.info("removed macvlan helper %s", name)


shim = Shim()


# --------------------------------------------------------------------------
# Services and config
# --------------------------------------------------------------------------
class Service:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.last_activity = time.time()
        self.active = 0              # requests currently being proxied
        self.status = "unknown"      # last status seen from Docker
        self.transition = None       # "starting" / "stopping" while in progress
        self.error = None
        self.resolved = None         # upstream worked out from docker inspect
        self.warning = None          # e.g. macvlan helper not set up
        self.via_macvlan = False
        self.start_began = None      # when the current/last start began
        self.start_duration = None   # how long the last successful start took
        self.stats = None            # {"cpu": %, "net": KB/s or None}
        self.sample = None           # previous raw counters, for rates
        self.busy_now = False
        self.last_busy = 0.0
        self.was_forced = False      # held or scheduled on the previous check
        self.auto_start_at = 0.0
        self.skip_until = 0.0        # "Sleep now" pressed during awake hours
        self.update_progress = None  # progress while a new version is installed
        self.direct = None           # {"host", "port"} or {"reason"}: the app's own address
        self.running_since = None    # when it was last seen starting to run
        self.acct_at = None          # last time sleep was credited
        self.own_addrs = set()       # host:port forms of the app's own address, for fixing redirects
        self.update_info = None      # result of the last update check
        self.update_error = None
        self.companion_error = None
        self.lock = asyncio.Lock()
        self.comp_samples: dict = {}  # companion -> previous raw counters
        self.update(cfg)

    def update(self, cfg: dict):
        self.container = cfg.get("container") or self.name
        self.hosts = [h.strip().lower() for h in cfg.get("hosts", []) if h.strip()]
        self.port = int(cfg.get("port", 80))
        self.idle_timeout = int(cfg.get("idle_timeout", 600))
        self.start_timeout = int(cfg.get("start_timeout", 60))
        self.custom_upstream = cfg.get("upstream") or None
        self.link_port = int(cfg["link_port"]) if cfg.get("link_port") else None
        self.start_page = cfg.get("start_page") or None           # None = use Settings default
        rd = cfg.get("ready_delay")
        self.ready_delay = float(rd) if rd not in (None, "") else None
        self.busy_check = bool(cfg.get("busy_check", True))
        self.busy_cpu = float(cfg["busy_cpu"]) if cfg.get("busy_cpu") not in (None, "") else None
        self.busy_net = float(cfg["busy_net"]) if cfg.get("busy_net") not in (None, "") else None
        self.awake_hours = [dict(w) for w in cfg.get("awake_hours") or []]
        ka = cfg.get("keep_awake")
        self.keep_awake = "forever" if ka == "forever" else (float(ka) if ka else None)
        self.open_mode = cfg.get("open_mode") if cfg.get("open_mode") in ("proxy", "direct") else "proxy"
        self.update_on_wake = bool(cfg.get("update_on_wake", False))
        self.update_every = float(cfg.get("update_every", 24))       # hours; 0 = every wake
        self.update_checked = float(cfg.get("update_checked") or 0)
        self.update_skip = cfg.get("update_skip") or None            # version that failed; don't retry it
        self.block_wake = bool(cfg.get("block_wake", False))         # "don't wake": visitors can't start it
        # Companion containers start and sleep together with this app (e.g. tdarr's nodes).
        self.companions = [c for c in dict.fromkeys(cfg.get("companions") or []) if c and c != self.container]

    @property
    def upstream(self):
        return self.custom_upstream or self.resolved

    def to_config(self) -> dict:
        cfg = {
            "container": self.container,
            "link_port": self.link_port,
            "port": self.port,
            "idle_timeout": self.idle_timeout,
            "start_timeout": self.start_timeout,
            "hosts": self.hosts,
        }
        if self.custom_upstream:
            cfg["upstream"] = self.custom_upstream
        if self.start_page:
            cfg["start_page"] = self.start_page
        if self.ready_delay is not None:
            cfg["ready_delay"] = self.ready_delay
        cfg["busy_check"] = self.busy_check
        if self.busy_cpu is not None:
            cfg["busy_cpu"] = self.busy_cpu
        if self.busy_net is not None:
            cfg["busy_net"] = self.busy_net
        if self.awake_hours:
            cfg["awake_hours"] = self.awake_hours
        if self.keep_awake:
            cfg["keep_awake"] = self.keep_awake
        if self.open_mode != "proxy":
            cfg["open_mode"] = self.open_mode
        if self.update_on_wake:
            cfg["update_on_wake"] = True
            cfg["update_every"] = self.update_every
        if self.update_checked:
            cfg["update_checked"] = self.update_checked
        if self.update_skip:
            cfg["update_skip"] = self.update_skip
        if self.block_wake:
            cfg["block_wake"] = True
        if self.companions:
            cfg["companions"] = self.companions
        return cfg

    def to_api(self) -> dict:
        return {
            "name": self.name,
            **self.to_config(),
            "upstream": self.upstream,
            "custom_upstream": self.custom_upstream,
            "status": self.status,
            "transition": self.transition,
            "active": self.active,
            "last_activity": self.last_activity,
            "error": self.error,
            "warning": self.warning,
            "via_macvlan": self.via_macvlan,
            **activity_api(self),
            "https_url": https_url(self),
            "update": self.update_progress,
            "direct": self.direct,
            "update_info": self.update_info,
            "update_error": self.update_error,
            "block_wake": self.block_wake,
            "companions": self.companions,
            "companion_error": self.companion_error,
            "companion_status": {c: driver.cached_status(c) for c in self.companions},
            "maintenance": maint_brief(self.container),
        }


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.services: dict[str, Service] = {}
        self.maintenance: dict[str, dict] = {}   # container name -> {"schedule", "anchor", "last", ...}
        self.settings = {"ignore_user_agents": [], "ignore_ips": [], "macvlan_shim_ip": "",
                         "start_page": "loading_name", "start_page_custom": "", "ready_delay": 0,
                         "busy_cpu": 5.0, "busy_net": 50.0, "timezone": "",
                         "https_enabled": False, "https_port": 8443, "https_domain": "",
                         "cert_source": "selfsigned", "le_email": "", "le_staging": False,
                         "duckdns_token": "", "cloudflare_token": "", "http_challenge_port": 8480,
                         "admin_lan_only": True, "homarr_url": "", "homarr_key": ""}
        self.load()

    def load(self):
        data = {}
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text()) or {}
        self.settings.update(data.get("settings") or {})
        for name, cfg in (data.get("services") or {}).items():
            self.services[name] = Service(name, cfg or {})
        self.maintenance = {n: dict(e) for n, e in (data.get("maintenance") or {}).items() if isinstance(e, dict)}
        log.info("loaded %d service(s) from %s", len(self.services), self.path)

    def save(self):
        data = {
            "settings": self.settings,
            "services": {n: s.to_config() for n, s in self.services.items()},
            "maintenance": self.maintenance,
        }
        write_private(self.path, yaml.safe_dump(data, sort_keys=False))

    def route(self, host: str, server_port, header_port):
        for svc in self.services.values():
            if host in svc.hosts or (host and host == auto_host(svc)):
                return svc
        if https_on() and server_port == self.settings.get("https_port"):
            return None          # the HTTPS port only routes by name
        for port in (server_port, header_port):
            if port:
                for svc in self.services.values():
                    if svc.link_port == port:
                        return svc
        return None

    def companion_of(self, container: str):
        for svc in self.services.values():
            if container in svc.companions:
                return svc
        return None

    def by_container(self, container: str):
        for svc in self.services.values():
            if svc.container == container:
                return svc
        return None

    def port_owner(self, port, exclude=None):
        for svc in self.services.values():
            if svc.name != exclude and port and svc.link_port == port:
                return svc.name
        return None

    def host_owner(self, host: str, exclude: str | None = None):
        for svc in self.services.values():
            if svc.name != exclude and (host in svc.hosts or host == auto_host(svc)):
                return svc.name
        return None

    def is_passive(self, request) -> bool:
        """Requests from dashboards' status checks: never wake or keep awake."""
        ua = request.headers.get("user-agent", "").lower()
        if any(p.strip().lower() in ua for p in self.settings.get("ignore_user_agents", []) if p.strip()):
            return True
        ip = request.client.host if request.client else ""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for net in self.settings.get("ignore_ips", []):
            try:
                if addr in ipaddress.ip_network(net.strip(), strict=False):
                    return True
            except ValueError:
                continue
        return False


def https_on() -> bool:
    return bool(reg.settings.get("https_enabled")) if "reg" in globals() else False


def https_domain() -> str:
    return (reg.settings.get("https_domain") or "").strip().lower()


def auto_host(svc) -> str | None:
    """Name the app gets under the HTTPS domain, e.g. jellyfin.myhome.duckdns.org."""
    if not https_on() or not https_domain():
        return None
    return f"{dns_label(svc.name)}.{https_domain()}"


def https_suffix() -> str:
    port = int(reg.settings.get("https_port") or 443)
    return "" if port == 443 else f":{port}"


def https_url(svc) -> str | None:
    h = auto_host(svc)
    return f"https://{h}{https_suffix()}" if h and certmgr.current() else None


def wanted_names() -> list[str]:
    d = https_domain()
    if not d:
        return []
    return [f"stowaway.{d}"] + [auto_host(s) for s in reg.services.values() if auto_host(s)]


reg = Registry(CONFIG_PATH)
# MACVLAN_HELPER_IP in docker-compose.yml sets the macvlan helper address (and locks the Settings field).
MACVLAN_HELPER_IP = os.environ.get("MACVLAN_HELPER_IP", "").strip()
if MACVLAN_HELPER_IP:
    try:
        if ipaddress.ip_address(MACVLAN_HELPER_IP).version != 4:
            raise ValueError
        reg.settings["macvlan_shim_ip"] = MACVLAN_HELPER_IP
    except ValueError:
        log.error("MACVLAN_HELPER_IP=%s isn't an IPv4 address; ignoring it", MACVLAN_HELPER_IP)
        MACVLAN_HELPER_IP = ""
accounts = AuthStore(CONFIG_PATH.parent / "auth.json")
api_tokens = TokenStore(CONFIG_PATH.parent / "tokens.json")
savings = Savings(CONFIG_PATH.parent / "savings.json", lambda path, text: write_private(path, text))
HOST = host_info()
certmgr = CertManager(CONFIG_PATH.parent, lambda: reg.settings, wanted_names)
_tasks: set[asyncio.Task] = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# --------------------------------------------------------------------------
# Link ports: one listener per controlled container, opened on the fly
# --------------------------------------------------------------------------
class QuietServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to main()."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self):  # older uvicorn versions
        pass

    async def main_loop(self):
        # uvicorn wakes every 0.1 s per port just to refresh the Date header and
        # check for shutdown. With a port per app that adds up, so tick once a
        # second instead (the Date header only has 1-second resolution anyway).
        counter = 0
        while not await self.on_tick(counter):
            await asyncio.sleep(1)
            counter = (counter + 10) % 864000


def bind_socket(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((BIND_HOST, port))
    except OSError:
        s.close()
        raise
    s.listen(128)
    s.set_inheritable(True)
    return s


def port_is_free(port: int) -> bool:
    try:
        bind_socket(port).close()
        return True
    except OSError:
        return False


class Listeners:
    def __init__(self):
        # port -> (server, task, tls) where tls is the certificate fingerprint or None
        self.servers: dict[int, tuple] = {}
        self.enabled = False     # off under the test client
        self.https_error = None
        self.lock = asyncio.Lock()

    async def open(self, port: int, tls=None):
        sock = bind_socket(port)   # raises OSError if something else holds the port
        kw = {}
        if tls:
            cert, key = certmgr.current()
            kw = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
        cfg = uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False,
                             proxy_headers=False, timeout_keep_alive=30,
                             ws="websockets", ws_max_size=64 * 1024 * 1024, **kw)
        server = QuietServer(cfg)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        self.servers[port] = (server, task, tls)
        log.info("listening on port %d%s", port, " (HTTPS)" if tls else "")

    async def close(self, port: int):
        server, task, _ = self.servers.pop(port)
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 10)
        log.info("closed port %d", port)

    def is_ours(self, port) -> bool:
        return port in self.servers

    async def sync(self):
        if not self.enabled:
            return
        async with self.lock:        # one change at a time, or two updates race for a port
            await self._sync()

    async def _sync(self):
        wanted = {DASHBOARD_PORT: None}
        wanted.update({s.link_port: None for s in reg.services.values() if s.link_port})
        self.https_error = None
        if https_on():
            fp = certmgr.fingerprint()
            hp = int(reg.settings.get("https_port") or 8443)
            if fp and hp not in wanted:
                wanted[hp] = fp
        for port in sorted(self.servers):
            if port not in wanted or self.servers[port][2] != wanted[port]:
                await self.close(port)
        for port in sorted(set(wanted) - set(self.servers)):
            try:
                await self.open(port, wanted[port])
            except Exception as e:
                log.error("cannot open port %d: %s", port, e)
                if wanted[port]:
                    self.https_error = f"Couldn't open HTTPS port {port}: {e}"
                for s in reg.services.values():
                    if s.link_port == port:
                        s.error = f"Link port {port} is in use by another program. Pick a different one."

    async def close_all(self):
        for port in list(self.servers):
            await self.close(port)


listeners = Listeners()


# --------------------------------------------------------------------------
# Lifecycle: start, wait until ready, stop, idle reaper
# --------------------------------------------------------------------------
def own_addresses(svc: Service) -> set:
    addrs = set()
    if svc.upstream:
        u = urlsplit(svc.upstream)
        addrs.add(f"{u.hostname}:{u.port or 80}")
    d = svc.direct or {}
    if d.get("port"):
        hosts = [d["host"]] if d.get("host") else local_ips() + ["localhost"]
        addrs |= {f"{h}:{d['port']}" for h in hosts}
    return addrs


async def resolve(svc: Service):
    svc.warning = None
    if svc.custom_upstream:
        u = urlsplit(svc.custom_upstream)
        local = u.hostname in ("127.0.0.1", "localhost", "::1")
        svc.direct = {"host": None if local else u.hostname, "port": u.port or 80}
        svc.own_addrs = own_addresses(svc)
        return
    info = await driver.inspect(svc.container)
    if not info:
        svc.resolved, svc.via_macvlan = None, False
        return
    mv = await driver.macvlans()
    svc.direct = direct_target(info, svc.port, mv)
    svc.resolved, need = upstream_for(info, svc.port, mv)
    log.debug("%s: reachable at %s%s", svc.name, svc.resolved, " (macvlan/ipvlan)" if need else "")
    svc.own_addrs = own_addresses(svc)
    svc.via_macvlan = bool(need)
    if need:
        try:
            await shim.ensure(*need)
        except Exception as e:
            svc.warning = str(e)


async def wait_ready(svc: Service):
    """Wait until the container accepts TCP connections on its port."""
    if DEMO:
        return
    u = urlsplit(svc.upstream)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    deadline = time.time() + svc.start_timeout
    while time.time() < deadline:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 2)
            w.close()
            return
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(0.5)
    raise RuntimeError(f"{svc.name} did not answer on port {port} within {svc.start_timeout}s. "
                       f"Check the app port in its settings.")


START_PAGES = ("please_wait", "starting_service", "loading_name", "countdown", "custom")


def page_style(svc: Service) -> str:
    return svc.start_page or reg.settings.get("start_page") or "loading_name"


def ready_delay(svc: Service) -> float:
    d = svc.ready_delay if svc.ready_delay is not None else reg.settings.get("ready_delay", 0)
    return max(0.0, float(d or 0))


async def check_for_update(svc: Service, force=False):
    """Ask the registry about a newer version; records and returns the result."""
    now = time.time()
    svc.update_checked = now
    reg.save()
    try:
        info = await asyncio.wait_for(driver.check_update(svc.container), 20)
        log.debug("update check for %s: %s", svc.name, info)
    except Exception as e:
        info = {"status": "error", "detail": str(e) or "no answer from the image registry"}
        log.warning("update check for %s failed: %s", svc.name, info["detail"])
    svc.update_info = {**info, "at": now}
    return svc.update_info


async def install_update(svc: Service):
    """If a newer version is due, download it and swap the container.
    Returns (new_id, old_id, info) when swapped, else None. Never raises."""
    if not svc.update_on_wake:
        return None
    if svc.update_every and time.time() - svc.update_checked < svc.update_every * 3600:
        return None
    info = await check_for_update(svc)
    if info["status"] != "update" or info.get("remote") == svc.update_skip:
        return None
    log.info("updating %s to the newest %s", svc.name, info["ref"])
    svc.transition = "updating"
    svc.update_progress = {"phase": "downloading", "pct": 0, "done_bytes": 0, "total_bytes": 0, "ref": info["ref"]}
    old_image = new_image = None
    try:
        old_image = await driver.image_of(svc.container)
        new_image = await driver.pull(info["ref"], lambda p: svc.update_progress.update(p))
        svc.update_progress.update(phase="installing", pct=100)
        new_id, old_id = await driver.recreate(svc.container, info["ref"])
    except Exception as e:
        if old_image and new_image:
            try:
                await driver.restore_tag(info["ref"], old_image, new_image)
            except Exception as e2:
                log.error("couldn't restore the image name for %s: %s", svc.name, e2)
        svc.update_error = f"Couldn't update to the new version ({e}). Still using the current one."
        svc.update_skip = info.get("remote")
        reg.save()
        log.error("update of %s failed: %s", svc.name, e)
        return None
    finally:
        svc.update_progress = None
        svc.transition = "starting"
    return new_id, old_id, {**info, "old_image": old_image, "new_image": new_image}


async def start_companions(svc: Service):
    """Start the app's companion containers. A companion that fails to start is
    logged and shown on the card, but doesn't stop the app itself from opening."""
    async def one(c):
        try:
            if await driver.status(c) != "running":
                log.info("starting %s (companion of %s)", c, svc.name)
                await driver.start(c)
        except Exception as e:
            log.error("couldn't start %s (companion of %s): %s", c, svc.name, e)
            return f"{c}: {e}"
    problems = [p for p in await asyncio.gather(*(one(c) for c in svc.companions)) if p]
    svc.companion_error = "Companion didn't start: " + "; ".join(problems) if problems else None


async def stop_companions(svc: Service):
    async def one(c):
        try:
            if await driver.status(c) == "running":
                log.info("stopping %s (companion of %s)", c, svc.name)
                await driver.stop(c)
        except Exception as e:
            log.error("couldn't stop %s (companion of %s): %s", c, svc.name, e)
    await asyncio.gather(*(one(c) for c in svc.companions))


async def start_and_wait(svc: Service):
    svc.start_began = time.time()
    await driver.start(svc.container)
    if svc.companions:
        spawn(start_companions(svc))      # the app comes first; companions (e.g. worker nodes) follow
    await resolve(svc)   # container IPs only exist once it is running
    if svc.warning:
        raise RuntimeError(svc.warning)
    if not svc.upstream:
        raise RuntimeError(f"can't find a way to reach {svc.container} on port {svc.port}")
    await wait_ready(svc)
    delay = ready_delay(svc)
    if delay:
        log.info("%s is up, waiting %gs before sending visitors to it", svc.name, delay)
        await asyncio.sleep(delay)
    svc.start_duration = time.time() - svc.start_began


async def ensure_running(svc: Service):
    async with svc.lock:
        try:
            if await driver.status(svc.container) != "running":
                log.info("starting %s", svc.container)
                svc.transition, svc.error = "starting", None
                svc.start_began = svc.start_began or time.time()
                swapped = await install_update(svc)
                try:
                    await start_and_wait(svc)
                except Exception as e:
                    if not swapped:
                        svc.error = str(e)
                        svc.status = await driver.status(svc.container)
                        raise
                    # The new version didn't come up: put the previous one back.
                    new_id, old_id, info = swapped
                    swapped = None
                    log.error("%s's new version failed to start (%s); restoring the previous one", svc.name, e)
                    try:
                        await driver.rollback(svc.container, new_id, old_id)
                        try:
                            await driver.restore_tag(info["ref"], info["old_image"], info["new_image"])
                        except Exception as e3:
                            log.error("couldn't restore the image name for %s: %s", svc.name, e3)
                        svc.update_error = f"The new version failed to start ({e}). Went back to the previous version."
                        svc.update_skip = info.get("remote")
                        reg.save()
                        await start_and_wait(svc)
                    except Exception as e2:
                        svc.error = str(e2)
                        svc.status = await driver.status(svc.container)
                        raise
                if swapped:
                    new_id, old_id, info = swapped
                    svc.update_error, svc.update_skip = None, None
                    svc.update_info = {**(svc.update_info or {}), "status": "current", "updated_at": time.time()}
                    reg.save()
                    log.info("%s is now running the newest %s", svc.name, info["ref"])
                    spawn(driver.commit(old_id))
            elif not svc.upstream:
                await resolve(svc)
            svc.status = "running"
            svc.last_activity = time.time()
        finally:
            svc.transition = None
            svc.start_began = None


async def safe_start(svc: Service):
    try:
        await ensure_running(svc)
    except Exception as e:
        log.error("failed to start %s: %s", svc.name, e)


def kick(svc: Service):
    """Begin starting a service in the background, marking it as starting now
    so a status check right after sees "starting" rather than an old error."""
    if not svc.transition:
        svc.transition, svc.error = "starting", None
        svc.start_began = time.time()
    spawn(safe_start(svc))


async def stop_service(svc: Service):
    async with svc.lock:
        log.info("stopping %s", svc.container)
        svc.transition = "stopping"
        try:
            if svc.companions:
                await stop_companions(svc)    # workers first, then the app they depend on
            await driver.stop(svc.container)
        except Exception as e:
            svc.error = str(e)
            log.error("failed to stop %s: %s", svc.name, e)
        finally:
            svc.transition = None
        svc.status = await driver.status(svc.container)


# --------------------------------------------------------------------------
# Activity, keep awake and awake hours
# --------------------------------------------------------------------------
def local_tz():
    for name in (reg.settings.get("timezone"), os.environ.get("TZ"), "UTC"):
        if name:
            try:
                return ZoneInfo(name)
            except Exception:
                continue
    return timezone.utc


def hm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def at(day: datetime, minutes: int) -> datetime:
    return day.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)


def windows(svc: Service, now_ts: float, days_back=1, days_ahead=0):
    """Yield (start_ts, end_ts) for awake-hours windows near now."""
    now = datetime.fromtimestamp(now_ts, local_tz())
    for w in svc.awake_hours:
        a, b = hm(w["from"]), hm(w["to"])
        for off in range(-days_back, days_ahead + 1):
            day = now + timedelta(days=off)
            if day.weekday() not in w["days"]:
                continue
            start = at(day, a)
            end = at(day if b > a else day + timedelta(days=1), b)
            yield start.timestamp(), end.timestamp()


def schedule_end(svc: Service, now: float):
    ends = [e for s0, e in windows(svc, now) if s0 <= now < e]
    return max(ends) if ends else None


def next_window(svc: Service, now: float):
    starts = [s0 for s0, e in windows(svc, now, days_back=0, days_ahead=7) if s0 > now]
    return min(starts) if starts else None


def forced(svc: Service, now: float):
    """Why the app must stay awake right now: ("hold"|"schedule", until) or (None, None)."""
    if svc.keep_awake == "forever":
        return "hold", None
    if svc.keep_awake and svc.keep_awake > now:
        return "hold", svc.keep_awake
    end = schedule_end(svc, now)
    if end and now >= svc.skip_until:
        return "schedule", end
    return None, None


def thresholds(svc: Service):
    cpu = svc.busy_cpu if svc.busy_cpu is not None else float(reg.settings.get("busy_cpu", 5))
    net = svc.busy_net if svc.busy_net is not None else float(reg.settings.get("busy_net", 50))
    return cpu, net


async def sample_activity(svc: Service, now: float):
    cur = await driver.stats(svc.container)
    if not cur:
        svc.stats, svc.sample, svc.busy_now = None, None, False
        return
    cur["t"] = now
    prev, svc.sample = svc.sample, cur
    if not prev or cur["t"] <= prev["t"]:
        return
    dsys = cur["system"] - prev["system"]
    cpu = max(0.0, (cur["cpu"] - prev["cpu"]) / dsys * cur["ncpu"] * 100) if dsys > 0 else 0.0
    net = None
    if cur["net"] is not None and prev["net"] is not None:
        net = max(0.0, (cur["net"] - prev["net"]) / (cur["t"] - prev["t"]) / 1024)
    mem_mb = cur["mem"] / 1048576 if cur.get("mem") is not None else None
    svc.stats = {"cpu": round(cpu, 1), "net": None if net is None else round(net, 1),
                 "mem": None if mem_mb is None else round(mem_mb)}
    log.debug("%s: CPU %.1f%%, network %s, memory %s MB, idle %ds of %ds", svc.name, cpu,
              "n/a" if net is None else f"{net:.1f} KB/s", svc.stats["mem"],
              now - max(svc.last_activity, svc.last_busy), svc.idle_timeout)
    cpu_t, net_t = thresholds(svc)
    busy = cpu >= cpu_t or (net is not None and net >= net_t)
    # Companions count too: tdarr's server is quiet while its nodes transcode.
    total_cpu, total_mem, busy_by = cpu, mem_mb, None
    for c in svc.companions:
        cc = await driver.stats(c)
        if not cc:
            svc.comp_samples.pop(c, None)
            continue
        cc["t"] = now
        cp, svc.comp_samples[c] = svc.comp_samples.get(c), cc
        if not cp or cc["t"] <= cp["t"]:
            continue
        ds = cc["system"] - cp["system"]
        ccpu = max(0.0, (cc["cpu"] - cp["cpu"]) / ds * cc["ncpu"] * 100) if ds > 0 else 0.0
        cnet = None
        if cc["net"] is not None and cp["net"] is not None:
            cnet = max(0.0, (cc["net"] - cp["net"]) / (cc["t"] - cp["t"]) / 1024)
        total_cpu += ccpu
        if cc.get("mem") is not None and total_mem is not None:
            total_mem += cc["mem"] / 1048576
        if ccpu >= cpu_t or (cnet is not None and cnet >= net_t):
            busy, busy_by = True, busy_by or f"{c} ({ccpu:.0f}% CPU)"
    if svc.companions:
        svc.stats["cpu_total"] = round(total_cpu, 1)
        svc.stats["busy_by"] = busy_by
    svc.busy_now = svc.busy_check and busy
    if svc.busy_now:
        svc.last_busy = now
    # Learn what the app (with its companions) uses when idle: that's what sleeping it saves.
    if not busy and svc.active == 0 and svc.running_since and now - svc.running_since > LEARN_AFTER_START:
        savings.learn(svc.name, total_cpu, total_mem)


def activity_api(svc: Service) -> dict:
    now = time.time()
    reason, until = forced(svc, now)
    cpu_t, net_t = thresholds(svc)
    return {
        "idle_since": max(svc.last_activity, svc.last_busy),
        "stats": svc.stats,
        "busy": svc.busy_now,
        "busy_cpu_effective": cpu_t,
        "busy_net_effective": net_t,
        "forced": reason,
        "forced_until": until,
        "next_awake": next_window(svc, now) if svc.awake_hours else None,
    }


STATS_EVERY = float(os.environ.get("STATS_INTERVAL", "15"))   # seconds between CPU/network samples


async def check(svc: Service, now: float):
    cached = driver.cached_status(svc.container)
    svc.status = cached if cached is not None else await driver.status(svc.container)
    if svc.status == "running":
        svc.running_since = svc.running_since or now
    else:
        svc.running_since = None
        if svc.acct_at:
            savings.credit(svc.name, now - svc.acct_at, now)
    svc.acct_at = now
    if isinstance(svc.keep_awake, float) and svc.keep_awake <= now:
        log.info("keep awake for %s ended", svc.name)
        svc.keep_awake = None
        reg.save()
    reason, _ = forced(svc, now)
    if svc.was_forced and not reason:
        svc.last_activity = now          # full idle period after a hold or awake hours end
    svc.was_forced = bool(reason)

    idle_for = now - max(svc.last_activity, svc.last_busy)
    about_to_stop = (not reason and svc.status == "running" and svc.idle_timeout > 0
                     and svc.active == 0 and idle_for > svc.idle_timeout)
    if svc.status == "running":
        # Sample CPU/network every STATS_EVERY seconds, and always just before a stop,
        # so an app that just got busy isn't put to sleep.
        last = svc.sample["t"] if svc.sample else 0
        if now - last >= STATS_EVERY or (about_to_stop and now - last >= REAP_INTERVAL):
            await sample_activity(svc, now)
    else:
        svc.stats, svc.sample, svc.busy_now = None, None, False

    if reason:
        # Start right away; after a failed start, retry every 5 minutes. Not while "don't wake" is on.
        if svc.status != "running" and not svc.block_wake and (not svc.error or now - svc.auto_start_at > 300):
            svc.auto_start_at = now
            log.info("starting %s (%s)", svc.name, "keep awake" if reason == "hold" else "awake hours")
            kick(svc)
        return

    idle_for = now - max(svc.last_activity, svc.last_busy)
    if (svc.status == "running" and svc.idle_timeout > 0
            and svc.active == 0 and idle_for > svc.idle_timeout):
        log.info("%s idle for %ds, stopping", svc.name, idle_for)
        await stop_service(svc)


reaper_state = {"docker_down": False}


async def reaper():
    while True:
        await asyncio.sleep(REAP_INTERVAL)
        now = time.time()
        for svc in list(reg.services.values()):
            if svc.transition:
                continue
            try:
                await check(svc, now)
                if reaper_state["docker_down"]:
                    reaper_state["docker_down"] = False
                    log.info("Docker is reachable again")
            except (httpx.TransportError, OSError) as e:
                # One clear line per outage instead of a traceback every few seconds.
                if not reaper_state["docker_down"]:
                    reaper_state["docker_down"] = True
                    log.error("can't reach Docker (%s); will keep trying", e or type(e).__name__)
                break
            except Exception:
                log.exception("check failed for %s", svc.name)
        try:
            savings.flush()
        except Exception:
            log.exception("couldn't save savings figures")
        if diagnostics.tick():
            reg.settings["debug_until"] = 0
            reg.save()


# --------------------------------------------------------------------------
# App status, as shown to dashboards, Home Assistant and the v1 API
# --------------------------------------------------------------------------
def _short_when(ts: float) -> str:
    d = datetime.fromtimestamp(ts, local_tz())
    today = datetime.now(local_tz()).date()
    clock = d.strftime("%I:%M %p").lstrip("0")
    if d.date() == today:
        return f"today {clock}"
    if (d.date() - today).days == 1:
        return f"tomorrow {clock}"
    if (d.date() - today).days < 7:
        return f"{d.strftime('%a')} {clock}"
    return f"{d.strftime('%b')} {d.day} {clock}"


def maint_brief(container: str):
    """Scheduled restart info for a container, or None if it has no schedule."""
    e = reg.maintenance.get(container) or {}
    sched = e.get("schedule")
    if not sched:
        return None
    nxt = None
    with contextlib.suppress(Exception):
        nxt = maint.next_run(sched, e.get("anchor") or time.time(), local_tz())
    last = e.get("last")
    return {
        "schedule": maint.describe(sched),
        "updates": bool(sched.get("update")),
        "next_restart": nxt,
        "next_restart_text": _short_when(nxt) if nxt else None,
        "running": container in maint_state,
        "phase": (maint_state.get(container) or {}).get("phase"),
        "waiting": e.get("waiting"),
        "last_result": last.get("result") if last else None,
        "last_at": last.get("at") if last else None,
        "last_message": last.get("message") if last else None,
    }


def app_status(svc: "Service") -> dict:
    now = time.time()
    if svc.transition:
        state = svc.transition
    elif svc.status == "running":
        state = "running"
    elif svc.error:
        state = "failed"
    else:
        state = "sleeping"
    reason, until = forced(svc, now)
    sleeps_at = None
    if state == "running" and not reason and svc.idle_timeout > 0 and not svc.active and not svc.busy_now:
        sleeps_at = max(svc.last_activity, svc.last_busy) + svc.idle_timeout
    eta = None
    if state == "starting" and svc.start_began and svc.start_duration:
        eta = max(0.0, svc.start_duration - (now - svc.start_began))
    m = maint_brief(svc.container)
    upd = svc.update_info or {}
    if state == "running":
        if reason == "hold":
            summary = "Awake · kept awake" + (f" until {_short_when(until)}" if until else "")
        elif reason == "schedule":
            summary = f"Awake · awake hours until {_short_when(until)}"
        elif svc.active:
            summary = "Awake · in use"
        elif svc.busy_now:
            summary = "Awake · busy"
        elif sleeps_at:
            summary = f"Awake · sleeps in {max(1, round((sleeps_at - now) / 60))} min"
        else:
            summary = "Awake"
    elif state == "sleeping":
        summary = "Asleep · won't wake" if svc.block_wake else "Asleep"
    elif state == "failed":
        summary = "Failed to start"
    else:
        summary = state.capitalize()
    if m and m["next_restart_text"] and not m["running"]:
        summary += f" · restart {m['next_restart_text']}"
    if m and m["running"]:
        summary += " · maintenance running"
    return {
        "name": svc.name,
        "container": svc.container,
        "controlled": True,
        "state": state,
        "running": state == "running",
        "summary": summary,
        "wake_blocked": svc.block_wake,
        "sleeps_at": sleeps_at,
        "kept_awake": reason == "hold",
        "kept_awake_until": until if reason == "hold" else None,
        "awake_hours_until": until if reason == "schedule" else None,
        "in_use": svc.active,
        "busy": svc.busy_now,
        "cpu": (svc.stats or {}).get("cpu"),
        "memory_mb": (svc.stats or {}).get("mem"),
        "eta": eta,
        "error": svc.error if state == "failed" else None,
        "update": svc.update_progress if state == "updating" else None,
        "update_available": upd.get("status") == "update" and upd.get("remote") != svc.update_skip,
        "link_port": svc.link_port,
        "maintenance": m,
    }


async def container_status(name: str):
    """Status for a container Stowaway doesn't sleep, but restarts on a schedule."""
    m = maint_brief(name)
    if not m:
        return None
    cached = driver.cached_status(name)
    st = cached if cached is not None else await driver.status(name)
    if st == "missing":
        return None
    running = st == "running"
    summary = ("Running" if running else "Stopped")
    if m["running"]:
        summary += " · maintenance running"
    elif m["next_restart_text"]:
        summary += f" · restart {m['next_restart_text']}"
    return {"name": name, "container": name, "controlled": False, "state": "running" if running else "stopped",
            "running": running, "summary": summary, "wake_blocked": False, "maintenance": m}

# --------------------------------------------------------------------------
# Scheduled maintenance: restart containers on a schedule, optionally updating
# them first. Works on any container, not only ones Stowaway puts to sleep.
# --------------------------------------------------------------------------
STABLE_FOR = float(os.environ.get("MAINT_STABLE_SECONDS", "20"))   # must stay up this long
HEALTH_WAIT = float(os.environ.get("MAINT_HEALTH_WAIT", "300"))     # wait for "healthy" up to this
BUSY_RETRY = float(os.environ.get("MAINT_BUSY_RETRY", "600"))       # re-check a busy container after this
maint_state: dict[str, dict] = {}      # container -> live progress of a running job
maint_queue: list[str] = []            # "Run now" requests, in order
maint_wake = asyncio.Event()


def maint_entry(name: str) -> dict:
    return reg.maintenance.setdefault(name, {})


async def busy_reason(name: str, svc: Service | None):
    """Why the container shouldn't be interrupted right now, or None."""
    if svc and svc.active:
        return "someone is using it"
    a = await driver.stats(name)
    if not a:
        return None
    t0 = time.time()
    await asyncio.sleep(5)
    b = await driver.stats(name)
    if not b:
        return None
    dsys = b["system"] - a["system"]
    cpu = max(0.0, (b["cpu"] - a["cpu"]) / dsys * b["ncpu"] * 100) if dsys > 0 else 0.0
    net = None
    if a.get("net") is not None and b.get("net") is not None:
        net = max(0.0, (b["net"] - a["net"]) / max(time.time() - t0, 0.1) / 1024)
    if svc:
        cpu_t, net_t = thresholds(svc)
    else:
        cpu_t, net_t = float(reg.settings.get("busy_cpu", 5)), float(reg.settings.get("busy_net", 50))
    if cpu >= cpu_t:
        return f"busy: using {cpu:.0f}% CPU"
    if net is not None and net >= net_t:
        return f"busy: moving {net:.0f} KB/s over the network"
    return None


async def wait_stable(name: str):
    """After a (re)start: None if the container stays up, else what went wrong.

    With a health check, waits for "healthy". Without one, it must keep running,
    without restarting itself, for STABLE_FOR seconds."""
    first = await driver.health(name)
    if not first:
        return "the container disappeared"
    started = first["started_at"]
    t0 = time.time()
    while True:
        h = await driver.health(name)
        if not h:
            return "the container disappeared"
        if h["restarting"] or (h["status"] == "running" and h["started_at"] != started):
            return "it keeps crashing and restarting"
        if h["status"] != "running":
            why = f" ({h['error']})" if h["error"] else ""
            return f"it stopped right away with exit code {h['exit_code']}{why}"
        waited = time.time() - t0
        if h["health"]:
            if h["health"] == "healthy":
                return None
            if h["health"] == "unhealthy":
                return "its health check reports unhealthy"
            if waited > HEALTH_WAIT:
                return f"it didn't report healthy within {HEALTH_WAIT / 60:.0f} minutes"
        elif waited >= STABLE_FOR:
            return None
        await asyncio.sleep(2)


def remember_bad_version(name: str, svc: Service | None, remote):
    maint_entry(name)["update_skip"] = remote
    if svc:
        svc.update_skip = remote


async def restart_now(name: str, st: dict):
    st.update(phase="restarting", pct=None)
    await driver.restart(name)
    st.update(phase="checking")
    return await wait_stable(name)


async def maintain_update(name: str, svc: Service | None, info: dict, running: bool, st: dict):
    ref = info["ref"]
    old_image = new_image = None
    st.update(phase="downloading", pct=0, ref=ref)
    try:
        old_image = await driver.image_of(name)
        # Downloaded while the app keeps running: it's only down for the swap itself.
        new_image = await driver.pull(ref, lambda p: st.update(p))
    except Exception as e:
        msg = f"Couldn't download the new version ({e})."
        if running:
            problem = await restart_now(name, st)
            return "failed", msg + (f" Restarted the current version, but {problem}." if problem
                                    else " Restarted the current version instead.")
        return "failed", msg

    async def put_back_tag():
        try:
            await driver.restore_tag(ref, old_image, new_image)
        except Exception as e2:
            log.error("couldn't restore the image name for %s: %s", name, e2)

    st.update(phase="installing", pct=100)
    if running:
        await driver.stop(name, timeout=30)
    try:
        new_id, old_id = await driver.recreate(name, ref)
    except Exception as e:
        await put_back_tag()
        remember_bad_version(name, svc, info.get("remote"))
        msg = f"Couldn't install the new version ({e})."
        if running:
            await driver.start(name)
            problem = await wait_stable(name)
            msg += f" The current version was started again, but {problem}." if problem else " Still on the current version."
        return "failed", msg

    st.update(phase="starting", pct=None)
    problem = None
    try:
        await driver.start(name)
    except Exception as e:
        problem = str(e)
    if not problem:
        st.update(phase="checking")
        problem = await wait_stable(name)
    if problem:
        log.error("%s's new version didn't work (%s); putting the previous one back", name, problem)
        st.update(phase="rolling back")
        with contextlib.suppress(Exception):
            await driver.stop(name)
        await driver.rollback(name, new_id, old_id)
        await put_back_tag()
        remember_bad_version(name, svc, info.get("remote"))
        msg = f"The new version didn't work ({problem}), so the previous version was put back."
        if running:
            try:
                await driver.start(name)
                again = await wait_stable(name)
            except Exception as e:
                again = str(e)
            if again:
                return "failed", msg + f" But the previous version didn't come back up either: {again}."
        return "rolled_back", msg

    spawn(driver.commit(old_id))
    maint_entry(name).pop("update_skip", None)
    if svc:
        svc.update_skip, svc.update_error = None, None
        svc.update_info = {**(svc.update_info or {}), "status": "current", "updated_at": time.time()}
    if not running:
        # It was asleep: it was only started to make sure the new version works.
        if not (svc and forced(svc, time.time())[0]):
            await driver.stop(name)
        return "updated", "Updated to the newest version. It was started to check it works, then put back to sleep."
    return "updated", "Updated to the newest version and restarted."


async def maintain(name: str, sched: dict, st: dict):
    """One maintenance job. Returns (result, message);
    result is ok | updated | skipped | failed | rolled_back."""
    svc = reg.by_container(name)
    status = await driver.status(name)
    if status == "missing":
        return "failed", "The container doesn't exist any more."
    running = status in ("running", "restarting")      # restarting = crashing and being restarted by Docker
    if not running and not svc:
        return "skipped", "It wasn't running, so there was nothing to restart."
    note, upd = "", None
    if sched.get("update"):
        st.update(phase="checking for updates")
        try:
            info = await asyncio.wait_for(driver.check_update(name), 30)
        except Exception as e:
            info = {"status": "error", "detail": str(e) or "no answer from the image registry"}
        if info["status"] == "update":
            bad = {maint_entry(name).get("update_skip"), svc.update_skip if svc else None} - {None}
            if info.get("remote") in bad:
                note = "A newer version exists, but it failed last time, so it was skipped."
            else:
                upd = info
        elif info["status"] == "current":
            note = "Already the newest version."
        elif info["status"] == "unsupported":
            note = f"Can't update: {info.get('detail')}."
        else:
            note = f"Couldn't check for updates: {info.get('detail')}."
    if not running and not upd:
        return "skipped", ("Asleep, so there was nothing to restart. " + note).strip()
    if not running and svc and svc.block_wake:
        return "skipped", "It's switched off (don't wake), so the update was left for later."
    if upd:
        log.info("maintenance: updating %s to the newest %s", name, upd["ref"])
        return await maintain_update(name, svc, upd, running, st)
    log.info("maintenance: restarting %s", name)
    t0 = time.time()
    problem = await restart_now(name, st)
    if problem:
        return "failed", f"Restarted, but {problem}."
    return "ok", (f"Restarted ({time.time() - t0:.0f}s). " + note).strip()


async def run_maintenance(name: str, manual: bool = False):
    entry = maint_entry(name)
    sched = entry.get("schedule") or {"update": False}
    svc = reg.by_container(name)
    began = time.time()
    st = maint_state[name] = {"phase": "starting", "pct": None, "since": began, "manual": manual}
    try:
        async with (svc.lock if svc else contextlib.nullcontext()):
            if svc:
                svc.transition = "maintenance"
            try:
                result, msg = await maintain(name, sched, st)
            finally:
                if svc:
                    svc.transition = None
                    svc.error = None
                    with contextlib.suppress(Exception):
                        svc.status = await driver.status(svc.container)
                        svc.sample, svc.stats = None, None
                        if svc.status == "running":
                            svc.running_since = time.time()
                            svc.last_activity = max(svc.last_activity, time.time() - 60)
                            await resolve(svc)
    except Exception as e:
        log.exception("maintenance of %s failed", name)
        result, msg = "failed", updates.friendly(e) or "unexpected error"
    finally:
        maint_state.pop(name, None)
    entry = maint_entry(name)
    entry["last"] = {"at": began, "took": round(time.time() - began), "result": result,
                     "message": msg, "manual": manual}
    reg.save()
    log.info("maintenance of %s: %s - %s", name, result, msg)
    return result, msg


def record(name: str, result: str, msg: str):
    maint_entry(name)["last"] = {"at": time.time(), "took": 0, "result": result, "message": msg, "manual": False}


async def maintenance_tick():
    while maint_queue:
        await run_maintenance(maint_queue[0], manual=True)
        maint_queue.pop(0)
    now, tz = time.time(), local_tz()
    for name, entry in list(reg.maintenance.items()):
        sched = entry.get("schedule")
        if not sched or maint_queue:
            continue
        due = maint.next_run(sched, entry.get("anchor") or now, tz)
        if now < due:
            continue
        late = now - due
        wait = sched.get("busy_wait", 0) * 3600
        if late > max(wait, 3600) + 600:
            # Stowaway (or the server) was off at the scheduled time; don't do it hours late.
            if entry.get("waiting"):
                record(name, "skipped", f"Still {entry['waiting'].split(': ', 1)[-1]} after waiting; will try next time.")
            else:
                record(name, "missed", "Stowaway wasn't running at the scheduled time.")
            entry["anchor"] = now
            entry.pop("waiting", None), entry.pop("retry_at", None)
            reg.save()
            continue
        if now < entry.get("retry_at", 0):
            continue
        if wait > 0:
            why = await busy_reason(name, reg.by_container(name))
            if why:
                if late + BUSY_RETRY <= wait:
                    if entry.get("waiting") != why:
                        log.info("maintenance of %s postponed: %s", name, why)
                    entry["waiting"], entry["retry_at"] = why, time.time() + BUSY_RETRY
                else:
                    record(name, "skipped", f"It was still {why.split(': ', 1)[-1]} after waiting; will try next time.")
                    entry["anchor"] = now
                    entry.pop("waiting", None), entry.pop("retry_at", None)
                reg.save()
                continue
        entry["anchor"] = now          # set first, so a crash mid-job doesn't repeat it
        entry.pop("waiting", None), entry.pop("retry_at", None)
        reg.save()
        await run_maintenance(name)


async def maintenance_loop():
    while True:
        try:
            await maintenance_tick()
        except Exception:
            log.exception("maintenance scheduler")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(maint_wake.wait(), 30)
        maint_wake.clear()


async def macvlan_peers_loop():
    """Give every running macvlan/ipvlan container a way back to the server.

    The helper interface only helps if the server's replies go out through it.
    Apps Stowaway manages get a route when they're resolved; this adds one for
    every other container on those networks too, so e.g. a dashboard like Homarr
    on macvlan can reach Stowaway (and the server) at the helper IP. Only runs
    once a helper IP is set. Routes are cheap and harmless if left behind: the
    helper reaches ordinary devices on the network just as well."""
    routed: dict[str, float] = {}     # container IP -> when its route was last checked
    failed: set[str] = set()          # error messages already logged
    gen = shim.generation
    while True:
        await asyncio.sleep(30)
        if DEMO or not reg.settings.get("macvlan_shim_ip"):
            continue
        if shim.generation != gen:    # helper was rebuilt: its routes are gone
            gen = shim.generation
            routed.clear()
        try:
            mv = await driver.macvlans()
            if not mv:
                continue
            now = time.time()
            for c in await driver.list():
                if c["status"] != "running":
                    continue
                for n in c["nets"]:
                    net = mv.get(n["name"])
                    if not net or not n["ip"] or now - routed.get(n["ip"], 0) < 600:
                        continue
                    try:
                        await shim.ensure(net["parent"], n["ip"], net["driver"])
                        routed[n["ip"]] = now
                        log.debug("route to %s (%s) goes through the macvlan helper", c["name"], n["ip"])
                    except Exception as e:
                        if str(e) not in failed:
                            failed.add(str(e))
                            log.warning("couldn't give %s a route through the macvlan helper: %s", c["name"], e)
        except asyncio.CancelledError:
            raise
        except (httpx.TransportError, OSError):
            pass
        except Exception:
            log.exception("macvlan route check failed")


_started = False


async def startup():
    global _started
    if _started:
        return
    _started = True
    log.info("Stowaway %s starting", VERSION)
    if float(reg.settings.get("debug_until") or 0) > time.time():
        diagnostics.set_debug(True, float(reg.settings["debug_until"]))
    await driver.connect()
    app.state.watch = asyncio.create_task(driver.watch())
    for svc in reg.services.values():
        try:
            svc.status = await driver.status(svc.container)
            await resolve(svc)
        except Exception as e:
            svc.error = str(e)
    _own_samples.append((time.time(), sum(os.times()[:2])))
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5), follow_redirects=False)
    app.state.reaper = asyncio.create_task(reaper())
    app.state.maintenance = asyncio.create_task(maintenance_loop())
    app.state.peers = asyncio.create_task(macvlan_peers_loop())


async def shutdown():
    app.state.peers.cancel()
    app.state.reaper.cancel()
    app.state.maintenance.cancel()
    app.state.watch.cancel()
    with contextlib.suppress(Exception):
        savings.flush(force=True)
    await app.state.http.aclose()


@contextlib.asynccontextmanager
async def lifespan(app):
    # Only used when the app is run directly by uvicorn or a test client;
    # main() below runs startup itself.
    await startup()
    yield
    await shutdown()


app = App(lifespan=lifespan)


# --------------------------------------------------------------------------
# Dashboard + API
# --------------------------------------------------------------------------
_failures: dict[str, list[float]] = {}
MAX_FAILURES, FAIL_WINDOW = 10, 600
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain", ".localhost")
DASHBOARD_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def is_home_network(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local


def lockout_key(ip: str) -> str:
    """IPv6 visitors get a whole /64 each, so group by that."""
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6 and not a.ipv4_mapped:
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    except ValueError:
        pass
    return ip


def split_host(request: Request):
    host, _, hport = (request.headers.get("host") or "").lower().partition(":")
    server_port = (request.scope.get("server") or (None, None))[1]
    return host, server_port, (int(hport) if hport.isdigit() else None)


def dashboard_host_ok(host: str) -> bool:
    """Blocks DNS-rebinding: only names that can only mean this server on the home network."""
    if not host or host in ALLOWED_HOSTS or host == "localhost":
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass
    if "." not in host or host.endswith(LOCAL_SUFFIXES):
        return True
    d = https_domain()
    return bool(d) and host in (d, f"stowaway.{d}")


def is_dashboard(request: Request) -> bool:
    host, server_port, _ = split_host(request)
    if server_port == DASHBOARD_PORT:
        return True
    d = https_domain()
    return (https_on() and bool(d) and server_port == int(reg.settings.get("https_port") or 0)
            and host in (d, f"stowaway.{d}"))


def gate(request: Request):
    """Checks every dashboard request passes before sign-in is even considered."""
    # Only on the dashboard port (or stowaway.<domain> over HTTPS), never on apps' links.
    if not is_dashboard(request):
        raise HTTPException(404)
    host, _, _ = split_host(request)
    if not dashboard_host_ok(host):
        raise HTTPException(403, f"Stowaway doesn't answer to the name '{host}'. Open it by IP address, "
                                 "or add the name to ALLOWED_HOSTS in docker-compose.yml.")
    ip = request.client.host if request.client else ""
    if reg.settings.get("admin_lan_only", True) and not is_home_network(ip):
        raise HTTPException(403, "The Stowaway dashboard is only available on your home network "
                                 "(Settings → HTTPS → 'Only allow the dashboard from my home network').")
    # Changes must come from the dashboard itself, not from another website (CSRF).
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        if request.headers.get("x-stowaway") != "1" or \
                (origin and origin.split("://", 1)[-1].lower() != (request.headers.get("host") or "").lower()):
            raise HTTPException(403, "Request blocked: it didn't come from the Stowaway dashboard.")


def signed_in(request: Request):
    if not AUTH_REQUIRED:
        return "demo"
    accounts.refresh()
    return accounts.session_user(request.cookies.get(COOKIE))


def bearer(request: Request):
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else None


def check_admin(request: Request):
    tok = bearer(request)
    if tok is not None:
        # API token (Home Assistant etc.): only the /api/v1 endpoints, never settings or the account.
        if not is_dashboard(request):
            raise HTTPException(404)
        if not request.url.path.startswith(ADMIN + "/api/v1/"):
            raise HTTPException(403, "API tokens only work with /_stowaway/api/v1/.")
        ip = request.client.host if request.client else ""
        if reg.settings.get("admin_lan_only", True) and not is_home_network(ip):
            raise HTTPException(403, "API access is only allowed from your home network.")
        key = throttle(request)
        entry = api_tokens.check(tok)
        if not entry:
            _failures.setdefault(key, []).append(time.time())
            log.warning("wrong API token from %s", ip or "?")
            raise HTTPException(401, "That API token isn't valid (it may have been revoked).",
                                headers={"WWW-Authenticate": "Bearer"})
        request.state.token = entry["name"]
        return
    gate(request)
    if not signed_in(request):
        raise HTTPException(401, "Please sign in.")


def throttle(request: Request):
    """Refuse when this address has had too many wrong passwords lately."""
    ip = request.client.host if request.client else ""
    key, now = lockout_key(ip), time.time()
    if len(_failures) > 10000:
        for k in [k for k, v in _failures.items() if not v or now - v[-1] > FAIL_WINDOW]:
            _failures.pop(k, None)
    recent = [t for t in _failures.get(key, []) if now - t < FAIL_WINDOW]
    _failures[key] = recent
    if len(recent) >= MAX_FAILURES:
        raise HTTPException(429, "Too many wrong passwords. Try again in 10 minutes.")
    return key


def failed(request: Request, key: str):
    _failures.setdefault(key, []).append(time.time())
    log.warning("wrong dashboard password from %s", request.client.host if request.client else "?")


def set_session(response, request: Request):
    response.set_cookie(COOKIE, accounts.issue(), max_age=SESSION_DAYS * 86400, path=ADMIN,
                        httponly=True, samesite="strict", secure=request.url.scheme == "https")


admin = Router(prefix=ADMIN, dependencies=[check_admin])


class ServiceIn(BaseModel):
    name: str | None = None
    container: str | None = None
    hosts: list[str] | None = None
    port: int | None = None
    idle_timeout: int | None = None
    start_timeout: int | None = None
    upstream: str | None = None
    link_port: int | None = None
    start_page: str | None = None
    ready_delay: float | None = None
    update_on_wake: bool | None = None
    update_every: float | None = None
    open_mode: str | None = None
    busy_check: bool | None = None
    busy_cpu: float | None = None
    busy_net: float | None = None
    awake_hours: list[dict] | None = None
    companions: list[str] | None = None


class HoldIn(BaseModel):
    minutes: float | None = None   # None with forever=False releases the hold
    forever: bool = False


class SettingsIn(BaseModel):
    ignore_user_agents: list[str] = []
    ignore_ips: list[str] = []
    macvlan_shim_ip: str = ""
    start_page: str = "loading_name"
    start_page_custom: str = ""
    ready_delay: float = 0
    busy_cpu: float = 5.0
    busy_net: float = 50.0
    timezone: str = ""
    https_enabled: bool = False
    https_port: int = 8443
    https_domain: str = ""
    cert_source: str = "selfsigned"
    le_email: str = ""
    le_staging: bool = False
    duckdns_token: str | None = None       # None or "" keeps the saved token
    cloudflare_token: str | None = None
    http_challenge_port: int = 8480
    admin_lan_only: bool = True
    homarr_url: str = ""
    homarr_key: str | None = None          # None or "" keeps the saved key


def get_svc(name: str) -> Service:
    svc = reg.services.get(name)
    if not svc:
        raise HTTPException(404, f"no service named '{name}'")
    return svc


def check_page_options(start_page=None, delay=None):
    if start_page and start_page not in START_PAGES:
        raise HTTPException(400, f"unknown start page '{start_page}'")
    if delay is not None and not 0 <= delay <= 300:
        raise HTTPException(400, "ready delay must be between 0 and 300 seconds")


def check_activity(cpu=None, net=None, hours=None):
    if cpu is not None and not 0 <= cpu <= 10000:
        raise HTTPException(400, "CPU threshold must be between 0 and 10000 %")
    if net is not None and not 0 <= net <= 10_000_000:
        raise HTTPException(400, "network threshold must be between 0 and 10,000,000 KB/s")
    if hours is None:
        return None
    if len(hours) > 10:
        raise HTTPException(400, "at most 10 awake-hours entries")
    clean = []
    for w in hours:
        try:
            days = sorted({int(d) for d in w.get("days", [])})
            a, b = hm(str(w["from"])), hm(str(w["to"]))
            ok = days and all(0 <= d <= 6 for d in days) and 0 <= a < 1440 and 0 <= b < 1440 and a != b
        except Exception:
            ok = False
        if not ok:
            raise HTTPException(400, "each awake-hours entry needs at least one day and different start and end times")
        clean.append({"days": days, "from": f"{a // 60:02d}:{a % 60:02d}", "to": f"{b // 60:02d}:{b % 60:02d}"})
    return clean


async def check_companions(companions, container: str, exclude: str | None = None):
    for c in companions or []:
        if c == container:
            raise HTTPException(400, "an app can't be its own companion")
        if c == SELF_NAME:
            raise HTTPException(400, "Stowaway can't be a companion")
        owner = reg.by_container(c)
        if owner and owner.name != exclude:
            raise HTTPException(409, f"'{c}' is managed by Stowaway as an app of its own; disable that first")
        other = reg.companion_of(c)
        if other and other.name != exclude:
            raise HTTPException(409, f"'{c}' is already a companion of '{other.name}'")
        if await driver.status(c) == "missing":
            raise HTTPException(404, f"no container named '{c}'")


def check_conflicts(hosts=None, link_port=None, exclude=None):
    if link_port is not None:
        if not 1 <= link_port <= 65535:
            raise HTTPException(400, "link port must be between 1 and 65535")
        if link_port == DASHBOARD_PORT:
            raise HTTPException(409, f"port {link_port} is the dashboard's port")
        if https_on() and link_port == int(reg.settings.get("https_port") or 0):
            raise HTTPException(409, f"port {link_port} is the HTTPS port")
        owner = reg.port_owner(link_port, exclude)
        if owner:
            raise HTTPException(409, f"port {link_port} is already used by '{owner}'")
        mine = exclude and reg.services[exclude].link_port == link_port
        if listeners.enabled and not mine and link_port not in listeners.servers and not port_is_free(link_port):
            raise HTTPException(409, f"port {link_port} is already in use by another program on this server")
    for h in hosts or []:
        owner = reg.host_owner(h.strip().lower(), exclude)
        if owner:
            raise HTTPException(409, f"host '{h}' is already used by '{owner}'")


NOT_WEB = {21, 22, 23, 25, 53, 67, 68, 110, 123, 137, 138, 139, 143, 161, 445, 465, 587,
           993, 995, 1883, 1900, 3306, 5353, 5432, 6379, 8883, 27017}


def suggest(info: dict, taken: set[int]):
    """Default app port and a free link port for a container."""
    web = [p for p in sorted(info["published"]) if p not in NOT_WEB] or \
          [p for p in info["exposed"] if p not in NOT_WEB]
    app_port = web[0] if web else 80
    base = info["published"].get(app_port) or app_port
    candidate = base + 10000 if base + 10000 < 65536 else 18000
    for _ in range(500):
        if candidate not in taken and (not listeners.enabled or port_is_free(candidate)):
            return app_port, candidate
        candidate += 1
    return app_port, None


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


class AccountIn(BaseModel):
    current_password: str = ""
    username: str | None = None
    new_password: str | None = None


@app.get(ADMIN)
@app.get(ADMIN + "/")
async def dashboard(request: Request):
    gate(request)
    page = "index.html" if signed_in(request) else "login.html"
    return FileResponse(STATIC / page, headers=DASHBOARD_HEADERS)


@app.get(ADMIN + "/auth/state")
async def auth_state(request: Request):
    gate(request)
    accounts.refresh()
    ip = request.client.host if request.client else ""
    return {"setup_needed": AUTH_REQUIRED and accounts.needs_setup,
            "setup_allowed": is_home_network(ip),
            "signed_in": bool(signed_in(request))}


@app.post(ADMIN + "/auth/setup")
async def auth_setup(request: Request, body: LoginIn):
    gate(request)
    accounts.refresh()
    if not accounts.needs_setup:
        raise HTTPException(409, "An account already exists. Sign in instead.")
    ip = request.client.host if request.client else ""
    if not is_home_network(ip):
        raise HTTPException(403, "The first account can only be created from your home network.")
    name = body.username.strip()
    problem = username_problem(name)
    if problem:
        raise HTTPException(400, problem)
    missing = password_problems(body.password)
    if missing:
        raise HTTPException(400, "Password needs " + ", ".join(missing) + ".")
    accounts.create(name, body.password)
    log.info("dashboard account '%s' created from %s", name, ip)
    resp = JSONResponse({"ok": True})
    set_session(resp, request)
    return resp


@app.post(ADMIN + "/auth/login")
async def auth_login(request: Request, body: LoginIn):
    gate(request)
    accounts.refresh()
    if accounts.needs_setup:
        raise HTTPException(409, "No account yet. Create one first.")
    key = throttle(request)
    ok = await asyncio.to_thread(accounts.check, body.username.strip(), body.password)
    if not ok:
        failed(request, key)
        raise HTTPException(401, "Wrong username or password.")
    _failures.pop(key, None)
    resp = JSONResponse({"ok": True})
    set_session(resp, request)
    return resp


@app.post(ADMIN + "/auth/logout")
async def auth_logout(request: Request):
    gate(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path=ADMIN)
    return resp


@admin.post("/api/account")
async def change_account(request: Request, body: AccountIn):
    key = throttle(request)
    ok = await asyncio.to_thread(accounts.check, accounts.username, body.current_password)
    if not ok:
        failed(request, key)
        raise HTTPException(401, "Your current password is wrong.")
    name = (body.username or "").strip() or None
    if name and name != accounts.username:
        problem = username_problem(name)
        if problem:
            raise HTTPException(400, problem)
    else:
        name = None
    if body.new_password:
        missing = password_problems(body.new_password)
        if missing:
            raise HTTPException(400, "New password needs " + ", ".join(missing) + ".")
    if not name and not body.new_password:
        raise HTTPException(400, "Nothing to change.")
    accounts.change(username=name, password=body.new_password or None)
    log.info("dashboard account updated")
    resp = JSONResponse({"ok": True, "username": accounts.username})
    set_session(resp, request)       # keep this browser signed in; others are signed out
    return resp


@admin.get("/api/services")
async def list_services():
    async def refresh(svc):
        if not svc.transition:
            try:
                cached = driver.cached_status(svc.container)
                svc.status = cached if cached is not None else await driver.status(svc.container)
            except Exception as e:
                svc.error = str(e)
    await asyncio.gather(*(refresh(s) for s in reg.services.values()))
    cur = certmgr.current() if https_on() else None
    trusted = bool(cur) and reg.settings.get("cert_source") != "selfsigned"
    return {"now": time.time(), "demo": DEMO, "dashboard_port": DASHBOARD_PORT, "https_trusted": trusted,
            "username": accounts.username if AUTH_REQUIRED else None,
            "services": [s.to_api() for s in reg.services.values()]}


_own_samples: list[tuple[float, float]] = []    # (time, CPU seconds used), first one after startup


def own_usage():
    """Stowaway's own memory, and its CPU over the last ~10 minutes, to show next to what it saves."""
    try:
        rss = int(next(l for l in open("/proc/self/status") if l.startswith("VmRSS")).split()[1]) / 1024
    except (OSError, StopIteration, ValueError):
        return None
    now, cpu = time.time(), sum(os.times()[:2])
    if not _own_samples or now - _own_samples[-1][0] > 60:
        _own_samples.append((now, cpu))
    while len(_own_samples) > 2 and now - _own_samples[1][0] > 600:
        _own_samples.pop(0)
    t0, c0 = _own_samples[0]
    # CPU only once there are a few minutes to average over (start-up would skew it).
    return {"mem_mb": round(rss), "cpu_pct": 100 * (cpu - c0) / (now - t0) if now - t0 > 300 else None}


@admin.get("/api/savings")
async def get_savings():
    asleep, learning, freed_cpu, freed_mem = [], [], 0.0, 0.0
    for svc in reg.services.values():
        b = savings.baseline(svc.name)
        if not b:
            learning.append(svc.name)
        if svc.status != "running" and not svc.transition:
            asleep.append(svc.name)
            if b:
                freed_cpu += b["cpu"]
                freed_mem += b["mem"]

    def summary(p):
        return {"cpu_hours": p["cpu_s"] / 3600, "cycles": p["cpu_s"] * HOST["mhz"] * 1e6,
                "gb_hours": p["mem_mbs"] / 1024 / 3600, "asleep_hours": p["asleep_s"] / 3600}

    return {
        "now": {"asleep": len(asleep), "controlled": len(reg.services),
                "mem_mb": freed_mem, "cpu_pct": freed_cpu,
                "mem_share": freed_mem / HOST["mem_mb"] if HOST["mem_mb"] else None,
                "cpu_share": freed_cpu / (HOST["ncpu"] * 100)},
        "week": summary(savings.period(7)),
        "total": summary(savings.period(None)),
        "since": savings.data["since"],
        "learning": learning,
        "host": HOST,
        "self": own_usage(),
    }


@admin.get("/api/containers")
async def list_containers():
    infos = await driver.list()
    mv = await driver.macvlans()
    taken = {s.link_port for s in reg.services.values() if s.link_port} | {DASHBOARD_PORT}
    out = []
    for info in sorted(infos, key=lambda i: (i["project"] or "~", i["name"])):
        if info["name"] == SELF_NAME:
            continue
        svc = reg.by_container(info["name"])
        app_port, link_port = suggest(info, taken)
        if not svc and link_port:
            taken.add(link_port)
        out.append({
            **info,
            "published": [[c, h] for c, h in sorted(info["published"].items())],
            "controlled_by": svc.name if svc else None,
            "companion_of": (reg.companion_of(info["name"]).name if reg.companion_of(info["name"]) else None),
            "macvlan_ip": macvlan_ip(info, mv),
            "suggested_port": app_port,
            "suggested_link_port": link_port,
        })
    return out


@admin.get("/api/settings")
async def get_settings():
    mv = await driver.macvlans()
    tz = local_tz()
    public = {k: v for k, v in reg.settings.items() if k not in SECRET_SETTINGS}
    return {**public, "macvlan_networks": [{"name": k, **v} for k, v in mv.items()],
            "duckdns_token_set": bool(reg.settings.get("duckdns_token")),
            "cloudflare_token_set": bool(reg.settings.get("cloudflare_token")),
            "homarr_key_set": bool(reg.settings.get("homarr_key")),
            "timezone_effective": str(tz),
            "macvlan_shim_ip_locked": bool(MACVLAN_HELPER_IP),
            "server_time": datetime.now(tz).strftime("%a %H:%M")}


@admin.put("/api/settings")
async def put_settings(body: SettingsIn):
    for net in body.ignore_ips:
        try:
            ipaddress.ip_network(net.strip(), strict=False)
        except ValueError:
            raise HTTPException(400, f"'{net}' is not an IP address or range")
    shim_ip = MACVLAN_HELPER_IP or body.macvlan_shim_ip.strip()
    if shim_ip:
        try:
            if ipaddress.ip_address(shim_ip).version != 4:
                raise ValueError
        except ValueError:
            raise HTTPException(400, f"'{shim_ip}' is not an IPv4 address")
    check_page_options(body.start_page, body.ready_delay)
    check_activity(body.busy_cpu, body.busy_net)
    tzname = body.timezone.strip()
    if tzname:
        try:
            ZoneInfo(tzname)
        except Exception:
            raise HTTPException(400, f"'{tzname}' is not a known time zone (example: America/New_York)")
    domain = body.https_domain.strip().lower().rstrip(".")
    if domain.startswith("*."):
        domain = domain[2:]
    if domain and not DOMAIN_RE.match(domain):
        raise HTTPException(400, f"'{body.https_domain}' doesn't look like a domain name (example: myhome.duckdns.org)")
    if body.cert_source not in CERT_SOURCES:
        raise HTTPException(400, f"unknown certificate source '{body.cert_source}'")
    if body.https_enabled:
        hp = body.https_port
        if not 1 <= hp <= 65535:
            raise HTTPException(400, "HTTPS port must be between 1 and 65535")
        if hp == DASHBOARD_PORT:
            raise HTTPException(409, f"port {hp} is the dashboard's port")
        owner = reg.port_owner(hp)
        if owner:
            raise HTTPException(409, f"port {hp} is already the link port for '{owner}'")
        if listeners.enabled and not listeners.is_ours(hp) and not port_is_free(hp):
            raise HTTPException(409, f"port {hp} is already in use by another program on this server")
        if not domain and body.cert_source != "selfsigned":
            raise HTTPException(400, "enter your domain to use this kind of certificate")
    if body.cert_source == "http" and not 1 <= body.http_challenge_port <= 65535:
        raise HTTPException(400, "HTTP check port must be between 1 and 65535")
    for label, tok in (("DuckDNS token", body.duckdns_token), ("Cloudflare token", body.cloudflare_token)):
        if tok and not re.fullmatch(r"[A-Za-z0-9._-]{8,200}", tok.strip()):
            raise HTTPException(400, f"that {label} doesn't look right; copy it again without spaces")
    homarr_url = body.homarr_url.strip().rstrip("/")
    homarr_key = "".join((body.homarr_key or "").split())
    try:
        if homarr_url:
            homarr_url = dashsetup.check_url(homarr_url, "Homarr's address")
        if homarr_key:
            dashsetup.check_homarr_key(homarr_key)
    except dashsetup.SetupError as e:
        raise HTTPException(400, str(e))
    old = dict(reg.settings)
    changed = shim_ip != reg.settings.get("macvlan_shim_ip", "")
    reg.settings = {
        "ignore_user_agents": [u.strip() for u in body.ignore_user_agents if u.strip()],
        "ignore_ips": [i.strip() for i in body.ignore_ips if i.strip()],
        "macvlan_shim_ip": shim_ip,
        "start_page": body.start_page,
        "start_page_custom": body.start_page_custom.strip()[:200],
        "ready_delay": body.ready_delay,
        "busy_cpu": body.busy_cpu,
        "busy_net": body.busy_net,
        "timezone": tzname,
        "https_enabled": body.https_enabled,
        "https_port": body.https_port,
        "https_domain": domain,
        "cert_source": body.cert_source,
        "le_email": body.le_email.strip(),
        "le_staging": body.le_staging,
        "duckdns_token": body.duckdns_token.strip() if body.duckdns_token else old.get("duckdns_token", ""),
        "cloudflare_token": body.cloudflare_token.strip() if body.cloudflare_token else old.get("cloudflare_token", ""),
        "http_challenge_port": body.http_challenge_port,
        "admin_lan_only": body.admin_lan_only,
        # Clearing Homarr's address forgets its key too.
        "homarr_url": homarr_url,
        "homarr_key": (homarr_key or old.get("homarr_key", "")) if homarr_url else "",
        "debug_until": old.get("debug_until", 0),
    }
    https_keys = ("https_enabled", "https_port", "https_domain", "cert_source", "le_email", "le_staging",
                  "duckdns_token", "cloudflare_token", "http_challenge_port")
    if any(old.get(k) != reg.settings.get(k) for k in https_keys):
        # new settings deserve a fresh attempt, not the 6-hour back-off
        certmgr.error = None
        spawn(apply_https())
    reg.save()
    if changed:
        await shim.reset()
        for svc in reg.services.values():
            await resolve(svc)
    return await get_settings()


@admin.post("/api/services", status_code=201)
async def add_service(body: ServiceIn):
    name = (body.name or body.container or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    if name in reg.services:
        raise HTTPException(409, f"'{name}' is already controlled")
    if name == SELF_NAME:
        raise HTTPException(400, "stowaway can't control itself")
    check_conflicts(body.hosts, body.link_port)
    check_page_options(body.start_page, body.ready_delay)
    if reg.companion_of(body.container or name):
        raise HTTPException(409, f"'{name}' is a companion of '{reg.companion_of(body.container or name).name}'; remove it there first")
    await check_companions(body.companions, body.container or name)
    body.awake_hours = check_activity(body.busy_cpu, body.busy_net, body.awake_hours)
    svc = Service(name, body.model_dump(exclude_none=True))
    svc.status = await driver.status(svc.container)
    if svc.status == "missing":
        raise HTTPException(404, f"no container named '{svc.container}'")
    await resolve(svc)
    reg.services[name] = svc
    reg.save()
    await listeners.sync()
    return svc.to_api()


@admin.patch("/api/services/{name}")
async def update_service(name: str, body: ServiceIn):
    svc = get_svc(name)
    changes = body.model_dump(exclude_unset=True)
    changes.pop("name", None)
    check_conflicts(changes.get("hosts"), changes.get("link_port"), exclude=name)
    check_page_options(changes.get("start_page"), changes.get("ready_delay"))
    if changes.get("companions") is not None:
        await check_companions(changes["companions"], svc.container, exclude=name)
        changes["companions"] = list(dict.fromkeys(changes["companions"]))
    if changes.get("open_mode") is not None and changes["open_mode"] not in ("proxy", "direct"):
        raise HTTPException(400, "open mode must be 'proxy' or 'direct'")
    if changes.get("update_every") is not None and not 0 <= changes["update_every"] <= 720:
        raise HTTPException(400, "update check interval must be between 0 and 720 hours")
    if "awake_hours" in changes or "busy_cpu" in changes or "busy_net" in changes:
        cleaned = check_activity(changes.get("busy_cpu"), changes.get("busy_net"), changes.get("awake_hours"))
        if "awake_hours" in changes:
            changes["awake_hours"] = cleaned or []
    cfg = svc.to_config()
    cfg.update(changes)
    svc.update(cfg)
    svc.error = None
    await resolve(svc)
    reg.save()
    await listeners.sync()
    return svc.to_api()


@admin.delete("/api/services/{name}", status_code=204)
async def delete_service(name: str, start: bool = False):
    svc = get_svc(name)
    del reg.services[name]
    reg.save()
    await listeners.sync()
    if start:
        spawn(driver.start(svc.container))
        for c in svc.companions:
            spawn(driver.start(c))


@admin.post("/api/services/{name}/start", status_code=202)
async def start(name: str):
    svc = get_svc(name)
    if svc.block_wake:
        svc.block_wake = False          # waking it by hand switches it back on
        reg.save()
    kick(svc)
    return svc.to_api()


@admin.post("/api/services/{name}/check-update")
async def check_update_now(name: str):
    svc = get_svc(name)
    await check_for_update(svc)
    return svc.to_api()


@admin.post("/api/services/{name}/retry-update")
async def retry_update(name: str):
    svc = get_svc(name)
    svc.update_skip, svc.update_error, svc.update_checked = None, None, 0.0
    reg.save()
    return svc.to_api()


@admin.post("/api/services/{name}/hold")
async def hold(name: str, body: HoldIn):
    svc = get_svc(name)
    now = time.time()
    if body.forever:
        svc.keep_awake = "forever"
    elif body.minutes:
        if not 1 <= body.minutes <= 60 * 24 * 30:
            raise HTTPException(400, "keep awake must be between 1 minute and 30 days")
        svc.keep_awake = now + body.minutes * 60
    else:
        svc.keep_awake = None
        svc.last_activity = now
    if svc.keep_awake and svc.block_wake:
        svc.block_wake = False          # keeping it awake switches it back on
    reg.save()
    if svc.keep_awake and (svc.status != "running" or svc.transition == "stopping"):
        svc.auto_start_at = now
        kick(svc)
    return svc.to_api()


def sleep_app(svc: Service, block: bool | None = None):
    """Put an app to sleep now. Ends a keep-awake and skips the rest of today's awake
    hours. block=True also stops visitors from waking it until it's switched back on."""
    changed = False
    if svc.keep_awake:
        svc.keep_awake = None
        changed = True
    if block is not None and svc.block_wake != block:
        svc.block_wake = block
        changed = True
        log.info("%s: waking %s", svc.name, "switched off" if block else "allowed again")
    if changed:
        reg.save()
    end = schedule_end(svc, time.time())
    if end:
        svc.skip_until = end
    svc.was_forced = False
    svc.transition = svc.transition or "stopping"
    spawn(stop_service(svc))


def set_block(svc: Service, on: bool):
    if svc.block_wake != on:
        svc.block_wake = on
        reg.save()
        log.info("%s: waking %s", svc.name, "switched off" if on else "allowed again")


@admin.post("/api/services/{name}/stop", status_code=202)
async def stop(name: str, block: bool = False):
    svc = get_svc(name)
    sleep_app(svc, True if block else None)
    return svc.to_api()


class BlockIn(BaseModel):
    on: bool = True


@admin.post("/api/services/{name}/block")
async def block_wake(name: str, body: BlockIn):
    svc = get_svc(name)
    set_block(svc, body.on)
    return svc.to_api()


# ---- scheduled maintenance ----
class MaintIn(BaseModel):
    freq: str = "off"
    time: str = "04:00"
    weekday: int = 6
    mday: int | str = 1
    update: bool = False
    busy_wait: float = 6


def maint_item(name: str, info: dict | None, now: float, tz):
    e = reg.maintenance.get(name) or {}
    sched = e.get("schedule")
    nxt = None
    if sched:
        with contextlib.suppress(Exception):
            nxt = maint.next_run(sched, e.get("anchor") or now, tz)
    return {
        "name": name,
        "image": info["image"] if info else None,
        "status": info["status"] if info else "missing",
        "project": info["project"] if info else None,
        "controlled": bool(reg.by_container(name)),
        "managed_by": info.get("managed_by") if info else None,
        "schedule": sched,
        "summary": maint.describe(sched),
        "next_run": nxt,
        "waiting": e.get("waiting"),
        "last": e.get("last"),
        "bad_version": bool(e.get("update_skip")),
        "progress": maint_state.get(name),
        "queued": name in maint_queue and name not in maint_state,
    }


def maint_target(name: str):
    if name == SELF_NAME or name.endswith(updates.OLD_SUFFIX):
        raise HTTPException(400, "Stowaway can't restart itself")


@admin.get("/api/maintenance")
async def list_maintenance():
    infos = {i["name"]: i for i in await driver.list()}
    now, tz = time.time(), local_tz()
    names = [n for n in infos if n != SELF_NAME and not n.endswith(updates.OLD_SUFFIX)]
    names += [n for n in reg.maintenance if n not in infos and reg.maintenance[n].get("schedule")]
    items = [maint_item(n, infos.get(n), now, tz) for n in names]
    items.sort(key=lambda i: (not i["schedule"], (i["project"] or "~"), i["name"]))
    return {"now": now, "timezone": str(tz), "server_time": datetime.now(tz).strftime("%a %H:%M"),
            "items": items}


@admin.put("/api/maintenance/{name}")
async def set_maintenance(name: str, body: MaintIn):
    maint_target(name)
    entry = reg.maintenance.get(name) or {}
    if body.freq == "off":
        entry.pop("schedule", None)
        entry.pop("waiting", None), entry.pop("retry_at", None)
    else:
        if await driver.status(name) == "missing":
            raise HTTPException(404, f"no container named '{name}'")
        try:
            entry["schedule"] = maint.clean_schedule(body.model_dump())
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        entry["anchor"] = time.time()      # first run is the next scheduled time from now
        entry.pop("waiting", None), entry.pop("retry_at", None)
    if entry:
        reg.maintenance[name] = entry
    else:
        reg.maintenance.pop(name, None)
    reg.save()
    info = await driver.inspect(name)
    return maint_item(name, info, time.time(), local_tz())


@admin.delete("/api/maintenance/{name}", status_code=204)
async def delete_maintenance(name: str):
    if name in maint_state:
        raise HTTPException(409, "it's being worked on right now")
    reg.maintenance.pop(name, None)
    reg.save()


@admin.post("/api/maintenance/{name}/run", status_code=202)
async def run_maintenance_now(name: str):
    maint_target(name)
    if name in maint_state or name in maint_queue:
        raise HTTPException(409, "already running or waiting its turn")
    if await driver.status(name) == "missing":
        raise HTTPException(404, f"no container named '{name}'")
    maint_queue.append(name)
    maint_wake.set()
    return {"queued": True}


@admin.post("/api/maintenance/{name}/retry-update")
async def maintenance_retry_update(name: str):
    e = reg.maintenance.get(name)
    if e:
        e.pop("update_skip", None)
    svc = reg.by_container(name)
    if svc:
        svc.update_skip, svc.update_error = None, None
    reg.save()
    return {"ok": True}


# ---- API tokens (managed from the dashboard; a token can't manage tokens) ----
class TokenIn(BaseModel):
    name: str = ""


@admin.get("/api/tokens")
async def list_tokens():
    return {"tokens": api_tokens.list()}


@admin.post("/api/tokens", status_code=201)
async def create_token(body: TokenIn):
    name = body.name.strip()[:60]
    if not name:
        raise HTTPException(400, "give the token a name, like 'Home Assistant'")
    if len(api_tokens.tokens) >= 50:
        raise HTTPException(400, "that's a lot of tokens; revoke some first")
    entry, token = api_tokens.create(name)
    log.info("API token '%s' created", name)
    return {**entry, "token": token}


@admin.delete("/api/tokens/{token_id}", status_code=204)
async def revoke_token(token_id: str):
    if not api_tokens.revoke(token_id):
        raise HTTPException(404, "no such token")
    log.info("API token %s revoked", token_id)


class HeimdallTileIn(BaseModel):
    container: str
    name: str
    link: str
    base: str


@admin.post("/api/heimdall/tile")
async def heimdall_tile(body: HeimdallTileIn):
    """Create or update this app's Heimdall tile: Stowaway tile type, the app's own
    icon, its Stowaway link and status switched on. An existing tile for the app
    keeps its title and any icon you gave it."""
    (item,) = await dash_items([{"name": body.name, "link": body.link}])
    dashboard = dash_base(body.base)
    dashboard = re.sub(r"/_stowaway$", "", dashboard)
    found = {c["name"]: c for c in await heimdall_containers()}
    c = found.get(body.container)
    if not c:
        raise HTTPException(404, f"{body.container} isn't a Heimdall container")
    if not c["running"]:
        raise HTTPException(409, f"Start {body.container} first.")
    if DEMO:
        return {"ok": True, "message": f"Added {item['name']} to Heimdall with its own icon (demo)."}
    dk = driver.dk
    apps_dir, icons_dir = await heimdall_paths(body.container)
    # The app's own icon, from the dashboard-icons collection.
    icon_note, icon_value = "", ""
    got = await dashsetup.fetch_icon([item["icon"], re.sub(r"[^a-z0-9-]+", "-", item["name"].lower()).strip("-")])
    if got:
        data, ext, slug = got
        fname = f"stowaway-{slug}.{ext}"
        await dk.put_archive(body.container, icons_dir, dashsetup.tar_one(f"{icons_dir}/{fname}", data, None))
        await dk.exec_run(body.container, ["sh", "-c", f"chown abc:abc '{icons_dir}/{fname}' 2>/dev/null || true"])
        icon_value = f"icons/{fname}"
    else:
        icon_note = (f" Couldn't find an icon for {item['name']} in the dashboard-icons collection, so it shows "
                     "the Stowaway icon; upload the app's icon in the tile to change it.")
    params = {"name": item["name"], "title": item["name"][:1].upper() + item["name"][1:], "link": item["link"],
              "dashboard": dashboard, "icon": icon_value}
    script, pfile = f"{apps_dir}/Stowaway/.stowaway-tile.php", f"{apps_dir}/Stowaway/.stowaway-tile.json"

    async def run():
        if not (await dk.exec_run(body.container, ["test", "-f", f"{apps_dir}/Stowaway/app.json"]))[0] == 0:
            return {"result": "not_registered"}
        await dk.put_archive(body.container, f"{apps_dir}/Stowaway", dashsetup.tar_one(script, HEIMDALL_TILE_PHP.encode(), None))
        await dk.put_archive(body.container, f"{apps_dir}/Stowaway", dashsetup.tar_one(pfile, json.dumps(params).encode(), None))
        code, out = await dk.exec_run(body.container, ["php", script, pfile], user="abc")
        await dk.exec_run(body.container, ["rm", "-f", script, pfile])
        line = next((l for l in reversed(out.splitlines()) if l.strip().startswith("{")), "")
        try:
            return json.loads(line)
        except ValueError:
            log.warning("Heimdall tile for %s: %s", item["name"], out.strip()[-500:])
            raise HTTPException(500, f"Heimdall couldn't save the tile: {out.strip()[-300:] or 'no details'}")

    r = await run()
    if r.get("result") == "not_registered":
        await heimdall_install(HeimdallIn({"container": body.container}))      # first time: add the tile type
        r = await run()
    if r.get("result") == "created":
        msg = f"Added a {r['title']} tile to Heimdall"
    elif r.get("result") == "updated":
        msg = f"Updated your {r['title']} tile in Heimdall"
        if r.get("kept_icon"):
            icon_note = " Its icon was kept."
    else:
        raise HTTPException(500, "Heimdall didn't say whether the tile was saved.")
    msg += f": it opens {item['name']} through Stowaway and shows whether it's awake." + icon_note
    log.info("%s", msg)
    return {"ok": True, "message": msg}


# ---- Dashboards: write Stowaway's status into Homarr, Homepage and Glance (only on request) ----
DASH_IMAGES = {"homarr": "homarr", "homepage": "homepage", "glance": "glance"}


async def dash_containers(kind: str) -> list[dict]:
    if DEMO:
        return [{"name": kind, "running": True}]
    key = DASH_IMAGES[kind]
    return [{"name": c["name"], "running": c["status"] == "running"}
            for c in await driver.list()
            if key in (c["image"] or "").lower() or (key != "homepage" and c["name"].lower() == key)]


class DashApp(BaseModel):
    name: str
    link: str


async def dash_items(apps: list) -> list[dict]:
    """The apps the user picked, with their links checked and an icon guessed."""
    if not apps:
        raise HTTPException(400, "Pick at least one app.")
    images = {} if DEMO else {c["name"]: c["image"] for c in await driver.list()}
    out = []
    for a in apps:
        a = a if isinstance(a, dict) else a.model_dump()
        svc = reg.services.get(a.get("name"))
        if not svc:
            raise HTTPException(404, f"{a.get('name')} isn't an app Stowaway manages")
        try:
            link = dashsetup.check_url(a.get("link"), f"The link for {svc.name}")
        except dashsetup.SetupError as e:
            raise HTTPException(400, str(e))
        out.append({"name": svc.name, "link": link,
                    "icon": dashsetup.icon_slug(images.get(svc.container, ""), svc.name)})
    return out


def dash_base(base: str) -> str:
    try:
        return dashsetup.check_url(base, "Stowaway's address")
    except dashsetup.SetupError as e:
        raise HTTPException(400, str(e))


async def dash_target(kind: str, container: str) -> dict:
    found = {c["name"]: c for c in await dash_containers(kind)}
    c = found.get(container)
    if not c:
        raise HTTPException(404, f"{container} isn't a {kind.title()} container")
    if not c["running"]:
        raise HTTPException(409, f"Start {container} first.")
    return {} if DEMO else await driver.dk.inspect(container)


def glance_path(attrs: dict) -> str:
    return dashsetup.config_path(attrs, "--config", "/app/config/glance.yml")


def homepage_path(attrs: dict) -> str:
    d = dashsetup.env_of(attrs).get("HOMEPAGE_CONFIG_DIR") or "/app/config"
    return d.rstrip("/") + "/services.yaml"


@admin.get("/api/dash/{kind}")
async def dash_info(kind: str):
    if kind not in DASH_IMAGES:
        raise HTTPException(404)
    out = {"containers": await dash_containers(kind)}
    if kind == "glance" and not DEMO:
        for c in out["containers"]:
            if c["running"]:
                with contextlib.suppress(Exception):
                    attrs = await driver.dk.inspect(c["name"])
                    data, _ = await driver.dk.get_archive(c["name"], glance_path(attrs))
                    c["included"] = bool(data) and dashsetup.glance_includes(data.decode("utf-8", "replace"))
    return out


class HomepageIn(BaseModel):
    container: str
    base: str
    group: str = "Stowaway"
    apps: list[DashApp]


@admin.post("/api/dash/homepage")
async def dash_homepage(body: HomepageIn):
    items, base = await dash_items(body.apps), dash_base(body.base)
    group = (body.group or "").strip()[:60] or "Stowaway"
    attrs = await dash_target("homepage", body.container)
    block = dashsetup.homepage_block(group, items, base)
    if DEMO:
        return {"ok": True, "message": f"Wrote {len(items)} app(s) to services.yaml (demo)."}
    path = homepage_path(attrs)
    data, info = await driver.dk.get_archive(body.container, path)
    text = (data or b"").decode("utf-8", "replace")
    try:
        new = dashsetup.merge_services(text, block)
    except dashsetup.SetupError as e:
        raise HTTPException(400, str(e))
    backup = path + ".before-stowaway"
    have_backup, _ = await driver.dk.get_archive(body.container, backup)
    if data and not have_backup:
        await dashsetup.write_file(driver.dk, body.container, backup, data, info)
    await dashsetup.write_file(driver.dk, body.container, path, new.encode(), info)
    log.info("wrote %d app(s) to Homepage's services.yaml in %s", len(items), body.container)
    msg = f"Wrote {len(items)} app(s) to the \"{group}\" group in services.yaml."
    if data and not have_backup:
        msg += " Your original was saved as services.yaml.before-stowaway."
    if not dashsetup.on_volume(attrs, path):
        msg += " Note: services.yaml isn't on a volume, so recreating Homepage would undo this."
    return {"ok": True, "message": msg}


class GlanceIn(BaseModel):
    container: str
    base: str
    title: str = "Apps"
    apps: list[DashApp]


@admin.post("/api/dash/glance")
async def dash_glance(body: GlanceIn):
    items, base = await dash_items(body.apps), dash_base(body.base)
    attrs = await dash_target("glance", body.container)
    content = dashsetup.glance_file(items, base, (body.title or "").strip()[:60] or "Apps")
    if DEMO:
        return {"ok": True, "included": False, "message": f"Wrote stowaway.yml with {len(items)} app(s) (demo)."}
    main_cfg = glance_path(attrs)
    data, info = await driver.dk.get_archive(body.container, main_cfg)
    if data is None:
        raise HTTPException(400, f"Couldn't find Glance's config at {main_cfg} in {body.container}.")
    path = posixpath.join(posixpath.dirname(main_cfg), dashsetup.GLANCE_FILE)
    await dashsetup.write_file(driver.dk, body.container, path, content.encode(), info)
    included = dashsetup.glance_includes(data.decode("utf-8", "replace"))
    log.info("wrote %s with %d app(s) in %s", path, len(items), body.container)
    msg = f"Wrote stowaway.yml with {len(items)} app(s)."
    msg += (" Glance picks up the change by itself." if included else
            " One more step: add the line below to glance.yml.")
    if not dashsetup.on_volume(attrs, path):
        msg += " Note: Glance's config folder isn't on a volume, so recreating Glance would undo this."
    return {"ok": True, "included": included, "message": msg}


DEMO_HOMARR = [{"id": "a1", "name": "Jellyfin", "href": "http://192.168.1.2:8096", "pingUrl": None,
                "iconUrl": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/jellyfin.svg", "description": None}]


class HomarrTestIn(BaseModel):
    url: str = ""
    key: str = ""


def homarr_conn(url: str = "", key: str = ""):
    """Homarr's address and key: the ones given, or the ones saved in Settings."""
    url = url.strip() or reg.settings.get("homarr_url", "")
    key = "".join((key or "").split()) or reg.settings.get("homarr_key", "")
    if not url or not key:
        raise HTTPException(409, "Set Homarr's address and API key in Settings first.")
    try:
        return dashsetup.check_url(url, "Homarr's address"), key
    except dashsetup.SetupError as e:
        raise HTTPException(400, str(e))


async def homarr_apps(url: str, key: str) -> list[dict]:
    if DEMO:
        return [dict(a) for a in DEMO_HOMARR]
    try:
        return await dashsetup.homarr_call("GET", url, key, "/api/apps")
    except dashsetup.SetupError as e:
        raise HTTPException(400, str(e))


@admin.post("/api/homarr/test")
async def homarr_test(body: HomarrTestIn):
    """Check Homarr's address and key (the typed ones, or the saved ones)."""
    url, key = homarr_conn(body.url, body.key)
    apps = await homarr_apps(url, key)
    return {"ok": True, "message": f"Connected to Homarr; it has {len(apps)} app(s)."}


@admin.get("/api/homarr/app/{name}")
async def homarr_app(name: str, link: str = ""):
    """Whether Homarr already has this app (matched by link, address and port, or name)."""
    svc = get_svc(name)
    if not reg.settings.get("homarr_url") or not reg.settings.get("homarr_key"):
        return {"configured": False}
    url, key = homarr_conn()
    apps = await homarr_apps(url, key)
    mid = dashsetup.homarr_match(apps, svc.name, link) if link else None
    match = next(({k: a.get(k) for k in ("id", "name", "href", "pingUrl")} for a in apps if a["id"] == mid), None)
    return {"configured": True, "url": url, "match": match}


class HomarrAddIn(BaseModel):
    name: str
    link: str
    base: str


@admin.post("/api/homarr/add")
async def homarr_add(body: HomarrAddIn):
    """Put one app in Homarr: update the matching Homarr app (its name, icon and
    description are kept; link and Ping URL are set), or add it as a new app."""
    (item,) = await dash_items([{"name": body.name, "link": body.link}])
    base = dash_base(body.base)
    url, key = homarr_conn()
    apps = await homarr_apps(url, key)
    _, dot = dashsetup.status_urls(base, item["name"])
    mid = dashsetup.homarr_match(apps, item["name"], item["link"])
    a = next((x for x in apps if x["id"] == mid), None)
    try:
        if a:
            payload = {"id": a["id"], "name": a["name"], "description": a.get("description"),
                       "iconUrl": a.get("iconUrl") or "", "href": item["link"], "pingUrl": dot}
            if not DEMO:
                await dashsetup.homarr_call("PATCH", url, key, f"/api/apps/{quote(a['id'], safe='')}", payload)
            msg = (f"Updated “{a['name']}” in Homarr: it now opens {item['name']} through Stowaway "
                   "and its status dot shows whether it's awake.")
        else:
            payload = {"name": item["name"][:64], "description": "Wakes when opened · managed by Stowaway",
                       "iconUrl": f"https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/{item['icon']}.svg",
                       "href": item["link"], "pingUrl": dot}
            if not DEMO:
                await dashsetup.homarr_call("POST", url, key, "/api/apps", payload)
            msg = (f"Added {item['name']} to Homarr's apps. To show it, edit a board and add an App widget "
                   f"with {item['name']}; turn on its status option for the dot.")
    except dashsetup.SetupError as e:
        raise HTTPException(400, str(e))
    log.info("Homarr: %s", msg)
    return {"ok": True, "updated": bool(a), "message": msg}


# ---- Heimdall: install the Stowaway tile ("enhanced app") into a Heimdall container ----
HEIMDALL_APP = Path(__file__).resolve().parent.parent / "heimdall" / "Stowaway"
HEIMDALL_REGISTER_PHP = r"""<?php
// Registers the Stowaway tile type, like Heimdall's "php artisan register:app Stowaway"
// (which Heimdall 2.8.3 and earlier drop at startup), then works around how
// Heimdall handles icons of locally added tile types.
require '/app/www/vendor/autoload.php';
$app = require '/app/www/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$dir = app_path('SupportedApps/Stowaway');
$details = json_decode(file_get_contents($dir . '/app.json'));
$application = App\Application::find($details->appid);
if ($application) {
    echo "Application already registered - Stowaway\n";
} else {
    $application = App\SupportedApps::saveApp($details, new App\Application);
    echo "Application Added - Stowaway\n";
}
Illuminate\Support\Facades\Storage::disk('public')->put('icons/' . $details->icon, file_get_contents($dir . '/' . $details->icon));
// When a tile is added, Heimdall turns the icon into a full address on Heimdall
// itself and then refuses to download it ("private or reserved IPs"), so the tile
// can't be saved or ends up without an icon. An icon value containing "://" is
// used as is instead. "../storage/icons/x" shows in the add-tile form, and as a
// tile Heimdall prefixes it with /storage/, which browsers resolve to the same
// file. The "#://" is ignored by browsers.
$icon = '../storage/icons/' . $details->icon . '#://';
$application->icon = $icon;
$application->save();
// Repair Stowaway tiles saved without an icon.
$fixed = App\Item::where('appid', $details->appid)->where(function ($q) {
    $q->whereNull('icon')->orWhere('icon', '');
})->update(['icon' => $icon]);
if ($fixed) {
    echo "Added the icon to $fixed existing tile(s)\n";
}
"""


async def heimdall_containers() -> list[dict]:
    if DEMO:
        return [{"name": "heimdall", "running": True}]
    return [{"name": c["name"], "running": c["status"] == "running"}
            for c in await driver.list() if "heimdall" in (c["image"] or "").lower()]


HEIMDALL_TILE_PHP = r"""<?php
// Creates or updates the Heimdall tile for one Stowaway app. Parameters come from
// the JSON file named on the command line. Prints one JSON line with the result.
require '/app/www/vendor/autoload.php';
$app = require '/app/www/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$p = json_decode(file_get_contents($argv[1]));
$details = json_decode(file_get_contents(app_path('SupportedApps/Stowaway/app.json')));
$application = App\Application::find($details->appid);
if (!$application) {
    echo json_encode(['result' => 'not_registered']), "\n";
    exit(0);
}
$items = App\Item::withoutGlobalScope('user_id')->where('type', 0)->orderBy('id')->get();
$find = function ($test) use ($items) {
    foreach ($items as $i) {
        if ($test($i)) {
            return $i;
        }
    }
    return null;
};
// The tile for this app: a Stowaway tile already set up for it, else a tile that
// opens its Stowaway link, else a tile with the app's name (e.g. your old Dozzle tile).
$item = $find(function ($i) use ($p, $details) {
    if ($i->appid !== $details->appid) {
        return false;
    }
    $c = json_decode($i->description ?: '{}');
    return isset($c->app) && strcasecmp((string) $c->app, $p->name) === 0;
}) ?? $find(fn ($i) => rtrim((string) $i->url, '/') === rtrim($p->link, '/'))
   ?? $find(fn ($i) => strcasecmp(trim((string) $i->title), $p->name) === 0);
$config = json_encode(['enabled' => '1', 'override_url' => $p->dashboard, 'app' => $p->name, 'dataonly' => '1']);
$generic = fn ($icon) => !$icon || strpos($icon, 'stowaway.svg') !== false;
if ($item) {
    $keep = !$generic($item->icon);
    $item->update([
        'url' => $p->link,
        'appid' => $details->appid,
        'class' => $application->class,
        'description' => $config,
        'icon' => $keep ? $item->icon : ($p->icon ?: $application->icon),
    ]);
    echo json_encode(['result' => 'updated', 'title' => $item->title, 'kept_icon' => $keep]), "\n";
} else {
    $user = App\User::where('public_front', true)->first() ?? App\User::orderBy('id')->first();
    $item = App\Item::create([
        'title' => $p->title,
        'url' => $p->link,
        'colour' => '#161b1f',
        'icon' => $p->icon ?: $application->icon,
        'description' => $config,
        'pinned' => 1,
        'order' => 0,
        'type' => 0,
        'class' => $application->class,
        'appid' => $details->appid,
        'user_id' => $user ? $user->id : 0,
    ]);
    $item->parents()->sync([0]);
    echo json_encode(['result' => 'created', 'title' => $item->title]), "\n";
}
"""


async def heimdall_paths(container: str) -> tuple[str, str]:
    """Heimdall's app folder and icon folder (following its links into /config)."""
    code, out = await driver.dk.exec_run(container, ["sh", "-c",
        "test -f /app/www/artisan && readlink -f /app/www/app/SupportedApps"
        " && mkdir -p /app/www/storage/app/public/icons && readlink -f /app/www/storage/app/public/icons"])
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if code != 0 or len(lines) < 2:
        raise HTTPException(400, f"{container} doesn't look like the linuxserver Heimdall image, so this can't be "
                                 "done automatically. See heimdall/README.md in the Stowaway project for doing it by hand.")
    return lines[0], lines[1]


class HeimdallIn(BaseModel):
    container: str


@admin.get("/api/heimdall")
async def heimdall_list():
    return {"containers": await heimdall_containers(), "available": HEIMDALL_APP.is_dir() or DEMO}


@admin.post("/api/heimdall/install")
async def heimdall_install(body: HeimdallIn):
    """Copy the Stowaway tile into Heimdall's app folder and register it. In the
    linuxserver image that folder is on Heimdall's /config volume, so it stays
    through Heimdall updates; Heimdall's app-list sync leaves local apps alone."""
    found = {c["name"]: c for c in await heimdall_containers()}
    c = found.get(body.container)
    if not c:
        raise HTTPException(404, f"{body.container} isn't a Heimdall container")
    if not c["running"]:
        raise HTTPException(409, f"Start {body.container} first; the tile can only be installed while Heimdall runs.")
    if DEMO:
        return {"ok": True, "message": "Installed (demo)."}
    if not HEIMDALL_APP.is_dir():
        raise HTTPException(500, "This copy of Stowaway doesn't include the Heimdall tile files.")
    dk = driver.dk
    code, out = await dk.exec_run(body.container, ["sh", "-c", "readlink -f /app/www/app/SupportedApps && test -f /app/www/artisan"])
    if code != 0:
        raise HTTPException(400, f"{body.container} doesn't look like the linuxserver Heimdall image, so the tile "
                                 "can't be added automatically. See heimdall/README.md in the Stowaway project "
                                 "for adding it by hand.")
    apps_dir = out.strip().splitlines()[0].strip()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(HEIMDALL_APP), arcname="Stowaway",
                filter=lambda ti: None if ti.name.endswith((".md", "__pycache__")) else ti)
    await dk.put_archive(body.container, apps_dir, buf.getvalue())
    # Heimdall runs as user "abc"; it needs to own its files. Harmless if that user doesn't exist.
    await dk.exec_run(body.container, ["sh", "-c", f"chown -R abc:abc '{apps_dir}/Stowaway' 2>/dev/null || true"])
    # Register through Heimdall's own code. Its "artisan register:app" command is
    # missing in Heimdall 2.8.3 and earlier (linuxserver/Heimdall#1606), and the icon
    # needs adjusting anyway (see HEIMDALL_REGISTER_PHP).
    script = f"{apps_dir}/Stowaway/.stowaway-register.php"
    await dk.put_archive(body.container, f"{apps_dir}/Stowaway", dashsetup.tar_one(
        script, HEIMDALL_REGISTER_PHP.encode(), None))
    code, out = await dk.exec_run(body.container, ["php", script], user="abc")
    if code != 0 and "Application" not in out:
        code, out = await dk.exec_run(body.container, ["php", script])
    await dk.exec_run(body.container, ["rm", "-f", script])
    if code != 0:
        log.warning("Heimdall tile install in %s: %s", body.container, out.strip()[-500:])
        raise HTTPException(500, f"Heimdall couldn't register the tile: {out.strip()[-300:] or 'no details'}")
    updated = "already registered" in out
    # PHP caches compiled code and doesn't look for changes; restart it so the new files are used.
    await dk.exec_run(body.container, ["sh", "-c", "s6-svc -r /run/service/svc-php-fpm 2>/dev/null || true"])
    log.info("Stowaway tile %s in Heimdall (%s)", "updated" if updated else "installed", body.container)
    msg = "Updated the Stowaway tile type in Heimdall." if updated else "Added the Stowaway tile type to Heimdall."
    fixed = re.search(r"Added the icon to (\d+)", out)
    if fixed:
        msg += f" Also gave {fixed.group(1)} existing tile(s) their icon."
    return {"ok": True, "updated": updated, "message": msg}


# ---- v1 API: stable endpoints for Home Assistant and other automation ----
class SleepIn(BaseModel):
    block: bool | None = None


class KeepAwakeIn(BaseModel):
    minutes: float | None = None
    forever: bool = False


def _with_link(item: dict, request: Request) -> dict:
    host = split_host(request)[0] or "localhost"
    if item.get("link_port"):
        item["link"] = f"http://{host}:{item['link_port']}"
    return item


async def _v1_item(name: str, request: Request):
    svc = reg.services.get(name) or reg.by_container(name)
    if svc:
        fresh(svc)
        return svc, _with_link(app_status(svc), request)
    item = await container_status(name)
    if item:
        return None, item
    raise HTTPException(404, f"no app or scheduled container named '{name}'")


@admin.get("/api/v1/info")
async def v1_info():
    asleep = [s for s in reg.services.values() if s.status != "running" and not s.transition]
    freed_mem = sum((savings.baseline(s.name) or {}).get("mem", 0) for s in asleep)
    freed_cpu = sum((savings.baseline(s.name) or {}).get("cpu", 0) for s in asleep)
    week = savings.period(7)
    own = own_usage() or {}
    return {"version": VERSION, "apps": len(reg.services), "asleep": len(asleep),
            "awake": len(reg.services) - len(asleep),
            "scheduled_restarts": sum(1 for e in reg.maintenance.values() if e.get("schedule")),
            "memory_freed_mb": round(freed_mem), "cpu_freed_percent": round(freed_cpu, 2),
            "cpu_hours_saved_7d": round(week["cpu_s"] / 3600, 3),
            "stowaway_memory_mb": own.get("mem_mb")}


@admin.get("/api/v1/apps")
async def v1_apps(request: Request):
    items = []
    for svc in reg.services.values():
        fresh(svc)
        items.append(_with_link(app_status(svc), request))
    for name in reg.maintenance:
        if not reg.by_container(name):
            c = await container_status(name)
            if c:
                items.append(c)
    return {"apps": items}


@admin.get("/api/v1/apps/{name}")
async def v1_app(request: Request, name: str):
    return (await _v1_item(name, request))[1]


def _need_svc(svc, name):
    if not svc:
        raise HTTPException(409, f"'{name}' isn't put to sleep by Stowaway (it only has a restart schedule)")
    return svc


@admin.post("/api/v1/apps/{name}/wake")
async def v1_wake(request: Request, name: str):
    svc = _need_svc((await _v1_item(name, request))[0], name)
    set_block(svc, False)                  # waking on purpose switches it back on
    svc.last_activity = time.time()
    if svc.status != "running" or svc.transition:
        kick(svc)
    return _with_link(app_status(svc), request)


@admin.post("/api/v1/apps/{name}/sleep")
async def v1_sleep(request: Request, name: str, body: SleepIn):
    svc = _need_svc((await _v1_item(name, request))[0], name)
    sleep_app(svc, body.block)
    return _with_link(app_status(svc), request)


class PowerIn(BaseModel):
    on: bool = True
    block: bool | None = None


@admin.post("/api/v1/apps/{name}/power")
async def v1_power(request: Request, name: str, body: PowerIn):
    """One endpoint for both directions, for Home Assistant's RESTful switch."""
    if body.on:
        return await v1_wake(request, name)
    return await v1_sleep(request, name, SleepIn({"block": body.block}))


@admin.post("/api/v1/apps/{name}/block")
async def v1_block(request: Request, name: str, body: BlockIn):
    svc = _need_svc((await _v1_item(name, request))[0], name)
    set_block(svc, body.on)
    return _with_link(app_status(svc), request)


@admin.post("/api/v1/apps/{name}/keep-awake")
async def v1_keep_awake(request: Request, name: str, body: KeepAwakeIn):
    svc = _need_svc((await _v1_item(name, request))[0], name)
    await hold(svc.name, HoldIn({"minutes": body.minutes, "forever": body.forever}))
    return _with_link(app_status(svc), request)


@admin.post("/api/v1/apps/{name}/restart", status_code=202)
async def v1_restart(request: Request, name: str):
    svc, item = await _v1_item(name, request)
    container = svc.container if svc else name
    await run_maintenance_now(container)
    return {"queued": True, "container": container}

async def apply_https(force=False):
    await certmgr.ensure(force=force)
    await listeners.sync()


@admin.get("/api/cert")
async def cert_status():
    st = certmgr.status()
    hp = int(reg.settings.get("https_port") or 8443)
    st.update({
        "https_enabled": https_on(),
        "listening": listeners.is_ours(hp) and bool(listeners.servers[hp][2]),
        "listen_error": listeners.https_error,
        "dashboard_url": f"https://stowaway.{https_domain()}{https_suffix()}{ADMIN}" if https_domain() else None,
    })
    return st


# ---- diagnostics ----
class DebugIn(BaseModel):
    on: bool = False


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, local_tz()).strftime("%Y-%m-%d %H:%M:%S") if ts else "-"


async def build_report(private: bool = True) -> str:
    """A plain-text snapshot of everything useful for finding a problem. Secrets are
    always removed; with `private`, the domain, email, username and public IPs too."""
    now = time.time()
    out = []
    w = out.append

    def section(title):
        w("")
        w(f"== {title} " + "=" * max(3, 60 - len(title)))

    w(f"Stowaway diagnostic report, made {_fmt_ts(now)} ({local_tz()})")
    w(f"Stowaway {VERSION}{' (demo mode)' if DEMO else ''}")

    section("System")
    u = os.uname()
    w(f"Kernel: {u.sysname} {u.release} ({u.machine})")
    w(f"Python: {sys.version.split()[0]}")
    w(f"Server: {HOST['ncpu']} CPU cores, {HOST['mem_mb']} MB memory, {HOST['mhz']} MHz")
    own = own_usage() or {}
    w(f"Stowaway uses: {own.get('mem_mb')} MB, CPU {own.get('cpu_pct') if own.get('cpu_pct') is None else round(own['cpu_pct'], 3)}%")
    w(f"Running for: {fmt_secs(now - _own_samples[0][0]) if _own_samples else '?'}")
    w(f"Detailed logging: {'on until ' + _fmt_ts(diagnostics.debug_until) if diagnostics.debug_until > now else ('on (LOG_LEVEL)' if diagnostics.debug_on() else 'off')}")
    w(f"Dashboard port: {DASHBOARD_PORT}   Reap interval: {REAP_INTERVAL}s   Stats interval: {STATS_EVERY:g}s")

    section("Docker")
    if DEMO:
        w("demo mode: no Docker")
    else:
        v, info = getattr(driver, "version_info", {}), getattr(driver, "system_info", {})
        w(f"Engine: {v.get('Version')} (API {v.get('ApiVersion')}, using {driver.dk.api_version})")
        w(f"System: {info.get('OperatingSystem')} | kernel {info.get('KernelVersion')} | {info.get('Architecture')}")
        w(f"Storage: {info.get('Driver')} | cgroup v{info.get('CgroupVersion')} | containers {info.get('Containers')} "
          f"({info.get('ContainersRunning')} running)")
        w(f"Event stream: {'connected' if driver.events_live else 'NOT connected (status is looked up each time)'}")

    section("Settings")
    hidden = set(SECRET_SETTINGS)
    for k, val in sorted(reg.settings.items()):
        if k in hidden:
            val = "(set)" if val else "(not set)"
        w(f"{k}: {val}")

    section("Ports")
    for port, (server, task, tls) in sorted(listeners.servers.items()):
        owner = "dashboard" if port == DASHBOARD_PORT else next(
            (s.name for s in reg.services.values() if s.link_port == port), "HTTPS" if tls else "?")
        w(f"{port}: {owner}{' (HTTPS)' if tls else ''}{' - stopped!' if task.done() else ''}")
    if listeners.https_error:
        w(f"HTTPS error: {listeners.https_error}")

    section(f"Apps under Stowaway ({len(reg.services)})")
    for svc in reg.services.values():
        reason, until = forced(svc, now)
        w(f"[{svc.name}] container={svc.container} status={svc.status} transition={svc.transition}")
        w(f"  link port {svc.link_port} -> app port {svc.port} via {svc.upstream} "
          f"{'(macvlan)' if svc.via_macvlan else ''} open_mode={svc.open_mode} hosts={svc.hosts}")
        w(f"  idle timeout {svc.idle_timeout}s, last used {fmt_secs(now - svc.last_activity)} ago, "
          f"in use {svc.active}, busy={svc.busy_now} stats={svc.stats}")
        w(f"  busy check={svc.busy_check} thresholds={thresholds(svc)} keep awake={svc.keep_awake} "
          f"forced={reason} awake hours={svc.awake_hours}")
        w(f"  updates on wake={svc.update_on_wake} every {svc.update_every}h; last check "
          f"{(svc.update_info or {}).get('status')} {(svc.update_info or {}).get('detail') or ''}")
        for label, val in (("WARNING", svc.warning), ("ERROR", svc.error), ("UPDATE ERROR", svc.update_error)):
            if val:
                w(f"  {label}: {val}")
        b = savings.baseline(svc.name)
        w("  idle baseline: " + ("learning" if not b else f"{b['cpu']:.2f}% CPU, {b['mem']:.0f} MB"))

    section("Scheduled maintenance")
    if not reg.maintenance:
        w("none")
    for name, e in reg.maintenance.items():
        sched = e.get("schedule")
        w(f"[{name}] {maint.describe(sched)}"
          + (f", busy wait {sched.get('busy_wait')}h" if sched else ""))
        if e.get("waiting"):
            w(f"  waiting: {e['waiting']}")
        if e.get("last"):
            L = e["last"]
            w(f"  last: {_fmt_ts(L.get('at'))} {L.get('result')} - {L.get('message')}")
        if e.get("update_skip"):
            w("  a failed version is being skipped")
    if maint_state:
        w(f"running now: {maint_state}")

    section("Containers on this server")
    try:
        mv = await driver.macvlans()
        for c in sorted(await driver.list(), key=lambda c: c["name"]):
            nets = ", ".join(f"{n['name']}={n['ip'] or '-'}" for n in c["nets"])
            pub = ", ".join(f"{h}->{cp}" for cp, h in sorted(c["published"].items()))
            w(f"{c['name']}: {c['status']} | {c['image']} | net {c['network_mode']} [{nets}]"
              + (f" | ports {pub}" if pub else "") + (f" | {c['managed_by']}" if c.get("managed_by") else ""))
        section("Macvlan / ipvlan networks")
        for n, d in mv.items():
            w(f"{n}: {d['driver']} on {d['parent'] or '?'} {d['subnet']}")
        if not mv:
            w("none")
    except Exception as e:
        w(f"couldn't list containers: {e}")

    if not DEMO:
        section("Network helper (host side)")
        rc, links = await shim.ip("-d", "-o", "link", "show")
        rows = [ln for ln in links.splitlines() if "macvlan" in ln or "ipvlan" in ln or Shim.PREFIX in ln]
        w("\n".join(re.sub(r"\s+", " ", r)[:240] for r in rows) or "no macvlan/ipvlan interfaces on the host")
        rc, routes = await shim.ip("-o", "route", "show")
        rows = [r for r in routes.splitlines() if Shim.PREFIX in r or "shim" in r or "vhost" in r]
        if rows:
            w("routes: " + " | ".join(rows))

    section("Certificate")
    try:
        st = await cert_status()
        for k in ("https_enabled", "source", "has_cert", "names", "not_after", "issuer", "self_signed",
                  "missing_names", "listening", "listen_error", "error"):
            w(f"{k}: {_fmt_ts(st[k]) if k == 'not_after' and st[k] else st.get(k)}")
        if st.get("log"):
            w("certbot output (last lines):")
            w(st["log"])
    except Exception as e:
        w(f"couldn't read certificate status: {e}")

    section("Recent warnings and errors")
    probs = diagnostics.recent_problems(30)
    w("\n".join(f"{_fmt_ts(p['at'])} {p['level']} {p['message']}" for p in reversed(probs)) or "none")

    section("Recent log")
    w("\n".join(diagnostics.recent_lines(400)))

    text = diagnostics.redact("\n".join(str(x) for x in out))
    return hide(text) if private else text


def hide(text: str) -> str:
    words = []
    d = https_domain()
    if d:
        words.append((d, "<your-domain>"))
    if reg.settings.get("le_email"):
        words.append((reg.settings["le_email"], "<email>"))
    if AUTH_REQUIRED and accounts.username:
        words.append((accounts.username, "<user>"))
    return diagnostics.hide_personal(text, words)


def fmt_secs(s: float) -> str:
    s = int(max(0, s))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m {s}s"


@admin.get("/api/diagnostics")
async def diagnostics_state():
    return {
        "version": VERSION,
        "debug_until": diagnostics.debug_until if diagnostics.debug_until > time.time() else 0,
        "debug_forced": diagnostics._forced_level == "DEBUG",
        "problems": [{**p, "message": hide(p["message"])} for p in diagnostics.recent_problems(6)],
        "log_bytes": sum(f.stat().st_size for f in diagnostics.log_files()),
        "issue_url": f"{REPO_URL}/issues/new",
        "summary": system_summary(),
    }


def system_summary() -> str:
    if DEMO:
        return f"Stowaway {VERSION} (demo mode)"
    v, info = driver.version_info or {}, driver.system_info or {}
    return (f"Stowaway {VERSION} · Docker {v.get('Version', '?')} (API {v.get('ApiVersion', '?')}) · "
            f"{info.get('OperatingSystem', '?')} · {info.get('Architecture', os.uname().machine)}")


@admin.post("/api/diagnostics/debug")
async def diagnostics_debug(body: DebugIn):
    diagnostics.set_debug(body.on)
    reg.settings["debug_until"] = diagnostics.debug_until
    reg.save()
    return await diagnostics_state()


@admin.get("/api/diagnostics/report")
async def diagnostics_report(private: bool = True, download: bool = False):
    text = await build_report(private)
    stamp = datetime.now(local_tz()).strftime("%Y%m%d-%H%M")
    if not download:
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.txt", text)
        for f in diagnostics.log_files():
            try:
                body = diagnostics.redact(f.read_text(encoding="utf-8", errors="replace"))
                z.writestr(f"logs/{f.name}", hide(body) if private else body)
            except OSError:
                pass
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="stowaway-diagnostics-{stamp}.zip"',
                             "Cache-Control": "no-store"})


@admin.post("/api/cert/renew", status_code=202)
async def cert_renew():
    if not https_on():
        raise HTTPException(400, "turn HTTPS on first")
    if certmgr.busy:
        raise HTTPException(409, "a certificate request is already running")
    certmgr.error = None
    certmgr.busy = True                 # show "working" right away
    async def run():
        certmgr.busy = False
        await apply_https(force=True)
    spawn(run())
    return await cert_status()


@admin.get("/preview")
async def preview(request: Request, page: str = "start", style: str = "", name: str = "Jellyfin",
                  custom: str | None = None, delay: float | None = None):
    fake = Service(name, {})
    fake.start_page = style or None
    return start_page(request, fake, preview=page, custom=custom, blocked=page == "off",
                      error="jellyfin did not answer on port 8096 within 60s. Check the app port in its settings.")


app.include_router(admin)


def public_svc(request: Request, name: str) -> Service:
    """On the dashboard port any app may be asked about; on an app's own link
    or HTTPS name, only that app."""
    svc = get_svc(name)
    if not is_dashboard(request):
        host, server_port, hport = split_host(request)
        if reg.route(host, server_port, hport) is not svc:
            raise HTTPException(404)
    return svc


PUBLIC_HEADERS = {"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"}


def lan_ok(request: Request) -> bool:
    """The all-apps list follows the dashboard's home-network-only setting."""
    ip = request.client.host if request.client else ""
    return not reg.settings.get("admin_lan_only", True) or is_home_network(ip)
AWAKE_STATES = ("running", "starting", "updating", "maintenance")


def fresh(svc: "Service"):
    """Pick up the latest container state from Docker's events."""
    if not svc.transition:
        cached = driver.cached_status(svc.container)
        if cached is not None:
            svc.status = cached


def public_view(item: dict) -> dict:
    out = dict(item)
    if out.get("maintenance"):
        out["maintenance"] = {k: v for k, v in out["maintenance"].items() if k != "last_message"}
    return out


@app.get(ADMIN + "/status")
async def public_status_all(request: Request, code: bool = False):
    """Every app's status in one call (dashboard port only), for list widgets."""
    if not is_dashboard(request) or not lan_ok(request):
        raise HTTPException(404)
    items = []
    for svc in reg.services.values():
        fresh(svc)
        items.append(public_view(app_status(svc)))
    for name in reg.maintenance:
        if not reg.by_container(name):
            c = await container_status(name)
            if c:
                items.append(public_view(c))
    awake = sum(1 for i in items if i["controlled"] and i["state"] in AWAKE_STATES)
    asleep = sum(1 for i in items if i["controlled"] and i["state"] not in AWAKE_STATES)
    return JSONResponse({"apps": items, "awake": awake, "asleep": asleep,
                         "summary": f"{awake} awake · {asleep} asleep"}, headers=PUBLIC_HEADERS)


@app.get(ADMIN + "/status/{name}")
async def public_status(request: Request, name: str, code: bool = False):
    """Unauthenticated status for dashboard tiles. Never wakes the container.
    Normally always 200 (a sleeping app is still available). With ?code=1 it
    answers 503 while the app is asleep, for dashboards that only show a dot."""
    try:
        if name == "this" and not is_dashboard(request):
            # On an app's own link: "this app", so dashboard tiles need no app name.
            host, server_port, hport = split_host(request)
            svc = reg.route(host, server_port, hport)
            if not svc:
                raise HTTPException(404)
        else:
            svc = public_svc(request, name)
        fresh(svc)
        item = app_status(svc)
    except HTTPException:
        # On the dashboard port, containers that only have a restart schedule can be asked about too.
        item = (await container_status(name)
                if is_dashboard(request) and lan_ok(request) and name in reg.maintenance else None)
        if not item:
            raise
    status = 503 if code and item["state"] not in AWAKE_STATES else 200
    return JSONResponse(public_view(item), status, headers=PUBLIC_HEADERS)


@app.post(ADMIN + "/wake/{name}", status_code=202)
async def public_wake(request: Request, name: str):
    """Used by the error page's Try again button. Same effect as visiting the link."""
    svc = public_svc(request, name)
    if request.headers.get("x-stowaway") != "1":
        raise HTTPException(403, "Request blocked: it didn't come from a Stowaway page.")
    if svc.block_wake and svc.status != "running":
        raise HTTPException(409, f"{svc.name} is switched off for now and can't be woken.")
    svc.last_activity = time.time()
    if svc.status != "running" or svc.transition:
        kick(svc)
    return {"name": svc.name}


# --------------------------------------------------------------------------
# Proxy
# --------------------------------------------------------------------------
START_TEMPLATE = (STATIC / "start.html").read_text()


def direct_url(svc: Service, request: Request, with_path: bool = True):
    d = svc.direct or {}
    if not d.get("port"):
        return None
    host = d.get("host") or (request.headers.get("host") or "").rsplit(":", 1)[0] or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    url = f"http://{host}:{d['port']}"
    if with_path:
        url += request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return url


def start_page(request: Request, svc: Service, preview: str | None = None,
               custom: str | None = None, error: str | None = None, blocked: bool = False) -> HTMLResponse:
    host = (request.headers.get("host") or "").split(":")[0] or "localhost"
    eta = None
    if svc.start_duration:
        eta = max(0.0, svc.start_duration - (time.time() - (svc.start_began or time.time())))
    total = svc.start_duration or 10
    if preview:
        eta, total = 5, 5
    config = {
        "name": svc.name,
        "style": page_style(svc),
        "custom": reg.settings.get("start_page_custom", "") if custom is None else custom,
        "status_url": f"{ADMIN}/status/{svc.name}",
        "wake_url": f"{ADMIN}/wake/{svc.name}",
        "dashboard_url": (f"https://stowaway.{https_domain()}{https_suffix()}{ADMIN}"
                          if request.url.scheme == "https" and https_domain()
                          else f"http://{host}:{DASHBOARD_PORT}{ADMIN}"),
        "eta": eta if eta is not None else (None if not preview else 5),
        "eta_total": total,
        "preview": preview,
        "error": error,
        "blocked": blocked,
        "state": svc.transition,
        "update": svc.update_progress,
        # after starting: go to the app's own address, or reload through Stowaway
        "go_to": direct_url(svc, request) if svc.open_mode == "direct" and not preview else None,
    }
    blob = json.dumps(config).replace("<", "\\u003c")
    return HTMLResponse(START_TEMPLATE.replace("__CONFIG__", blob), 200 if preview else 503,
                        headers={"Retry-After": "2", "Cache-Control": "no-store",
                                 "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})


async def forward(request: Request, svc: Service, retry: bool = True, passive: bool = False):
    if DEMO:
        return HTMLResponse(f"<h1>Hello from {svc.name}</h1><p>(demo mode: no real container behind this)</p>")
    if not svc.upstream:
        await resolve(svc)
        if not svc.upstream:
            return JSONResponse({"error": f"can't find a way to reach {svc.container}"}, 502)

    url = svc.upstream.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    # Visitors can't be allowed to claim an address: apps may skip their login
    # for "local" addresses, so drop whatever they sent and state the real one.
    headers = [(k, v) for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP and k.lower() not in SPOOFABLE and k.lower() != "cookie"]
    # Pass the visitor's cookies on, minus Stowaway's own session cookie.
    cookies = [c for c in request.headers.get("cookie", "").split(";")
               if c.strip() and c.split("=", 1)[0].strip() != COOKIE]
    if cookies:
        headers.append(("cookie", ";".join(cookies).strip()))
    client_ip = request.client.host if request.client else ""
    headers += [
        ("x-forwarded-for", client_ip),
        ("x-real-ip", client_ip),
        ("x-forwarded-host", request.headers.get("host", "")),
        ("x-forwarded-proto", request.url.scheme),
    ]
    has_body = "content-length" in request.headers or "transfer-encoding" in request.headers

    client: httpx.AsyncClient = request.app.state.http
    svc.active += 1
    try:
        req = client.build_request(request.method, url, headers=headers,
                                   content=request.stream() if has_body else None)
        resp = await client.send(req, stream=True)
        log.debug("%s %s for %s from %s -> %d", request.method, request.url.path, svc.name, client_ip,
                  resp.status_code)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        svc.active -= 1
        # Stopped or recreated outside of us (new IP): look it up again, start, retry once.
        svc.status = await driver.status(svc.container)
        await resolve(svc)
        if retry and not has_body:
            try:
                await ensure_running(svc)
            except Exception as e:
                return JSONResponse({"error": str(e)}, 502)
            return await forward(request, svc, retry=False, passive=passive)
        return JSONResponse({"error": f"could not reach {svc.name}"}, 502)
    except Exception:
        svc.active -= 1
        raise

    async def finished():
        await resp.aclose()
        svc.active -= 1
        if not passive:
            svc.last_activity = time.time()

    out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code,
                            background=BackgroundTask(finished))
    out.raw_headers = [
        (k.encode("latin-1"), fix_redirect(k, v, svc, request).encode("latin-1"))
        for k, v in resp.headers.multi_items() if k.lower() not in HOP_BY_HOP
    ]
    return out


REDIRECT_HEADERS = {"location", "content-location", "refresh"}


def fix_redirect(name: str, value: str, svc: Service, request: Request) -> str:
    """An app that redirects to its own address (its IP and port) would take the
    browser away from Stowaway. Point such redirects back at the address the
    browser used. Relative redirects and other sites are left alone."""
    if name.lower() not in REDIRECT_HEADERS or not svc.own_addrs:
        return value
    prefix, url = "", value
    if name.lower() == "refresh":                       # "0; url=http://..."
        m = re.match(r"(?i)(\s*\d+\s*;\s*url\s*=\s*)(.+)", value)
        if not m:
            return value
        prefix, url = m.group(1), m.group(2).strip("'\" ")
    try:
        u = urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return value
    if u.scheme not in ("http", "https") or not u.hostname or f"{u.hostname}:{port}" not in svc.own_addrs:
        return value
    here = f"{request.url.scheme}://{request.headers.get('host', '')}"
    rest = u.path or "/"
    if u.query:
        rest += "?" + u.query
    if u.fragment:
        rest += "#" + u.fragment
    return prefix + here + rest


# --------------------------------------------------------------------------
# WebSockets (live connections: noVNC screens, Home Assistant, code-server...)
# --------------------------------------------------------------------------
WS_SKIP = HOP_BY_HOP | SPOOFABLE | {"host", "cookie", "sec-websocket-key", "sec-websocket-version",
                                    "sec-websocket-extensions", "sec-websocket-protocol"}


def ws_connection_class(host_header: str):
    """The websockets client always writes its own Host header; apps that compare
    Origin with Host (code-server, for one) need the browser's original Host."""
    class Conn(ClientConnection):
        def __init__(self, protocol, *args, **kwargs):
            make_request = protocol.connect

            def connect():
                req = make_request()
                if host_header:
                    del req.headers["Host"]
                    req.headers["Host"] = host_header
                return req
            protocol.connect = connect
            super().__init__(protocol, *args, **kwargs)
    return Conn


@app.websocket("/{path:path}")
async def ws_proxy(ws: WebSocket, path: str):
    host, server_port, hport = split_host(ws)
    if ws.url.path == ADMIN or ws.url.path.startswith(ADMIN + "/"):
        await ws.close(code=1008)
        return
    svc = reg.route(host, server_port, hport)
    if svc is None:
        await ws.close(code=1008)
        return
    if svc.block_wake and (svc.status != "running" or svc.transition == "stopping"):
        await ws.close(code=1013)          # "try again later": switched off for now
        return
    svc.last_activity = time.time()
    if svc.status != "running" or svc.transition:
        try:
            await ensure_running(svc)          # a live connection to a sleeping app wakes it
        except Exception:
            await ws.close(code=1011)
            return
    if not svc.upstream:
        await resolve(svc)
    if not svc.upstream:
        await ws.close(code=1011)
        return

    target = svc.upstream.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    target += ws.url.path + (f"?{ws.url.query}" if ws.url.query else "")
    client_ip = ws.client.host if ws.client else ""
    headers = [(k, v) for k, v in ws.headers.items() if k.lower() not in WS_SKIP]
    cookies = [c for c in ws.headers.get("cookie", "").split(";")
               if c.strip() and c.split("=", 1)[0].strip() != COOKIE]
    if cookies:
        headers.append(("Cookie", ";".join(cookies).strip()))
    headers += [("X-Forwarded-For", client_ip), ("X-Real-IP", client_ip),
                ("X-Forwarded-Host", ws.headers.get("host", "")),
                ("X-Forwarded-Proto", "https" if ws.url.scheme == "wss" else "http")]
    subprotocols = ws.scope.get("subprotocols") or None

    try:
        upstream = await ws_connect(
            target, additional_headers=headers, subprotocols=subprotocols,
            compression=None, max_size=None, ping_interval=None, proxy=None,
            open_timeout=15, user_agent_header=None,
            create_connection=ws_connection_class(ws.headers.get("host", "")))
    except Exception as e:
        log.warning("websocket to %s failed: %s", svc.name, e)
        await ws.close(code=1011)
        return

    await ws.accept(subprotocol=upstream.subprotocol)
    log.debug("websocket opened: %s %s from %s", svc.name, ws.url.path, client_ip)
    svc.active += 1            # an open live connection means the app is in use
    try:
        async def to_app():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return msg.get("code", 1000)
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])

        async def to_browser():
            try:
                async for m in upstream:
                    if isinstance(m, bytes):
                        await ws.send_bytes(m)
                    else:
                        await ws.send_text(m)
            except ConnectionClosed:
                pass                 # the app closed; its close code is read below

        a, b = asyncio.create_task(to_app()), asyncio.create_task(to_browser())
        done, pending = await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if a in done:        # browser left: tell the app
            code = a.result() if not a.exception() else 1011
            await upstream.close(code=code if code and 1000 <= code < 5000 and code not in (1005, 1006) else 1000)
        else:                # app closed: tell the browser the same
            code = upstream.close_code or 1000
            with contextlib.suppress(Exception):
                await ws.close(code=code if 1000 <= code < 5000 and code not in (1005, 1006) else 1000)
    except (WebSocketDisconnect, Exception) as e:
        if not isinstance(e, WebSocketDisconnect):
            log.debug("websocket to %s ended: %s", svc.name, e)
        with contextlib.suppress(Exception):
            await upstream.close()
    finally:
        svc.active -= 1
        svc.last_activity = time.time()


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str):
    host, server_port, hport = split_host(request)
    if request.url.path == ADMIN or request.url.path.startswith(ADMIN + "/"):
        raise HTTPException(404)        # Stowaway's own paths are never handed to apps
    svc = reg.route(host, server_port, hport)
    if svc is None:
        dash_host = https_domain() and host in (f"stowaway.{https_domain()}", https_domain())
        if request.url.path == "/" and (server_port == DASHBOARD_PORT or dash_host):
            return RedirectResponse(ADMIN)
        dash = (f"{ADMIN}" if dash_host else f"//{html.escape(host, quote=True)}:{DASHBOARD_PORT}{ADMIN}")
        return HTMLResponse(
            f"<p>Nothing is set up for this address. "
            f'Open the <a href="{dash}">Stowaway dashboard</a>.</p>', 404,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    if not svc.transition:
        # Docker's events keep this current, so a container stopped outside Stowaway
        # gets the start page right away instead of a connection error.
        cached = driver.cached_status(svc.container)
        if cached is not None:
            svc.status = cached

    who = request.client.host if request.client else "?"
    if reg.is_passive(request):
        # Dashboard status check: answer without waking or resetting the timer.
        log.debug("%s %s for %s from %s: status check, %s", request.method, request.url.path, svc.name, who,
                  "answered for it" if svc.status != "running" or svc.transition else "passed through")
        if svc.status != "running" or svc.transition:
            return JSONResponse({"name": svc.name, "state": svc.transition or "sleeping"},
                                headers={"X-Stowaway-State": "sleeping"})
        return await forward(request, svc, passive=True)

    wants_page = request.method == "GET" and "text/html" in request.headers.get("accept", "")
    if svc.block_wake and (svc.status != "running" or svc.transition == "stopping"):
        log.debug("%s %s for %s from %s: switched off (don't wake)", request.method, request.url.path, svc.name, who)
        if wants_page:
            return start_page(request, svc, blocked=True)
        return JSONResponse({"error": f"{svc.name} is switched off for now"}, 503,
                            headers={"Retry-After": "300", "X-Stowaway-State": "switched-off"})
    svc.last_activity = time.time()
    direct = direct_url(svc, request) if svc.open_mode == "direct" else None
    if svc.status != "running" or svc.transition:
        log.debug("%s %s for %s from %s: app is %s, %s", request.method, request.url.path, svc.name, who,
                  svc.transition or svc.status, "showing start page" if wants_page else "waiting for it to start")
        if wants_page:
            kick(svc)
            return start_page(request, svc)
    elif direct and wants_page:
        log.debug("%s %s for %s from %s: sending browser to %s", request.method, request.url.path, svc.name, who, direct)
        # "Go to the app's own address": a browser opening the page is sent on;
        # everything else (API clients, apps) keeps going through the link.
        return RedirectResponse(direct, status_code=302, headers={"Cache-Control": "no-store"})
    if svc.status != "running" or svc.transition:
        try:
            await ensure_running(svc)
        except Exception as e:
            return JSONResponse({"error": f"could not start {svc.name}: {e}"}, 502)
    return await forward(request, svc)


# --------------------------------------------------------------------------
# Entry point: python -m app.main
# --------------------------------------------------------------------------
async def main():
    await startup()
    listeners.enabled = True
    await listeners.sync()
    if DASHBOARD_PORT not in listeners.servers:
        log.error("could not open dashboard port %d; is something else using it?", DASHBOARD_PORT)
        raise SystemExit(1)
    log.info("dashboard: http://<this-server>:%d%s", DASHBOARD_PORT, ADMIN)
    if os.environ.get("ADMIN_PASSWORD"):
        log.info("ADMIN_PASSWORD is no longer used: sign-in is set up in the dashboard itself.")
    if AUTH_REQUIRED and accounts.needs_setup:
        log.warning("No dashboard account yet. Open http://<this-server>:%d from your home network "
                    "to create one.", DASHBOARD_PORT)

    async def follow_certs():
        while True:
            await certmgr.changed.wait()
            certmgr.changed.clear()
            await listeners.sync()

    async def cert_loop():
        while True:
            try:
                await apply_https()
            except Exception:
                log.exception("certificate check failed")
            await asyncio.sleep(12 * 3600)

    spawn(follow_certs())
    spawn(cert_loop())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()
    log.info("shutting down")
    await listeners.close_all()
    await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
