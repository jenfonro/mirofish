import httpx
import pytest

from mirofish.api import create_app
from mirofish.api.state import AppState
from mirofish.config import Settings
from mirofish.store import utc_now

AUTH_BASE = "https://auth.test"
RELAY_BASE = "https://relay.test"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MASTER_KEY", "unit-test-master-key")
    monkeypatch.delenv("MIROFISH_PROXY_SUBSCRIPTION_URL", raising=False)
    monkeypatch.delenv("MIROFISH_PROXY_SUBSCRIPTION_URL_FILE", raising=False)
    return Settings(auth_base=AUTH_BASE, relay_base=RELAY_BASE,
                    data_dir=tmp_path / "data", cred_backend="file", timeout=5.0)


@pytest.fixture
async def state(settings):
    app_state = AppState(settings)
    yield app_state
    await app_state.aclose()


@pytest.fixture
async def client(state):
    app = create_app(state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay.local") as http:
        yield http


@pytest.fixture
def auth_headers(state):
    return {"X-Mirofish-Proxy-Key": state.proxy_key}


def add_account(state, alias: str, email: str | None = None) -> None:
    state.store.save(alias, email or f"{alias}@example.com",
                     f"access-{alias}", f"refresh-{alias}",
                     {"user_id": "u-" + alias, "plan": "pro", "tenant": "t1",
                      "quota": {}, "last_usage": {}, "checked_at": utc_now()})
