"""Authentication policy for non-public HTTP routes.

No missing credential or malformed configuration may implicitly select anonymous
access. The explicit ``none`` mode transfers caller authorization to deployment
reachability; browser Host and Fetch Metadata checks still precede routing.
"""

from __future__ import annotations

import hmac
from email.message import Message

from .api_manifest import DOCUMENTATION_URL
from .hostnames import is_trusted_request


def authentication_error(
    headers: Message,
    mode: str,
    token: str,
    trusted_hosts: frozenset[str],
) -> dict[str, str] | None:
    """Return a 403 envelope, or ``None`` when this request clears the policy."""
    if mode == "none":
        if is_trusted_request(headers, trusted_hosts):
            return None
        return {"error": "untrusted request origin", "code": "UNTRUSTED_ORIGIN"}
    values = headers.get_all("Authorization") or []
    if mode == "bearer" and token and len(values) == 1:
        value = values[0]
        if value.startswith("Bearer "):
            try:
                if hmac.compare_digest(value[len("Bearer ") :], token):
                    return None
            except TypeError:
                # Non-ASCII headers are invalid credentials, not server errors.
                pass
    return {
        "error": "dashboard authentication required",
        "code": "AUTHENTICATION_REQUIRED",
        "hint": (
            "Send 'Authorization: Bearer <capability>'. A managed "
            "service stores it in the private access-token file beside "
            "its configuration (~/.config/mocop/access-token by default); "
            "a foreground run prints it once as the URL fragment."
        ),
        "documentation": DOCUMENTATION_URL,
    }
