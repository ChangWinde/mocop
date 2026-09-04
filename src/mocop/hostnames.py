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
from urllib.parse import urlsplit

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
