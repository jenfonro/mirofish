import base64
import gzip
import json
import uuid
from pathlib import Path

import pytest

from tools.request_profile import (_SAFE_HEADER_VALUE_NAMES, UnsafeProfile,
                                   compare_profiles, request_profile,
                                   validate_profile)


FIXTURE_DIR = Path(__file__).parent / "fixtures/request_profiles"
FIXTURE = FIXTURE_DIR / "messages_beta_official.json"

OFFICIAL_FIXTURE_NAMES = {
    "auth_me_official.json",
    "auth_providers_official.json",
    "auth_referral_official.json",
    "desktop_update_official.json",
    "device_session_official.json",
    "events_official.json",
    "limits_initial_official.json",
    "limits_signed_official.json",
    "messages_beta_official.json",
    "models_official.json",
    "codex_responses_official.json",
}
#: The 0.0.272 capture's Codex identity: the desktop's bundled Codex names the
#: product, not ``codex_cli_rs``, and carries one conversation id in three
#: header slots.
CODEX_USER_AGENT = (
    "mirasim/0.150.1 (Mac OS 26.6.2; x86_64) Apple_Terminal/470.2 (mirasim; 0.1.0)")
CODEX_CONVERSATION = "c10482cc-6726-48fc-a4e8-965da883d620"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _body(secret: str = "body text must disappear") -> bytes:
    def blocks(*types):
        return [{"type": kind, "text": secret} for kind in types]

    def cached(block):
        """Mark a block the way the capture does: three ephemeral breakpoints
        on the marker system block, the last system block and the last user
        turn, with none on the 29 tools."""
        return {**block, "cache_control": {"type": "ephemeral"}}

    messages = [
        {"role": "user", "content": blocks("text", "text", "text")},
        {"role": "system", "content": secret},
        {"role": "assistant", "content": blocks("text")},
        {"role": "user", "content": blocks("text", "text")},
        {"role": "system", "content": secret},
        {"role": "assistant", "content": blocks("text")},
        {"role": "user", "content": blocks("text", "text")},
        {"role": "system", "content": secret},
        {"role": "assistant", "content": blocks("text")},
        {"role": "user", "content": blocks("text")},
        {"role": "system", "content": secret},
        {"role": "assistant", "content": blocks("thinking", "text")},
        {"role": "user", "content": blocks("text")},
        {"role": "system", "content": secret},
        {"role": "assistant", "content": blocks("text")},
        {"role": "user", "content": blocks("text")},
        {"role": "system", "content": secret},
        {"role": "assistant", "content": blocks("text")},
        {"role": "user", "content": [cached(*blocks("text"))]},
        {"role": "system", "content": secret},
    ]
    system = blocks("text", "text", "text")
    return json.dumps({
        "model": "claude-sonnet-5",
        "messages": messages,
        "system": [system[0], cached(system[1]), cached(system[2])],
        "tools": [{"name": secret} for _ in range(29)],
        "metadata": {"user_id": secret},
        "max_tokens": 64000,
        "thinking": {"type": "adaptive"},
        "context_management": {"edits": []},
        "output_config": {"effort": "high"},
        "stream": True,
    }, separators=(",", ":")).encode()


def _headers(session: str, authorization: str = "Bearer ticket-secret"):
    return [
        ("accept", "application/json"),
        ("content-type", "application/json"),
        ("user-agent", "claude-cli/2.1.252 (external, mirasim)"),
        ("x-claude-code-session-id", session),
        ("x-stainless-arch", "arm64"),
        ("x-stainless-lang", "js"),
        ("x-stainless-os", "MacOS"),
        ("x-stainless-package-version", "0.112.1"),
        ("x-stainless-retry-count", "0"),
        ("x-stainless-runtime", "node"),
        ("x-stainless-runtime-version", "v26.3.0"),
        ("x-stainless-timeout", "600"),
        ("anthropic-beta", "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,context-management-2025-06-27,prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,advisor-tool-2026-03-01,effort-2025-11-24"),
        ("anthropic-dangerous-direct-browser-access", "true"),
        ("anthropic-version", "2023-06-01"),
        ("x-app", "cli"),
        ("accept-encoding", "gzip, deflate, br, zstd"),
        ("authorization", authorization),
        ("x-mirasim-client", "0.0.272"),
        # ephemeral X25519 key + nonce + ciphertext/tag of a small envelope.
        ("x-mirasim-enc", _b64url(b"e" * 32 + b"n" * 12 + b"c" * 180)),
        ("content-length", str(len(_body()))),
        ("Host", "relay.mirasim.ai"),
        ("Connection", "keep-alive"),
    ]


def codex_body(secret: str = "codex prompt text must disappear") -> bytes:
    """A Responses body with the captured turn shape: 12 top-level keys, a
    leading ``additional_tools`` developer item, and 14 typed messages."""
    def message(role, kind, count=1):
        return {"type": "message", "role": role,
                "content": [{"type": kind, "text": secret} for _ in range(count)]}

    turns = [
        {"type": "additional_tools", "role": "developer", "tools": [secret]},
        message("developer", "input_text"),
        message("user", "input_text"),
        message("user", "input_text"),
        message("assistant", "output_text"),
        message("user", "input_text"),
        message("user", "input_text"),
        message("user", "input_text"),
        message("user", "input_text"),
        message("assistant", "output_text"),
        message("user", "input_text"),
        message("user", "input_text"),
        message("developer", "input_text", 4),
        message("user", "input_text", 2),
        message("user", "input_text"),
    ]
    return json.dumps({
        "model": "gpt-5.6-sol",
        "input": turns,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "xhigh", "context": "all_turns"},
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "service_tier": "priority",
        "prompt_cache_key": CODEX_CONVERSATION,
        "text": {"verbosity": "low"},
        "client_metadata": {"cwd": secret},
    }, separators=(",", ":")).encode()


def codex_caller_headers(
        conversation: str = CODEX_CONVERSATION,
        authorization: str = "Bearer codex-caller-secret",
        user_agent: str = "codex_cli_rs/0.150.1 (Mac OS 26.6.2; arm64) Apple_Terminal",
        originator: str = "codex_cli_rs") -> list[tuple[str, str]]:
    """What a Codex CLI hands the relay, in the order the binary emits it."""
    return [
        ("x-codex-beta-features", "remote_compaction_v2"),
        ("x-codex-window-id", conversation + ":0"),
        ("x-codex-turn-metadata", json.dumps({
            "installation_id": "install-secret", "session_id": conversation})),
        ("x-openai-internal-codex-responses-lite", "true"),
        ("x-codex-routing-hint", "model=gpt-5.6-sol;tier=priority"),
        ("x-client-request-id", conversation),
        ("session-id", conversation),
        ("thread-id", conversation),
        ("accept", "text/event-stream"),
        ("content-type", "application/json"),
        ("chatgpt-account-id", "0b0b0b0b-1c1c-4d2d-8e3e-4f4f4f4f4f4f"),
        ("originator", originator),
        ("user-agent", user_agent),
        ("authorization", authorization),
    ]


def _codex_wire_headers(body: bytes) -> list[tuple[str, str]]:
    """The same request as the relay puts it on the wire."""
    caller = codex_caller_headers(
        authorization="Bearer synthetic-device-ticket",
        user_agent=CODEX_USER_AGENT, originator="mirasim")
    return [
        *caller[:-1],
        ("cookie", "__cflb=synthetic-lb; _cfuvid=synthetic-uvid; __cf_bm=synthetic-bm"),
        caller[-1],
        ("x-mirasim-client", "0.0.272"),
        ("x-mirasim-enc", _b64url(b"e" * 32 + b"n" * 12 + b"c" * 300)),
        ("content-length", str(len(body))),
        ("Host", "relay.mirasim.ai"),
        ("Connection", "keep-alive"),
    ]


def _signed_identity_headers():
    return [
        ("x-mirasim-device", _b64url(b"d" * 16)),
        ("x-mirasim-ts", "1787560634123"),
        ("x-mirasim-nonce", _b64url(b"n" * 12)),
        ("x-mirasim-sig", _b64url(b"s" * 64)),
    ]


def _representative_profile(name: str):
    """Recreate the non-Messages profiles from secret-bearing raw inputs."""
    if name == "desktop_update_official.json":
        return request_profile(
            "GET", "https://cdn-assets.mirasim.ai/mirasim/releases/latest.json",
            "HTTP/1.1", [
                ("Accept", "application/json"),
                ("User-Agent", "mirasim-desktop/0.0.272"),
                ("accept-encoding", "identity"),
                ("Host", "cdn-assets.mirasim.ai"),
                ("Connection", "keep-alive"),
            ])
    if name == "limits_initial_official.json":
        return request_profile(
            "GET", "https://relay.mirasim.ai/v1/limits", "HTTP/1.1", [
                ("x-mirasim-probe", "usage"),
                ("Authorization", "Bearer synthetic-access-token"),
                ("x-mirasim-client", "0.0.272"),
                ("accept-encoding", "identity"),
                ("Host", "relay.mirasim.ai"),
                ("Connection", "keep-alive"),
            ])
    if name == "device_session_official.json":
        body = json.dumps({
            "publicKey": "synthetic-public-key",
            "deviceId": "synthetic-device-id",
        }, separators=(",", ":")).encode()
        return request_profile(
            "POST", "https://relay.mirasim.ai/v1/device/session", "HTTP/1.1", [
                ("content-type", "application/json"),
                ("authorization", "Bearer synthetic-access-token"),
                *_signed_identity_headers(),
                ("x-mirasim-client", "0.0.272"),
                ("accept-encoding", "identity"),
                ("content-length", str(len(body))),
                ("Host", "relay.mirasim.ai"),
                ("Connection", "keep-alive"),
            ], body)
    if name == "auth_referral_official.json":
        return request_profile(
            "GET", "https://auth.mirasim.ai/auth/referral", "HTTP/1.1", [
                ("authorization", "Bearer synthetic-access-token"),
                ("accept-encoding", "identity"),
                ("Host", "auth.mirasim.ai"),
                ("Connection", "keep-alive"),
            ])
    if name == "auth_me_official.json":
        return request_profile(
            "GET", "https://auth.mirasim.ai/auth/me", "HTTP/1.1", [
                ("Authorization", "Bearer synthetic-access-token"),
                ("accept-encoding", "identity"),
                ("Host", "auth.mirasim.ai"),
                ("Connection", "keep-alive"),
            ])
    if name == "auth_providers_official.json":
        return request_profile(
            "GET", "https://auth.mirasim.ai/auth/oauth/providers", "HTTP/1.1", [
                ("accept-encoding", "identity"),
                ("Host", "auth.mirasim.ai"),
                ("Connection", "keep-alive"),
            ])
    if name == "events_official.json":
        body = gzip.compress(json.dumps({
            "events": [{"content": "synthetic-private-event"}],
        }).encode())
        return request_profile(
            "POST", "https://relay.mirasim.ai/events", "HTTP/1.1", [
                ("content-type", "application/json"),
                ("content-encoding", "gzip"),
                ("authorization", "Bearer synthetic-access-token"),
                ("accept-encoding", "identity"),
                ("content-length", str(len(body))),
                ("Host", "relay.mirasim.ai"),
                ("Connection", "keep-alive"),
            ], body)
    if name == "models_official.json":
        return request_profile(
            "GET", "https://relay.mirasim.ai/v1/models", "HTTP/1.1", [
                ("authorization", "Bearer synthetic-device-ticket"),
                *_signed_identity_headers(),
                ("x-mirasim-client", "0.0.272"),
                ("accept-encoding", "identity"),
                ("Host", "relay.mirasim.ai"),
                ("Connection", "keep-alive"),
            ])
    if name == "codex_responses_official.json":
        body = codex_body()
        return request_profile(
            "POST", "https://relay.mirasim.ai/v1/responses", "HTTP/1.1",
            _codex_wire_headers(body), body)
    if name == "limits_signed_official.json":
        return request_profile(
            "GET", "https://relay.mirasim.ai/v1/limits", "HTTP/1.1", [
                ("x-mirasim-probe", "usage"),
                ("Authorization", "Bearer synthetic-device-ticket"),
                *_signed_identity_headers(),
                ("x-mirasim-client", "0.0.272"),
                ("accept-encoding", "identity"),
                ("Host", "relay.mirasim.ai"),
                ("Connection", "keep-alive"),
            ])
    raise AssertionError(f"no representative request for {name}")


def test_all_observed_official_profiles_are_sanitized_and_valid():
    fixture_paths = sorted(FIXTURE_DIR.glob("*.json"))

    assert {path.name for path in fixture_paths} == OFFICIAL_FIXTURE_NAMES
    for path in fixture_paths:
        profile = json.loads(path.read_text())
        validate_profile(profile)


@pytest.mark.parametrize(
    "name", sorted(OFFICIAL_FIXTURE_NAMES - {"messages_beta_official.json"}))
def test_sanitized_golden_profiles_match_representative_request_shapes(name):
    expected = json.loads((FIXTURE_DIR / name).read_text())
    actual = _representative_profile(name)

    assert compare_profiles(expected, actual) == []
    encoded = json.dumps(actual)
    assert "synthetic-access-token" not in encoded
    assert "synthetic-device-ticket" not in encoded
    assert "synthetic-private-event" not in encoded
    assert "synthetic-public-key" not in encoded
    assert "synthetic-device-id" not in encoded
    assert "synthetic-lb" not in encoded
    assert "install-secret" not in encoded
    assert "codex prompt text" not in encoded
    assert "0b0b0b0b" not in encoded
    assert CODEX_CONVERSATION not in encoded


def test_sanitized_golden_profile_matches_same_request_shape():
    expected = json.loads(FIXTURE.read_text())
    validate_profile(expected)
    session = str(uuid.uuid4())
    actual = request_profile(
        "POST", "https://relay.mirasim.ai/v1/messages?beta=true", "HTTP/1.1",
        _headers(session), _body())

    assert compare_profiles(expected, actual) == []


def test_profile_never_emits_credentials_account_ids_or_body_text():
    session = str(uuid.uuid4())
    body_secret = "private prompt and account@example.test"
    ticket_secret = "Bearer super-secret-ticket"
    profile = request_profile(
        "POST", "https://relay.mirasim.ai/v1/messages?beta=true", "HTTP/1.1",
        [*_headers(session, ticket_secret), ("X-Mirofish-Account", "private-alias")],
        _body(body_secret))
    encoded = json.dumps(profile)

    assert ticket_secret not in encoded
    assert body_secret not in encoded
    assert "private-alias" not in encoded
    assert "account@example.test" not in encoded
    assert "<redacted:authorization>" in encoded
    assert "<redacted:account>" in encoded


@pytest.mark.parametrize("lower_name", sorted(_SAFE_HEADER_VALUE_NAMES))
def test_literal_header_allowlist_redacts_unrecognized_values(lower_name):
    name = {"host": "Host", "user-agent": "User-Agent"}.get(
        lower_name, lower_name)
    secret = "account-token-secret"
    profile = request_profile(
        "GET", "https://relay.mirasim.ai/v1/limits", "HTTP/1.1",
        [(name, secret)])

    assert profile["headers"] == [{"name": name, "value": "<redacted:opaque>"}]
    assert secret not in json.dumps(profile)

    profile["headers"][0]["value"] = secret
    with pytest.raises(UnsafeProfile):
        validate_profile(profile)


def test_url_authority_userinfo_never_enters_profile():
    profile = request_profile(
        "GET",
        "https://url-user:url-password@relay.mirasim.ai/v1/limits",
        "HTTP/1.1", [("Host", "relay.mirasim.ai")])
    encoded = json.dumps(profile)

    assert "url-user" not in encoded
    assert "url-password" not in encoded
    assert profile["path"] == "/v1/limits"
    assert profile["headers"][0]["value"] == "relay.mirasim.ai"


@pytest.mark.parametrize(("name", "value"), [
    ("x-mirasim-ts", "not-ms"),
    ("x-mirasim-nonce", _b64url(b"too-short")),
    ("x-mirasim-sig", _b64url(b"too-short")),
    ("x-mirasim-call", str(uuid.uuid1())),
    ("x-mirasim-device", "not-a-device"),
    ("x-codex-window-id", "not-a-window"),
    ("x-codex-window-id", str(uuid.uuid4())),
])
def test_dynamic_identity_fields_are_typed(name, value):
    with pytest.raises(UnsafeProfile):
        request_profile("GET", "https://relay.mirasim.ai/v1/limits", "HTTP/1.1",
                        [(name, value)])


def test_session_relationship_and_header_order_are_part_of_profile():
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    same = request_profile("GET", "https://example.test", "HTTP/1.1", [
        ("x-claude-code-session-id", first), ("x-mirasim-session", first)])
    different = request_profile("GET", "https://example.test", "HTTP/1.1", [
        ("x-claude-code-session-id", first), ("x-mirasim-session", second)])
    reordered = {**same, "headers": list(reversed(same["headers"]))}

    assert same["headers"][0]["value"] == same["headers"][1]["value"]
    assert different["headers"][0]["value"] != different["headers"][1]["value"]
    assert compare_profiles(same, reordered)


def test_validator_rejects_unredacted_sensitive_header():
    profile = request_profile("GET", "https://example.test", "HTTP/1.1", [])
    profile["headers"] = [{"name": "Authorization", "value": "Bearer leaked"}]

    with pytest.raises(UnsafeProfile):
        validate_profile(profile)


def test_validator_rejects_body_values_and_query_credentials_are_redacted():
    profile = request_profile(
        "GET", "https://example.test/x?beta=true&access_token=query-secret",
        "HTTP/1.1", [])
    assert profile["path"] == "/<redacted:path>"
    assert profile["query"] == "beta=true&<redacted:query-field>"

    profile["body"] = {
        "kind": "json", "keys": ["messages"], "types": ["array"],
        "messages": [{"role": "user", "content_type": "string",
                      "text": "leaked body text"}],
    }
    with pytest.raises(UnsafeProfile):
        validate_profile(profile)


@pytest.mark.parametrize(("name", "value"), [
    ("Authorization", "<redacted:Bearer still-secret>"),
    ("x-mirasim-sig", "<dynamic:actual-signature-secret>"),
    ("X-Session-Token", "token-secret"),
])
def test_validator_rejects_fake_markers_and_unknown_header_values(name, value):
    profile = request_profile("GET", "https://example.test", "HTTP/1.1", [])
    profile["headers"] = [{"name": name, "value": value}]

    with pytest.raises(UnsafeProfile):
        validate_profile(profile)


def test_unknown_headers_and_body_keys_are_opaque():
    secret = "adversarial-secret"
    profile = request_profile(
        "POST", "https://example.test/x", "HTTP/1.1",
        [("X-Future-Header", secret)],
        json.dumps({secret: "also-secret", "messages": []}).encode())
    encoded = json.dumps(profile)

    assert secret not in encoded
    assert profile["headers"][0] == {
        "name": "<redacted:header-name>", "value": "<redacted:opaque>"}
    assert profile["body"]["keys"] == ["<other>", "messages"]


@pytest.mark.parametrize(("method", "http_version"), [
    ("PRIVATE_ACCOUNT_TOKEN", "HTTP/1.1"),
    ("GET", "HTTP/private-account-token"),
])
def test_method_and_http_version_fail_closed(method, http_version):
    with pytest.raises(UnsafeProfile):
        request_profile(
            method, "https://relay.mirasim.ai/v1/limits", http_version, [])


def test_validator_rejects_raw_unknown_header_name():
    profile = request_profile(
        "GET", "https://relay.mirasim.ai/v1/limits", "HTTP/1.1", [])
    profile["headers"] = [{
        "name": "X-Account-private@example.test", "value": "<redacted:opaque>"}]

    with pytest.raises(UnsafeProfile):
        validate_profile(profile)


@pytest.mark.parametrize("query", [
    "access_token=still-secret",
    "beta=still-secret",
    "Beta=true",
    "beta=%74rue",
])
def test_validator_rejects_unredacted_query_loaded_from_disk(query):
    profile = request_profile("GET", "https://example.test", "HTTP/1.1", [])
    profile["query"] = query

    with pytest.raises(UnsafeProfile):
        validate_profile(profile)


@pytest.mark.parametrize("query", [
    "beta=private-ticket",
    "Beta=true",
    "beta=%74rue",
    "beta",
])
def test_only_exact_beta_true_query_is_preserved(query):
    profile = request_profile(
        "GET", f"https://relay.mirasim.ai/v1/messages?{query}", "HTTP/1.1", [])

    assert profile["query"] == "<redacted:query-field>"
    assert "private-ticket" not in json.dumps(profile)


def test_unknown_path_is_redacted_and_validator_rejects_raw_path():
    secret = "private-account@example.test"
    profile = request_profile(
        "GET", f"https://auth.mirasim.ai/auth/users/{secret}", "HTTP/1.1", [])

    assert profile["path"] == "/<redacted:path>"
    assert secret not in json.dumps(profile)

    profile["path"] = f"/auth/users/{secret}"
    with pytest.raises(UnsafeProfile):
        validate_profile(profile)
