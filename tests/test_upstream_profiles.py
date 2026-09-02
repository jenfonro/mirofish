"""Capture-derived HTTPX request profiles for upstream endpoints.

The assertions run on the HTTPX request model, which is also the wire order:
``mirofish.wire`` makes h11 emit fields as held instead of Host-first
(``tests/test_wire_profile.py`` measures that on a loopback socket).  TLS
remains the one layer that is not byte-identical; see ``upstream.tls_context``.
"""

import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from mirofish.config import DEFAULT_CODEX_USER_AGENT
from mirofish.upstream import CLAUDE_AGENT_SYSTEM_MARKER, LIMITS_PATH
from tests.conftest import AUTH_BASE, RELAY_BASE, add_account
from tests.mirasim_protocol import relay_metadata, verify_signature
from tests.test_request_profile import _body as captured_messages_body
from tests.test_request_profile import _headers as captured_messages_headers
from tests.test_request_profile import codex_body, codex_caller_headers
from tools.request_profile import compare_profiles, request_profile


FIXTURE_DIR = Path(__file__).parent / "fixtures/request_profiles"
OFFICIAL_AUTH_BASE = "https://auth.mirasim.ai"
OFFICIAL_RELAY_BASE = "https://relay.mirasim.ai"


_MACHINE_SLOT = ("x-stainless-arch", "x-stainless-os")


def _header_names(request: httpx.Request) -> list[str]:
    return [name.decode("ascii") for name, _ in request.headers.raw]


def _machine_slot(request: httpx.Request) -> tuple[str, str]:
    return tuple(request.headers[name] for name in _MACHINE_SLOT)


def _stainless_block(request: httpx.Request) -> dict[str, str]:
    return {name: value for name, value in request.headers.items()
            if name.startswith("x-stainless-")}


def _assert_matches_golden(request: httpx.Request, fixture_name: str) -> None:
    expected = json.loads((FIXTURE_DIR / fixture_name).read_text())
    actual = request_profile(
        request.method,
        str(request.url),
        "HTTP/1.1",
        request.headers.raw,
        request.content,
    )
    assert compare_profiles(expected, actual) == []


def _mock_device_session(ticket: str = "device-ticket"):
    return respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(200, json={"ticket": ticket, "expiresIn": 900}))


def _verify_signature(state, request: httpx.Request, path: str) -> None:
    """The mrs-sig-v2 record binds the bearer the request carries, the
    relay metadata (sealed or clear) and the exact body to this pathname."""
    verify_signature(state, request, path)


#: Field order inside a sealed model envelope: the desktop assigns these onto
#: its request-header object in this sequence, then signs, then seals every
#: ``x-mirasim-*`` field except the clear client marker.
SEALED_MODEL_FIELDS = [
    "x-mirasim-session",
    "x-mirasim-agent",
    "x-mirasim-device",
    "x-mirasim-locale",
    "x-mirasim-call",
    "x-mirasim-ts",
    "x-mirasim-nonce",
    "x-mirasim-sig",
]


@pytest.mark.parametrize(("path", "authorization_name"), [
    ("/auth/me", "Authorization"),
    ("/auth/referral", "authorization"),
])
@respx.mock
async def test_auth_get_profile_has_only_captured_order(
        state, path, authorization_name):
    route = respx.get(AUTH_BASE + path).mock(
        return_value=httpx.Response(200, json={"ok": True}))

    status, _, _ = await state.upstream.json(
        "GET", AUTH_BASE, path, access="account-token")

    assert status == 200
    assert _header_names(route.calls.last.request) == [
        authorization_name,
        "accept-encoding",
        "Host",
        "Connection",
    ]
    assert route.calls.last.request.headers["accept-encoding"] == "identity"
    assert "user-agent" not in route.calls.last.request.headers
    assert "accept" not in route.calls.last.request.headers


@respx.mock
async def test_unauthenticated_control_and_auth_post_profiles(state):
    providers = respx.get(AUTH_BASE + "/auth/oauth/providers").mock(
        return_value=httpx.Response(200, json={"providers": []}))
    code = respx.post(AUTH_BASE + "/auth/code").mock(
        return_value=httpx.Response(200, json={"sent": True}))

    await state.upstream.json("GET", AUTH_BASE, "/auth/oauth/providers")
    payload = {"email": "person@example.test"}
    await state.upstream.json("POST", AUTH_BASE, "/auth/code", payload)

    assert _header_names(providers.calls.last.request) == [
        "accept-encoding", "Host", "Connection",
    ]
    assert _header_names(code.calls.last.request) == [
        "content-type",
        "accept-encoding",
        "content-length",
        "Host",
        "Connection",
    ]
    assert code.calls.last.request.content == json.dumps(
        payload, separators=(",", ":")).encode("utf-8")


@respx.mock
async def test_unsigned_initial_limits_profile_is_probe_first(state):
    route = respx.get(RELAY_BASE + LIMITS_PATH).mock(
        return_value=httpx.Response(200, json={"windows": []}))

    await state.upstream.json(
        "GET", RELAY_BASE, LIMITS_PATH, access="account-token")

    request = route.calls.last.request
    assert _header_names(request) == [
        "x-mirasim-probe",
        "Authorization",
        "x-mirasim-client",
        "accept-encoding",
        "Host",
        "Connection",
    ]
    assert request.headers["authorization"] == "Bearer account-token"


@respx.mock
async def test_device_session_profile_has_exact_captured_order(state):
    add_account(state, "work")
    route = _mock_device_session()

    await state.upstream._mint_device_ticket("work", "access-work")

    request = route.calls.last.request
    assert _header_names(request) == [
        "content-type",
        "authorization",
        "x-mirasim-device",
        "x-mirasim-ts",
        "x-mirasim-nonce",
        "x-mirasim-sig",
        "x-mirasim-client",
        "accept-encoding",
        "content-length",
        "Host",
        "Connection",
    ]
    assert request.headers["authorization"] == "Bearer access-work"
    assert request.headers["accept-encoding"] == "identity"
    assert "user-agent" not in request.headers
    assert "accept" not in request.headers
    _verify_signature(state, request, "/v1/device/session")


@respx.mock
async def test_signed_limits_profile_is_probe_first_and_lean(state):
    add_account(state, "work")
    _mock_device_session()
    route = respx.get(RELAY_BASE + LIMITS_PATH).mock(
        return_value=httpx.Response(200, json={"windows": []}))

    await state.upstream.signed_json("work", "GET", LIMITS_PATH)

    request = route.calls.last.request
    assert _header_names(request) == [
        "x-mirasim-probe",
        "Authorization",
        "x-mirasim-device",
        "x-mirasim-ts",
        "x-mirasim-nonce",
        "x-mirasim-sig",
        "x-mirasim-client",
        "accept-encoding",
        "Host",
        "Connection",
    ]
    assert request.headers["authorization"] == "Bearer device-ticket"
    assert request.headers["accept-encoding"] == "identity"
    assert "user-agent" not in request.headers
    assert "accept" not in request.headers
    _verify_signature(state, request, LIMITS_PATH)


@respx.mock
async def test_signed_models_profile_matches_the_capture(state):
    """``/v1/models`` is the one signed GET in the 0.0.272 capture: lower-case
    ``authorization`` and no probe marker, unlike the usage probe."""
    add_account(state, "work")
    state.settings.relay_base = OFFICIAL_RELAY_BASE
    respx.post(OFFICIAL_RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(
            200, json={"ticket": "device-ticket", "expiresIn": 900}))
    route = respx.get(OFFICIAL_RELAY_BASE + "/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []}))

    await state.upstream.signed_json("work", "GET", "/v1/models")

    request = route.calls.last.request
    assert _header_names(request) == [
        "authorization",
        "x-mirasim-device",
        "x-mirasim-ts",
        "x-mirasim-nonce",
        "x-mirasim-sig",
        "x-mirasim-client",
        "accept-encoding",
        "Host",
        "Connection",
    ]
    assert "x-mirasim-probe" not in request.headers
    _assert_matches_golden(request, "models_official.json")
    _verify_signature(state, request, "/v1/models")


CLAUDE_HEADERS = [
    ("accept", "application/json"),
    ("content-type", "application/json"),
    ("user-agent", "claude-cli/2.1.252 (external, mirasim)"),
    ("x-claude-code-session-id", "0f20cf48-c292-42e9-a99e-994511307deb"),
    ("x-stainless-arch", "arm64"),
    ("x-stainless-lang", "js"),
    ("x-stainless-os", "MacOS"),
    ("x-stainless-package-version", "0.112.1"),
    ("x-stainless-retry-count", "0"),
    ("x-stainless-runtime", "node"),
    ("x-stainless-runtime-version", "v26.3.0"),
    ("x-stainless-timeout", "600"),
    ("anthropic-beta", "claude-code-20250219,oauth-2025-04-20,effort-2025-11-24"),
    ("anthropic-dangerous-direct-browser-access", "true"),
    ("anthropic-version", "2023-06-01"),
    ("x-app", "cli"),
    ("accept-encoding", "gzip, deflate, br, zstd"),
]


@respx.mock
async def test_messages_preserve_sdk_order_and_isolate_caller_credentials(state):
    add_account(state, "work")
    _mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages?beta=true").mock(
        return_value=httpx.Response(200, json={"content": [], "usage": {}}))
    incoming = httpx.Headers([
        *CLAUDE_HEADERS,
        ("authorization", "Bearer caller-secret"),
        ("x-api-key", "caller-api-key"),
        ("x-mirofish-proxy-key", "local-proxy-key"),
        ("x-stainless-secret", "must-not-forward"),
    ])
    payload = {
        "model": "model-under-test",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 8,
    }

    await state.upstream.messages(
        "work", payload, request_headers=incoming,
        session_id="0f20cf48-c292-42e9-a99e-994511307deb", beta=True)

    request = route.calls.last.request
    assert _header_names(request) == [
        *[name for name, _ in CLAUDE_HEADERS],
        "authorization",
        "x-mirasim-client",
        "x-mirasim-enc",
        "content-length",
        "Host",
        "Connection",
    ]
    assert request.content == json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert request.headers["authorization"] == "Bearer device-ticket"
    assert request.headers["x-mirasim-client"] == "0.0.272"
    sealed = relay_metadata(request, "/v1/messages")
    assert [name for name in sealed if name != "x-mirasim-client"] == \
        SEALED_MODEL_FIELDS
    assert sealed["x-mirasim-session"] == "0f20cf48-c292-42e9-a99e-994511307deb"
    assert sealed["x-mirasim-agent"] == "claude"
    assert sealed["x-mirasim-locale"] == "zh-HK"
    assert request.headers["anthropic-beta"] == (
        "claude-code-20250219,effort-2025-11-24")
    assert "x-api-key" not in request.headers
    assert "x-mirofish-proxy-key" not in request.headers
    assert "x-stainless-secret" not in request.headers
    assert all(secret.encode("ascii") not in request.content for secret in (
        "caller-secret", "caller-api-key", "local-proxy-key", "must-not-forward"))
    _verify_signature(state, request, "/v1/messages")


@respx.mock
async def test_generic_messages_get_the_captured_cli_identity(state):
    """A caller with no SDK identity is given the official one, in full.

    A partial fingerprint (Claude betas, no user-agent, no x-stainless-*)
    matches no shipped client and is therefore more distinctive than the real
    one, so the captured profile is synthesized rather than left incomplete.
    """
    add_account(state, "work")
    _mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [], "usage": {}}))

    await state.upstream.messages(
        "work", {"model": "gpt-compatible", "messages": [], "max_tokens": 8},
        session_id="mirofish-session")

    request = route.calls.last.request
    assert _header_names(request) == [
        *[name for name, _ in CLAUDE_HEADERS],
        "authorization",
        "x-mirasim-client",
        "x-mirasim-enc",
        "content-length",
        "Host",
        "Connection",
    ]
    for name, value in CLAUDE_HEADERS:
        if name in ("anthropic-beta", "x-claude-code-session-id"):
            continue
        assert request.headers[name] == value
    assert _machine_slot(request) == ("arm64", "MacOS")
    # The official client pairs these two exactly; keeping them equal preserves
    # that correlation without inventing a second session identifier.
    assert request.headers["x-claude-code-session-id"] == "mirofish-session"
    assert relay_metadata(request)["x-mirasim-session"] == "mirofish-session"
    # Only the routing beta is synthesized. context-1m, interleaved-thinking and
    # friends change request semantics and stay opt-in.
    assert request.headers["anthropic-beta"] == "claude-code-20250219"


@respx.mock
async def test_caller_sdk_fingerprint_cannot_survive_beside_the_cli_identity(state):
    """Overwrite, never default: no python/CPython residue in a js/node claim."""
    add_account(state, "work")
    _mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [], "usage": {}}))

    await state.upstream.messages(
        "work", {"model": "gpt-compatible", "messages": [], "max_tokens": 8},
        request_headers=httpx.Headers([
            ("user-agent", "OpenAI/Python 1.0"),
            ("x-stainless-lang", "python"),
            ("x-stainless-runtime", "CPython"),
            ("x-stainless-runtime-version", "3.11.9"),
            ("x-stainless-os", "Linux"),
            ("x-stainless-arch", "x64"),
            ("anthropic-beta", "effort-2025-11-24,oauth-2025-04-20"),
            ("anthropic-version", "2023-01-01"),
            ("accept", "text/event-stream"),
            ("content-type", "application/json; charset=utf-8"),
            ("accept-encoding", "identity"),
            ("anthropic-dangerous-direct-browser-access", "false"),
            ("x-app", "not-cli"),
        ]),
        session_id="mirofish-session")

    request = route.calls.last.request
    # Fingerprint slots are the captured constants even when the caller sent
    # its own; the relay decompresses upstream itself, so the caller's
    # accept-encoding has no bearing on what it receives back.
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-encoding"] == "gzip, deflate, br, zstd"
    assert request.headers["anthropic-dangerous-direct-browser-access"] == "true"
    assert request.headers["x-app"] == "cli"
    assert request.headers["user-agent"] == "claude-cli/2.1.252 (external, mirasim)"
    assert request.headers["x-stainless-lang"] == "js"
    assert request.headers["x-stainless-runtime"] == "node"
    assert request.headers["x-stainless-runtime-version"] == "v26.3.0"
    # A non-CLI caller receives the one captured coherent CLI profile.
    assert _machine_slot(request) == ("arm64", "MacOS")
    assert _machine_slot(request) != ("x64", "Linux")
    assert len(request.headers.get_list("x-stainless-lang")) == 1
    # Caller-owned protocol choices survive; the routing beta is added, the
    # retired OAuth beta is still dropped.
    assert request.headers["anthropic-version"] == "2023-01-01"
    assert request.headers["anthropic-beta"] == \
        "claude-code-20250219,effort-2025-11-24"


@respx.mock
async def test_openai_caller_reaches_upstream_as_the_official_client(
        client, state, auth_headers):
    """End-to-end: the entry point most callers use produces the capture."""
    add_account(state, "work")
    _mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}], "usage": {},
            "stop_reason": "end_turn", "model": "claude-fable-5"}))

    response = await client.post("/v1/chat/completions", headers={
        **auth_headers,
        "user-agent": "OpenAI/Python 1.30.0",
        "x-stainless-lang": "python",
        "x-stainless-runtime": "CPython",
    }, json={"model": "claude-fable-5", "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "hi"},
    ]})

    assert response.status_code == 200
    request = route.calls.last.request
    assert _header_names(request)[:len(CLAUDE_HEADERS)] == \
        [name for name, _ in CLAUDE_HEADERS]
    # Both session headers carry the same bare v4 UUID, as the capture does;
    # the relay no longer names itself in an upstream header.
    session = request.headers["x-claude-code-session-id"]
    assert uuid.UUID(session).version == 4
    assert relay_metadata(request)["x-mirasim-session"] == session


@respx.mock
async def test_model_probe_stays_lean_and_unfingerprinted(state):
    """The zero-metadata probe profile predates and outranks the CLI identity."""
    add_account(state, "work")
    _mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [], "usage": {}}))

    await state.upstream.messages(
        "work", {"model": "gpt-compatible", "messages": [], "max_tokens": 8},
        session_id="mirofish-session", probe=True)

    request = route.calls.last.request
    assert request.headers["accept-encoding"] == "identity"
    assert "user-agent" not in request.headers
    assert "x-claude-code-session-id" not in request.headers
    assert not any(name.lower().startswith(b"x-stainless-")
                   for name, _ in request.headers.raw)


async def _messages_request(state, alias: str, **kwargs) -> httpx.Request:
    add_account(state, alias)
    route = respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [], "usage": {}}))
    await state.upstream.messages(
        alias, {"model": "gpt-compatible", "messages": [], "max_tokens": 8},
        session_id="mirofish-session", **kwargs)
    return route.calls.last.request


@respx.mock
async def test_synthesized_cli_profile_is_installation_wide(state):
    """The desktop does not manufacture a different machine per account."""
    _mock_device_session()

    first = await _messages_request(state, "work")
    second = await _messages_request(state, "main")

    assert _machine_slot(first) == _machine_slot(second) == ("arm64", "MacOS")
    assert _stainless_block(first) == _stainless_block(second)
    assert first.headers["user-agent"] == second.headers["user-agent"]


@respx.mock
async def test_an_accounts_machine_is_the_same_on_every_request(state):
    """A box that changed between turns would be stranger than a shared one."""
    _mock_device_session()

    first = await _messages_request(state, "work")
    second = await _messages_request(state, "work")

    assert _machine_slot(first) == _machine_slot(second)


@respx.mock
async def test_installation_fingerprint_fields_stay_shared(state):
    _mock_device_session()

    first = await _messages_request(state, "work")
    second = await _messages_request(state, "main")

    for header in (*_MACHINE_SLOT, "x-mirasim-client",
                   "x-stainless-runtime-version", "x-stainless-package-version"):
        assert first.headers[header] == second.headers[header]
    first_fields, second_fields = relay_metadata(first), relay_metadata(second)
    assert first_fields["x-mirasim-locale"] == second_fields["x-mirasim-locale"]
    assert first_fields["x-mirasim-device"] == second_fields["x-mirasim-device"]
    assert first_fields["x-mirasim-locale"] == state.settings.mirasim_locale


@respx.mock
async def test_a_real_cli_caller_keeps_its_own_machine_headers(state):
    _mock_device_session()
    cli_headers = httpx.Headers(dict(CLAUDE_HEADERS) | {
        "x-stainless-arch": "x64",
        "x-stainless-os": "Linux",
    })

    request = await _messages_request(state, "work", request_headers=cli_headers)

    assert _machine_slot(request) == ("x64", "Linux")
    assert request.headers["x-stainless-runtime-version"] == "v26.3.0"
    assert request.headers["x-stainless-package-version"] == "0.112.1"
    assert request.headers["user-agent"] == "claude-cli/2.1.252 (external, mirasim)"


@respx.mock
async def test_runtime_requests_bridge_to_capture_derived_golden_profiles(state):
    """Exercise real runtime builders before comparing sanitized profiles.

    The standalone profile tests deliberately reconstruct representative
    requests. This bridge prevents those reconstructions and the actual httpx
    requests from drifting together without anyone noticing.

    The golden body pins the capture's own arch/os, which generic callers now
    receive as one coherent official-client profile.
    """
    alias = "capture"
    add_account(state, alias)
    state.settings.auth_base = OFFICIAL_AUTH_BASE
    state.settings.relay_base = OFFICIAL_RELAY_BASE
    auth_me = respx.get(OFFICIAL_AUTH_BASE + "/auth/me").mock(
        return_value=httpx.Response(200, json={"id": "user"}))
    limits = respx.get(OFFICIAL_RELAY_BASE + LIMITS_PATH).mock(
        return_value=httpx.Response(200, json={"windows": []}))
    device = respx.post(OFFICIAL_RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(
            200, json={"ticket": "device-ticket", "expiresIn": 900}))
    messages = respx.post(
        OFFICIAL_RELAY_BASE + "/v1/messages?beta=true").mock(
            return_value=httpx.Response(200, json={"content": [], "usage": {}}))

    await state.upstream.json(
        "GET", OFFICIAL_AUTH_BASE, "/auth/me", access="account-token")
    await state.upstream.json(
        "GET", OFFICIAL_RELAY_BASE, LIMITS_PATH, access="account-token")
    await state.upstream._mint_device_ticket(alias, "account-token")
    await state.upstream.signed_json(alias, "GET", LIMITS_PATH)

    session_id = "0f20cf48-c292-42e9-a99e-994511307deb"
    payload = json.loads(captured_messages_body())
    # Keep the captured three-system-block shape while making the official
    # marker idempotent, so the compatibility layer leaves this body untouched.
    # Index 1 is where the capture carries it, behind the billing-header block;
    # the golden breakpoint layout is asserted against that position.
    payload["system"][1]["text"] = CLAUDE_AGENT_SYSTEM_MARKER
    await state.upstream.messages(
        alias,
        payload,
        request_headers=httpx.Headers(captured_messages_headers(session_id)),
        session_id=session_id,
        beta=True,
    )

    _assert_matches_golden(auth_me.calls.last.request, "auth_me_official.json")
    _assert_matches_golden(
        limits.calls[0].request, "limits_initial_official.json")
    _assert_matches_golden(
        device.calls[0].request, "device_session_official.json")
    _assert_matches_golden(
        limits.calls[1].request, "limits_signed_official.json")
    _assert_matches_golden(
        messages.calls.last.request, "messages_beta_official.json")


#: Cloudflare cookies as relay.mirasim.ai served them in the 0.0.272 capture.
#: ``__cf_bm`` arrives scoped to chatgpt.com (the upstream relays ChatGPT's
#: own Set-Cookie), which a conforming cookie store must not replay here.
CLOUDFLARE_SET_COOKIES = [
    ("set-cookie", "__oailb=lb-secret; Path=/; Max-Age=3600; HttpOnly; Secure; SameSite=Lax"),
    ("set-cookie", "__cf_bm=bm-secret; HttpOnly; SameSite=None; Secure; Path=/; Domain=chatgpt.com"),
    ("set-cookie", "__cflb=cflb-secret; HttpOnly; SameSite=None; Secure; Path=/"),
]


@respx.mock
async def test_codex_relay_request_matches_the_official_capture(state):
    """A Codex CLI's request leaves the relay as the desktop's bundled Codex
    would have sent it: product user-agent and originator, the CLI's own
    protocol headers in their order, the route's Cloudflare cookies ahead of
    the credential, no ``openai-beta`` / ``accept-encoding``."""
    alias = "capture"
    add_account(state, alias)
    state.settings.relay_base = OFFICIAL_RELAY_BASE
    respx.post(OFFICIAL_RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(
            200, json={"ticket": "device-ticket", "expiresIn": 900}))
    route = respx.post(OFFICIAL_RELAY_BASE + "/v1/responses").mock(side_effect=[
        httpx.Response(200, json={"id": "first"}, headers=CLOUDFLARE_SET_COOKIES),
        httpx.Response(200, json={"id": "second"}),
    ])
    body = codex_body()
    caller = httpx.Headers([
        *codex_caller_headers(),
        # A stand-alone CLI adds these; the bundled Codex does not.
        ("openai-beta", "responses=experimental"),
        ("accept-encoding", "gzip"),
        ("cookie", "caller-jar=must-not-forward"),
    ])

    for _ in range(2):
        response = await state.upstream.stream_responses(
            alias, body, request_headers=caller,
            session_id="0f20cf48-c292-42e9-a99e-994511307deb",
            account_id="u-capture")
        await response.aclose()

    first, second = (call.request for call in route.calls)
    # The first request of a fresh install carries no cookie yet; everything
    # else already matches the capture.
    assert "cookie" not in first.headers
    _assert_matches_golden(second, "codex_responses_official.json")
    assert second.headers["user-agent"] == DEFAULT_CODEX_USER_AGENT
    assert second.headers["originator"] == "mirasim"
    assert second.headers["authorization"] == "Bearer device-ticket"
    for name in ("openai-beta", "accept-encoding"):
        assert name not in second.headers
    cookie = second.headers["cookie"]
    assert "__cflb=cflb-secret" in cookie
    assert "__oailb=lb-secret" in cookie
    assert "__cf_bm" not in cookie  # scoped to chatgpt.com, not this host
    assert "caller-jar" not in cookie
    assert "codex-caller-secret" not in second.headers.values()
    assert second.content == body
    _verify_signature(state, second, "/v1/responses")
