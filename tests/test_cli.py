import logging

from mirofish.cli import (_cmd_add, _cmd_remove, _configure_logging,
                          _proxy_key_notice)


def test_proxy_key_notice_never_contains_secret(state):
    notice = _proxy_key_notice(state)

    assert state.proxy_key not in notice
    assert str(state.store.proxy_key_path) in notice
    assert "不会写入日志" in notice


class _FakeState:
    def __init__(self):
        self.removed = []

    def remove_account(self, alias):
        self.removed.append(alias)


async def test_cli_add_resets_account_runtime(state, monkeypatch):
    from tests.conftest import add_account
    add_account(state, "work")
    resets = []
    async def start(*args, **kwargs):
        pass
    async def finish(*args, **kwargs):
        return {}
    monkeypatch.setattr(state.accounts, "start_login", start)
    monkeypatch.setattr(state.accounts, "finish_login", finish)
    monkeypatch.setattr(state, "reset_account_runtime", resets.append)
    monkeypatch.setattr("mirofish.cli.getpass.getpass", lambda _prompt: "123456")
    monkeypatch.setattr("mirofish.cli._print", lambda _value: None)

    await _cmd_add(state, "work", "work@example.com")

    assert resets == ["work"]


async def test_cli_relogin_keeps_selected_proxy(state, monkeypatch):
    from tests.conftest import add_account
    add_account(state, "work")
    proxy = state.pool.add({"scheme": "http", "host": "proxy.test", "port": 8080})
    state.store.set_account_proxy("work", proxy["id"])
    calls = []
    async def start(alias, email, proxy_url=None):
        calls.append(proxy_url)
    async def finish(alias, email, code, proxy_url=None, proxy_id=None):
        calls.append(proxy_url)
        assert proxy_id == proxy["id"]
        return {}
    monkeypatch.setattr(state.accounts, "start_login", start)
    monkeypatch.setattr(state.accounts, "finish_login", finish)
    monkeypatch.setattr("mirofish.cli.getpass.getpass", lambda _prompt: "123456")
    monkeypatch.setattr("mirofish.cli._print", lambda _value: None)
    await _cmd_add(state, "work", "work@example.com")
    assert calls == ["http://proxy.test:8080"] * 2
    assert state.store.row("work")["proxy_id"] == proxy["id"]


def test_cli_remove_uses_state_lifecycle(capsys):
    state = _FakeState()

    _cmd_remove(state, "work")

    assert state.removed == ["work"]
    assert "已删除本地账号：work" in capsys.readouterr().out


def test_serve_logging_makes_package_info_visible():
    """The relay's own INFO lines must reach stdout.

    Without this the one-token probe log — the only record of which caller is
    sending them — is dropped by logging's WARNING-level last-resort handler
    and never appears in ``docker logs``.
    """
    root = logging.getLogger()
    package = logging.getLogger("mirofish")
    saved = (root.level, list(root.handlers), package.level)
    try:
        _configure_logging()

        assert logging.getLogger("mirofish.relay").isEnabledFor(logging.INFO)
        # Third-party chatter stays off: one line per upstream request would
        # double the log volume this change is meant to reduce.
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)
        assert root.handlers
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]
        package.setLevel(saved[2])
