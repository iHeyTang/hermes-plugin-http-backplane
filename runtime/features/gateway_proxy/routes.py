"""Reverse-proxy ``/v1/*`` to ``127.0.0.1:8642/v1/*``.

See :mod:`runtime.features.gateway_proxy` for the rationale; this
module is the wire-level plumbing.
"""

from __future__ import annotations

import logging
import os

from aiohttp import ClientSession, ClientTimeout, web

logger = logging.getLogger(__name__)

GATEWAY_BASE = "http://127.0.0.1:8642"

# Hop-by-hop headers (RFC 7230 §6.1) plus things aiohttp derives itself.
# Forwarding these breaks framing or duplicates host info.
_HOP_HEADERS = frozenset(
    h.lower()
    for h in (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    )
)

_CLIENT_KEY = "gateway_proxy_client"


def _api_server_key() -> str:
    return (os.environ.get("API_SERVER_KEY") or "").strip()


_SESSION_HEADERS = frozenset(("x-hermes-session-id", "x-hermes-session-key"))


def _filter_request_headers(src) -> dict:
    """Copy inbound headers, strip hop-by-hop + inbound Authorization,
    inject the gateway's API_SERVER_KEY if set.

    Also strips ``X-Hermes-Session-*`` headers when no key is configured
    — the gateway rejects them with 403 in that mode, and we don't want
    callers to need to know which mode they're in. With this strip the
    extension can always send the headers and the proxy decides whether
    they reach upstream.
    """
    out: dict[str, str] = {}
    key = _api_server_key()
    for k, v in src.items():
        lk = k.lower()
        if lk in _HOP_HEADERS:
            continue
        if lk == "authorization":
            # Inbound Authorization is the backplane key, not the
            # gateway key. Different trust domain — never forward.
            continue
        if not key and lk in _SESSION_HEADERS:
            # Gateway 403s session headers when auth isn't configured.
            continue
        out[k] = v
    if key:
        out["Authorization"] = f"Bearer {key}"
    return out


def _filter_response_headers(src) -> dict:
    return {k: v for k, v in src.items() if k.lower() not in _HOP_HEADERS}


async def _proxy_handler(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app[_CLIENT_KEY]
    tail = request.match_info["tail"]
    upstream_url = f"{GATEWAY_BASE}/v1/{tail}"
    headers = _filter_request_headers(request.headers)

    body = await request.read() if request.can_read_body else None

    try:
        upstream = await session.request(
            request.method,
            upstream_url,
            headers=headers,
            data=body,
            params=request.rel_url.query,
            allow_redirects=False,
        )
    except Exception as exc:
        logger.warning("gateway_proxy: upstream request failed: %s", exc)
        return web.json_response(
            {"ok": False, "error": f"gateway unreachable: {exc}"},
            status=502,
        )

    try:
        resp = web.StreamResponse(
            status=upstream.status,
            headers=_filter_response_headers(upstream.headers),
        )
        await resp.prepare(request)
        # iter_any yields whatever bytes arrived since the last call,
        # which keeps SSE token deltas un-buffered. Don't switch to
        # iter_chunked(N) — that re-buffers and stalls streaming.
        async for chunk in upstream.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    except ConnectionResetError:
        # Client closed mid-stream. Nothing to do; the finally below
        # cleans up the upstream connection.
        logger.debug("gateway_proxy: client disconnected mid-stream")
        raise
    finally:
        upstream.release()


async def _on_startup(app: web.Application) -> None:
    # total=None disables the per-request timeout — chat completions
    # can run for minutes. Per-connection limits + SSE keepalive
    # protect against true hangs.
    app[_CLIENT_KEY] = ClientSession(timeout=ClientTimeout(total=None))


async def _on_cleanup(app: web.Application) -> None:
    sess = app.get(_CLIENT_KEY)
    if sess is not None:
        await sess.close()


def register(app: web.Application) -> None:
    app.router.add_route("*", "/v1/{tail:.*}", _proxy_handler)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
