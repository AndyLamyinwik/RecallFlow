"""Strict URL normalization and SSRF target validation for capture v1."""

from collections.abc import Callable
from enum import StrEnum
import ipaddress
import socket
from urllib.parse import SplitResult, urlsplit, urlunsplit


class URLPolicyErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    DISALLOWED_SCHEME = "DISALLOWED_SCHEME"
    EMBEDDED_CREDENTIALS = "EMBEDDED_CREDENTIALS"
    DISALLOWED_PORT = "DISALLOWED_PORT"
    PRIVATE_TARGET = "PRIVATE_TARGET"
    DNS_ERROR = "DNS_ERROR"


class URLPolicyError(ValueError):
    def __init__(self, code: URLPolicyErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


Resolver = Callable[[str, int], set[str]]


def default_resolver(host: str, port: int) -> set[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise URLPolicyError(URLPolicyErrorCode.DNS_ERROR, "The source host could not be resolved") from exc
    return {record[4][0] for record in records}


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_global


def normalize_url_v1(raw_url: str) -> str:
    value = raw_url.strip()
    if not value or any(ord(character) < 32 for character in value) or "\\" in value:
        raise URLPolicyError(URLPolicyErrorCode.INVALID_URL, "Enter a valid public article URL")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise URLPolicyError(URLPolicyErrorCode.INVALID_URL, "Enter a valid public article URL") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise URLPolicyError(URLPolicyErrorCode.DISALLOWED_SCHEME, "Only HTTP and HTTPS URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise URLPolicyError(URLPolicyErrorCode.EMBEDDED_CREDENTIALS, "URLs containing credentials are not allowed")
    if parsed.hostname is None:
        raise URLPolicyError(URLPolicyErrorCode.INVALID_URL, "Enter a valid public article URL")
    if port is not None and port != (443 if scheme == "https" else 80):
        raise URLPolicyError(URLPolicyErrorCode.DISALLOWED_PORT, "This URL uses a disallowed port")

    host = parsed.hostname.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLPolicyError(URLPolicyErrorCode.INVALID_URL, "Enter a valid public article URL") from exc
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise URLPolicyError(URLPolicyErrorCode.PRIVATE_TARGET, "The URL must use a public host")

    try:
        literal_ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise URLPolicyError(URLPolicyErrorCode.PRIVATE_TARGET, "Private or reserved network targets are not allowed")

    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    normalized = SplitResult(scheme, netloc, parsed.path or "/", parsed.query, "")
    return urlunsplit(normalized)


def public_addresses_v1(url: str, resolver: Resolver = default_resolver) -> set[str]:
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise URLPolicyError(URLPolicyErrorCode.INVALID_URL, "Enter a valid public article URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = resolver(host, port)
    if not addresses:
        raise URLPolicyError(URLPolicyErrorCode.DNS_ERROR, "The source host could not be resolved")
    if any(not _is_public_ip(address) for address in addresses):
        raise URLPolicyError(URLPolicyErrorCode.PRIVATE_TARGET, "The source resolves to a private or reserved address")
    return addresses
