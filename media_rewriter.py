"""Outbound markdown rewriter for local file references.

Supports two modes:

* ``link`` (default) — register the file with :class:`FileServer` and
  rewrite to ``http://<host>/v1/files/<token>``.  Smaller payloads, works
  for any file type (images render inline; PDFs/audio/video become
  links).
* ``inline`` — base64-encode images as ``data:image/...;base64,...``
  URLs (the original behaviour from v0.2.0).  Universal renderer
  support, but bloats responses ~33% per image.

Selection is per-rewriter-instance so the adapter can branch on an
env var at request time.

Streaming: :class:`StreamingMediaRewriter` buffers just enough of the
agent's token stream to detect a complete ``![alt](path)`` before
rewriting.  Designed to replace the v0.2.0 ``StreamingImageRewriter``
shim with a mode-aware version.

Failures (missing file, registration error, encode error) silently
fall back to the original markdown — never crash the agent reply.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import re
from pathlib import Path
from typing import Callable, Optional

from .file_server import FileServer

logger = logging.getLogger(__name__)

# Inline-mode cap: 8 MiB raw -> ~10.7 MiB base64.
INLINE_MAX_BYTES = 8 * 1024 * 1024

# Modes
MODE_LINK = "link"
MODE_INLINE = "inline"
MODE_OFF = "off"
VALID_MODES = frozenset({MODE_LINK, MODE_INLINE, MODE_OFF})

# Standard markdown image syntax: ![alt](url)
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")
# Plain markdown link syntax: [label](url) — used in link mode for
# non-image files (PDFs etc.) so BoltAI shows a clickable link.
_LINK_RE = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)\n]+)\)")


def _looks_like_local_path(p: str) -> bool:
    """Heuristic: is ``p`` plausibly a local filesystem path?"""
    if not p:
        return False
    p = p.strip()
    if p.startswith(("http://", "https://", "data:", "//", "ftp://", "mailto:")):
        return False
    return p.startswith("/") or p.startswith("~") or p.startswith("./")


def _is_image_mime(mime: Optional[str]) -> bool:
    return bool(mime and mime.startswith("image/"))


# ---------------------------------------------------------------------------
# Inline mode (base64 data URLs) — kept for compatibility / fallback
# ---------------------------------------------------------------------------


def _encode_inline(path: str) -> Optional[str]:
    """Read a local image file and return ``data:image/...;base64,...``,
    or ``None`` on any failure (missing, too big, non-image, …)."""
    try:
        full = Path(path.strip()).expanduser()
        if not full.is_file():
            return None
        size = full.stat().st_size
        if size > INLINE_MAX_BYTES:
            logger.info(
                "Skipping inline encode: %s is %d bytes (cap %d)",
                full, size, INLINE_MAX_BYTES,
            )
            return None
        mime, _ = mimetypes.guess_type(str(full))
        if not _is_image_mime(mime):
            return None
        data = base64.b64encode(full.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Inline encode failed for %r: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Link mode (token-keyed HTTP URLs)
# ---------------------------------------------------------------------------


def _register_link(
    path: str,
    file_server: FileServer,
    url_builder: Callable[[str], str],
) -> Optional[tuple[str, str]]:
    """Register ``path`` with the file server and return ``(url, mime)``,
    or ``None`` on failure."""
    try:
        result = file_server.register(path.strip())
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Link register failed for %r: %s", path, exc)
        return None
    if result is None:
        return None
    token, mime = result
    try:
        url = url_builder(token)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("URL build failed for token %s: %s", token, exc)
        return None
    logger.debug("[media-rewriter] %s -> %s (mime=%s)", path.strip(), url, mime)
    return url, mime


# ---------------------------------------------------------------------------
# One-shot rewriter
# ---------------------------------------------------------------------------


def rewrite(
    text: str,
    *,
    mode: str,
    file_server: Optional[FileServer] = None,
    url_builder: Optional[Callable[[str], str]] = None,
) -> str:
    """Rewrite local file refs in ``text`` according to ``mode``.

    * ``link`` requires both ``file_server`` and ``url_builder``.  In
      this mode, image syntax stays as image syntax (rewritten URL),
      and plain ``[label](path)`` links to local non-HTTP paths get
      rewritten too so users can click through to PDFs etc.
    * ``inline`` only touches image syntax; non-image files are left
      alone.
    * ``off`` returns ``text`` unchanged.
    """
    if mode == MODE_OFF or not text:
        return text
    if "[" not in text:
        return text

    if mode == MODE_INLINE:
        return _rewrite_inline(text)
    if mode == MODE_LINK:
        if file_server is None or url_builder is None:
            logger.debug("link mode requested but no file_server/url_builder; passthrough")
            return text
        return _rewrite_link(text, file_server, url_builder)
    return text


def _rewrite_inline(text: str) -> str:
    def _sub(match: re.Match) -> str:
        alt, path = match.group(1), match.group(2)
        if not _looks_like_local_path(path):
            return match.group(0)
        encoded = _encode_inline(path)
        if encoded is None:
            return match.group(0)
        return f"![{alt}]({encoded})"
    return _IMG_RE.sub(_sub, text)


def _rewrite_link(
    text: str,
    file_server: FileServer,
    url_builder: Callable[[str], str],
) -> str:
    # Pass 1: image syntax → image with new URL.
    def _sub_img(match: re.Match) -> str:
        alt, path = match.group(1), match.group(2)
        if not _looks_like_local_path(path):
            return match.group(0)
        result = _register_link(path, file_server, url_builder)
        if result is None:
            return match.group(0)
        url, _mime = result
        return f"![{alt}]({url})"

    text = _IMG_RE.sub(_sub_img, text)

    # Pass 2: plain link syntax → link with new URL (only for local
    # paths).  Note: we exclude image syntax via the negative lookbehind
    # in the regex.  For images we already rewrote in pass 1 — their
    # URLs now start with http:// so the `_looks_like_local_path` check
    # will skip them.
    def _sub_link(match: re.Match) -> str:
        label, path = match.group(1), match.group(2)
        if not _looks_like_local_path(path):
            return match.group(0)
        result = _register_link(path, file_server, url_builder)
        if result is None:
            return match.group(0)
        url, mime = result
        # If it's an image, prefer image syntax even when the agent used
        # a plain link — BoltAI will then render it inline.
        if _is_image_mime(mime):
            return f"![{label or Path(path).name}]({url})"
        return f"[{label or Path(path).name}]({url})"

    text = _LINK_RE.sub(_sub_link, text)
    return text


# ---------------------------------------------------------------------------
# Streaming rewriter
# ---------------------------------------------------------------------------


class StreamingMediaRewriter:
    """Buffered, incremental rewriter for SSE token streams.

    Holds back text from the last unmatched ``[`` (image or link) onward
    until the closing ``)`` arrives, then runs the rewrite and emits.

    Mode-aware: same instance can do link, inline, or off depending on
    the constructor args.

    Thread safety: not synchronised.  Single feeder contract — the
    agent calls the wrapped callback from a single executor thread.
    """

    __slots__ = ("_emit", "_buf", "_mode", "_file_server", "_url_builder")

    def __init__(
        self,
        emit: Callable[[str], None],
        *,
        mode: str = MODE_LINK,
        file_server: Optional[FileServer] = None,
        url_builder: Optional[Callable[[str], str]] = None,
    ) -> None:
        if mode not in VALID_MODES:
            logger.warning("Unknown rewrite mode %r — falling back to off", mode)
            mode = MODE_OFF
        self._emit = emit
        self._buf = ""
        self._mode = mode
        self._file_server = file_server
        self._url_builder = url_builder

    def feed(self, text: str) -> None:
        """Accept a delta and emit (rewritten or buffered) text downstream."""
        if not text:
            return
        if self._mode == MODE_OFF:
            self._emit(text)
            return

        self._buf += text

        # Find the rightmost potential markdown-bracket start.  Track
        # both ``![`` (image) and ``[`` (link) — whichever is later is
        # the one that could still be in flight.
        idx_img = self._buf.rfind("![")
        idx_link = self._buf.rfind("[")
        # `![` always overlaps a `[` one position later.  When that's
        # the case, the image start wins — otherwise we'd emit the
        # bang as plain text before rewriting and BoltAI renders a
        # stray `!` in front of the image.  In every other case the
        # later bracket is the one still in flight.
        if idx_link == idx_img + 1 and idx_img != -1:
            idx = idx_img
        else:
            idx = max(idx_img, idx_link)
        if idx == -1:
            self._emit(self._buf)
            self._buf = ""
            return

        # Closing paren after the candidate start?
        close = self._buf.find(")", idx)
        if close == -1:
            prefix = self._buf[:idx]
            if prefix:
                self._emit(self._do_rewrite(prefix))
            self._buf = self._buf[idx:]
            # Safety valve: held tail too long → flush as plain text.
            if len(self._buf) > 4096:
                self._emit(self._buf)
                self._buf = ""
        else:
            self._emit(self._do_rewrite(self._buf))
            self._buf = ""

    def flush(self) -> None:
        """Emit any held-back text.  Safe to call multiple times."""
        if self._buf:
            self._emit(self._do_rewrite(self._buf))
            self._buf = ""

    def _do_rewrite(self, text: str) -> str:
        return rewrite(
            text,
            mode=self._mode,
            file_server=self._file_server,
            url_builder=self._url_builder,
        )
