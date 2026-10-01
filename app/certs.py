"""HTTPS certificates for Stowaway.

Sources:
  selfsigned  generated here; works immediately, browsers show a warning
  duckdns     Let's Encrypt wildcard via DuckDNS DNS check (no ports to open)
  cloudflare  Let's Encrypt wildcard via Cloudflare DNS check (no ports to open)
  http        Let's Encrypt per-app names via HTTP check (router forwards port 80)
  custom      the user's own fullchain.pem / privkey.pem in config/certs/custom/

Let's Encrypt requests go through certbot, run as a subprocess.
"""
import asyncio
import datetime as dt
import ipaddress
import logging
import os
import re
import socket
import sys
import time
from pathlib import Path

# cryptography is imported only when HTTPS is used: it's ~7 MB of memory otherwise wasted.

log = logging.getLogger("stowaway")

SOURCES = ("selfsigned", "duckdns", "cloudflare", "http", "custom")
LE_SOURCES = ("duckdns", "cloudflare", "http")
RENEW_BEFORE = 30 * 86400
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def dns_label(name: str) -> str:
    """Turn a container name into something usable as a hostname label."""
    label = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return label[:63] or "app"


def local_ips():
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        out = os.popen("ip -4 -o addr show 2>/dev/null").read()
        ips.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out))
    except Exception:
        pass
    return sorted(ips)


def read_cert(path: Path):
    """Names, expiry and issuer of a PEM certificate, or None."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except Exception:
        return None
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = san.get_values_for_type(x509.DNSName) + [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    issuer = cert.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME) or \
        cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=dt.timezone.utc)
    return {
        "names": names,
        "not_after": not_after.timestamp(),
        "issuer": issuer[0].value if issuer else "",
        "self_signed": cert.issuer == cert.subject,
    }


def covers(names: list[str], host: str) -> bool:
    host = host.lower()
    for n in names:
        n = n.lower()
        if n == host:
            return True
        if n.startswith("*.") and host.count(".") == n.count(".") and host.endswith(n[1:]):
            return True
    return False


class CertManager:
    def __init__(self, config_dir: Path, settings, wanted_names):
        """settings() -> dict; wanted_names() -> names the cert must cover."""
        self.dir = config_dir
        self.settings = settings
        self.wanted_names = wanted_names
        self.busy = False
        self.error = None
        self.log_tail = ""
        self.last_attempt = 0.0
        self.changed = asyncio.Event()     # set whenever the files on disk change

    # ---- where things live ----
    @property
    def le_dir(self):
        return self.dir / "letsencrypt"

    def paths(self, source=None):
        source = source or self.settings().get("cert_source", "selfsigned")
        if source == "selfsigned":
            return self.dir / "certs" / "selfsigned.crt", self.dir / "certs" / "selfsigned.key"
        if source == "custom":
            return self.dir / "certs" / "custom" / "fullchain.pem", self.dir / "certs" / "custom" / "privkey.pem"
        live = self.le_dir / "live" / "stowaway"
        return live / "fullchain.pem", live / "privkey.pem"

    def current(self):
        """(cert, key) paths if a usable certificate exists for the chosen source."""
        cert, key = self.paths()
        return (cert, key) if cert.exists() and key.exists() else None

    def fingerprint(self):
        cur = self.current()
        if not cur:
            return None
        return tuple((str(p), p.stat().st_mtime) for p in cur)

    def status(self):
        s = self.settings()
        source = s.get("cert_source", "selfsigned")
        cur = self.current()
        info = read_cert(cur[0]) if cur else None
        missing = []
        if info:
            missing = [n for n in self.wanted_names() if not covers(info["names"], n)]
        return {
            "source": source,
            "busy": self.busy,
            "error": self.error,
            "log": self.log_tail,
            "has_cert": bool(info),
            "names": info["names"] if info else [],
            "not_after": info["not_after"] if info else None,
            "issuer": info["issuer"] if info else None,
            "self_signed": info["self_signed"] if info else None,
            "missing_names": missing,
            "custom_dir": str(self.dir / "certs" / "custom"),
        }

    # ---- self-signed ----
    def make_selfsigned(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
        s = self.settings()
        domain = s.get("https_domain", "").strip().lower()
        dns = ["localhost"]
        if domain:
            dns += [domain, f"*.{domain}"]
        dns += [n for n in self.wanted_names() if not covers(dns, n)]
        ips = local_ips()
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, domain or "Stowaway"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Stowaway self-signed"),
        ])
        now = dt.datetime.now(dt.timezone.utc)
        san = [x509.DNSName(d) for d in dict.fromkeys(dns)] + \
              [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=825))
                .add_extension(x509.SubjectAlternativeName(san), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = self.paths("selfsigned")
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        os.chmod(key_path, 0o600)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        log.info("created self-signed certificate for %s", ", ".join(dns + ips))

    # ---- Let's Encrypt ----
    def certbot_command(self):
        s = self.settings()
        source = s["cert_source"]
        domain = s.get("https_domain", "").strip().lower()
        le = self.le_dir
        cmd = [sys.executable, "-c", "import sys; from certbot.main import main; sys.exit(main())",
               "certonly", "--non-interactive", "--agree-tos",
               "--config-dir", str(le), "--work-dir", str(le / "work"), "--logs-dir", str(le / "logs"),
               "--cert-name", "stowaway", "--key-type", "ecdsa", "--expand"]
        email = s.get("le_email", "").strip()
        cmd += ["--email", email] if email else ["--register-unsafely-without-email"]
        if s.get("le_staging"):
            cmd.append("--test-cert")
        if os.environ.get("STOWAWAY_ACME_SERVER"):          # for testing against a local ACME server
            cmd += ["--server", os.environ["STOWAWAY_ACME_SERVER"]]
        env = dict(os.environ)

        if source == "duckdns":
            # DuckDNS holds one TXT record at a time, so ask for the wildcard only.
            names = [f"*.{domain}"]
            env["DUCKDNS_TOKEN"] = s.get("duckdns_token", "")
            cmd += ["--authenticator", "dns-duckdns", "--dns-duckdns-propagation-seconds", "60"]
        elif source == "cloudflare":
            names = [domain, f"*.{domain}"]
            ini = le / "cloudflare.ini"
            ini.parent.mkdir(parents=True, exist_ok=True)
            ini.write_text(f"dns_cloudflare_api_token = {s.get('cloudflare_token', '')}\n")
            os.chmod(ini, 0o600)
            cmd += ["--authenticator", "dns-cloudflare", "--dns-cloudflare-credentials", str(ini),
                    "--dns-cloudflare-propagation-seconds", "30"]
        else:  # http: no wildcards, one name per app
            names = self.wanted_names()
            cmd += ["--authenticator", "standalone", "--http-01-port", str(int(s.get("http_challenge_port") or 8480))]
        for n in names:
            cmd += ["-d", n]
        return cmd, env, names

    def problems(self):
        """Settings that must be filled in before asking Let's Encrypt."""
        s = self.settings()
        source = s.get("cert_source")
        domain = s.get("https_domain", "").strip().lower()
        if not domain:
            return "Enter your domain first."
        if not DOMAIN_RE.match(domain):
            return f"'{domain}' doesn't look like a domain name."
        if source == "duckdns":
            if not domain.endswith(".duckdns.org"):
                return "A DuckDNS domain ends in .duckdns.org, e.g. myhome.duckdns.org."
            if not s.get("duckdns_token"):
                return "Enter your DuckDNS token (shown at the top of duckdns.org after you sign in)."
        if source == "cloudflare" and not s.get("cloudflare_token"):
            return "Enter a Cloudflare API token with Zone → DNS → Edit permission."
        if source == "http" and not self.wanted_names():
            return "Enable Stowaway for at least one app first."
        return None

    async def request_le(self, force=False):
        problem = self.problems()
        if problem:
            self.error = problem
            return False
        cmd, env, names = self.certbot_command()
        if force:
            cmd.append("--force-renewal")
        else:
            cmd.append("--keep-until-expiring")
        log.info("requesting certificate from Let's Encrypt for %s", ", ".join(names))
        self.last_attempt = time.time()
        before = self.fingerprint()
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 600)
        except asyncio.TimeoutError:
            proc.kill()
            self.error = "Let's Encrypt took more than 10 minutes and was stopped."
            return False
        text = out.decode(errors="replace")
        for secret in (s_ := self.settings()).get("duckdns_token"), s_.get("cloudflare_token"):
            if secret:
                text = text.replace(secret, "********")
        logs = self.le_dir / "logs"
        if logs.exists():
            os.chmod(logs, 0o700)
        self.log_tail = "\n".join(text.strip().splitlines()[-25:])
        if proc.returncode != 0:
            self.error = self.explain(text)
            log.error("certificate request failed: %s", self.error)
            return False
        self.error = None
        if self.fingerprint() != before:
            log.info("new certificate installed")
        return True

    @staticmethod
    def explain(text: str) -> str:
        t = text.lower()
        if "too many certificates" in t or "ratelimited" in t or "rate limit" in t:
            return ("Let's Encrypt's rate limit was reached. Wait a while (up to a week) or tick "
                    "'Test certificates' while experimenting.")
        if any(k in t for k in ("max retries exceeded", "failed to establish a new connection",
                                "name or service not known", "temporary failure in name resolution",
                                "proxyerror", "network is unreachable")):
            where = "DuckDNS" if "duckdns.org" in t else "Cloudflare" if "cloudflare.com" in t else "Let's Encrypt"
            return f"Couldn't reach {where}. Check that the server can reach the internet."
        if "duckdns" in t and (re.search(r"\bko\b", t) or "invalid token" in t or "unauthorized" in t):
            return "DuckDNS rejected the token or domain. Check both on duckdns.org."
        if "cloudflare" in t and ("authentication error" in t or "invalid api token" in t
                                  or "9109" in t or "10000" in t or "could not find a zone" in t):
            return ("Cloudflare rejected the request. The API token needs Zone → DNS → Edit "
                    "permission for this domain, and the domain must be in that Cloudflare account.")
        if "could not bind" in t or "address already in use" in t or "problem binding" in t:
            return "The HTTP check port is in use by another program. Pick a different one."
        if "invalid response from" in t and "acme-challenge" in t:
            return ("Let's Encrypt reached a different web server instead of Stowaway. Make sure your "
                    "router forwards port 80 to this server's HTTP check port, not to another program.")
        if "timeout during connect" in t or ("connection" in t and ("timeout" in t or "refused" in t)):
            return ("Let's Encrypt couldn't reach this server. Check that your router forwards "
                    "port 80 to the HTTP check port, and that the names point to your public IP.")
        if "nxdomain" in t or "no valid ip addresses" in t or "dns problem" in t:
            return "Let's Encrypt couldn't look up your domain. Check that its DNS records exist."
        if "incorrect txt record" in t or "no txt record found" in t:
            return "The DNS check record wasn't found yet. Try again in a few minutes."
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        generic = ("ask for help", "see the logfile", "re-run certbot", "saving debug log")
        useful = [l for l in lines if l.lower().startswith(("detail:", "error:", "type:"))] or \
                 [l for l in lines if not any(g in l.lower() for g in generic)]
        return "Certificate request failed: " + (useful[-1][:300] if useful else "no details")

    # ---- keep things current ----
    async def ensure(self, force=False):
        """Make sure the chosen source has a certificate covering what's needed."""
        if self.busy:
            return
        s = self.settings()
        if not s.get("https_enabled"):
            return
        source = s.get("cert_source", "selfsigned")
        self.busy = True
        before = self.fingerprint()
        try:
            st = self.status()
            if source == "selfsigned":
                if force or not st["has_cert"] or st["missing_names"] or \
                        (st["not_after"] and st["not_after"] - time.time() < RENEW_BEFORE):
                    await asyncio.to_thread(self.make_selfsigned)
                self.error = None
            elif source == "custom":
                self.error = None if st["has_cert"] else (
                    f"Put fullchain.pem and privkey.pem in {st['custom_dir']} (config/certs/custom in the Stowaway folder).")
            elif source in LE_SOURCES:
                due = (not st["has_cert"] or st["missing_names"] or st["self_signed"] or
                       (st["not_after"] and st["not_after"] - time.time() < RENEW_BEFORE))
                if force or due:
                    # after a failure, retry at most every 6 hours unless asked
                    if force or not self.error or time.time() - self.last_attempt > 6 * 3600:
                        await self.request_le(force=force and st["has_cert"] and not st["missing_names"])
        finally:
            self.busy = False
            if self.fingerprint() != before:
                self.changed.set()

    async def loop(self):
        while True:
            try:
                await self.ensure()
            except Exception:
                log.exception("certificate check failed")
            await asyncio.sleep(12 * 3600)
