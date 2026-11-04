"""Internal network or access from outside?

The basis is the visitor's real address. Behind a reverse proxy, proxy.py takes it from
X-Forwarded-For – but only if the connection comes from a trusted proxy.

Security rules:
* If a request comes via the proxy without the proxy sending the visitor address, it counts as
  **external** – otherwise every access from the internet would look like the (internal) proxy.
* When in doubt (address not readable): external.
* Loopback (127.0.0.1, ::1) does not count as internal by itself: it mostly carries forwards
  without a visitor address (SSH tunnel, stunnel, socat …), behind which anyone could be. If you want that,
  add 127.0.0.0/8 or ::1 explicitly under "Internal network".
"""
from __future__ import annotations

import ipaddress
import logging

from flask import has_request_context, request

from .i18n import _
from .models import get_setting

log = logging.getLogger(__name__)

# Without 172.16.0.0/12: Docker creates its networks there – otherwise e.g. every access arriving via the
# Docker gateway (172.17.0.1 …) would count as internal. If your LAN is there, add it explicitly.
DEFAULT_INTERNAL = "10.0.0.0/8\n192.168.0.0/16\nfc00::/7"


def parse(text: str) -> tuple[list, list[str]]:
    """Networks from text (one per line, commas also allowed); returns (networks, invalid entries)."""
    nets, bad = [], []
    for part in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        part = part.split("#")[0].strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            bad.append(part)
    return nets, bad


def internal_networks() -> list:
    text = get_setting("internal_networks")
    nets, _ = parse(text if text.strip() else DEFAULT_INTERNAL)
    return nets


def client_ip() -> str:
    return (request.remote_addr or "") if has_request_context() else ""


def via_proxy_without_client_ip() -> bool:
    """Proxy headers present but no visitor address – the origin is then unknown."""
    env = request.environ
    orig = env.get("werkzeug.proxy_fix.orig") or {}
    came_via_proxy = any(env.get(h) or orig.get(h) for h in
                         ("HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_REAL_IP", "HTTP_FORWARDED"))
    return came_via_proxy and not (env.get("HTTP_X_FORWARDED_FOR") or orig.get("HTTP_X_FORWARDED_FOR"))


# ---------------------------------------------------------------- own internet connection

HOST_RE = __import__("re").compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?$", 2)
RESOLVE_TTL = 300          # seconds: re-resolve DynDNS names every 5 minutes
_cache: dict[str, tuple[float, set, float]] = {}


def parse_own(text: str) -> tuple[list[str], list[str]]:
    """Entries for the own connection: IP addresses or DNS names; returns (valid, invalid)."""
    ok: list[str] = []
    bad: list[str] = []
    for part in (text or "").replace(",", "\n").replace(";", "\n").splitlines():
        part = part.split("#")[0].strip().lower()
        if not part:
            continue
        try:
            ok.append(str(ipaddress.ip_address(part)))
        except ValueError:
            (ok if HOST_RE.match(part) else bad).append(part)
    return ok, bad


def _resolve(host: str, now: float) -> set:
    """Addresses of a DynDNS name, re-resolved at most every RESOLVE_TTL seconds.

    Cache: host → (time of last successful resolution, addresses, time of last attempt). A failed
    attempt is remembered too – otherwise every request would re-resolve during a DNS outage."""
    import socket
    cached = _cache.get(host)
    if cached and now - cached[2] < RESOLVE_TTL:
        return cached[1]
    try:
        ips = {str(info[4][0]).split("%")[0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except OSError as exc:
        log.warning("Own connection: cannot resolve %s (%s)", host, exc)
        # keep using the last known address for at most one hour after the last successful resolution
        if cached and now - cached[0] < 3600:
            _cache[host] = (cached[0], cached[1], now)
            return cached[1]
        _cache[host] = (cached[0] if cached else 0.0, set(), now)
        return set()
    _cache[host] = (now, ips, now)
    return ips


def own_public_ips() -> set:
    """Current public address(es) of the own connection (static IP or DynDNS name)."""
    import time
    entries, _ = parse_own(get_setting("own_public_hosts"))
    now = time.time()
    ips = set()
    for e in entries:
        try:
            ips.add(str(ipaddress.ip_address(e)))
        except ValueError:
            ips |= _resolve(e, now)
    return ips


# ---------------------------------------------------------------- Classification

def _client_address() -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(client_ip().split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def classify() -> str:
    """'lan' (internal network), 'own' (via the own internet connection) or 'extern' (outside)."""
    if not has_request_context() or via_proxy_without_client_ip():
        return "extern"
    ip = _client_address()
    if ip is None:
        return "extern"
    if any(ip in n for n in internal_networks()):
        return "lan"
    if str(ip) in own_public_ips():
        return "own"
    return "extern"


def is_internal() -> bool:
    return classify() != "extern"


def describe() -> dict:
    """For the settings page: how is the current connection classified?"""
    zone = classify()
    return {"ip": client_ip() or _("unknown"), "internal": zone != "extern", "zone": zone,
            "own_ips": sorted(own_public_ips()),
            "proxy_without_ip": has_request_context() and via_proxy_without_client_ip()}
