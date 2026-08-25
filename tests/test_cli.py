from mirofish.cli import _cmd_add, _cmd_remove, _proxy_key_notice


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
