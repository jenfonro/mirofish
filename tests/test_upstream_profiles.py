"""Capture-derived HTTPX request profiles for upstream endpoints.

The assertions run before h11 serialization. h11 moves Host to the first wire
field per RFC 7230; the tests intentionally cover profile construction and
default-header isolation, not a byte-identical socket/TLS fingerprint.
"""

import base64
import hashlib
import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization

from mirofish.upstream import (CLAUDE_AGENT_SYSTEM_MARKER,
                               _CLI_MACHINE_PROFILES, _machine_profile,
                               LIMITS_PATH)
from tests.conftest import AUTH_BASE, RELAY_BASE, add_account
from tests.test_request_profile import _body as captured_messages_body
from tests.test_request_profile import _headers as captured_messages_headers
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
    signed = "\n".join((
        "mrs-sig-v1",
        request.method,
        path,
        request.headers["x-mirasim-ts"],
        request.headers["x-mirasim-nonce"],
        hashlib.sha256(request.content).hexdigest(),
    )).encode("utf-8")
    encoded = request.headers["x-mirasim-sig"]
    signature = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    public = serialization.load_der_public_key(
        base64.b64decode(state.upstream._signer("work").public_key))
    public.verify(signature, signed)


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


CLAUDE_HEADERS = [
    ("accept", "application/json"),
    ("content-type", "application/json"),
    ("user-agent", "claude-cli/2.1.241 (external, mirasim)"),
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
        "x-mirasim-session",
        "x-mirasim-agent",
        "x-mirasim-device",
        "x-mirasim-client",
        "x-mirasim-locale",
        "x-mirasim-call",
        "x-mirasim-ts",
        "x-mirasim-nonce",
        "x-mirasim-sig",
        "content-length",
        "Host",
        "Connection",
    ]
    assert request.content == json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert request.headers["authorization"] == "Bearer device-ticket"
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
        "x-mirasim-session",
        "x-mirasim-agent",
        "x-mirasim-device",
        "x-mirasim-client",
        "x-mirasim-locale",
        "x-mirasim-call",
        "x-mirasim-ts",
        "x-mirasim-nonce",
        "x-mirasim-sig",
        "content-length",
        "Host",
        "Connection",
    ]
    for name, value in CLAUDE_HEADERS:
        if name in ("anthropic-beta", "x-claude-code-session-id", *_MACHINE_SLOT):
            continue
        assert request.headers[name] == value
    # Every slot but the machine one is the captured constant; that one is the
    # account's, so a set of accounts is not one workstation.
    assert _machine_slot(request) == _machine_profile("work")
    # The official client pairs these two exactly; keeping them equal preserves
    # that correlation without inventing a second session identifier.
    assert request.headers["x-claude-code-session-id"] == "mirofish-session"
    assert request.headers["x-mirasim-session"] == "mirofish-session"
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
    assert request.headers["user-agent"] == "claude-cli/2.1.241 (external, mirasim)"
    assert request.headers["x-stainless-lang"] == "js"
    assert request.headers["x-stainless-runtime"] == "node"
    assert request.headers["x-stainless-runtime-version"] == "v26.3.0"
    # The machine slot resolves to the account's platform, not to the Linux/x64
    # box the caller claimed, so a caller cannot pick its accounts' machines.
    assert _machine_slot(request) == _machine_profile("work")
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
    assert request.headers["x-mirasim-session"] == session


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
async def test_each_account_reaches_upstream_from_its_own_machine(state):
    """The fingerprint's whole purpose fails if a fleet reads as one desktop."""
    _mock_device_session()

    first = await _messages_request(state, "work")
    second = await _messages_request(state, "main")

    assert _machine_slot(first) != _machine_slot(second)
    # ...while every slot that describes the client build rather than the box
    # it runs on stays identical, so neither account looks bespoke.
    assert {name: value for name, value in _stainless_block(first).items()
            if name not in _MACHINE_SLOT} == \
        {name: value for name, value in _stainless_block(second).items()
         if name not in _MACHINE_SLOT}
    assert first.headers["user-agent"] == second.headers["user-agent"]


@respx.mock
async def test_an_accounts_machine_is_the_same_on_every_request(state):
    """A box that changed between turns would be stranger than a shared one."""
    _mock_device_session()

    first = await _messages_request(state, "work")
    second = await _messages_request(state, "work")

    assert _machine_slot(first) == _machine_slot(second)


@respx.mock
async def test_cross_checkable_fingerprint_fields_stay_shared_on_purpose(state):
    """The complement of the machine axis: locale and the client/runtime
    versions are held identical across accounts deliberately.

    Each is verifiable by upstream against an external fact — locale against the
    exit IP's geography, the versions against the set of builds that ever
    shipped — so a per-account value would forge a mismatch that is a sharper
    tell than the shared value.  This guards that boundary: a future change that
    "diversifies" one of these to match the arch/os treatment must justify
    itself against this test rather than pass silently.
    """
    _mock_device_session()

    first = await _messages_request(state, "work")
    second = await _messages_request(state, "main")

    # Precondition: these two do land on different machines, so any shared value
    # below is a real choice and not an artifact of identical inputs.
    assert _machine_slot(first) != _machine_slot(second)
    for header in ("x-mirasim-locale", "x-mirasim-client",
                   "x-stainless-runtime-version", "x-stainless-package-version"):
        assert first.headers[header] == second.headers[header]
    assert first.headers["x-mirasim-locale"] == state.settings.mirasim_locale


@respx.mock
async def test_a_real_cli_caller_reports_its_accounts_machine_not_its_own(state):
    """One CLI in front of N accounts must not stamp all N with one desktop."""
    _mock_device_session()
    cli_headers = httpx.Headers(dict(CLAUDE_HEADERS))

    request = await _messages_request(state, "work", request_headers=cli_headers)

    assert _machine_slot(request) == _machine_profile("work")
    assert _machine_slot(request) != ("arm64", "MacOS")
    # Only the machine slot is rewritten; what the CLI reports about itself is
    # real and stays its own.
    assert request.headers["x-stainless-runtime-version"] == "v26.3.0"
    assert request.headers["x-stainless-package-version"] == "0.112.1"
    assert request.headers["user-agent"] == "claude-cli/2.1.241 (external, mirasim)"


@pytest.mark.parametrize("alias", [
    "", "work", "personal", "team", "a", "b", "c", "d", "e",
    "acct-1", "acct-2", "acct-3", "acct-4", "acct-5", "acct-6",
    "用户", "a" * 200,
])
def test_every_machine_identity_is_a_platform_node_actually_ships(alias):
    assert _machine_profile(alias) in _CLI_MACHINE_PROFILES


def test_the_capture_is_reachable_and_the_table_spreads_accounts():
    aliases = [f"account-{n}" for n in range(60)]
    drawn = {_machine_profile(alias) for alias in aliases}

    # Nothing here is random, so this is a fact about the table, not a flake.
    assert drawn == set(_CLI_MACHINE_PROFILES)
    assert ("arm64", "MacOS") in _CLI_MACHINE_PROFILES


def test_no_machine_profile_describes_a_box_that_was_never_built():
    """arch and os are drawn as a unit precisely to keep this true."""
    assert len(set(_CLI_MACHINE_PROFILES)) == len(_CLI_MACHINE_PROFILES)
    for arch, os_name in _CLI_MACHINE_PROFILES:
        assert arch in ("arm64", "x64")
        assert os_name in ("MacOS", "Linux", "Windows")
        # Windows on ARM exists but is rare enough to be a fingerprint in
        # itself, which is the opposite of what this table is for.
        assert (arch, os_name) != ("arm64", "Windows")


@respx.mock
async def test_runtime_requests_bridge_to_capture_derived_golden_profiles(state):
    """Exercise real runtime builders before comparing sanitized profiles.

    The standalone profile tests deliberately reconstruct representative
    requests. This bridge prevents those reconstructions and the actual httpx
    requests from drifting together without anyone noticing.

    The golden body pins the capture's own arch/os, which the relay now treats
    as an account property, so this runs on an alias that draws that pair. The
    assertion below is what keeps that choice honest if the table changes.
    """
    alias = "capture"
    assert _machine_profile(alias) == ("arm64", "MacOS")
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
