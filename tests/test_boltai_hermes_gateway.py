"""Smoke tests for boltai_hermes_gateway plugin."""
import importlib

import pytest

from gateway.config import Platform, PlatformConfig


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip every env var that could leak into the adapter's config seeding."""
    for var in (
        "BOLTAI_HERMES_GW_ENABLED",
        "BOLTAI_HERMES_GW_PORT",
        "BOLTAI_HERMES_GW_HOST",
        "BOLTAI_HERMES_GW_KEY",
        "BOLTAI_HERMES_GW_CORS_ORIGINS",
        "BOLTAI_HERMES_GW_MODEL_NAME",
        "BOLTAI_HERMES_GW_SLASH_STREAM_MODE",
        "API_SERVER_PORT",
        "API_SERVER_HOST",
        "API_SERVER_KEY",
        "API_SERVER_CORS_ORIGINS",
        "API_SERVER_MODEL_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_plugin_module_imports():
    mod = importlib.import_module("plugins.platforms.boltai_hermes_gateway.adapter")
    assert hasattr(mod, "register")
    assert hasattr(mod, "BoltAIGatewayAdapter")


def test_default_port_is_8643():
    from plugins.platforms.boltai_hermes_gateway.adapter import DEFAULT_PORT
    assert DEFAULT_PORT == 8643


def test_env_prefix_is_namespaced():
    """Plugin must use BOLTAI_HERMES_GW_ prefix, not BOLTAI_GATEWAY_ or API_SERVER_."""
    from plugins.platforms.boltai_hermes_gateway.adapter import ENV_PREFIX
    assert ENV_PREFIX == "BOLTAI_HERMES_GW_"


def test_adapter_subclasses_api_server():
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    from gateway.platforms.api_server import APIServerAdapter
    assert issubclass(BoltAIGatewayAdapter, APIServerAdapter)


def test_adapter_default_port_when_none_configured():
    """With no port in config.extra and no env override, defaults to 8643."""
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._port == 8643


def test_adapter_default_host_is_localhost():
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._host == "127.0.0.1"


def test_adapter_uses_unique_platform_value():
    """Adapter must NOT report platform == Platform.API_SERVER, or registry collides."""
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter.platform != Platform.API_SERVER
    assert adapter.platform.value == "boltai_hermes_gateway"


def test_register_calls_register_platform():
    """register(ctx) must call ctx.register_platform with name='boltai_hermes_gateway'."""
    from plugins.platforms.boltai_hermes_gateway.adapter import register

    captured = {}
    class FakeCtx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)
    register(FakeCtx())
    assert captured["name"] == "boltai_hermes_gateway"
    assert callable(captured["adapter_factory"])
    assert callable(captured["check_fn"])


# ---------------------------------------------------------------------------
# Namespaced env vars: BOLTAI_HERMES_GW_* must override defaults AND must NOT
# fall through to API_SERVER_* (which would cause the two adapters to share
# secrets/settings).
# ---------------------------------------------------------------------------


def test_port_from_namespaced_env(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_PORT", "9000")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._port == 9000


def test_host_from_namespaced_env(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_HOST", "0.0.0.0")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._host == "0.0.0.0"


def test_key_from_namespaced_env(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_KEY", "secret-token-123")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._api_key == "secret-token-123"


def test_cors_origins_from_namespaced_env(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_CORS_ORIGINS", "https://app.example.com,https://other.example.com")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert "https://app.example.com" in adapter._cors_origins
    assert "https://other.example.com" in adapter._cors_origins


def test_does_not_fall_through_to_api_server_key(monkeypatch):
    """API_SERVER_KEY must NOT leak into the BoltAI gateway when its own KEY is unset.

    This is the core isolation guarantee: two side-by-side adapters,
    different secrets, no cross-contamination.
    """
    monkeypatch.setenv("API_SERVER_KEY", "leaky-shared-key")
    monkeypatch.delenv("BOLTAI_HERMES_GW_KEY", raising=False)
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    # Empty when no namespaced key is set, even though API_SERVER_KEY exists.
    assert adapter._api_key == ""


def test_does_not_fall_through_to_api_server_port(monkeypatch):
    """API_SERVER_PORT must NOT change BoltAI gateway port (we always default to 8643)."""
    monkeypatch.setenv("API_SERVER_PORT", "7777")
    monkeypatch.delenv("BOLTAI_HERMES_GW_PORT", raising=False)
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._port == 8643


def test_config_extra_takes_precedence_over_env(monkeypatch):
    """Explicit config.extra values win over env vars (standard Hermes precedence)."""
    monkeypatch.setenv("BOLTAI_HERMES_GW_PORT", "9000")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={"port": 8888})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._port == 8888


def test_env_enablement_seeds_all_namespaced_vars(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_ENABLED", "true")
    monkeypatch.setenv("BOLTAI_HERMES_GW_PORT", "9100")
    monkeypatch.setenv("BOLTAI_HERMES_GW_HOST", "0.0.0.0")
    monkeypatch.setenv("BOLTAI_HERMES_GW_KEY", "k")
    monkeypatch.setenv("BOLTAI_HERMES_GW_CORS_ORIGINS", "*")
    monkeypatch.setenv("BOLTAI_HERMES_GW_MODEL_NAME", "hermes-boltai")
    from plugins.platforms.boltai_hermes_gateway.adapter import _env_enablement
    seed = _env_enablement()
    assert seed["enabled"] is True
    assert seed["port"] == 9100
    assert seed["host"] == "0.0.0.0"
    assert seed["key"] == "k"
    assert seed["cors_origins"] == "*"
    assert seed["model_name"] == "hermes-boltai"


def test_env_enablement_returns_empty_when_nothing_set():
    from plugins.platforms.boltai_hermes_gateway.adapter import _env_enablement
    assert _env_enablement() == {}


def test_invalid_port_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_PORT", "not-a-number")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._port == 8643


# ===========================================================================
# Task 3 — slash command interception
# ===========================================================================


@pytest.mark.parametrize("text,expected", [
    ("/help", True),
    ("/stop", True),
    ("/status", True),
    ("hello world", False),
    ("/notarealcommand", False),
    ("", False),
    ("  /help  ", True),
    ("/", False),  # bare slash is not a command
])
def test_is_known_slash_command(text, expected):
    from plugins.platforms.boltai_hermes_gateway.adapter import is_known_slash_command
    assert is_known_slash_command(text) is expected


def test_stream_mode_constants():
    from plugins.platforms.boltai_hermes_gateway.adapter import (
        SLASH_STREAM_SINGLE_CHUNK, SLASH_STREAM_TOKEN_STREAM, VALID_STREAM_MODES,
    )
    assert SLASH_STREAM_SINGLE_CHUNK == "single_chunk"
    assert SLASH_STREAM_TOKEN_STREAM == "token_stream"
    assert SLASH_STREAM_SINGLE_CHUNK in VALID_STREAM_MODES
    assert SLASH_STREAM_TOKEN_STREAM in VALID_STREAM_MODES


def test_default_stream_mode_is_token_stream():
    from plugins.platforms.boltai_hermes_gateway.adapter import (
        BoltAIGatewayAdapter, SLASH_STREAM_TOKEN_STREAM,
    )
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter.slash_stream_mode == SLASH_STREAM_TOKEN_STREAM


def test_stream_mode_from_env_can_select_single_chunk(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_SLASH_STREAM_MODE", "single_chunk")
    from plugins.platforms.boltai_hermes_gateway.adapter import (
        BoltAIGatewayAdapter, SLASH_STREAM_SINGLE_CHUNK,
    )
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter.slash_stream_mode == SLASH_STREAM_SINGLE_CHUNK


def test_stream_mode_from_config_overrides_env(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_SLASH_STREAM_MODE", "token_stream")
    from plugins.platforms.boltai_hermes_gateway.adapter import (
        BoltAIGatewayAdapter, SLASH_STREAM_SINGLE_CHUNK,
    )
    cfg = PlatformConfig(enabled=True, extra={"slash_stream_mode": "single_chunk"})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter.slash_stream_mode == SLASH_STREAM_SINGLE_CHUNK


def test_invalid_stream_mode_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BOLTAI_HERMES_GW_SLASH_STREAM_MODE", "bogus")
    from plugins.platforms.boltai_hermes_gateway.adapter import (
        BoltAIGatewayAdapter, SLASH_STREAM_TOKEN_STREAM,
    )
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter.slash_stream_mode == SLASH_STREAM_TOKEN_STREAM


def test_resolve_stream_mode_header_overrides_config():
    from plugins.platforms.boltai_hermes_gateway.adapter import resolve_stream_mode
    assert resolve_stream_mode("single_chunk", "token_stream") == "token_stream"
    assert resolve_stream_mode("token_stream", None) == "token_stream"
    assert resolve_stream_mode("token_stream", "") == "token_stream"
    assert resolve_stream_mode("single_chunk", "bogus") == "single_chunk"  # invalid header ignored
    assert resolve_stream_mode("bogus", None) == "single_chunk"  # invalid config -> default


def test_max_message_length_is_unlimited():
    """HTTP adapter should not artificially chunk responses (10 MB ceiling)."""
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter.MAX_MESSAGE_LENGTH >= 1_000_000


# ---- Unlimited slash rendering (no truncation/pagination) ----

def test_render_unlimited_slash_help_returns_full_skill_list():
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    result = adapter._render_unlimited_slash("/help")
    assert result is not None
    # The truncated version says "and N more". The unlimited one must NOT.
    assert "more. Use `/commands`" not in result
    assert "Hermes Commands" in result


def test_render_unlimited_slash_commands_returns_all_pages():
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    result = adapter._render_unlimited_slash("/commands")
    assert result is not None
    # Should include the total count header but no page navigation
    assert "total" in result
    assert "page " not in result.lower() or "next →" not in result


def test_render_unlimited_slash_commands_with_page_arg_falls_through():
    """/commands 3 should still get the paginated version from the runner."""
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._render_unlimited_slash("/commands 3") is None


def test_render_unlimited_slash_returns_none_for_other_commands():
    """We only override /help and /commands; everything else falls through."""
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter
    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    assert adapter._render_unlimited_slash("/status") is None
    assert adapter._render_unlimited_slash("/stop") is None
    assert adapter._render_unlimited_slash("/model gpt-4") is None
    assert adapter._render_unlimited_slash("not a slash") is None
    assert adapter._render_unlimited_slash("") is None


# ---- Integration tests using aiohttp TestServer/TestClient ----

import contextlib


@contextlib.asynccontextmanager
async def _http_client(adapter):
    """Spin up an aiohttp TestServer wired to the adapter's chat completions
    handler, yield a TestClient, and clean up.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unauthenticated_slash_request_returns_401(monkeypatch):
    """Slash dispatch must NOT bypass auth.  With API key set and no
    Authorization header, return 401."""
    monkeypatch.setenv("BOLTAI_HERMES_GW_KEY", "test-secret")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter

    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    async with _http_client(adapter) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "claude-code", "messages": [{"role": "user", "content": "/help"}]},
        )
        assert resp.status == 401


@pytest.mark.asyncio
async def test_authenticated_slash_returns_command_output(monkeypatch):
    """Authenticated slash command must short-circuit the agent loop and return command text.

    Uses /status (not /help) because /help is intercepted by the unlimited
    renderer that bypasses the message handler — see
    test_render_unlimited_slash_help_returns_full_skill_list.
    """
    monkeypatch.setenv("BOLTAI_HERMES_GW_KEY", "test-secret")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter

    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)
    captured_event = []

    async def fake_handler(event):
        captured_event.append(event)
        return "Slash output for testing"

    adapter.set_message_handler(fake_handler)

    async with _http_client(adapter) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-secret"},
            json={"model": "claude-code", "messages": [{"role": "user", "content": "/status"}]},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["choices"][0]["message"]["content"] == "Slash output for testing"
        assert body["object"] == "chat.completion"
        # Confirm our event was dispatched (proves we didn't fall through to the agent loop)
        assert len(captured_event) == 1
        assert captured_event[0].text == "/status"


@pytest.mark.asyncio
async def test_authenticated_slash_streaming_single_chunk(monkeypatch):
    """single_chunk mode test — uses /status (not /help) for the same reason."""
    monkeypatch.setenv("BOLTAI_HERMES_GW_KEY", "test-secret")
    monkeypatch.setenv("BOLTAI_HERMES_GW_SLASH_STREAM_MODE", "single_chunk")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter

    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)

    async def fake_handler(event):
        return "ok"

    adapter.set_message_handler(fake_handler)

    async with _http_client(adapter) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-secret"},
            json={"model": "claude-code", "stream": True,
                  "messages": [{"role": "user", "content": "/status"}]},
        )
        assert resp.status == 200
        text = await resp.text()
        assert "data: " in text
        assert "[DONE]" in text
        assert '"content": "ok"' in text or '"content":"ok"' in text


@pytest.mark.asyncio
async def test_authenticated_slash_streaming_token_stream(monkeypatch):
    """token_stream mode must emit MULTIPLE data chunks for a long response."""
    monkeypatch.setenv("BOLTAI_HERMES_GW_KEY", "test-secret")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter

    cfg = PlatformConfig(enabled=True, extra={"slash_stream_mode": "token_stream"})
    adapter = BoltAIGatewayAdapter(cfg)
    long_response = "A" * 200

    async def fake_handler(event):
        return long_response

    adapter.set_message_handler(fake_handler)

    async with _http_client(adapter) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-secret"},
            json={"model": "claude-code", "stream": True,
                  "messages": [{"role": "user", "content": "/help"}]},
        )
        assert resp.status == 200
        text = await resp.text()
        data_chunks = [
            line for line in text.split("\n")
            if line.startswith("data: ") and "[DONE]" not in line
        ]
        # 200 chars / 20 per chunk = 10 streaming chunks + 1 finish
        assert len(data_chunks) >= 10, (
            f"Expected >=10 streaming chunks, got {len(data_chunks)}: {text[:500]}"
        )


@pytest.mark.asyncio
async def test_streaming_finalizes_on_client_disconnect(monkeypatch):
    """If the client disconnects mid-stream, write_eof must still be called.

    Simulates a ConnectionResetError mid-emit and asserts the method
    returns cleanly (does not raise) and the response object has been
    finalized.  Regression test for the try/finally cleanup added after
    the Task 3 quality review.
    """
    monkeypatch.setenv("BOLTAI_HERMES_GW_KEY", "test-secret")
    from plugins.platforms.boltai_hermes_gateway.adapter import (
        BoltAIGatewayAdapter, SLASH_STREAM_TOKEN_STREAM,
    )

    cfg = PlatformConfig(enabled=True, extra={"slash_stream_mode": SLASH_STREAM_TOKEN_STREAM})
    adapter = BoltAIGatewayAdapter(cfg)

    write_calls = []
    eof_called = []

    class StubResponse:
        async def prepare(self, request):
            pass
        async def write(self, data):
            write_calls.append(data)
            if len(write_calls) == 2:
                raise ConnectionResetError("client gone")
        async def write_eof(self):
            eof_called.append(True)

    class FakeRequest:
        headers = {}

    import plugins.platforms.boltai_hermes_gateway.adapter as adapter_mod
    monkeypatch.setattr(adapter_mod.web, "StreamResponse", lambda **kw: StubResponse())

    # Should NOT raise even though .write() throws ConnectionResetError mid-stream.
    result = await adapter._stream_slash_response(
        FakeRequest(), "A" * 200, SLASH_STREAM_TOKEN_STREAM, "hermes-agent",
    )
    assert result is not None
    assert eof_called == [True], "write_eof must be called even on disconnect"


@pytest.mark.asyncio
async def test_non_slash_falls_through_to_super(monkeypatch):
    """A non-slash message must NOT be intercepted — it falls through to APIServerAdapter."""
    monkeypatch.setenv("BOLTAI_HERMES_GW_KEY", "test-secret")
    from plugins.platforms.boltai_hermes_gateway.adapter import BoltAIGatewayAdapter

    cfg = PlatformConfig(enabled=True, extra={})
    adapter = BoltAIGatewayAdapter(cfg)

    fallthrough_marker = []

    async def fake_run_agent(**kwargs):
        fallthrough_marker.append(kwargs)
        return {"final_response": "fallthrough ok", "messages": []}

    # Patch the parent's _run_agent on this instance so we can detect
    # fallthrough WITHOUT actually starting the real agent loop.
    adapter._run_agent = fake_run_agent  # type: ignore[assignment]

    async with _http_client(adapter) as client:
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-secret"},
            json={"model": "claude-code",
                  "messages": [{"role": "user", "content": "what is 2+2?"}]},
        )
    assert len(fallthrough_marker) == 1, (
        "Expected APIServerAdapter._run_agent to be called for non-slash"
    )
