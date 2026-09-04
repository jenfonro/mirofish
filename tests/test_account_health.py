"""Parking accounts the upstream refuses for a reason that is about the ACCOUNT.

Two refusals qualify: 401 rejects its credentials/signed session, and a 503
``overloaded_error`` says the upstream has no capacity for it. Every further
request on such an account fails, so it is marked abnormal, dropped from
automatic selection, and the request fails over.

Any other 503 is a fault in front of the upstream (edge error page, the
relay's own "no device session" / "no proxy node" refusals) that every account
shares, so it parks nothing — see the transient-503 tests below.

How long it stays parked depends on whether the refusal heals by itself:

- 401 never expires on its own; the account has to be logged in again.
- 503 (`overloaded_error`) is an upstream capacity outage that does recover,
  so it carries a retry deadline (a day) after which automatic selection tries
  the account again on its own.

In both cases a request aimed explicitly at the account (the WebUI playground)
that succeeds clears the record immediately. Nothing else re-probes it, so a
dead account cannot quietly rejoin the rotation and burn real traffic.
"""

import datetime
import json
import time

import httpx
import pytest
import respx

from mirofish.api.state import HEALTH_RETRY_AFTER, AppState
from mirofish.errors import RelayError

from tests.conftest import RELAY_BASE, add_account

ANTHROPIC_OK = {
    "id": "msg_1", "type": "message", "role": "assistant",
    "model": "claude-haiku-4-5", "content": [{"type": "text", "text": "OK"}],
    "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
}
SIGNED_SESSION_401 = {"type": "error", "error": {
    "type": "authentication_error",
    "message": "this client version must upgrade to a signed session"}}
OVERLOADED_503 = {"type": "error", "error": {
    "type": "overloaded_error", "message": "no upstream available"}}
# The upstream benches an account after repeated rate-limit refusals and says
# exactly when it comes back.
SUSPENDED_403 = {"type": "error", "error": {
    "type": "permission_error",
    "message": "this account is temporarily suspended after repeated upstream "
               "rate-limit refusals; access resumes at 2026-09-05T07:11:58Z. "
               "Contact support if this is unexpected"}}
# The 503 shapes seen in production that say nothing about the account: an
# edge HTML error page and a rejection with no parseable envelope at all.
EDGE_HTML_503 = {"_raw": "<html><head><title>503 Service Unavailable</title>"}
TRANSIENT_503_BODIES = (None, EDGE_HTML_503, {"error": "service unavailable"},
                        {"kind": "device_session_required"})


def _conv(text):
    return {"messages": [{"role": "user", "content": text}]}


def _mock_device_session(ticket="device-ticket"):
    return respx.post(RELAY_BASE + "/v1/device/session").mock(
        return_value=httpx.Response(200, json={"ticket": ticket, "expiresIn": 900}))


def _messages(alias="work", model="claude-haiku-4-5"):
    return {"model": model, "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]}


def refusal(status, body=None):
    return RelayError("upstream refused", status, body)


@pytest.mark.parametrize("status,body", [(401, SIGNED_SESSION_401),
                                         (401, None),
                                         (503, OVERLOADED_503)])
def test_a_refusal_aimed_at_the_account_parks_it(state, status, body):
    add_account(state, "work")

    assert state.note_account_unserviceable("work", refusal(status, body))
    assert state.account_unhealthy("work")
    assert state.account_health("work")["status"] == status
    # A health verdict, not a quota one: the shared-quota cooldown is untouched.
    assert state.exhausted_cooldown("work") == 0


@pytest.mark.parametrize("body", TRANSIENT_503_BODIES)
def test_a_503_that_is_not_a_capacity_refusal_parks_nothing(state, body):
    """503 alone is not a verdict on the account.

    The relay answers 503 for its own reasons (no device ticket, no usable
    proxy exit) and the edge in front of the upstream serves 503 pages during a
    hiccup. Those hit whichever account carried the request, so parking on them
    walks the entire pool out of rotation over a shared fault — which is
    exactly what happened in production: one bad minute left 81 of 82 accounts
    benched for a day.
    """
    add_account(state, "work")

    assert not state.note_account_unserviceable("work", refusal(503, body))
    assert not state.account_unhealthy("work")
    assert state.account_health("work") == {}
    # Nor is it quota pressure: no cooldown either, the caller just retries.
    assert state.exhausted_cooldown("work") == 0


def test_a_transient_503_leaves_the_account_in_the_rotation(state):
    """The point of not parking: the pool still serves the next request."""
    add_account(state, "work")

    assert not state.note_account_unserviceable("work", refusal(503, EDGE_HTML_503))

    assert state.route_account("", "", _conv("a window")) == "work"


def test_a_suspension_403_parks_the_account_until_its_stated_deadline(state):
    """The upstream suspends an account after repeated rate-limit refusals and
    states when access returns.

    Every request until then is refused, so leaving the account in rotation
    means scheduling keeps electing it while the panel still calls it healthy
    — which is what happened in production: 1832 refusals across 10 accounts,
    none of them marked abnormal. The stated deadline is used verbatim because
    retrying before it is guaranteed to fail.
    """
    add_account(state, "work")
    add_account(state, "spare")

    assert state.note_account_unserviceable("work", refusal(403, SUSPENDED_403))

    assert state.account_unhealthy("work")
    health = state.account_health("work")
    assert health["status"] == 403
    assert health["retry_at"] == datetime.datetime(
        2026, 9, 5, 7, 11, 58, tzinfo=datetime.timezone.utc).timestamp()
    # It leaves automatic selection; the pinned recovery path still reaches it.
    assert state.route_account("", "", _conv("a window")) == "spare"
    assert state.route_account("work", "", {}) == "work"


def test_a_suspension_without_a_readable_deadline_still_parks(state):
    """A suspension whose deadline cannot be read must not stay in rotation.

    Falling back to the status window keeps it out long enough to stop
    absorbing refusals, without benching it for a day over an unparseable
    message.
    """
    add_account(state, "work")
    body = {"error": {"type": "permission_error",
                      "message": "this account is temporarily suspended"}}

    assert state.note_account_unserviceable("work", refusal(403, body))

    assert state.account_unhealthy("work")
    assert state.health_retry_in("work") == pytest.approx(
        HEALTH_RETRY_AFTER[403], abs=30)


@pytest.mark.parametrize("body", [
    None,                                                    # panel-disabled
    {"error": {"type": "permission_error", "message": "forbidden"}},
    {"error": {"type": "invalid_request_error", "message": "suspended"}},
])
def test_other_403s_are_not_account_suspensions(state, body):
    """Only the suspension envelope parks. The relay's own 403 for a
    panel-disabled account must not be read as an upstream verdict."""
    add_account(state, "work")

    assert not state.note_account_unserviceable("work", refusal(403, body))
    assert not state.account_unhealthy("work")


@respx.mock
async def test_a_401_shows_up_as_needing_a_login(client, state, auth_headers):
    """401 has no retry deadline, which is how the panel knows to say
    "log in again" instead of showing a countdown."""
    add_account(state, "alpha")
    add_account(state, "beta")
    _mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(401, json=SIGNED_SESSION_401),
        httpx.Response(401, json=SIGNED_SESSION_401),
        httpx.Response(200, json=ANTHROPIC_OK),
    ])

    response = await client.post("/v1/messages", headers=auth_headers,
                                 json=_messages())

    assert response.status_code == 200
    accounts = {entry["alias"]: entry for entry in
                (await client.get("/accounts", headers=auth_headers)).json()["accounts"]}
    parked = accounts["alpha"]
    assert parked["healthy"] is False
    assert parked["health"]["status"] == 401
    assert parked["health_retry_in"] is None


def test_the_upstream_reason_is_recorded_for_the_panel(state):
    add_account(state, "work")

    state.note_account_unserviceable("work", refusal(503, OVERLOADED_503))
    health = state.account_health("work")

    assert health["state"] == "error"
    assert health["kind"] == "upstream_503"
    assert "overloaded_error" in health["message"]
    assert "no upstream available" in health["message"]
    assert health["at"]


def test_a_parked_account_leaves_automatic_selection(state):
    add_account(state, "work")
    add_account(state, "spare")

    state.note_account_unserviceable("work", refusal(503, OVERLOADED_503))

    assert state.route_account("", "", _conv("a window")) == "spare"
    assert state.pick_account("") == "spare"
    # Explicit pinning still reaches it: that is the recovery path.
    assert state.route_account("work", "", {}) == "work"


def test_parking_the_last_account_surfaces_a_clear_error(state):
    add_account(state, "work")

    state.note_account_unserviceable("work", refusal(401, SIGNED_SESSION_401))

    with pytest.raises(RelayError) as excinfo:
        state.route_account("", "", _conv("a window"))
    assert excinfo.value.status == 503


def test_parking_detaches_live_sessions(state):
    add_account(state, "work")
    add_account(state, "spare")
    conv = _conv("pinned conversation")
    pinned = state.route_account("", "", conv)
    other = "spare" if pinned == "work" else "work"

    state.note_account_unserviceable(pinned, refusal(503, OVERLOADED_503))

    # The next turn of the same conversation moves off the parked account.
    assert state.route_account("", "", conv) == other


async def test_a_401_record_never_expires_on_its_own(state, settings):
    """Rejected credentials do not heal: no timer and no restart clears it."""
    add_account(state, "work")
    state.store.mark_account_error("work", 401, "signed session required",
                                   "upstream_401")
    assert state.health_retry_in("work") is None

    revived = AppState(settings)
    try:
        assert revived.account_unhealthy("work")
        assert revived.health_retry_in("work") is None
    finally:
        await revived.aclose()


def test_a_503_record_carries_a_one_day_retry_deadline(state):
    """Capacity outages recover, so the account comes back without an operator.

    Re-probing sooner just burns a request and re-parks it; the upstream's own
    message points at a status channel rather than promising a quick fix.
    """
    add_account(state, "work")

    state.note_account_unserviceable("work", refusal(503, OVERLOADED_503))

    assert state.account_unhealthy("work")
    assert state.health_retry_in("work") == pytest.approx(
        HEALTH_RETRY_AFTER[503], abs=30)
    assert HEALTH_RETRY_AFTER[503] == 86400.0


def test_a_503_record_stops_parking_once_the_deadline_passes(state):
    """The point of the deadline: it returns with no operator action."""
    add_account(state, "work")
    add_account(state, "spare")
    state.store.mark_account_error(
        "work", 503, "overloaded_error: busy", "upstream_503",
        retry_after=time.time() - 1.0)

    assert not state.account_unhealthy("work")
    assert state.health_retry_in("work") == 0.0
    assert state.route_account("", "", _conv("a window")) in {"work", "spare"}


def test_an_unreadable_retry_deadline_does_not_strand_the_account(state):
    """A corrupted record must fail open rather than bench it forever."""
    add_account(state, "work")
    state.store.merge_metadata("work", {"health": {
        "state": "error", "status": 503, "kind": "upstream_503",
        "message": "busy", "at": "now", "retry_at": "not-a-number"}})

    assert not state.account_unhealthy("work")


def _legacy_record(state, alias, status, at):
    """A record written before retry deadlines existed (no ``retry_at``)."""
    state.store.merge_metadata(alias, {"health": {
        "state": "error", "status": status, "kind": "upstream_%d" % status,
        "message": "recorded before deadlines existed", "at": at}})


def test_a_legacy_503_record_is_retried_a_day_after_it_was_parked(state):
    """Upgrading must not leave the previously parked accounts stuck forever.

    The deadline is measured from when the account was parked, not from the
    upgrade, so a stale 503 is eligible immediately instead of serving another
    full day of quarantine.
    """
    add_account(state, "fresh")
    add_account(state, "stale")
    now = datetime.datetime.now(datetime.timezone.utc)
    _legacy_record(state, "fresh", 503, (now - datetime.timedelta(hours=1)).isoformat())
    _legacy_record(state, "stale", 503, (now - datetime.timedelta(days=2)).isoformat())

    assert state.account_unhealthy("fresh")
    assert state.health_retry_in("fresh") == HEALTH_RETRY_AFTER[503]
    assert not state.account_unhealthy("stale")
    assert state.health_retry_in("stale") == 0.0


def test_a_legacy_401_record_still_never_expires(state):
    """401 has no window, so a legacy record stays parked as before."""
    add_account(state, "work")
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(days=30)).isoformat()
    _legacy_record(state, "work", 401, old)

    assert state.account_unhealthy("work")
    assert state.health_retry_in("work") is None


def test_a_legacy_record_with_an_unparseable_timestamp_is_retried(state):
    add_account(state, "work")
    _legacy_record(state, "work", 503, "not-a-timestamp")

    assert not state.account_unhealthy("work")


def test_a_new_login_clears_the_record(state):
    add_account(state, "work")
    state.store.mark_account_error("work", 401, "signed session required",
                                   "upstream_401")

    state.reset_account_runtime("work")

    assert not state.account_unhealthy("work")


@respx.mock
async def test_a_request_fails_over_and_parks_the_refused_account(
        client, state, auth_headers):
    add_account(state, "alpha")
    add_account(state, "beta")
    _mock_device_session()
    route = respx.post(RELAY_BASE + "/v1/messages").mock(side_effect=[
        httpx.Response(503, json=OVERLOADED_503),
        httpx.Response(200, json=ANTHROPIC_OK),
    ])

    response = await client.post("/v1/messages", headers=auth_headers,
                                 json=_messages())

    assert response.status_code == 200
    assert response.headers["X-Mirofish-Account"] == "beta"
    assert route.call_count == 2
    assert state.account_unhealthy("alpha")
    assert not state.account_unhealthy("beta")

    accounts = {entry["alias"]: entry for entry in
                (await client.get("/accounts", headers=auth_headers)).json()["accounts"]}
    assert accounts["alpha"]["healthy"] is False
    assert accounts["alpha"]["health"]["status"] == 503
    # The panel needs the countdown to tell "will retry itself" from
    # "needs a login".
    assert accounts["alpha"]["health_retry_in"] == pytest.approx(
        HEALTH_RETRY_AFTER[503], abs=60)
    assert accounts["beta"]["healthy"] is True
    assert accounts["beta"]["health"] == {}
    assert accounts["beta"]["health_retry_in"] is None


@respx.mock
async def test_an_explicitly_pinned_account_surfaces_the_error(
        client, state, auth_headers):
    """Never substitute a pinned account: the caller asked for that one."""
    add_account(state, "alpha")
    add_account(state, "beta")
    _mock_device_session()
    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(503, json=OVERLOADED_503))

    response = await client.post(
        "/v1/messages", headers={**auth_headers, "X-Mirofish-Account": "alpha"},
        json=_messages())

    assert response.status_code == 503


@respx.mock
async def test_an_explicit_retry_that_succeeds_clears_the_record(
        client, state, auth_headers):
    """The playground recovery path: pin the parked account, succeed, unpark."""
    add_account(state, "work")
    _mock_device_session()
    state.store.mark_account_error("work", 503, "no upstream available",
                                   "upstream_503")
    # Parked, so automatic routing cannot reach it at all.
    with pytest.raises(RelayError):
        state.route_account("", "", _conv("a window"))

    respx.post(RELAY_BASE + "/v1/messages").mock(
        return_value=httpx.Response(200, json=ANTHROPIC_OK))
    response = await client.post(
        "/v1/messages", headers={**auth_headers, "X-Mirofish-Account": "work"},
        json=_messages())

    assert response.status_code == 200
    assert state.account_health("work") == {}
    assert state.route_account("", "", _conv("a window")) == "work"


@respx.mock
async def test_the_model_catalog_still_answers_when_every_account_is_parked(
        client, state, auth_headers):
    """A parked account must not take the model catalog down with it.

    401/503 are verdicts about model traffic. `/v1/models` is a zero-cost read
    and the panel needs it precisely when accounts are failing — that is where
    the operator picks a model to retry with.
    """
    add_account(state, "work")
    add_account(state, "spare")
    _mock_device_session()
    respx.get(RELAY_BASE + "/v1/models").mock(return_value=httpx.Response(
        200, json={"data": [{"id": "claude-fable-5-1"}]}))
    for alias in ("work", "spare"):
        state.store.mark_account_error(alias, 503, "no upstream available",
                                       "upstream_503")

    response = await client.get("/v1/models", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["mirofish_model_ids"] == ["claude-fable-5-1"]
    # Model traffic stays refused: only the catalog is exempt.
    with pytest.raises(RelayError) as excinfo:
        state.route_account("", "", _conv("a window"))
    assert excinfo.value.status == 503


@respx.mock
async def test_the_catalog_response_holds_no_bare_string_list(
        client, state, auth_headers):
    """No top-level list may hold plain strings.

    A strict client (sub2api) decodes every top-level list in this response
    into model-object structs, so one string list fails its whole decode and
    its "sync upstream models" breaks. Keep the OpenAI shape clean and
    namespace the extras.
    """
    add_account(state, "work")
    _mock_device_session()
    respx.get(RELAY_BASE + "/v1/models").mock(return_value=httpx.Response(
        200, json={"data": [{"id": "claude-fable-5-1"}]}))

    body = (await client.get("/v1/models", headers=auth_headers)).json()

    offenders = [key for key, value in body.items()
                 if not key.startswith("mirofish_")
                 and isinstance(value, list)
                 and any(not isinstance(item, dict) for item in value)]
    assert offenders == [], offenders
    assert body["data"] == [{
        "id": "claude-fable-5-1", "object": "model", "type": "model",
        "display_name": "claude-fable-5-1",
        "created_at": "2024-01-01T00:00:00Z", "created": 0,
        "owned_by": "mirofish",
    }]
