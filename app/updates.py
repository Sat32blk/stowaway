"""Updating an app's container to a newer image version.

check()      ask the registry whether the image tag now points to something newer
pull()       download it, reporting progress
recreate()   swap the container for one built from the new image, keeping its name,
             settings, volumes (including anonymous ones), networks and IP addresses
commit()     after the new one started fine: remove the old container (and old image)
rollback()   the new one didn't work: remove it and put the old container back

The old container is only renamed, never deleted, until the new one has started.
"""
import logging

log = logging.getLogger("stowaway")
OLD_SUFFIX = "-stowaway-previous"


class UpdateError(Exception):
    pass


def friendly(e) -> str:
    """Docker's error text, minus the plumbing: 'exec: "/app": no such file or directory'."""
    msg = getattr(e, "explanation", None) or str(e)
    if isinstance(msg, bytes):
        msg = msg.decode(errors="replace")
    for marker in ("error during container init: ", "starting container process caused: ",
                   "OCI runtime create failed: "):
        if marker in msg:
            msg = msg.split(marker, 1)[1]
    return msg.strip().strip('"').rstrip(": ")[:300]


from app.dockerapi import NotFound, split_ref  # noqa: E402  (split_ref re-exported)


def is_updatable_ref(ref: str):
    if not ref:
        return "the container doesn't record which image it came from"
    if "@sha256:" in ref:
        return "the image is pinned to an exact version (a digest)"
    bare = ref.split(":", 1)[-1] if ref.startswith("sha256:") else ref
    if len(bare) in (12, 64) and all(c in "0123456789abcdef" for c in bare):
        return "the container was created from an image ID, not a name"
    return None


class Progress:
    """Turns docker pull events into one overall figure."""

    def __init__(self):
        self.layers = {}   # id -> dict(total, done, extract_total, extracted, state)
        self.phase = "downloading"

    def feed(self, ev: dict):
        if ev.get("error"):
            raise UpdateError(ev.get("error"))
        lid, status = ev.get("id"), (ev.get("status") or "").lower()
        if not lid or " " in lid:            # "Digest: ...", "Status: ..." lines
            return
        L = self.layers.setdefault(lid, {"total": 0, "done": 0, "xt": 0, "x": 0, "state": "waiting"})
        detail = ev.get("progressDetail") or {}
        if status.startswith("downloading"):
            L["state"] = "downloading"
            L["total"] = detail.get("total") or L["total"]
            L["done"] = detail.get("current") or L["done"]
        elif status.startswith(("verifying", "download complete")):
            L["state"] = "downloaded"
            L["done"] = L["total"] = max(L["total"], L["done"])
        elif status.startswith("extracting"):
            L["state"] = "extracting"
            L["done"] = L["total"] = max(L["total"], L["done"])
            L["xt"] = detail.get("total") or L["xt"]
            L["x"] = detail.get("current") or L["x"]
        elif status.startswith(("pull complete", "already exists")):
            L["state"] = "complete"
            L["done"] = L["total"] = max(L["total"], L["done"])
            L["x"] = L["xt"] = max(L["xt"], L["x"], 1)

    def snapshot(self):
        layers = [L for L in self.layers.values()]
        total = sum(L["total"] for L in layers)
        done = sum(min(L["done"], L["total"]) for L in layers)
        downloading = any(L["state"] in ("waiting", "downloading") for L in layers)
        if not layers:
            pct, phase = 0.0, "downloading"
        elif downloading:
            pct, phase = (done / total * 90 if total else 0), "downloading"
        else:
            # downloads finished: unpacking fills the last 10%
            n = len(layers)
            unpacked = sum(1 if L["state"] == "complete" else (L["x"] / L["xt"] if L["xt"] else 0) for L in layers)
            pct, phase = 90 + unpacked / n * 10, "unpacking"
        return {"phase": phase, "pct": round(min(pct, 100), 1), "done_bytes": done, "total_bytes": total}


# ---------------------------------------------------------------------------
# Real Docker (dk is an app.dockerapi.Docker)
# ---------------------------------------------------------------------------
IMAGE_DEFAULT_FIELDS = ("Cmd", "Entrypoint", "WorkingDir", "User", "StopSignal", "Healthcheck", "Shell", "OnBuild")


def managed_elsewhere(attrs: dict):
    """Containers another system owns and recreates itself; swapping them out would
    fight it. Returns a reason, or None."""
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    if "/.ix-apps/" in labels.get("com.docker.compose.project.working_dir", ""):
        return "it's a TrueNAS app; update it from the Apps page in TrueNAS"
    return None


async def _is_podman(dk) -> bool:
    try:
        r = await dk.http.get("/version")
        return "podman" in str(r.json().get("Components", "")).lower()
    except Exception:
        return False


async def check(dk, name: str):
    """{'status': 'update'|'current'|'unsupported', 'ref', 'remote', 'detail'}"""
    a = await dk.inspect(name)
    ref = (a.get("Config") or {}).get("Image", "")
    why = managed_elsewhere(a) or is_updatable_ref(ref)
    if why:
        return {"status": "unsupported", "ref": ref, "detail": why}
    img = await dk.image(a["Image"])
    local = [d.split("@", 1)[1] for d in (img.get("RepoDigests") or []) if "@" in d]
    if not local:
        return {"status": "unsupported", "ref": ref, "detail": "the image was built on this server, not downloaded"}
    try:
        remote = (await dk.distribution(ref))["Descriptor"]["digest"]
    except Exception as e:
        if isinstance(e, NotFound) and await _is_podman(dk):
            # Podman doesn't offer this check (it answers 404 for the whole endpoint).
            return {"status": "unsupported", "ref": ref,
                    "detail": "this container engine can't check registries for new versions"}
        raise UpdateError(f"couldn't reach the image registry: {friendly(e)}")
    if remote in local:
        return {"status": "current", "ref": ref, "remote": remote}
    return {"status": "update", "ref": ref, "remote": remote}


async def pull(dk, ref: str, on_progress):
    prog = Progress()
    try:
        async for ev in dk.pull(ref):
            prog.feed(ev)
            on_progress(prog.snapshot())
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(f"download failed: {friendly(e)}")
    img = await dk.image(ref)
    on_progress({**prog.snapshot(), "phase": "installing", "pct": 100.0})
    return img["Id"]


def _strip_image_defaults(config: dict, image_config: dict):
    """Leave out what the old image supplied, so the new image's defaults apply."""
    img = image_config or {}
    env_defaults = set(img.get("Env") or [])
    config["Env"] = [e for e in (config.get("Env") or []) if e not in env_defaults]
    img_labels = img.get("Labels") or {}
    config["Labels"] = {k: v for k, v in (config.get("Labels") or {}).items() if img_labels.get(k) != v}
    for f in IMAGE_DEFAULT_FIELDS:
        if f in config and config.get(f) == img.get(f):
            config.pop(f, None)
    img_ports = img.get("ExposedPorts") or {}
    ports = {p: v for p, v in (config.get("ExposedPorts") or {}).items() if p not in img_ports}
    config["ExposedPorts"] = ports or None
    img_vols = img.get("Volumes") or {}
    vols = {p: v for p, v in (config.get("Volumes") or {}).items() if p not in img_vols}
    config["Volumes"] = vols or None
    return config


async def recreate(dk, name: str, ref: str):
    """Swap `name` for a new container from `ref`. Returns (new_id, old_id)."""
    a = await dk.inspect(name)
    old_id = a["Id"]
    if (a.get("State") or {}).get("Status") == "running":
        raise UpdateError("the app is running; stop it before updating")
    try:
        old_image_cfg = (await dk.image(a["Image"])).get("Config") or {}
    except Exception:
        old_image_cfg = {}

    config = _strip_image_defaults(dict(a.get("Config") or {}), old_image_cfg)
    config["Image"] = ref
    if config.get("Hostname") == old_id[:12]:
        config.pop("Hostname", None)          # the auto-generated one; the new container gets its own

    host = dict(a.get("HostConfig") or {})
    # Keep anonymous volumes (the data would otherwise be left behind in the old container).
    covered = {m.get("Target") for m in (host.get("Mounts") or [])}
    covered |= {b.split(":")[1] for b in (host.get("Binds") or []) if b.count(":") >= 1}
    extra = []
    for m in a.get("Mounts") or []:
        if m.get("Type") == "volume" and m.get("Name") and m.get("Destination") not in covered:
            extra.append({"Type": "volume", "Source": m["Name"], "Target": m["Destination"],
                          "ReadOnly": not m.get("RW", True)})
    if extra:
        host["Mounts"] = list(host.get("Mounts") or []) + extra
    config["HostConfig"] = host

    mode = host.get("NetworkMode") or ""
    nets = (a.get("NetworkSettings") or {}).get("Networks") or {}
    endpoints = {}
    if mode not in ("host", "none") and not mode.startswith("container:"):
        for net, ep in nets.items():
            aliases = [x for x in (ep.get("Aliases") or []) if x not in (old_id[:12], old_id)]
            e = {"IPAMConfig": ep.get("IPAMConfig"), "Links": ep.get("Links"), "Aliases": aliases or None,
                 "DriverOpts": ep.get("DriverOpts")}
            if (ep.get("IPAMConfig") or {}).get("IPv4Address") and ep.get("MacAddress"):
                e["MacAddress"] = ep["MacAddress"]      # keeps DHCP reservations on macvlan
            endpoints[net] = {k: v for k, v in e.items() if v}
    first = next(iter(endpoints), None)
    if first:
        config["NetworkingConfig"] = {"EndpointsConfig": {first: endpoints[first]}}

    backup = name + OLD_SUFFIX
    try:
        await dk.remove(backup, force=True)     # leftover from an interrupted update
    except Exception:
        pass
    await dk.rename(old_id, backup)
    new_id = None
    try:
        new_id = await dk.create(config, name)
        for net, ep in list(endpoints.items())[1:]:
            ipam = {k: v for k, v in (ep.get("IPAMConfig") or {}).items()
                    if k in ("IPv4Address", "IPv6Address") and v}
            endpoint = {k: v for k, v in {"Aliases": ep.get("Aliases"), "Links": ep.get("Links"),
                                          "DriverOpts": ep.get("DriverOpts"),
                                          "IPAMConfig": ipam or None}.items() if v}
            await dk.connect(net, new_id, endpoint)
    except Exception as e:
        if new_id:
            try:
                await dk.remove(new_id, force=True)
            except Exception:
                pass
        await dk.rename(old_id, name)
        raise UpdateError(f"couldn't create the updated container: {friendly(e)}")
    return new_id, old_id


async def commit(dk, old_id: str, remove_old_image: bool = True):
    a = await dk.inspect(old_id)
    image_id = a.get("Image")
    await dk.remove(old_id)             # volumes stay: the new container uses them
    if remove_old_image and image_id:
        try:
            await dk.remove_image(image_id)
        except Exception:
            pass                        # still used by something else, or already gone


async def image_of(dk, name: str) -> str:
    return (await dk.inspect(name))["Image"]


async def restore_tag(dk, ref: str, good_image: str, bad_image: str | None = None):
    """After a failed update, point the image name back at the working version
    (so `docker compose up` doesn't pick up the broken one) and drop the download."""
    repo, tag = split_ref(ref)
    await dk.tag(good_image, repo, tag)
    if bad_image and bad_image != good_image:
        try:
            await dk.remove_image(bad_image)
        except Exception:
            pass


async def rollback(dk, name: str, new_id: str, old_id: str):
    try:
        await dk.remove(new_id, force=True)
    except Exception:
        pass
    await dk.rename(old_id, name)
