"""Bounded, redirect-aware HTTP fetching for public article capture."""

from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import time
from urllib.parse import urljoin

import httpx

from app.models import ExtractionErrorType
from app.services.url_policy import (
    Resolver,
    URLPolicyError,
    URLPolicyErrorCode,
    default_resolver,
    normalize_url_v1,
    public_addresses_v1,
)


@dataclass(frozen=True)
class FetchResult:
    content: bytes
    final_url: str
    content_type: str


class FetchError(RuntimeError):
    def __init__(
        self,
        code: ExtractionErrorType,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


PeerIPGetter = Callable[[httpx.Response], str | None]


def default_peer_ip_getter(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return None
    address = stream.get_extra_info("server_addr") or stream.get_extra_info("peername")
    if isinstance(address, tuple) and address:
        return str(address[0])
    if isinstance(address, str):
        return address
    return None


def peer_ip_is_allowed(peer: str, allowed: set[str]) -> bool:
    try:
        peer_address = ipaddress.ip_address(peer.split("%", 1)[0])
        return any(peer_address == ipaddress.ip_address(item.split("%", 1)[0]) for item in allowed)
    except ValueError:
        return False


class SafeHttpFetcher:
    """Small synchronous fetcher; tests inject HTTP transport, DNS, and peer data."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver = default_resolver,
        peer_ip_getter: PeerIPGetter = default_peer_ip_getter,
        connect_timeout_seconds: float = 3.0,
        read_budget_seconds: float = 10.0,
        max_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self._resolver = resolver
        self._peer_ip_getter = peer_ip_getter
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_budget_seconds = read_budget_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._monotonic = monotonic

    def fetch(self, normalized_url: str) -> FetchResult:
        current_url = normalized_url
        started_at = self._monotonic()

        for redirects_followed in range(self._max_redirects + 1):
            try:
                allowed_addresses = public_addresses_v1(current_url, self._resolver)
            except URLPolicyError as exc:
                if redirects_followed == 0 and exc.code is URLPolicyErrorCode.PRIVATE_TARGET:
                    raise
                code = (
                    ExtractionErrorType.DNS_ERROR
                    if exc.code is URLPolicyErrorCode.DNS_ERROR
                    else ExtractionErrorType.REDIRECT_POLICY
                )
                raise FetchError(code, "The source could not be fetched safely") from exc

            timeout = httpx.Timeout(
                connect=self._connect_timeout_seconds,
                read=self._read_budget_seconds,
                write=self._connect_timeout_seconds,
                pool=self._connect_timeout_seconds,
            )
            try:
                with self._client.stream(
                    "GET",
                    current_url,
                    follow_redirects=False,
                    timeout=timeout,
                    headers={
                        "User-Agent": "RecallFlow/1.0 article capture",
                        "Accept": "text/html,application/xhtml+xml",
                    },
                ) as response:
                    peer_ip = self._peer_ip_getter(response)
                    if peer_ip is None:
                        raise FetchError(
                            ExtractionErrorType.PEER_IP_UNAVAILABLE,
                            "The connected peer address could not be verified",
                        )
                    if not peer_ip_is_allowed(peer_ip, allowed_addresses):
                        raise FetchError(
                            ExtractionErrorType.PEER_IP_MISMATCH,
                            "The connected peer address changed during validation",
                        )

                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or redirects_followed >= self._max_redirects:
                            raise FetchError(
                                ExtractionErrorType.REDIRECT_LIMIT,
                                "The source redirected too many times",
                            )
                        try:
                            current_url = normalize_url_v1(urljoin(current_url, location))
                        except URLPolicyError as exc:
                            raise FetchError(
                                ExtractionErrorType.REDIRECT_POLICY,
                                "The source redirected to a disallowed target",
                            ) from exc
                        continue

                    if response.status_code < 200 or response.status_code >= 300:
                        raise FetchError(
                            ExtractionErrorType.HTTP_STATUS,
                            "The source returned an unsuccessful response",
                            status_code=response.status_code,
                        )

                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise FetchError(
                            ExtractionErrorType.UNSUPPORTED_MIME,
                            "The source is not a supported HTML article",
                        )
                    length_header = response.headers.get("content-length")
                    if length_header and length_header.isdigit() and int(length_header) > self._max_bytes:
                        raise FetchError(ExtractionErrorType.TOO_LARGE, "The source response is too large")

                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if self._monotonic() - started_at > self._read_budget_seconds:
                            raise FetchError(ExtractionErrorType.TIMEOUT, "The source fetch timed out")
                        if len(body) + len(chunk) > self._max_bytes:
                            raise FetchError(ExtractionErrorType.TOO_LARGE, "The source response is too large")
                        body.extend(chunk)
                    return FetchResult(bytes(body), current_url, content_type)
            except FetchError:
                raise
            except httpx.TimeoutException as exc:
                raise FetchError(ExtractionErrorType.TIMEOUT, "The source fetch timed out") from exc
            except httpx.HTTPError as exc:
                raise FetchError(ExtractionErrorType.NETWORK_ERROR, "The source could not be reached") from exc

        raise FetchError(ExtractionErrorType.REDIRECT_LIMIT, "The source redirected too many times")
