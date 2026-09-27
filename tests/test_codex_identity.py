"""A caller's Codex names its installation, thread and turns on every request
(live 0.0.367 capture of Codex 0.155.1); taken to two accounts they would
announce the same ids from both.  The relay substitutes this account's own,
keeping Codex's shapes: v7 thread/turn/context ids keep the caller's time
bits, the v4 installation id is the account's."""

import json
import uuid

import httpx
import respx

from mirofish.upstream import codex_identity_map
from tests.conftest import RELAY_BASE, add_account
from tests.test_api import mock_device_session

THREAD = "01a0dfa3-c57d-7f11-ad0e-6525842280a9"
TURN = "01a0dfa3-c616-7b10-831b-bc3742cb8ce8"
CONTEXT = "01a0dfa3-c57d-7f11-ad0e-dccd5a7950fc"
INSTALL = "bf4ed9d4-cb55-40e8-9f53-3c9334591ed1"


def _turn_metadata():
    return json.dumps({
        "installation_id": INSTALL, "session_id": THREAD, "thread_id": THREAD,
        "agent_name": "/root", "turn_id": TURN, "window_id": THREAD + ":0",
        "window_number": 0, "context_window_id": CONTEXT, "request_kind": "turn",
        "root_turn_id": TURN, "sandbox": "seccomp", "sandbox_mode": "read-only",
        "turn_started_at_unix_ms": 1790458823212})


def _caller_headers(auth):
    return {
        **auth,
        "x-codex-beta-features": "remote_compaction_v2",
        "x-codex-window-id": THREAD + ":0",
        "x-codex-turn-metadata": _turn_metadata(),
        "x-openai-internal-codex-responses-lite": "true",
        "x-client-request-id": THREAD, "session-id": THREAD, "thread-id": THREAD,
        "accept": "text/event-stream", "content-type": "application/json",
        "authorization": "Bearer codex-caller-secret",
        "originator": "codex_cli_rs",
        "user-agent": "codex_cli_rs/0.155.1 (Debian 12.0.0; aarch64) unknown",
    }


def _caller_body():
    return json.dumps({
        "model": "gpt-5.6-luna", "input": [{"role": "user", "content": "hi"}],
        "stream": True, "prompt_cache_key": THREAD,
        "client_metadata": {"x-codex-window-id": THREAD + ":0", "turn_id": TURN,
                            "root_turn_id": TURN, "x-codex-turn-metadata": _turn_metadata()},
    }, separators=(",", ":")).encode()


def test_ids_keep_codexs_shapes_and_differ_per_account():
    headers = [(name, value) for name, value in _caller_headers({}).items()]
    a = codex_identity_map(headers, _caller_body(), "a", "DEVICEID22charsaaaaaaa")
    b = codex_identity_map(headers, _caller_body(), "b", "DEVICEID22charsbbbbbbb")
    assert set(a) == {THREAD, TURN, CONTEXT, INSTALL}
    for original, replacement in a.items():
        assert replacement != original and a[original] != b[original]
        assert uuid.UUID(replacement).version == uuid.UUID(original).version
        if uuid.UUID(original).version == 7:
            assert uuid.UUID(replacement).bytes[:6] == uuid.UUID(original).bytes[:6]
    # The installation id is the account's own, whatever the caller's was.
    other = codex_identity_map(
        [("x-codex-turn-metadata", json.dumps({"installation_id": str(uuid.uuid4())}))],
        b"{}", "a", "DEVICEID22charsaaaaaaa")
    assert list(other.values()) == [a[INSTALL]]
    # Deterministic: the next turn of the same thread maps the thread the same way.
    assert codex_identity_map(headers, _caller_body(), "a", "DEVICEID22charsaaaaaaa")[THREAD] == a[THREAD]


@respx.mock
async def test_the_wire_carries_only_this_accounts_ids(client, state, auth_headers):
    add_account(state, "work")
    mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/responses").mock(
        return_value=httpx.Response(200, content=b"event: done\n\n",
                                    headers={"content-type": "text/event-stream"}))
    response = await client.post("/v1/responses", content=_caller_body(),
                                 headers=_caller_headers(auth_headers))
    assert response.status_code == 200
    sent = route.calls.last.request
    wire = sent.content.decode() + "\n".join(f"{k}: {v}" for k, v in sent.headers.items())
    for original in (THREAD, TURN, CONTEXT, INSTALL):
        assert original not in wire
    body = json.loads(sent.content)
    meta = json.loads(sent.headers["x-codex-turn-metadata"])
    assert meta["thread_id"] == meta["session_id"] == sent.headers["thread-id"] \
        == sent.headers["session-id"] == sent.headers["x-client-request-id"] \
        == body["prompt_cache_key"]
    assert sent.headers["x-codex-window-id"] == meta["window_id"] == meta["thread_id"] + ":0"
    assert body["client_metadata"]["x-codex-turn-metadata"] == sent.headers["x-codex-turn-metadata"]
    assert body["client_metadata"]["turn_id"] == meta["turn_id"] == meta["root_turn_id"]
    assert uuid.UUID(meta["installation_id"]).version == 4
    # Everything that is not an id is byte-identical to what the caller sent.
    assert meta["agent_name"] == "/root" and meta["sandbox_mode"] == "read-only"
    assert body["input"] == [{"role": "user", "content": "hi"}]
