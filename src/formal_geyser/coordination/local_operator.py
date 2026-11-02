"""Operator requests carry the project's operator token.

TaskHub and the Lean service each keep a random token in the user's runtime directory, in a
file only the user can read; the worker sandboxes do not see that directory. Local clients
send the token as a bearer credential. A browser signs in once with a link that holds it and
then keeps it in a strict, HTTP-only cookie. Every request must also name a loopback host, so
that a web page cannot reach a service through a name it controls (DNS rebinding), and a
browser's request must come from the service's own origin.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

COOKIE = "formal_geyser_operator"


def token_path(root: Path, service: str) -> Path:
    """Where `service` ("taskhub" or "lean-service") keeps the token of the project at `root`."""
    base = os.environ.get("FORMAL_GEYSER_RUNTIME_DIR") or os.path.join(
        os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "formal-geyser")
    project = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:24]
    return Path(base) / project / (service + ".token")


def operator_token(root: Path, service: str) -> str | None:
    try:
        return token_path(root, service).read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def issue_token(root: Path, service: str) -> str:
    """The service's token, created on first use and kept until the runtime directory is
    cleared, at reboot: a restarted service keeps its signed-in browsers."""
    path = token_path(root, service)
    existing = operator_token(root, service)
    if existing is not None:
        return existing
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(secrets.token_urlsafe(32))
    try:
        os.link(temporary, path)  # Never replaces a token another process issued meanwhile.
    except FileExistsError:
        pass
    finally:
        temporary.unlink()
    token = operator_token(root, service)
    if token is None:
        raise OSError("The operator token could not be read back: " + str(path))
    return token


def is_loopback(host: str) -> bool:
    """Whether a host name or address is this machine's loopback."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def loopback_url(value: str) -> str:
    """A service root URL on this machine, the only place operator credentials may go."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("The service URL must be an HTTP or HTTPS loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The service URL cannot contain credentials, a query, or a fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("The service URL must name the service root")
    if not is_loopback(parsed.hostname):
        raise ValueError("Operator credentials may only be sent to a loopback service")
    return value.rstrip("/")


def _same_token(supplied: str | None, token: str | None) -> bool:
    return bool(supplied) and token is not None and hmac.compare_digest(
        str(supplied).encode("utf-8"), token.encode("utf-8"))


def local_request(request: Any) -> bool:
    """A request that names a loopback host and, if a browser sent it, the same origin."""
    host = request.headers.get("host")
    try:
        name = urlsplit("//" + host).hostname if host else None
    except ValueError:
        return False
    if name is None or not is_loopback(name):
        return False
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == host


def operator_request(request: Any, token: str | None) -> bool:
    """A local request with the token, as a bearer credential or the signed-in cookie."""
    if not local_request(request):
        return False
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    supplied = credential.strip() if scheme.lower() == "bearer" else request.cookies.get(COOKIE)
    return _same_token(supplied, token)


def sign_in(request: Any, supplied: str, token: str | None) -> bool:
    """Whether a browser's sign-in link holds the token."""
    return local_request(request) and _same_token(supplied, token)


def bearer(root: Path, service: str) -> dict[str, str]:
    """The header a local client sends to the project's service; none before it has a token."""
    token = operator_token(root, service)
    return {"Authorization": "Bearer " + token} if token else {}
