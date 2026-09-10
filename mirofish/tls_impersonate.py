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

import logging
from typing import Any, Mapping, Optional, Sequence

import httpx

logger = logging.getLogger("mirofish.tls_impersonate")


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
        from curl_cffi.requests import AsyncSession

        self._impersonate = impersonate
        self._proxy = proxy or None
        self._timeout = timeout
        self._verify = verify
        self._session = AsyncSession(verify=verify)

    async def aclose(self) -> None:
        await self._session.close()

    async def send(self, request: httpx.Request, *,
                   stream: bool = False) -> httpx.Response:
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

        response = await self._session.request(
            method=request.method,
            url=str(request.url),
            content=body,
            headers=headers,
            proxy=self._proxy,
            impersonate=self._impersonate,
            # Our golden profiles pin HTTP/1.1 header order; Chrome's h2
            # framing would change the entire wire shape.
            http_version="v1",
            # Header/body fidelity comes from upstream.py, not from curl's
            # browser default header set.
            default_headers=False,
            accept_encoding=None,
            timeout=curl_timeout,
            stream=stream,
            allow_redirects=False,
            verify=self._verify,
        )
        return _to_httpx_response(response, stream=stream)


def _ordered_headers(request: httpx.Request) -> list[tuple[str, str]]:
    """Preserve wire order; httpx Headers.items() lowercases names.

    The capture-derived profiles care about order and casing, so read the raw
    list and decode each pair independently.
    """
    return [(name.decode("ascii"), value.decode("ascii"))
            for name, value in request.headers.raw]


class _CurlByteStream(httpx.AsyncByteStream):
    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aiter__(self):
        async for chunk in self._response.aiter_content():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


def _to_httpx_response(response: Any, *, stream: bool) -> httpx.Response:
    headers = [(k.encode("ascii"), v.encode("ascii"))
               for k, v in response.headers.multi_items()]
    if stream:
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            stream=_CurlByteStream(response),
            extensions={"http_version": b"HTTP/1.1"},
        )
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        content=response.content,
        extensions={"http_version": b"HTTP/1.1"},
    )
