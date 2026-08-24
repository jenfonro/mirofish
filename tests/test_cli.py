from mirofish.cli import _proxy_key_notice


def test_proxy_key_notice_never_contains_secret(state):
    notice = _proxy_key_notice(state)

    assert state.proxy_key not in notice
    assert str(state.store.proxy_key_path) in notice
    assert "不会写入日志" in notice
