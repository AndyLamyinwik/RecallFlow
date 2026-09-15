"""Fresh, bounded source reachability checks for the Open handoff boundary."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
import time
from urllib.parse import urljoin

import httpx

from app.services.fetch import default_peer_ip_getter, peer_ip_is_allowed
from app.services.url_policy import (
    Resolver,
    URLPolicyError,
    default_resolver,
    normalize_url_v1,
    public_addresses_v1,
)


class ReachabilityErrorCode(StrEnum):
    URL_POLICY = "url_policy"
    DNS_ERROR = "dns_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    HTTP_STATUS = "http_status"
    REDIRECT_LIMIT = "redirect_limit"
    REDIRECT_POLICY = "redirect_policy"
    PEER_IP_UNAVAILABLE = "peer_ip_unavailable"
    PEER_IP_MISMATCH = "peer_ip_mismatch"
    STALE_STATE = "stale_state"


class SourceReachabilityError(RuntimeError):
    """A safe, non-content error suitable for an allow-listed action event."""

    def __init__(
        self,
        code: ReachabilityErrorCode,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class SourceReachability:
    """Proof limited to reachability; this is never evidence of reading."""

    final_url: str
    method: str
    status_class: str


class SafeSourceReachabilityChecker:
    """Validate DNS and peer IP on every bounded HEAD/GET redirect hop."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver = default_resolver,
        peer_ip_getter: Callable[[httpx.Response], str | None] = default_peer_ip_getter,
        connect_timeout_seconds: float = 3.0,
        total_budget_seconds: float = 10.0,
        max_bytes: int = 16 * 1024,
        max_redirects: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self._resolver = resolver
        self._peer_ip_getter = peer_ip_getter
        self._connect_timeout_seconds = connect_timeout_seconds
        self._total_budget_seconds = total_budget_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._monotonic = monotonic

    def check(self, source_url: str) -> SourceReachability:
        try:
            current_url = normalize_url_v1(source_url)
        except URLPolicyError as exc:
            raise SourceReachabilityError(
                ReachabilityErrorCode.URL_POLICY,
                "The source address is no longer allowed",
            ) from exc

        started_at = self._monotonic()
        method = "HEAD"
        redirects_followed = 0

        while True:
            remaining = self._total_budget_seconds - (self._monotonic() - started_at)
            if remaining <= 0:
                raise SourceReachabilityError(
                    ReachabilityErrorCode.TIMEOUT,
                    "The source reachability check timed out",
                    retryable=True,
                )
            try:
                allowed_addresses = public_addresses_v1(current_url, self._resolver)
            except URLPolicyError as exc:
                if exc.code.value == "DNS_ERROR":
                    code = ReachabilityErrorCode.DNS_ERROR
                elif redirects_followed == 0:
                    code = ReachabilityErrorCode.URL_POLICY
                else:
                    code = ReachabilityErrorCode.REDIRECT_POLICY
                raise SourceReachabilityError(
                    code,
                    "The source could not be checked safely",
                    retryable=code is ReachabilityErrorCode.DNS_ERROR,
                ) from exc

            request_timeout = max(0.001, remaining)
            timeout = httpx.Timeout(
                connect=min(self._connect_timeout_seconds, request_timeout),
                read=request_timeout,
                write=min(self._connect_timeout_seconds, request_timeout),
                pool=min(self._connect_timeout_seconds, request_timeout),
            )
            headers = {
                "User-Agent": "RecallFlow/1.0 source reachability",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.1",
            }
            if method == "GET":
                headers["Range"] = f"bytes=0-{self._max_bytes - 1}"

            try:
                request = self._client.build_request(
                    method,
                    current_url,
                    timeout=timeout,
                    headers=headers,
                )
                # This Client is shared across pilot users. httpx keeps
                # Set-Cookie values received from remote sites, so strip the
                # synthesized Cookie header from every probe to keep checks
                # stateless and isolated between users.
                request.headers.pop("cookie", None)
                with closing(
                    self._client.send(
                        request,
                        stream=True,
                        follow_redirects=False,
                    )
                ) as response:
                    peer_ip = self._peer_ip_getter(response)
                    if peer_ip is None:
                        raise SourceReachabilityError(
                            ReachabilityErrorCode.PEER_IP_UNAVAILABLE,
                            "The connected peer address could not be verified",
                        )
                    if not peer_ip_is_allowed(peer_ip, allowed_addresses):
                        raise SourceReachabilityError(
                            ReachabilityErrorCode.PEER_IP_MISMATCH,
                            "The connected peer address changed during validation",
                        )

                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or redirects_followed >= self._max_redirects:
                            raise SourceReachabilityError(
                                ReachabilityErrorCode.REDIRECT_LIMIT,
                                "The source redirected too many times",
                            )
                        try:
                            current_url = normalize_url_v1(
                                urljoin(current_url, location)
                            )
                        except URLPolicyError as exc:
                            raise SourceReachabilityError(
                                ReachabilityErrorCode.REDIRECT_POLICY,
                                "The source redirected to a disallowed target",
                            ) from exc
                        redirects_followed += 1
                        continue

                    if method == "HEAD" and response.status_code in {405, 501}:
                        method = "GET"
                        continue

                    if not 200 <= response.status_code < 300:
                        raise SourceReachabilityError(
                            ReachabilityErrorCode.HTTP_STATUS,
                            "The source returned an unsuccessful response",
                            status_code=response.status_code,
                            retryable=response.status_code >= 500,
                        )

                    # The GET request is streamed, carries a bounded Range,
                    # and deliberately does not consume the article body.
                    return SourceReachability(
                        final_url=current_url,
                        method=method.lower(),
                        status_class=f"{response.status_code // 100}xx",
                    )
            except SourceReachabilityError:
                raise
            except httpx.TimeoutException as exc:
                raise SourceReachabilityError(
                    ReachabilityErrorCode.TIMEOUT,
                    "The source reachability check timed out",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise SourceReachabilityError(
                    ReachabilityErrorCode.NETWORK_ERROR,
                    "The source could not be reached",
                    retryable=True,
                ) from exc
