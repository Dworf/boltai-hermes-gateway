"""BoltAI Hermes Gateway — OpenAI-compatible adapter with slash command support.

Subclasses gateway.platforms.api_server.APIServerAdapter to inherit all
endpoints (chat completions, responses API, runs, models, capabilities,
health), streaming, session continuity, CORS, and bearer-token auth.

Task 3 adds slash command interception: messages starting with ``/`` that
resolve to a known gateway command are dispatched via the gateway message
handler instead of the agent loop.  Auth is enforced FIRST — slash dispatch
does not bypass authentication.

v0.3.0 adds local-file media handling.  See ``media_rewriter`` and
``file_server`` modules.  Three modes via
``BOLTAI_HERMES_GW_MEDIA_MODE``: ``link`` (default — token-keyed HTTP
URLs served from the gateway itself), ``inline`` (base64 data URLs,
v0.2.0 behaviour), ``off`` (no rewriting).

Configuration is namespaced via ``BOLTAI_HERMES_GW_*`` env vars so this
plugin can run side-by-side with the built-in ``api_server`` adapter
without sharing port, API key, CORS settings, or model name.

Recognized env vars (all optional):
- ``BOLTAI_HERMES_GW_ENABLED``        — "1"/"true"/"yes" to auto-enable from env
- ``BOLTAI_HERMES_GW_PORT``           — listen port (default: 8643)
- ``BOLTAI_HERMES_GW_HOST``           — bind host (default: 127.0.0.1)
- ``BOLTAI_HERMES_GW_KEY``            — bearer token; empty = open (local-only)
- ``BOLTAI_HERMES_GW_CORS_ORIGINS``   — comma-separated origins or "*"
- ``BOLTAI_HERMES_GW_MODEL_NAME``     — model name to advertise on /v1/models
- ``BOLTAI_HERMES_GW_SLASH_STREAM_MODE`` — single_chunk (default) | token_stream
- ``BOLTAI_HERMES_GW_MEDIA_MODE``     — link (default) | inline | off
- ``BOLTAI_HERMES_GW_FILE_TTL``       — file-token TTL in seconds (default: 86400)
- ``BOLTAI_HERMES_GW_FILE_URL_BASE``  — pin URL base (e.g. ``https://hermes.mydomain.com``);
                                        otherwise built from request Host header
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from aiohttp import web

from gateway.config import Platform
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.base import MessageEvent, MessageType
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

from .file_server import DEFAULT_TTL_SECONDS, FileServer
from .media_rewriter import (
    MODE_INLINE,
    MODE_LINK,
    MODE_OFF,
    VALID_MODES,
    StreamingMediaRewriter,
    rewrite as rewrite_media,
)

# Per-request context var so the streaming rewriter (created inside
# ``_run_agent``, with no direct access to the aiohttp request) can
# build URLs based on the inbound request's Host / X-Forwarded-* headers.
_REQUEST_CTX: contextvars.ContextVar[Optional["web.Request"]] = contextvars.ContextVar(
    "boltai_hermes_gw_request", default=None
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8643
DEFAULT_HOST = "127.0.0.1"
PLATFORM_NAME = "boltai_hermes_gateway"

# Env var prefix for ALL plugin settings.  Keep this in sync with the
# documentation block above and with plugin.yaml's optional_env entries.
ENV_PREFIX = "BOLTAI_HERMES_GW_"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

# Media-handling defaults
DEFAULT_MEDIA_MODE = MODE_LINK

# Slash response streaming modes
SLASH_STREAM_SINGLE_CHUNK = "single_chunk"
SLASH_STREAM_TOKEN_STREAM = "token_stream"
VALID_STREAM_MODES = frozenset({SLASH_STREAM_SINGLE_CHUNK, SLASH_STREAM_TOKEN_STREAM})
DEFAULT_SLASH_STREAM_MODE = SLASH_STREAM_TOKEN_STREAM
TOKEN_STREAM_CHUNK_SIZE = 20  # chars per delta
TOKEN_STREAM_DELAY_S = 0.02   # delay between deltas

# Effectively-no chunking for HTTP responses.  Other adapters (Discord 2 KB,
# Telegram 4 KB, etc.) chunk to fit platform message limits, but BoltAI and
# any OpenAI-compatible client can render a single multi-megabyte assistant
# message just fine, so we set this absurdly high.  Used by the gateway's
# stream_consumer when deciding whether to split outgoing text.
MAX_MESSAGE_LENGTH = 10_000_000


def _env(name: str) -> str | None:
    """Read a namespaced env var; return ``None`` if unset/empty."""
    raw = os.getenv(ENV_PREFIX + name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _env_bool(name: str) -> bool:
    val = _env(name)
    return val is not None and val.lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Slash dispatch helpers (module-level so they're trivially unit-testable)
# ---------------------------------------------------------------------------


def is_known_slash_command(text: str) -> bool:
    """Return True iff ``text`` starts with ``/`` and resolves via
    :func:`resolve_command` to a name present in
    :data:`GATEWAY_KNOWN_COMMANDS`.

    Tolerates leading/trailing whitespace.  Returns False for the empty
    string, plain text, bare ``/``, or unknown commands.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    head = stripped.split(maxsplit=1)[0]
    bare = head[1:]
    if not bare:
        return False
    cmd = resolve_command(bare)
    if cmd is None:
        return False
    canonical = cmd.name.replace("_", "-")
    if canonical in GATEWAY_KNOWN_COMMANDS:
        return True
    # Some entries are stored with underscores in the registry
    return cmd.name in GATEWAY_KNOWN_COMMANDS


def resolve_stream_mode(config_default: str, header_value: Optional[str]) -> str:
    """Pick the effective slash stream mode.

    Header (X-Hermes-Slash-Stream) overrides config; an invalid header is
    ignored and we fall back to the config value; an invalid config falls
    back to the single_chunk default.
    """
    if header_value and header_value in VALID_STREAM_MODES:
        return header_value
    if config_default in VALID_STREAM_MODES:
        return config_default
    return SLASH_STREAM_SINGLE_CHUNK


def _openai_chunk(
    content: str,
    *,
    finish_reason: Optional[str] = None,
    model: str = "claude-code",
    completion_id: Optional[str] = None,
    created: Optional[int] = None,
) -> dict:
    return {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": created if created is not None else int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        ],
    }


def _openai_full_response(content: str, *, model: str = "claude-code") -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def _make_envvar_rewrite_filter(adapter_name: str) -> logging.Filter:
    """Logging filter that rewrites parent api_server messages to reference
    this plugin's env vars.

    The parent ``APIServerAdapter`` hardcodes ``API_SERVER_KEY`` in two
    startup messages (the no-auth warning and the 0.0.0.0 refusal error).
    Both are formatted with ``self.name`` as the first positional arg,
    so we can target only records emitted about *our* adapter instance
    and leave a real ``api_server`` adapter running alongside untouched.
    """
    replacements = (
        ("API_SERVER_KEY / platforms.api_server.key",
         "BOLTAI_HERMES_GW_KEY / platforms.boltai_hermes_gateway.key"),
        ("API_SERVER_KEY", "BOLTAI_HERMES_GW_KEY"),
    )

    class _Rewriter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
            try:
                args = record.args
                first = args[0] if isinstance(args, tuple) and args else None
                if first != adapter_name:
                    return True
                msg = record.msg
                if isinstance(msg, str) and "API_SERVER_KEY" in msg:
                    for old, new in replacements:
                        msg = msg.replace(old, new)
                    record.msg = msg
            except Exception:
                pass
            return True

    f = _Rewriter()
    f._boltai_gw_marker = adapter_name  # type: ignore[attr-defined]
    return f


class BoltAIGatewayAdapter(APIServerAdapter):
    """OpenAI-compatible HTTP server with slash-command interception.

    Inherits all endpoint behavior from APIServerAdapter. Default port is
    8643 so it coexists with the built-in api_server (8642). Has its own
    ``BOLTAI_HERMES_GW_*`` env namespace for key/CORS/model so the two
    adapters never share secrets or settings.

    Platform value is overridden to ``boltai_hermes_gateway`` so the
    gateway platform registry can register both adapters simultaneously
    without name collision.
    """

    # No artificial chunking on HTTP — clients render long assistant
    # messages fine.  Used by the gateway's stream_consumer when it
    # decides whether to split the outbound text.  10 MB ceiling is
    # effectively "never split" for any real slash output.
    MAX_MESSAGE_LENGTH = 10_000_000

    def __init__(self, config):
        # Pre-seed config.extra with our namespaced env vars BEFORE calling
        # super().__init__, so APIServerAdapter's ``extra.get(K, os.getenv(API_SERVER_K, ...))``
        # lookups find OUR values first and never fall through to the
        # built-in API_SERVER_* env vars.
        extra = getattr(config, "extra", None)
        if extra is None:
            extra = {}
            try:
                config.extra = extra
            except Exception:
                pass

        # Port — accept already-configured value, then env, then default.
        if not extra.get("port"):
            port_val = _env("PORT")
            if port_val:
                try:
                    extra["port"] = int(port_val)
                except ValueError:
                    logger.warning(
                        "Invalid %sPORT=%r — falling back to %d",
                        ENV_PREFIX, port_val, DEFAULT_PORT,
                    )
                    extra["port"] = DEFAULT_PORT
            else:
                extra["port"] = DEFAULT_PORT

        # Host
        if not extra.get("host"):
            extra["host"] = _env("HOST") or DEFAULT_HOST

        # Bearer-token API key (separate from API_SERVER_KEY).  We always
        # set ``extra["key"]`` — even to "" — so the parent's
        # ``extra.get("key", os.getenv("API_SERVER_KEY", ""))`` lookup
        # cannot fall through to the built-in api_server's secret.
        # Note: the gateway's config builder may pre-seed extra["key"] = ""
        # for all platforms, so we treat empty/None as "not configured" and
        # honour BOLTAI_HERMES_GW_KEY in that case.
        if not extra.get("key"):
            extra["key"] = _env("KEY") or ""

        # CORS origins — same isolation: always seed extra so we never
        # inherit API_SERVER_CORS_ORIGINS.  Treat empty as "not configured".
        if not extra.get("cors_origins"):
            extra["cors_origins"] = _env("CORS_ORIGINS") or ""

        # Model name — same isolation.  Empty string lets APIServerAdapter
        # fall back to its hardcoded default ("hermes-agent") rather than
        # reading API_SERVER_MODEL_NAME.
        if not extra.get("model_name"):
            extra["model_name"] = _env("MODEL_NAME") or ""

        # Slash response streaming mode — config.extra > namespaced env >
        # default.  Captured BEFORE super().__init__ so subclassers and
        # tests see a stable attribute right after construction.
        if "slash_stream_mode" not in extra or not extra.get("slash_stream_mode"):
            env_mode = _env("SLASH_STREAM_MODE")
            if env_mode:
                extra["slash_stream_mode"] = env_mode

        # Media handling — same pattern.
        if "media_mode" not in extra or not extra.get("media_mode"):
            env_media = _env("MEDIA_MODE")
            if env_media:
                extra["media_mode"] = env_media
        if "file_ttl" not in extra or not extra.get("file_ttl"):
            ttl_raw = _env("FILE_TTL")
            if ttl_raw:
                try:
                    extra["file_ttl"] = int(ttl_raw)
                except ValueError:
                    logger.warning(
                        "Invalid %sFILE_TTL=%r — using default %d",
                        ENV_PREFIX, ttl_raw, DEFAULT_TTL_SECONDS,
                    )
        if "file_url_base" not in extra or not extra.get("file_url_base"):
            url_base = _env("FILE_URL_BASE")
            if url_base:
                extra["file_url_base"] = url_base

        super().__init__(config)
        # Override the platform value so we don't collide with the built-in
        # api_server in the platform registry.  Platform enum supports
        # dynamic members via _missing_().
        self.platform = Platform(PLATFORM_NAME)

        mode = extra.get("slash_stream_mode") or DEFAULT_SLASH_STREAM_MODE
        if mode not in VALID_STREAM_MODES:
            logger.warning(
                "Invalid slash_stream_mode=%r, falling back to %s",
                mode, DEFAULT_SLASH_STREAM_MODE,
            )
            mode = DEFAULT_SLASH_STREAM_MODE
        self.slash_stream_mode: str = mode

        # Media mode + file server.  ``link`` (default) registers files
        # with an in-process token store and serves them via
        # ``/v1/files/{token}``.  ``inline`` falls back to base64 data
        # URLs.  ``off`` skips rewriting entirely.
        media_mode = extra.get("media_mode") or DEFAULT_MEDIA_MODE
        if media_mode not in VALID_MODES:
            logger.warning(
                "Invalid media_mode=%r, falling back to %s",
                media_mode, DEFAULT_MEDIA_MODE,
            )
            media_mode = DEFAULT_MEDIA_MODE
        self.media_mode: str = media_mode
        self._file_ttl: int = int(extra.get("file_ttl") or DEFAULT_TTL_SECONDS)
        self._file_url_base: str = extra.get("file_url_base") or ""
        # FileServer instance is created lazily on connect() so unit
        # tests that construct the adapter without starting the server
        # don't leak any state.
        self._file_server: Optional[FileServer] = None

        # Install a logging filter on the parent api_server module's logger
        # so the "No API key configured" / "Refusing to start" messages
        # reference our env vars (BOLTAI_HERMES_GW_KEY) instead of the
        # parent's hardcoded API_SERVER_KEY.  Scoped to records that name
        # our adapter instance so we don't rewrite messages from a real
        # api_server adapter running alongside us.
        try:
            parent_logger = logging.getLogger("gateway.platforms.api_server")
            adapter_name = getattr(self, "name", PLATFORM_NAME)
            if not any(
                getattr(f, "_boltai_gw_marker", None) == adapter_name
                for f in parent_logger.filters
            ):
                parent_logger.addFilter(_make_envvar_rewrite_filter(adapter_name))
        except Exception:  # pragma: no cover — logging never breaks startup
            pass

    def __repr__(self) -> str:
        return f"<BoltAIGatewayAdapter host={self._host!r} port={self._port}>"

    # ------------------------------------------------------------------
    # File-serving route
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the API server, with our ``/v1/files/{token}`` route added.

        Implementation note: the parent's ``connect()`` builds the
        aiohttp app and freezes it (via ``AppRunner.setup``) without
        yielding control, so we can't add routes between construction
        and freeze from the outside.  Instead, we scope-patch the
        ``web.Application`` reference held by the parent module for the
        duration of ``super().connect()`` so the app it constructs
        auto-registers our route at ``__init__`` time.

        The patch is local to the api_server module (not global
        aiohttp), so other plugins importing aiohttp directly are
        unaffected.  The patch is reverted in a ``finally`` block.
        """
        if self.media_mode != MODE_OFF:
            try:
                self._file_server = FileServer(
                    ttl_seconds=self._file_ttl,
                    url_base_override=self._file_url_base or None,
                )
            except Exception:  # pragma: no cover — defensive
                logger.exception("Failed to construct FileServer")
                self._file_server = None

        if self._file_server is None:
            # Nothing to inject — fall through to the parent's connect.
            return await super().connect()

        # Scope-patch ``web.Application`` only for this connect call.
        from gateway.platforms import api_server as _api_server_mod
        original_app_cls = _api_server_mod.web.Application
        file_handler = self._handle_file_serve
        options_handler = self._handle_file_options
        adapter_name = self.name
        media_mode = self.media_mode
        file_ttl = self._file_ttl

        class _PatchedApplication(original_app_cls):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                try:
                    self.router.add_get(
                        "/v1/files/{token}", file_handler
                    )
                    self.router.add_options(
                        "/v1/files/{token}", options_handler
                    )
                    logger.info(
                        "[%s] File serving enabled at /v1/files/{token} "
                        "(mode=%s, ttl=%ds)",
                        adapter_name, media_mode, file_ttl,
                    )
                except Exception:
                    logger.exception(
                        "[%s] Failed to register /v1/files route",
                        adapter_name,
                    )

        _api_server_mod.web.Application = _PatchedApplication
        try:
            ok = await super().connect()
        finally:
            _api_server_mod.web.Application = original_app_cls

        if not ok:
            # Server didn't start — clear the file server so we don't
            # accumulate stale token state on retry.
            self._file_server = None
        return ok

    async def _handle_file_serve(self, request: "web.Request") -> "web.StreamResponse":
        """Token-keyed file-serving handler.

        Auth: token IS the auth — same model as S3 presigned URLs.  The
        gateway's bearer-key middleware deliberately does not gate this
        route, because BoltAI loads images via plain ``<img src>`` and
        won't supply an Authorization header.  256 bits of randomness +
        configurable TTL is the security boundary.
        """
        if self._file_server is None:
            return web.Response(status=404, text="not found")
        return await self._file_server.handle(request)

    async def _handle_file_options(self, request):
        """OPTIONS preflight for /v1/files/{token} — CORS handshake."""
        if self._file_server is None:
            return web.Response(status=204)
        return await self._file_server.handle_options(request)

    # ------------------------------------------------------------------
    # Slash interception override
    # ------------------------------------------------------------------

    async def _handle_chat_completions(self, request):
        """Override: short-circuit known slash commands; otherwise delegate.

        Order:
          1. Auth gate via ``self._check_auth`` (parent's logic).
          2. Read body once (cached).  If unparseable / not a slash, replay
             the body and call ``super()._handle_chat_completions``.
          3. If it's a known slash command, dispatch via ``_message_handler``
             and return the result (streaming or non-streaming).

        Also: stash the inbound request in a ContextVar so the streaming
        media rewriter (created deep inside ``_run_agent``) can read its
        Host / X-Forwarded-* headers when building file URLs.
        """
        # Make the inbound request visible to ``_run_agent`` and the
        # streaming rewriter via contextvars.  Reset on the way out so
        # we don't leak request state across handlers.
        ctx_token = _REQUEST_CTX.set(request)
        try:
            # 1. Auth — never bypassed.
            auth_err = self._check_auth(request)
            if auth_err is not None:
                return auth_err

            # 2. Peek at the body.
            raw_body = b""
            try:
                raw_body = await request.read()
            except Exception:
                # Couldn't read the body — let the parent's error path
                # handle it with a fresh, empty replay.
                return await super()._handle_chat_completions(
                    self._replay_request(request, b"")
                )
            try:
                body = json.loads(raw_body) if raw_body else {}
            except (json.JSONDecodeError, ValueError):
                return await super()._handle_chat_completions(
                    self._replay_request(request, raw_body)
                )

            last_user_text = self._extract_last_user_text(body)
            if not is_known_slash_command(last_user_text):
                return await super()._handle_chat_completions(
                    self._replay_request(request, raw_body)
                )

            # 3. Slash dispatch.
            cmd_text = last_user_text.strip()
            try:
                # Some slash commands have artificial output caps in the
                # gateway runner that exist for chat clients (Discord
                # 2KB, Telegram 4KB).  On HTTP we don't need them —
                # render the unfiltered version locally before falling
                # back to the generic dispatcher.
                response_text = self._render_unlimited_slash(cmd_text)
                if response_text is None:
                    response_text = await self._dispatch_slash(cmd_text, request)
            except Exception as exc:
                logger.exception("Slash dispatch crashed for %r: %s", cmd_text, exc)
                response_text = f"Error: {exc}"
            if response_text is None:
                response_text = ""  # silent commands — return empty content

            stream_requested = bool(body.get("stream"))
            header_mode = request.headers.get("X-Hermes-Slash-Stream")
            mode = resolve_stream_mode(self.slash_stream_mode, header_mode)
            model = body.get("model") or self._model_name or "claude-code"

            if not stream_requested:
                return web.json_response(
                    _openai_full_response(response_text, model=model)
                )
            return await self._stream_slash_response(
                request, response_text, mode, model
            )
        finally:
            _REQUEST_CTX.reset(ctx_token)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_last_user_text(body: dict) -> str:
        """Return the most-recent user message's text, or ``''``."""
        msgs = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(msgs, list):
            return ""
        for msg in reversed(msgs):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "") or ""
            return ""
        return ""

    @staticmethod
    def _replay_request(request, raw_body):
        """Make a one-shot consumed request re-readable.

        aiohttp's ``request.read()`` / ``.json()`` / ``.text()`` are
        one-shot.  We've already read the body to peek at it, so we
        monkey-patch the request to replay the cached bytes for the parent
        handler.
        """
        body_bytes = raw_body or b""

        async def _replay_read():
            return body_bytes

        async def _replay_text(encoding: Optional[str] = None):
            enc = encoding or "utf-8"
            return body_bytes.decode(enc, errors="replace")

        async def _replay_json(loads=json.loads):
            if not body_bytes:
                raise json.JSONDecodeError("empty body", "", 0)
            return loads(body_bytes)

        try:
            request._read_bytes = body_bytes  # aiohttp internal cache
        except Exception:
            pass
        request.read = _replay_read           # type: ignore[assignment]
        request.text = _replay_text          # type: ignore[assignment]
        request.json = _replay_json          # type: ignore[assignment]
        return request

    async def _dispatch_slash(self, cmd_text: str, request) -> Optional[str]:
        """Dispatch a slash command via the gateway message handler.

        Returns the response text, or ``None`` for silent commands.  Falls
        back to a polite error string if no handler is registered.
        """
        handler = self._message_handler
        if handler is None:
            logger.warning(
                "BoltAIGatewayAdapter received slash %r but no message handler registered",
                cmd_text,
            )
            return "Gateway is starting up — try again in a moment."

        chat_id = (
            request.headers.get("X-Hermes-Session-Id", "").strip()
            or "boltai-api-anon"
        )
        user_id = request.headers.get("X-Hermes-User-Id", "").strip() or "api-user"

        source = self.build_source(
            chat_id=chat_id,
            chat_name="BoltAI Hermes Gateway",
            chat_type="dm",
            user_id=user_id,
            user_name=user_id,
        )
        event = MessageEvent(
            text=cmd_text,
            message_type=MessageType.COMMAND,
            source=source,
            # Bearer-token auth has already gated this request via
            # _check_auth.  Mark the event ``internal`` so the gateway
            # runner does NOT additionally apply per-user allowlists —
            # the synthesized "api-user" id would otherwise be rejected
            # against telegram/discord/slack-style ALLOWED_USERS lists.
            internal=True,
        )

        try:
            response = await handler(event)
        except Exception as exc:
            logger.exception("Slash dispatch failed for %r: %s", cmd_text, exc)
            return f"Error: {exc}"

        if response is None:
            return None
        if isinstance(response, str):
            return response
        if isinstance(response, tuple):
            return str(response[0]) if response else ""
        if isinstance(response, dict):
            return str(response.get("text") or response.get("content") or response)
        return str(response)

    def _render_unlimited_slash(self, cmd_text: str) -> Optional[str]:
        """Render full, unchunked output for slash commands the gateway runner
        truncates for chat clients.

        Returns the rendered text, or ``None`` to indicate "fall through to
        the regular dispatcher".

        Currently overrides:
          * ``/help``   — gateway runner caps skill list at 10 entries.
          * ``/commands`` (no page or ``all``) — gateway runner paginates
            at 15-20 entries; we render every page concatenated.

        Anything with explicit args (e.g. ``/commands 3``) falls through so
        the user can still page if they want to.
        """
        if not cmd_text or not cmd_text.startswith("/"):
            return None
        head, _, rest = cmd_text[1:].partition(" ")
        head = head.lower()
        rest = rest.strip()

        if head == "help":
            return self._render_full_help()
        if head in ("commands", "command"):
            if not rest or rest.lower() in ("all", "*", "full"):
                return self._render_full_commands()
        return None

    @staticmethod
    def _render_full_help() -> str:
        """Render the full /help output without the 10-skill truncation."""
        from hermes_cli.commands import gateway_help_lines
        lines = ["📖 **Hermes Commands**", ""]
        lines.extend(gateway_help_lines())
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                lines.append("")
                lines.append(f"⚡ **Skill Commands** ({len(skill_cmds)} active):")
                for cmd in sorted(skill_cmds):
                    desc = (skill_cmds[cmd].get("description") or "").strip() or "Skill command"
                    lines.append(f"`{cmd}` — {desc}")
        except Exception as exc:
            logger.debug("skill_commands enumeration failed: %s", exc)
        return "\n".join(lines)

    @staticmethod
    def _render_full_commands() -> str:
        """Render every command page, concatenated — no pagination."""
        from hermes_cli.commands import gateway_help_lines
        entries = list(gateway_help_lines())
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                entries.append("")
                entries.append("⚡ **Skill Commands**:")
                for cmd in sorted(skill_cmds):
                    desc = (skill_cmds[cmd].get("description") or "").strip() or "Skill command"
                    entries.append(f"`{cmd}` — {desc}")
        except Exception as exc:
            logger.debug("skill_commands enumeration failed: %s", exc)
        if not entries:
            return "No commands available."
        return f"📚 **Commands** ({len(entries)} total)\n\n" + "\n".join(entries)

    async def _stream_slash_response(
        self,
        request,
        text: str,
        mode: str,
        model: str,
    ):
        """Emit OpenAI-style SSE for a slash command response."""
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)

        async def _emit(chunk: dict) -> None:
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())

        try:
            if not text:
                await _emit(
                    _openai_chunk(
                        "",
                        finish_reason="stop",
                        model=model,
                        completion_id=completion_id,
                        created=created,
                    )
                )
            elif mode == SLASH_STREAM_SINGLE_CHUNK:
                await _emit(
                    _openai_chunk(
                        text,
                        model=model,
                        completion_id=completion_id,
                        created=created,
                    )
                )
                await _emit(
                    _openai_chunk(
                        "",
                        finish_reason="stop",
                        model=model,
                        completion_id=completion_id,
                        created=created,
                    )
                )
            else:  # token_stream
                for i in range(0, len(text), TOKEN_STREAM_CHUNK_SIZE):
                    piece = text[i : i + TOKEN_STREAM_CHUNK_SIZE]
                    await _emit(
                        _openai_chunk(
                            piece,
                            model=model,
                            completion_id=completion_id,
                            created=created,
                        )
                    )
                    await asyncio.sleep(TOKEN_STREAM_DELAY_S)
                await _emit(
                    _openai_chunk(
                        "",
                        finish_reason="stop",
                        model=model,
                        completion_id=completion_id,
                        created=created,
                    )
                )

            await resp.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, asyncio.CancelledError) as exc:
            # Client disconnected mid-stream — log and let the finally
            # block clean up.  Do not re-raise; the response is already
            # half-sent and aiohttp expects us to return ``resp``.
            logger.info(
                "BoltAI gateway client disconnected during slash stream: %s", exc,
            )
        except Exception:
            logger.exception("Error while streaming slash response")
        finally:
            # Always finalize the response so aiohttp's connection
            # bookkeeping releases the writer.  ``write_eof`` is safe to
            # call even after a partial write or a disconnect.
            try:
                await resp.write_eof()
            except Exception:
                logger.debug("write_eof failed during slash stream cleanup", exc_info=True)
        return resp


    # ------------------------------------------------------------------
    # Media rewriting override
    # ------------------------------------------------------------------

    def _build_url_for_request(self, request: Optional["web.Request"]):
        """Return a ``url_builder(token) -> url`` closure for the rewriter.

        The closure captures the inbound request so the rewriter can
        read its Host / X-Forwarded-* headers when constructing URLs.
        Returns ``None`` if the file server isn't available.
        """
        if self._file_server is None:
            return None
        if request is None:
            # No request context available (e.g. agent triggered from
            # a non-HTTP code path).  Use the configured override or a
            # localhost guess so registration still succeeds.
            base = self._file_url_base or f"http://{self._host}:{self._port}"
            base = base.rstrip("/")
            return lambda tok: f"{base}/v1/files/{tok}"
        fs = self._file_server
        return lambda tok: fs.url_for(tok, request)

    def _resolve_media_mode(self) -> str:
        """Pick the effective rewrite mode.

        Falls back to ``inline`` if ``link`` is configured but the file
        server didn't initialise (e.g. registration failed during
        ``connect``).  Falls back to ``off`` if even inline can't run
        for some reason.
        """
        if self.media_mode == MODE_LINK and self._file_server is None:
            return MODE_INLINE
        return self.media_mode

    async def _run_agent(
        self,
        user_message: str,
        conversation_history,
        ephemeral_system_prompt=None,
        session_id=None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        agent_ref=None,
        gateway_session_key=None,
    ):
        """Override: rewrite local file refs in agent output.

        Mode-aware (see :mod:`media_rewriter`):
          * ``link`` (default) — register each local file with the in-process
            :class:`FileServer` and rewrite to ``http://.../v1/files/<token>``.
            Smaller payloads, supports any file type, BoltAI caches the URL.
          * ``inline`` — base64 encode images as data URLs (v0.2.0).
          * ``off`` — pass-through, no rewriting.

        Streaming and non-streaming paths are both handled.
        """
        effective_mode = self._resolve_media_mode()
        request = _REQUEST_CTX.get()
        url_builder = self._build_url_for_request(request)

        rewriter = None
        cb = stream_delta_callback
        if cb is not None and effective_mode != MODE_OFF:
            rewriter = StreamingMediaRewriter(
                emit=cb,
                mode=effective_mode,
                file_server=self._file_server,
                url_builder=url_builder,
            )
            cb = rewriter.feed

        try:
            result, usage = await super()._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=ephemeral_system_prompt,
                session_id=session_id,
                stream_delta_callback=cb,
                tool_progress_callback=tool_progress_callback,
                tool_start_callback=tool_start_callback,
                tool_complete_callback=tool_complete_callback,
                agent_ref=agent_ref,
                gateway_session_key=gateway_session_key,
            )
        finally:
            if rewriter is not None:
                try:
                    rewriter.flush()
                except Exception:
                    logger.exception("Media rewriter flush failed")

        # Non-streaming path uses result['final_response'] directly.
        # The streaming path also reads it as a fallback when no deltas
        # were emitted, so rewriting here covers both.
        if isinstance(result, dict) and effective_mode != MODE_OFF:
            fr = result.get("final_response")
            if isinstance(fr, str) and fr:
                try:
                    result["final_response"] = rewrite_media(
                        fr,
                        mode=effective_mode,
                        file_server=self._file_server,
                        url_builder=url_builder,
                    )
                except Exception:
                    logger.exception("final_response media rewrite failed")
        return result, usage


def _check_requirements() -> tuple[bool, str]:
    """Verify aiohttp is importable (same dep as built-in api_server)."""
    try:
        import aiohttp  # noqa: F401
        return True, ""
    except ImportError:
        return False, "aiohttp is required (it ships with hermes-agent)"


def _env_enablement() -> dict[str, Any]:
    """Auto-seed PlatformConfig.extra (and ``enabled``) from env vars.

    Recognized: ENABLED, PORT, HOST, KEY, CORS_ORIGINS, MODEL_NAME,
    SLASH_STREAM_MODE, MEDIA_MODE, FILE_TTL, FILE_URL_BASE.
    """
    seed: dict[str, Any] = {}
    # ENABLED is read by gateway plugin loader; we surface it via the
    # ``enabled`` key in the seed so config-builders can pick it up.
    if _env_bool("ENABLED"):
        seed["enabled"] = True
    if (port := _env("PORT")) is not None:
        try:
            seed["port"] = int(port)
        except ValueError:
            pass
    if (host := _env("HOST")) is not None:
        seed["host"] = host
    if (key := _env("KEY")) is not None:
        seed["key"] = key
    if (origins := _env("CORS_ORIGINS")) is not None:
        seed["cors_origins"] = origins
    if (model := _env("MODEL_NAME")) is not None:
        seed["model_name"] = model
    if (mode := _env("SLASH_STREAM_MODE")) is not None:
        seed["slash_stream_mode"] = mode
    if (mm := _env("MEDIA_MODE")) is not None:
        seed["media_mode"] = mm
    if (ttl := _env("FILE_TTL")) is not None:
        try:
            seed["file_ttl"] = int(ttl)
        except ValueError:
            pass
    if (base := _env("FILE_URL_BASE")) is not None:
        seed["file_url_base"] = base
    return seed


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="BoltAI Hermes Gateway",
        adapter_factory=lambda cfg: BoltAIGatewayAdapter(cfg),
        check_fn=_check_requirements,
        required_env=[],
        install_hint="No extra packages needed (uses aiohttp from gateway core)",
        env_enablement_fn=_env_enablement,
        emoji="🔌",
        pii_safe=True,
        platform_hint=(
            "You are responding via an OpenAI-compatible API endpoint "
            "(BoltAI client or similar). Markdown rendering is available."
        ),
    )
