"""A small async client for the Docker Engine API.

Stowaway only needs a couple of dozen calls, so it talks to the Docker socket
directly with httpx (already used for proxying) instead of the docker SDK.
That saves the SDK and `requests` in memory, and everything stays on the event
loop instead of in worker threads.

DOCKER_HOST is honoured (unix:///path or tcp://host:port), e.g. for a socket proxy.
"""
import base64
import json
import os
from pathlib import Path
from urllib.parse import quote

import httpx

MIN_API = "1.30"          # distribution inspect needs 1.30


class DockerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.explanation = message


class NotFound(DockerError):
    pass


def _ver(v: str):
    return tuple(int(x) for x in v.split("."))


def split_ref(ref: str):
    """'jellyfin/jellyfin:10.9' -> ('jellyfin/jellyfin', '10.9'); default tag 'latest'."""
    name, tag = ref, None
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
    return name, tag or "latest"


def registry_of(ref: str) -> str:
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def registry_auth(ref: str, config_path: str | None = None):
    """X-Registry-Auth header value from ~/.docker/config.json, if a login is stored."""
    path = Path(config_path or os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
    if path.is_dir():
        path = path / "config.json"
    try:
        auths = json.loads(path.read_text()).get("auths") or {}
    except (OSError, ValueError):
        return None
    reg = registry_of(ref)
    keys = [reg] + (["https://index.docker.io/v1/", "index.docker.io", "registry-1.docker.io"] if reg == "docker.io" else
                    [f"https://{reg}", f"http://{reg}", f"https://{reg}/v1/", f"https://{reg}/v2/"])
    for k in keys:
        a = auths.get(k)
        if a and a.get("auth"):
            user, _, pw = base64.b64decode(a["auth"]).decode().partition(":")
            payload = {"username": user, "password": pw, "serveraddress": k}
        elif a and a.get("identitytoken"):
            payload = {"identitytoken": a["identitytoken"], "serveraddress": k}
        else:
            continue
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return None


class Docker:
    def __init__(self, host: str | None = None):
        host = host or os.environ.get("DOCKER_HOST") or "unix:///var/run/docker.sock"
        if host.startswith("unix://"):
            transport = httpx.AsyncHTTPTransport(uds=host[len("unix://"):])
            base = "http://docker"
        elif host.startswith(("tcp://", "http://")):
            transport = httpx.AsyncHTTPTransport()
            base = "http://" + host.split("://", 1)[1]
        else:
            raise ValueError(f"unsupported DOCKER_HOST {host!r}")
        self.base = base
        self.http = httpx.AsyncClient(transport=transport, base_url=base, timeout=httpx.Timeout(60, connect=5))
        self.api_version = None

    async def negotiate(self):
        r = await self.http.get("/version")
        r.raise_for_status()
        v = r.json()
        self.api_version = v.get("ApiVersion") or "1.41"
        if _ver(self.api_version) < _ver(MIN_API):
            raise DockerError(0, f"Docker {v.get('Version')} is too old (API {self.api_version}); "
                                 f"Stowaway needs Docker 17.06 or newer")
        return v

    def supports(self, version: str) -> bool:
        return self.api_version is not None and _ver(self.api_version) >= _ver(version)

    def _url(self, path: str) -> str:
        return f"/v{self.api_version}{path}"

    @staticmethod
    def _raise(r: httpx.Response):
        if r.status_code < 400:
            return
        try:
            msg = r.json().get("message") or r.text
        except ValueError:
            msg = r.text or r.reason_phrase
        raise (NotFound if r.status_code == 404 else DockerError)(r.status_code, msg.strip())

    async def request(self, method: str, path: str, *, params=None, json_body=None, headers=None,
                      timeout=None, ok=(), raw=False):
        if self.api_version is None:
            await self.negotiate()
        kw = {"params": params, "headers": headers}
        if json_body is not None:
            kw["json"] = json_body
        if timeout is not None:
            kw["timeout"] = timeout
        r = await self.http.request(method, self._url(path), **kw)
        if r.status_code in ok:
            return None
        self._raise(r)
        if raw:
            return r
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    async def stream_json(self, method: str, path: str, *, params=None, headers=None, timeout=None):
        """Yield JSON objects from a streaming endpoint (pull progress, events)."""
        if self.api_version is None:
            await self.negotiate()
        async with self.http.stream(method, self._url(path), params=params, headers=headers,
                                    timeout=timeout if timeout is not None else httpx.Timeout(None, connect=5)) as r:
            if r.status_code >= 400:
                await r.aread()
                self._raise(r)
            async for line in r.aiter_lines():
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue

    # ---- system ----
    async def info(self):
        return await self.request("GET", "/info")

    # ---- containers ----
    async def inspect(self, name: str):
        return await self.request("GET", f"/containers/{quote(name, safe='')}/json")

    async def list(self, all: bool = True):
        return await self.request("GET", "/containers/json", params={"all": "1" if all else "0"})

    async def start(self, name: str):
        await self.request("POST", f"/containers/{quote(name, safe='')}/start", ok=(304,))

    async def stop(self, name: str, timeout: int = 15):
        await self.request("POST", f"/containers/{quote(name, safe='')}/stop", params={"t": timeout},
                           ok=(304,), timeout=timeout + 30)

    async def restart(self, name: str, timeout: int = 30):
        await self.request("POST", f"/containers/{quote(name, safe='')}/restart", params={"t": timeout},
                           timeout=timeout + 60)

    async def rename(self, name: str, new: str):
        await self.request("POST", f"/containers/{quote(name, safe='')}/rename", params={"name": new})

    async def remove(self, name: str, force: bool = False, volumes: bool = False):
        await self.request("DELETE", f"/containers/{quote(name, safe='')}",
                           params={"force": "1" if force else "0", "v": "1" if volumes else "0"})

    async def create(self, config: dict, name: str):
        return (await self.request("POST", "/containers/create", params={"name": name}, json_body=config))["Id"]

    async def connect(self, network: str, container: str, endpoint: dict):
        await self.request("POST", f"/networks/{quote(network, safe='')}/connect",
                           json_body={"Container": container, "EndpointConfig": endpoint})

    async def stats(self, name: str):
        params = {"stream": "0"}
        if self.supports("1.41"):
            params["one-shot"] = "1"
        return await self.request("GET", f"/containers/{quote(name, safe='')}/stats", params=params, timeout=30)

    # ---- images ----
    async def image(self, name: str):
        return await self.request("GET", f"/images/{quote(name, safe='')}/json")

    async def tag(self, image: str, repo: str, tag: str):
        await self.request("POST", f"/images/{quote(image, safe='')}/tag", params={"repo": repo, "tag": tag})

    async def remove_image(self, image: str):
        await self.request("DELETE", f"/images/{quote(image, safe='')}")

    async def distribution(self, ref: str):
        auth = registry_auth(ref)
        return await self.request("GET", f"/distribution/{ref}/json", timeout=30,
                                  headers={"X-Registry-Auth": auth} if auth else None)

    def pull(self, ref: str):
        repo, tag = split_ref(ref)
        auth = registry_auth(ref)
        return self.stream_json("POST", "/images/create", params={"fromImage": repo, "tag": tag},
                                headers={"X-Registry-Auth": auth} if auth else None)

    # ---- files and commands inside a container ----
    async def put_archive(self, name: str, path: str, tar_bytes: bytes):
        """Unpack a tar archive into a directory inside the container."""
        if self.api_version is None:
            await self.negotiate()
        r = await self.http.put(self._url(f"/containers/{quote(name, safe='')}/archive"),
                                params={"path": path}, content=tar_bytes,
                                headers={"Content-Type": "application/x-tar"})
        self._raise(r)

    async def get_archive(self, name: str, path: str):
        """Read one file from a container. Returns (bytes, tarinfo) or (None, None) if it doesn't exist."""
        if self.api_version is None:
            await self.negotiate()
        r = await self.http.get(self._url(f"/containers/{quote(name, safe='')}/archive"), params={"path": path})
        if r.status_code == 404:
            return None, None
        self._raise(r)
        import io, tarfile
        with tarfile.open(fileobj=io.BytesIO(r.content)) as tar:
            for m in tar.getmembers():
                if m.isfile():
                    return tar.extractfile(m).read(), m
        return None, None

    async def exec_run(self, name: str, cmd: "list[str]", user: str = "", timeout: float = 120):
        """Run a command in a running container. Returns (exit code, output)."""
        ex = await self.request("POST", f"/containers/{quote(name, safe='')}/exec", json_body={
            "Cmd": cmd, "User": user, "AttachStdout": True, "AttachStderr": True, "Tty": True})
        r = await self.request("POST", f"/exec/{ex['Id']}/start", json_body={"Detach": False, "Tty": True},
                               timeout=timeout, raw=True)
        out = r.content.decode("utf-8", "replace")
        info = await self.request("GET", f"/exec/{ex['Id']}/json")
        return info.get("ExitCode"), out

    # ---- networks and events ----
    async def networks(self):
        return await self.request("GET", "/networks")

    def events(self, filters: dict, since: int | None = None):
        params = {"filters": json.dumps(filters)}
        if since:
            params["since"] = str(since)
        return self.stream_json("GET", "/events", params=params)

    async def close(self):
        await self.http.aclose()
