"""BoltAI Hermes Gateway — OpenAI-compatible adapter with slash command support.

Subclasses gateway.platforms.api_server.APIServerAdapter to inherit all
endpoints (chat completions, responses API, runs, models, capabilities,
health), streaming, session continuity, CORS, and bearer-token auth.

Task 3 adds slash command interception: messages starting with ``/`` that
resolve to a known gateway command are dispatched via the gateway message
handler instead of the agent loop.  Auth is enforced FIRST — slash dispatch
does not bypass authentication.

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
"""
from __future__ import annotations

import asyncio
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

from .image_inliner import StreamingImageRewriter, rewrite_local_images

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8643
DEFAULT_HOST = "127.0.0.1"
PLATFORM_NAME = "boltai_hermes_gateway"

# Env var prefix for ALL plugin settings.  Keep this in sync with the
# documentation block above and with plugin.yaml's optional_env entries.
ENV_PREFIX = "BOLTAI_HERMES_GW_"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

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
        """
        # 1. Auth — never bypassed.
        auth_err = self._check_auth(request)
        if auth_err is not None:
            return auth_err

        # 2. Peek at the body.
        raw_body = b""
        try:
            raw_body = await request.read()
        except Exception:
            # Couldn't read the body — let the parent's error path handle it
            # with a fresh, empty replay.
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
            # Some slash commands have artificial output caps in the gateway
            # runner that exist for chat clients (Discord 2KB, Telegram 4KB).
            # On HTTP we don't need them — render the unfiltered version
            # locally before falling back to the generic dispatcher.
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
            return web.json_response(_openai_full_response(response_text, model=model))
        return await self._stream_slash_response(request, response_text, mode, model)

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
    # Image inlining override
    # ------------------------------------------------------------------

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
        """Override: rewrite local image paths to base64 data URLs.

        BoltAI / OpenAI-compatible clients can't read the gateway's
        filesystem, so ``![cat](/Users/.../foo.png)`` won't render.  We
        replace local-path markdown images with ``data:image/...;base64,``
        URLs so the markdown renderer displays them inline.

        * Streaming: wrap ``stream_delta_callback`` with a buffered
          rewriter that holds back text only when an unclosed ``![`` is
          in flight.  The original callback (which feeds the SSE queue)
          sees rewritten text.
        * Non-streaming: rewrite ``result['final_response']`` after the
          agent returns, before passing it back up to the parent's
          response-builder.
        * The buffer is flushed on agent completion so the trailing
          partial — if any — still reaches the client.
        """
        rewriter = None
        cb = stream_delta_callback
        if cb is not None:
            rewriter = StreamingImageRewriter(emit=cb)
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
                    logger.exception("Image rewriter flush failed")

        # Non-streaming path uses result['final_response'] directly.  The
        # streaming path also reads it as a fallback when no deltas were
        # emitted (see api_server.py L1789-1797), so rewriting it here
        # covers both.
        if isinstance(result, dict):
            fr = result.get("final_response")
            if isinstance(fr, str) and fr:
                try:
                    result["final_response"] = rewrite_local_images(fr)
                except Exception:
                    logger.exception("final_response image rewrite failed")
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
    SLASH_STREAM_MODE.
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
