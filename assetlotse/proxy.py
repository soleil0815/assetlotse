"""Automatic operation behind a reverse proxy – without configuration.

* X-Forwarded-For/-Proto/-Host are only accepted from trusted senders.
  Default: private networks (10/8, 172.16/12, 192.168/16, 127/8, fc00::/7, ::1) – that is
  practically always where the reverse proxy sits (e.g. Nginx Proxy Manager in the same Docker network).
  Requests coming directly from the internet therefore cannot forge the headers.
* Cookies get the Secure flag automatically as soon as the request came via HTTPS
  (directly or according to the proxy). Login via http://IP:9343 on the LAN keeps working.
* HTTPS is enforced for the proxy's host name (since 2.4.0): a request that reaches the proxy over plain HTTP
  (X-Forwarded-Proto: http) is redirected to https://<host>/…, and every HTTPS response carries
  Strict-Transport-Security. Direct access via http://IP:9343 is not affected.

Environment variables (optional):
  ASSETLOTSE_TRUSTED_PROXIES  "private" (default), "none" or a list of networks/IPs,
                               e.g. "172.18.0.0/16, 192.168.0.11"
  ASSETLOTSE_SECURE_COOKIES   "auto" (default), "1" (always) or "0" (never)
  ASSETLOTSE_FORCE_HTTPS      "auto" (default: redirect + HSTS as above) or "0" (neither)
  ASSETLOTSE_BEHIND_PROXY     deprecated; "0" equals TRUSTED_PROXIES=none
"""
from __future__ import annotations

import ipaddress
import logging
import re
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from flask import Flask

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wsgi import get_current_url

log = logging.getLogger(__name__)

VIA_PROXY = "assetlotse.via_proxy"     # environ key: the request came from a trusted proxy
HSTS = "max-age=31536000"               # one year; only this host name (no includeSubDomains)
# a DNS name (not an IP address, not "localhost"): only then is https://<host>/ a sensible target
_HOSTNAME = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")

PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128"]


def parse_networks(value: str | None) -> list:
    value = (value or "private").strip().lower()
    if value in ("none", "off", "0", "no", ""):
        return []
    nets = []
    for part in value.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part == "private":
            nets += [ipaddress.ip_network(n) for n in PRIVATE_NETS]
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("ASSETLOTSE_TRUSTED_PROXIES: invalid entry %r ignored", part)
    return nets


class TrustedProxyFix:
    """Apply ProxyFix only if the connection comes from a trusted proxy."""

    def __init__(self, app: WSGIApplication, networks: list[IPNetwork]) -> None:
        self.app = app
        self.networks = networks
        self.fixed = ProxyFix(app, x_for=1, x_proto=1, x_host=0, x_port=1)

    def _trusted(self, addr: str | None) -> bool:
        try:
            ip = ipaddress.ip_address((addr or "").split("%")[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in n for n in self.networks)

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        if self.networks and self._trusted(environ.get("REMOTE_ADDR")):
            environ[VIA_PROXY] = True
            return self.fixed(environ, start_response)
        return self.app(environ, start_response)


class SecureCookies:
    """Set the Secure flag on all cookies for HTTPS requests."""

    def __init__(self, app: WSGIApplication, mode: str = "auto") -> None:
        self.app = app
        self.mode = mode

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        secure = self.mode == "1" or (self.mode == "auto" and environ.get("wsgi.url_scheme") == "https")
        if not secure:
            return self.app(environ, start_response)

        def _start(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            new = []
            for name, value in headers:
                if name.lower() == "set-cookie" and "secure" not in value.lower():
                    value += "; Secure"
                new.append((name, value))
            return start_response(status, new, exc_info)

        return self.app(environ, _start)


class HttpsGuard:
    """Enforce HTTPS for the proxy's host name: redirect plain-HTTP requests that came through the proxy, and send
    HSTS on every HTTPS response. Direct requests (http://IP:9343 on the LAN) are left alone."""

    def __init__(self, app: WSGIApplication) -> None:
        self.app = app

    @staticmethod
    def https_target(environ: WSGIEnvironment) -> str | None:
        """The https:// address for a plain-HTTP request through the proxy, or None (leave it alone)."""
        if not environ.get(VIA_PROXY) or environ.get("wsgi.url_scheme") != "http":
            return None
        proto = (environ.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[-1].strip().lower()
        port = (environ.get("HTTP_X_FORWARDED_PORT") or "").split(",")[-1].strip()
        # plain HTTP: the proxy says so, or – without a protocol header – it reports port 80.
        # Without either the proxy did not say how the visitor came: do nothing (no redirect loop).
        if not (proto == "http" or (not proto and port == "80")):
            return None
        host = (environ.get("HTTP_HOST") or "").strip().lower()
        if host.startswith("["):       # IPv6 literal
            return None
        host = host.rsplit(":", 1)[0] if ":" in host else host
        if not _HOSTNAME.match(host):
            return None
        return get_current_url({**environ, "wsgi.url_scheme": "https", "HTTP_HOST": host, "SERVER_PORT": "443"})

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        target = self.https_target(environ)
        if target:
            start_response("308 Permanent Redirect", [("Location", target), ("Content-Length", "0"),
                                                      ("Cache-Control", "no-store")])
            return [b""]
        if environ.get("wsgi.url_scheme") != "https":
            return self.app(environ, start_response)

        def _start(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            if not any(name.lower() == "strict-transport-security" for name, _ in headers):
                headers = [*headers, ("Strict-Transport-Security", HSTS)]
            return start_response(status, headers, exc_info)

        return self.app(environ, _start)


def install(app: Flask, env: Mapping[str, str]) -> None:
    mode = (env.get("ASSETLOTSE_SECURE_COOKIES") or "auto").strip().lower()
    if mode not in ("auto", "1", "0"):
        mode = "auto"
    trusted = env.get("ASSETLOTSE_TRUSTED_PROXIES")
    legacy = env.get("ASSETLOTSE_BEHIND_PROXY")
    if trusted is None and legacy == "0":
        trusted = "none"
    elif trusted is None and legacy == "1":
        trusted = "private"
    networks = parse_networks(trusted)
    inner: WSGIApplication = SecureCookies(app.wsgi_app, mode)
    if (env.get("ASSETLOTSE_FORCE_HTTPS") or "auto").strip().lower() not in ("0", "off", "no", "false"):
        inner = HttpsGuard(inner)
    # order: evaluate proxy headers first (sets https), then enforce HTTPS, then adjust cookies
    app.wsgi_app = TrustedProxyFix(inner, networks)  # type: ignore[method-assign]
