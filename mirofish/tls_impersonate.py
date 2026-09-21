"""Optional BoringSSL-shaped TLS transport via curl-impersonate.

Python's OpenSSL ClientHello is the one layer this relay cannot configure into
the official Electron/BoringSSL shape (JA3/JA4). When
``Settings.tls_impersonate`` is set (e.g. ``chrome136``), upstream HTTP is
sent through ``curl_cffi`` so the handshake matches a Chromium family client
while header/body fidelity stays in :mod:`mirofish.upstream`.

The default path remains httpx + OpenSSL so the respx-based suite keeps
working and deployments without the extra can still run.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Optional

import anyio
import httpx


def impersonation_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


class ImpersonatingClient:
    """httpx-shaped façade over one ``curl_cffi.AsyncSession``."""

    def __init__(self, impersonate: str, proxy: Optional[str],
                 timeout: float, *, verify: bool = True) -> None:
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession

        self._impersonate = impersonate
        self._proxy = "" if not proxy or proxy == "direct" else proxy
        self._timeout = timeout
        self._verify = verify
        self._session = AsyncSession(
            verify=verify, trust_env=False, discard_cookies=True,
            curl_options={
                # libcurl also reads proxy environment variables itself.
                CurlOpt.PROXY: self._proxy,
                CurlOpt.NOPROXY: "",
                # httpx owns decoding; Codex aiter_raw must remain raw.
                CurlOpt.HTTP_CONTENT_DECODING: 0,
            })

    async def aclose(self) -> None:
        await self._session.close()

    async def send(self, request: httpx.Request, *,
                   stream: bool = False) -> httpx.Response:
        from curl_cffi import CurlError

        headers = _ordered_headers(request)
        body = request.content or None
        timeout = request.extensions.get("timeout") if request.extensions else None
        # httpx Timeout.as_dict() → {"connect": x, "read": y, ...}
        if isinstance(timeout, dict):
            read = float(timeout.get("read") or self._timeout)
            connect = float(timeout.get("connect") or min(10.0, read))
            curl_timeout: Any = (connect, read)
        else:
            curl_timeout = self._timeout

        try:
            response = await self._session.request(
                method=request.method,
                url=str(request.url),
                content=body,
                headers=headers,
                proxy=self._proxy,
                impersonate=self._impersonate,
                # Golden profiles use HTTP/1.1 and our headers, not Chrome's.
                http_version="v1",
                default_headers=False,
                accept_encoding=None,
                timeout=curl_timeout,
                stream=stream,
                allow_redirects=False,
                verify=self._verify,
                # Only upstream.py's account/exit Codex jar may retain cookies.
                discard_cookies=True,
            )
        except CurlError as exc:
            raise _httpx_error(exc, request) from exc
        return _to_httpx_response(response, request=request, stream=stream)


def _httpx_error(exc: Exception, request: httpx.Request, *,
                 reading: bool = False) -> httpx.HTTPError:
    from curl_cffi.requests import exceptions

    if isinstance(exc, exceptions.Timeout):
        kind = httpx.ReadTimeout if reading else httpx.ConnectTimeout
    elif isinstance(exc, exceptions.ProxyError):
        kind = httpx.ProxyError
    elif isinstance(exc, exceptions.ConnectionError):
        kind = httpx.ReadError if reading else httpx.ConnectError
    else:
        kind = httpx.ReadError if reading else httpx.RequestError
    return kind(str(exc), request=request)


def _ordered_headers(request: httpx.Request) -> list[tuple[str, str]]:
    """Preserve wire order; httpx Headers.items() lowercases names.

    The capture-derived profiles care about order and casing, so read the raw
    list and decode each pair independently.
    """
    return [(name.decode("ascii"), value.decode("latin1"))
            for name, value in request.headers.raw]


class _CurlByteStream(httpx.AsyncByteStream):
    def __init__(self, response: Any, request: httpx.Request) -> None:
        self._response = response
        self._request = request
        self._closed = False

    async def __aiter__(self):
        from curl_cffi import CurlError

        try:
            async for chunk in self._response.aiter_content():
                yield chunk
        except CurlError as exc:
            raise _httpx_error(exc, self._request, reading=True) from exc
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            # curl_cffi.aclose waits for EOF; a caller disconnecting from a
            # quiet SSE stream must instead stop the transfer immediately.
            quit_now = getattr(self._response, "quit_now", None)
            if quit_now is not None:
                quit_now.set()
            task = getattr(self._response, "astream_task", None)
            if task is not None and not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await self._response.aclose()


def _to_httpx_response(response: Any, *, request: httpx.Request,
                       stream: bool) -> httpx.Response:
    headers = [(k.encode("ascii"), v.encode("latin1"))
               for k, v in response.headers.multi_items()]
    if stream:
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            request=request,
            stream=_CurlByteStream(response, request),
            extensions={"http_version": b"HTTP/1.1"},
        )
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        request=request,
        content=response.content,
        extensions={"http_version": b"HTTP/1.1"},
    )
