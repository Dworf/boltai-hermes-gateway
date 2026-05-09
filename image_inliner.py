"""Inline local image paths as base64 data URLs in agent output.

Why: BoltAI (and other OpenAI-compatible HTTP clients) cannot read the
server's filesystem.  When an agent emits markdown like ``![cat](/Users/...)``
the client sees a broken/missing image.  This module rewrites those local
paths into ``data:image/<mime>;base64,...`` URLs that any markdown renderer
can display inline — matching how OpenRouter image-models and OpenAI's
Responses API ship images (base64 in-band).

Two entry points:

* ``rewrite_local_images(text)`` — one-shot rewrite of a complete string.
  Use for ``final_response`` text and any other place where the full
  message is available.
* ``StreamingImageRewriter(emit)`` — incremental rewriter for SSE token
  streams.  Buffers just enough to detect ``![alt](path)`` syntax, then
  emits rewritten chunks via the ``emit`` callback.  Call ``flush()``
  once the upstream stream ends.

Limits and decisions:

* Files larger than ``MAX_INLINE_BYTES`` (default 8 MiB) are NOT inlined —
  the original markdown is left intact.  Base64 inflates payloads by ~33%,
  so a 10 MiB image becomes ~13 MiB of SSE traffic.
* Only mime types beginning ``image/`` are inlined.  Anything else is left
  alone.
* HTTP(S) URLs and existing ``data:`` URLs are passed through untouched.
* ``~`` is expanded to the user home dir.
* Failures (missing file, unreadable, mime guess fails) silently fall
  back to the original markdown — no exceptions propagate to the agent.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import re
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Cap to keep SSE responses sane.  8 MiB raw -> ~10.7 MiB base64.
MAX_INLINE_BYTES = 8 * 1024 * 1024

# Match standard markdown image: ![alt text](url)
# Path captured greedy up to the closing paren on the same line.  We
# deliberately don't support reference-style images — agents emit inline.
_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")


def _looks_like_local_path(p: str) -> bool:
    """Return True if ``p`` is plausibly a local filesystem path."""
    if not p:
        return False
    p = p.strip()
    if p.startswith(("http://", "https://", "data:", "//", "ftp://")):
        return False
    return p.startswith("/") or p.startswith("~") or p.startswith("./")


def _encode_file(path: str) -> Optional[str]:
    """Read a local image file and return a ``data:image/...;base64,...``
    URL, or ``None`` on any failure (missing, too big, non-image, …)."""
    try:
        full = Path(path.strip()).expanduser()
        if not full.is_file():
            return None
        size = full.stat().st_size
        if size > MAX_INLINE_BYTES:
            logger.info(
                "Skipping image inline: %s is %d bytes (cap %d)",
                full, size, MAX_INLINE_BYTES,
            )
            return None
        mime, _ = mimetypes.guess_type(str(full))
        if not mime or not mime.startswith("image/"):
            return None
        data = base64.b64encode(full.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("Image inline failed for %r: %s", path, exc)
        return None


def rewrite_local_images(text: str) -> str:
    """Replace every ``![alt](local-path)`` with a base64 data URL.

    Non-local URLs and unreadable paths are left untouched.
    """
    if not text or "![" not in text:
        return text

    def _sub(match: re.Match) -> str:
        alt, path = match.group(1), match.group(2)
        if not _looks_like_local_path(path):
            return match.group(0)
        encoded = _encode_file(path)
        if encoded is None:
            return match.group(0)
        return f"![{alt}]({encoded})"

    return _IMG_RE.sub(_sub, text)


class StreamingImageRewriter:
    """Buffered, incremental rewriter for SSE token streams.

    The agent feeds text deltas one chunk at a time.  A markdown image
    can split across chunks (``![ca`` … ``t](/path/foo.png)``), so we
    can't blindly run the regex on each delta.  Instead we hold back
    text from the last unmatched ``![`` onward, and only flush once we
    see the closing ``)`` (or the upstream calls :meth:`flush`).

    Thread safety: not synchronised.  The agent calls
    ``stream_delta_callback`` from a single executor thread, so a single
    feeder is the contract.

    Usage::

        rewriter = StreamingImageRewriter(emit=original_callback)
        # pass rewriter.feed to the agent in place of the raw callback
        ...
        rewriter.flush()  # at end of stream
    """

    __slots__ = ("_emit", "_buf")

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buf = ""

    def feed(self, text: str) -> None:
        """Accept a delta from the agent and emit (possibly rewritten,
        possibly buffered) text downstream."""
        if text is None:
            return
        if not text:
            return
        self._buf += text

        # Find the rightmost potential image start — that's the only
        # place an unclosed image syntax could begin.
        idx = self._buf.rfind("![")
        if idx == -1:
            # No image syntax in flight — flush everything.
            self._emit(self._buf)
            self._buf = ""
            return

        # Is there a closing paren after that ``![``?  If yes, the
        # whole buffer is safe to rewrite and emit.  If no, hold from
        # ``idx`` onward and flush only the prefix.
        close = self._buf.find(")", idx)
        if close == -1:
            prefix = self._buf[:idx]
            if prefix:
                self._emit(rewrite_local_images(prefix))
            self._buf = self._buf[idx:]
            # Safety valve: if the held tail grows unreasonably, the
            # stream probably won't close it (malformed output).  Cap
            # at 4096 chars and force-flush as plain text.
            if len(self._buf) > 4096:
                self._emit(self._buf)
                self._buf = ""
        else:
            self._emit(rewrite_local_images(self._buf))
            self._buf = ""

    def flush(self) -> None:
        """Emit any held-back text.  Call once when the upstream stream
        has ended; safe to call multiple times."""
        if self._buf:
            # Try a final rewrite — the closing ``)`` may have arrived
            # in the last delta.  If still unmatched, emit raw.
            out = rewrite_local_images(self._buf)
            self._emit(out)
            self._buf = ""
