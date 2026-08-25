"""Canonical Host/Origin hostname normalization.

One shared spelling backs every trust comparison in the configuration loader
and the HTTP boundary: lowercase, unbracketed IP literals, and the absolute
DNS form (trailing dot) folded away, so equal names can never be split into
trusted and untrusted variants.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?")


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
