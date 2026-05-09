"""Token-keyed file serving for the BoltAI Hermes Gateway plugin.

Why: BoltAI (and other OpenAI-compatible HTTP clients) cannot read the
gateway's filesystem.  Two ways to make local images render:

* ``inline`` mode (see :mod:`image_inliner`) — encode each file as a
  ``data:image/...;base64,...`` URL.  Universal but bloats responses.
* ``link`` mode (this module) — register the file under a random,
  short-lived token and rewrite markdown to point at
  ``http://<host>/v1/files/<token>``.  Smaller payloads, works for
  arbitrary file types (images, PDFs, audio, video).

Security model — token IS the auth.  The token is generated via
``secrets.token_urlsafe(32)`` (256 bits, presigned-URL-style
unguessability) and the entry is dropped from the store after
``DEFAULT_TTL_SECONDS`` (configurable via ``BOLTAI_HERMES_GW_FILE_TTL``).
The ``/v1/files/{token}`` route deliberately does NOT require the
gateway's bearer key, because BoltAI loads images via plain
``<img src="...">`` and won't include an Authorization header.

Path safety — paths are resolved (symlinks expanded) at registration
time and the resolved path is what we serve.  No traversal possible at
serve time because the token maps to a fixed absolute path.

Reachability — the URL base is built from the inbound request's
``Host`` header (with ``X-Forwarded-Proto``/``X-Forwarded-Host``
honoured for reverse-proxy setups), so the gateway can bind to
``0.0.0.0`` or a specific LAN IP and the rewritten URLs always match
how the client is actually reaching us.

Persistence — in-memory only.  A gateway restart invalidates all
tokens; old BoltAI history will show broken images until the next
agent turn.  This is an acceptable tradeoff for simplicity.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

from aiohttp import web

logger = logging.getLogger(__name__)

# Defaults — overridable via env when constructing the FileServer.
DEFAULT_TTL_SECONDS = 24 * 60 * 60     # 24 hours
DEFAULT_MAX_ENTRIES = 4096             # bounded LRU-ish cap
DEFAULT_CHUNK_BYTES = 64 * 1024        # streaming read size

# Mime fallback for unknown extensions — generic binary so clients still
# download / display via Content-Disposition: inline.
FALLBACK_MIME = "application/octet-stream"

# CORS headers — applied to successful /v1/files/* responses and to
# the OPTIONS preflight.  Token-in-URL is the auth (unguessable +
# short-lived), so allowing any origin is safe.  Required for
# Electron/Chromium clients that fetch via XHR for "save image as"
# or copy-image operations.  Not applied to 404/410 — those don't
# need cross-origin disclosure.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Cross-Origin-Resource-Policy": "cross-origin",
    "Timing-Allow-Origin": "*",
}


class FileServer:
    """Token store + aiohttp handler for ``/v1/files/{token}``.

    Thread safety: the gateway runs everything on a single asyncio
    event loop, so we don't lock the dict.  All access is from the
    async handler or the registration helper called inside the agent
    runner's executor — both serialised by the loop.
    """

    __slots__ = ("_ttl", "_max_entries", "_store", "_url_base_override")

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        url_base_override: Optional[str] = None,
    ) -> None:
        self._ttl = max(60, int(ttl_seconds))  # don't allow absurdly low TTLs
        self._max_entries = max(16, int(max_entries))
        # token -> (resolved_path, mime, expires_at)
        self._store: dict[str, Tuple[Path, str, float]] = {}
        # Optional explicit override (e.g. user pinned a public URL).
        self._url_base_override = url_base_override

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, path: str | os.PathLike) -> Optional[Tuple[str, str]]:
        """Register ``path`` under a fresh token.

        Returns ``(token, mime)`` on success, ``None`` if the file is
        missing / not a regular file / unreadable.  Never raises —
        registration failures should silently fall back to the original
        markdown so the agent's reply is never corrupted.
        """
        try:
            p = Path(path).expanduser().resolve(strict=True)
        except (FileNotFoundError, RuntimeError, OSError) as exc:
            logger.debug("file_server.register: cannot resolve %r: %s", path, exc)
            return None
        if not p.is_file():
            logger.debug("file_server.register: not a regular file: %s", p)
            return None

        mime, _ = mimetypes.guess_type(str(p))
        if not mime:
            mime = FALLBACK_MIME

        self._gc()
        if len(self._store) >= self._max_entries:
            # Evict the oldest entry to make room — bounded memory.
            oldest = min(self._store.items(), key=lambda kv: kv[1][2])[0]
            self._store.pop(oldest, None)

        token = secrets.token_urlsafe(32)
        self._store[token] = (p, mime, time.time() + self._ttl)
        return token, mime

    def url_for(self, token: str, request: "web.Request") -> str:
        """Build the absolute URL for ``token`` based on the inbound request.

        Honours ``X-Forwarded-Proto`` and ``X-Forwarded-Host`` for
        reverse-proxy setups; otherwise uses the request's own scheme +
        ``Host`` header.  Falls back to ``url_base_override`` if set
        explicitly via env var.
        """
        if self._url_base_override:
            base = self._url_base_override.rstrip("/")
            return f"{base}/v1/files/{token}"

        # Reverse-proxy aware
        fwd_proto = request.headers.get("X-Forwarded-Proto", "").strip()
        fwd_host = request.headers.get("X-Forwarded-Host", "").strip()
        if fwd_host:
            scheme = fwd_proto or ("https" if request.secure else "http")
            return f"{scheme}://{fwd_host}/v1/files/{token}"

        host = request.headers.get("Host", "").strip()
        if not host:
            # Last-resort fallback — should never trigger on HTTP/1.1+.
            host = "127.0.0.1"
        scheme = "https" if request.secure else "http"
        return f"{scheme}://{host}/v1/files/{token}"

    # ------------------------------------------------------------------
    # Serving
    # ------------------------------------------------------------------

    async def handle(self, request: "web.Request") -> "web.StreamResponse":
        """aiohttp handler for ``GET /v1/files/{token}``."""
        token = request.match_info.get("token", "")
        if not token:
            return web.Response(status=404, text="not found")

        entry = self._store.get(token)
        if entry is None:
            logger.debug("[file-server] token miss: %s…", token[:12])
            return web.Response(status=404, text="not found")

        path, mime, expires_at = entry
        if time.time() > expires_at:
            self._store.pop(token, None)
            return web.Response(status=410, text="gone")

        try:
            stat = path.stat()
        except OSError as exc:
            logger.debug("file_server.handle: stat failed for %s: %s", path, exc)
            self._store.pop(token, None)
            return web.Response(status=404, text="not found")

        # Stream the file with sensible headers.  Inline disposition lets
        # browsers render images directly; filename is preserved for
        # downloads of non-image types (PDFs etc.).
        headers = {
            "Content-Type": mime,
            "Content-Length": str(stat.st_size),
            "Content-Disposition": f'inline; filename="{path.name}"',
            # Clients may cache for the token's lifetime — the URL
            # is unguessable and short-lived, so caching is safe.
            "Cache-Control": f"private, max-age={self._ttl}",
        }
        headers.update(_CORS_HEADERS)
        resp = web.StreamResponse(status=200, headers=headers)
        await resp.prepare(request)
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(DEFAULT_CHUNK_BYTES)
                    if not chunk:
                        break
                    await resp.write(chunk)
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.debug("file_server.handle: client disconnected: %s", exc)
        except Exception:
            logger.exception("file_server.handle: read/write failed for %s", path)
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    async def handle_options(self, request: "web.Request") -> "web.Response":
        """CORS preflight — Electron/Chromium may issue this for save-as."""
        return web.Response(status=204, headers=_CORS_HEADERS)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _gc(self) -> None:
        """Drop expired entries.  Lazy — only called on registration."""
        now = time.time()
        expired = [t for t, (_, _, exp) in self._store.items() if now > exp]
        for t in expired:
            self._store.pop(t, None)

    # Test/debug helpers — not part of the public API.
    def _size(self) -> int:
        return len(self._store)
