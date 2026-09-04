import logging

from mirofish.cli import (_cmd_add, _cmd_remove, _configure_logging,
                          _proxy_key_notice)


def test_proxy_key_notice_never_contains_secret(state):
    notice = _proxy_key_notice(state)

    assert state.proxy_key not in notice
    assert str(state.store.proxy_key_path) in notice
    assert "不会写入日志" in notice


class _FakeAccounts:
    async def start_login(self, alias, email, proxy_url=None):
        return None

    async def finish_login(self, alias, email, code, proxy_url=None, proxy_id=None):
        return {"alias": alias, "email": email, "code": code}


class _FakeState:
    def __init__(self):
        self.accounts = _FakeAccounts()
        self.reset = []
        self.removed = []

    async def with_pending_proxy(self, alias, operation):
        return None, await operation(None)

    async def with_fixed_proxy(self, alias, proxy, operation):
        return await operation(None)

    def reset_account_runtime(self, alias):
        self.reset.append(alias)

    def remove_account(self, alias):
        self.removed.append(alias)


async def test_cli_add_resets_account_runtime(monkeypatch):
    state = _FakeState()
    monkeypatch.setattr("mirofish.cli.getpass.getpass", lambda _prompt: "123456")
    monkeypatch.setattr("mirofish.cli._print", lambda _value: None)

    await _cmd_add(state, "work", "work@example.com")

    assert state.reset == ["work"]


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
