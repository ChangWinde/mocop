"""Canonical Host/Origin hostname normalization and web trust policy.

One shared spelling backs every trust comparison in the configuration loader
and the HTTP boundary: lowercase, unbracketed IP literals, and the absolute
DNS form (trailing dot) folded away, so equal names can never be split into
trusted and untrusted variants. The trust-policy builder lives beside the
normalizer so both sides of every comparison use the same grammar.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?")
# Hostnames a browser can present when it genuinely reached this server over
# the loopback interface. DNS rebinding presents the attacker's own domain in
# Host/Origin instead, so pinning these names closes the rebinding bypass.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_WILDCARD_BIND_HOSTS = frozenset({"", "0.0.0.0", "::"})


def trusted_web_policy(
    bind_host: str, trusted_hosts: Iterable[str] | None
) -> tuple[frozenset[str], frozenset[str]]:
    """Return exact Host authorities and HTTPS-only Origin suffixes.

    Reverse proxies commonly rewrite ``Host`` to the loopback upstream while
    preserving the browser's public ``Origin``. Exact entries remain valid for
    both headers. A leading ``*.`` is deliberately narrower: it authorizes only
    HTTPS origins below that DNS suffix and never relaxes the Host check.
    """
    trusted = set(_LOOPBACK_HOSTNAMES)
    origin_suffixes: set[str] = set()
    if str(bind_host).strip().lower() not in _WILDCARD_BIND_HOSTS:
        bind_hostname = normalize_web_hostname(bind_host)
        if bind_hostname is not None:
            trusted.add(bind_hostname)
    for candidate in trusted_hosts or ():
        if isinstance(candidate, str) and candidate.strip().startswith("*."):
            suffix = normalize_web_hostname(candidate.strip()[2:])
            if suffix is None or "." not in suffix:
                raise ValueError(f"invalid trusted web origin suffix: {candidate!r}")
            try:
                ipaddress.ip_address(suffix)
            except ValueError:
                origin_suffixes.add(suffix)
                continue
            raise ValueError(f"invalid trusted web origin suffix: {candidate!r}")
        hostname = normalize_web_hostname(candidate)
        if hostname is None:
            raise ValueError(f"invalid trusted web host: {candidate!r}")
        trusted.add(hostname)
    return frozenset(trusted), frozenset(origin_suffixes)


class _Headers(Protocol):
    """The two reads the guards need from ``http.server`` request headers."""

    def get(self, name: str, default: str | None = None) -> str | None: ...

    def get_all(self, name: str) -> list[str] | None: ...


_SAME_ORIGIN_FETCH_SITES = frozenset({"", "same-origin", "none"})


def has_trusted_host(headers: _Headers, trusted_hostnames: frozenset[str]) -> bool:
    """Require exactly one Host header naming one of the server's own names.

    A loopback bind alone does not stop DNS rebinding: an attacker domain
    re-resolved to 127.0.0.1 makes the victim's browser send same-origin
    requests here, but with the attacker's hostname in Host. Pinning Host to
    the loopback/configured allowlist closes that path.
    """
    host_values = headers.get_all("Host") or []
    if len(host_values) != 1:
        return False
    hostname = normalize_web_hostname(host_values[0], allow_port=True)
    return hostname is not None and hostname in trusted_hostnames


def is_dashboard_read(headers: _Headers, trusted_hostnames: frozenset[str]) -> bool:
    """A marked read from a trusted Host that Fetch Metadata does not call cross-site."""
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    return (
        has_trusted_host(headers, trusted_hostnames)
        and headers.get("X-Monitor-Request") == "dashboard"
        and fetch_site in _SAME_ORIGIN_FETCH_SITES
    )


def is_dashboard_write(
    headers: _Headers,
    trusted_hostnames: frozenset[str],
    trusted_origin_suffixes: frozenset[str],
) -> bool:
    """A marked write whose Origin is one trusted authority or HTTPS suffix.

    The Origin must be a bare scheme and authority: credentials, a path other
    than ``/``, a query, or a fragment mark a forged or proxied value.
    """
    if not is_dashboard_read(headers, trusted_hostnames):
        return False
    origin = headers.get("Origin")
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and _has_trusted_origin(parsed, trusted_hostnames, trusted_origin_suffixes)
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _has_trusted_origin(
    origin: SplitResult,
    trusted_hostnames: frozenset[str],
    trusted_origin_suffixes: frozenset[str],
) -> bool:
    """Match an exact authority or one configured HTTPS subdomain suffix."""
    hostname = normalize_web_hostname(origin.hostname)
    if hostname is None:
        return False
    if hostname in trusted_hostnames:
        return True
    if origin.scheme != "https":
        return False
    return any(hostname.endswith(f".{suffix}") for suffix in trusted_origin_suffixes)


def normalize_web_hostname(value: object, *, allow_port: bool = False) -> str | None:
    """Return the canonical lowercase hostname for a Host-style value.

    Accepts DNS names, IPv4 literals, and IPv6 literals (bare or bracketed).
    A port suffix is accepted only when ``allow_port`` is true; schemes,
    credentials, paths, and queries are always rejected. Returns ``None``
    when the value cannot be interpreted as a plain hostname.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 260
        or any(
            unicodedata.category(character).startswith("C") for character in candidate
        )
    ):
        return None
    if candidate.count(":") >= 2 and not candidate.startswith("["):
        candidate = f"[{candidate}]"  # bare IPv6 literal
    try:
        parsed = urlsplit(f"//{candidate}")
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and not allow_port)
    ):
        return None
    if "[" in parsed.netloc:
        # Bracketed hosts must be IP literals even on Pythons whose urlsplit
        # does not validate them.
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return None
    else:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if len(hostname) > 253 or not hostname.isascii():
                return None
            labels = hostname.removesuffix(".").split(".")
            if any(
                not label or len(label) > 63 or not _LABEL.fullmatch(label)
                for label in labels
            ):
                return None
            # A trailing dot is the absolute form of the same DNS name; keep
            # one canonical spelling so trusted-host and Origin comparisons
            # cannot be split by it.
            return hostname.removesuffix(".")
    return hostname
