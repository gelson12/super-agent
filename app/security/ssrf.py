"""
SSRF (Server-Side Request Forgery) protection utilities.

Provides:
  - assert_safe_url()  — raises ValueError for unsafe URLs
  - is_safe_url()      — boolean wrapper
  - assert_internal_host_ok() — for env-var hostnames that MUST be loopback/internal
  - safe_requests_get() / safe_httpx_get() — drop-in wrappers that validate before fetching

Design principles:
  - Fail closed: any parse error → rejected
  - Resolve hostname to IP *before* connecting to defeat DNS rebinding
  - Block RFC-1918, loopback, link-local, cloud metadata ranges
  - Allowlist-first: callers declare allowed domains; anything else is rejected
  - Redirects NOT followed by default — caller must re-validate each hop
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Iterable
from urllib.parse import urlparse

_log = logging.getLogger("ssrf")

# ---------------------------------------------------------------------------
# Blocked network ranges
# ---------------------------------------------------------------------------
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    # RFC-1918 private
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    # Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    # Link-local / AWS+GCP+Azure IMDS
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    # Reserved / unspecified
    ipaddress.ip_network("0.0.0.0/8"),
    # Carrier-grade NAT
    ipaddress.ip_network("100.64.0.0/10"),
    # IPv6 ULA
    ipaddress.ip_network("fc00::/7"),
    # Documentation / TEST-NET
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
]

# Hostnames that are always blocked regardless of DNS resolution
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({
    "169.254.169.254",           # AWS / GCP / Azure IMDS IPv4
    "fd00:ec2::254",             # AWS IMDS IPv6
    "metadata.google.internal",  # GCP metadata
    "metadata.google",
    "169.254.170.2",             # ECS task metadata
    "localhost",
})

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"https", "http"})


# ---------------------------------------------------------------------------
# Core IP check (after DNS resolution)
# ---------------------------------------------------------------------------

def _is_ip_blocked(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → block
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_unspecified:
        return True
    return any(addr in net for net in _BLOCKED_NETWORKS)


def _resolve_and_check(hostname: str) -> None:
    """Resolve hostname to all IPs and raise ValueError if any is blocked."""
    try:
        results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"SSRF: hostname '{hostname}' could not be resolved: {exc}") from exc
    if not results:
        raise ValueError(f"SSRF: no addresses resolved for '{hostname}'")
    for result in results:
        ip_str = result[4][0]
        if _is_ip_blocked(ip_str):
            raise ValueError(
                f"SSRF: hostname '{hostname}' resolves to blocked IP {ip_str}"
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assert_safe_url(
    url: str,
    *,
    allowed_domains: Iterable[str] | None = None,
    allowed_schemes: Iterable[str] | None = None,
    resolve_dns: bool = True,
) -> None:
    """
    Raise ValueError if `url` is unsafe to fetch server-side.

    Args:
        url:             The URL to validate.
        allowed_domains: If given, the hostname must equal or be a subdomain of
                         one of these domains (e.g. ["claude.ai", "claude.com"]).
        allowed_schemes: Allowed URL schemes. Defaults to {"https", "http"}.
        resolve_dns:     If True (default), resolve the hostname and block private IPs.
                         Set False only for loopback/internal-only destinations
                         (e.g. OLLAMA_HOST=127.0.0.1 is handled separately).
    """
    if not url or not isinstance(url, str):
        raise ValueError("SSRF: empty or non-string URL rejected")

    schemes = frozenset(allowed_schemes) if allowed_schemes is not None else _ALLOWED_SCHEMES
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValueError(f"SSRF: URL parse failed: {exc}") from exc

    if parsed.scheme not in schemes:
        raise ValueError(f"SSRF: scheme '{parsed.scheme}' not in allowed {schemes}")

    if parsed.username or parsed.password:
        raise ValueError("SSRF: embedded credentials in URL rejected")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("SSRF: missing hostname in URL")

    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError(f"SSRF: hostname '{hostname}' is blocked (cloud metadata / loopback)")

    if allowed_domains is not None:
        allowed = [d.lower().lstrip(".") for d in allowed_domains]
        if not any(hostname == d or hostname.endswith(f".{d}") for d in allowed):
            raise ValueError(
                f"SSRF: hostname '{hostname}' not in allowed domains {allowed}"
            )

    if resolve_dns:
        _resolve_and_check(hostname)


def is_safe_url(url: str, *, allowed_domains: Iterable[str] | None = None, **kw) -> bool:
    """Boolean wrapper around assert_safe_url. Logs the reason on rejection."""
    try:
        assert_safe_url(url, allowed_domains=allowed_domains, **kw)
        return True
    except ValueError as exc:
        _log.warning("%s", exc)
        return False


def assert_host_not_ssrf(host: str, *, allow_loopback: bool = False) -> None:
    """
    Validate a bare hostname/IP (no scheme) from an env var or config.
    Used for OLLAMA_HOST, N8N_BASE_URL, etc.

    allow_loopback=True permits 127.x.x.x / ::1 (needed for same-container services).
    All other private/RFC-1918 ranges are always blocked.
    """
    hostname = host.split(":")[0].strip()  # strip optional :port
    if not hostname:
        return  # empty → caller's problem

    if hostname in ("localhost", "127.0.0.1", "::1"):
        if allow_loopback:
            return
        raise ValueError(f"SSRF: loopback host '{hostname}' not permitted in this context")

    try:
        addr = ipaddress.ip_address(hostname)
        # It's a literal IP
        if addr.is_loopback:
            if allow_loopback:
                return
            raise ValueError(f"SSRF: loopback IP '{hostname}' not permitted")
        if addr.is_private or addr.is_link_local or addr.is_reserved:
            raise ValueError(f"SSRF: private/reserved IP '{hostname}' rejected")
        if any(addr in net for net in _BLOCKED_NETWORKS):
            raise ValueError(f"SSRF: IP '{hostname}' is in a blocked network range")
    except ValueError as exc:
        if "SSRF:" in str(exc):
            raise
        # Not a literal IP — it's a hostname; resolve it
        _resolve_and_check(hostname)


# ---------------------------------------------------------------------------
# Drop-in safe fetch wrappers
# ---------------------------------------------------------------------------

def safe_requests_get(url: str, allowed_domains: Iterable[str] | None = None, **kwargs):
    """
    Validate URL then call requests.get() with redirects disabled by default.
    Raises ValueError for SSRF-unsafe URLs; propagates requests exceptions normally.
    """
    import requests
    assert_safe_url(url, allowed_domains=allowed_domains)
    kwargs.setdefault("allow_redirects", False)
    kwargs.setdefault("timeout", 10)
    return requests.get(url, **kwargs)


def safe_httpx_get(url: str, allowed_domains: Iterable[str] | None = None, **kwargs):
    """
    Validate URL then call httpx.get() with redirects disabled by default.
    """
    import httpx
    assert_safe_url(url, allowed_domains=allowed_domains)
    kwargs.setdefault("follow_redirects", False)
    kwargs.setdefault("timeout", 10)
    return httpx.get(url, **kwargs)
