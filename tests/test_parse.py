import base64

import pytest

from mirofish.errors import RelayError
from mirofish.proxy import (DIRECT, parse_proxy_uris, proxy_from_uri,
                            proxy_identity, proxy_url)
from mirofish.validate import proxy_node_value

URI_LIST = """
http://user:pass@proxy-a.example.com:8080#Node%20A
socks5://proxy-b.example.com:1080#NodeB
vmess://ignored-encrypted-node
"""

def test_uri_list():
    nodes, failed = parse_proxy_uris(URI_LIST)
    assert [n["name"] for n in nodes] == ["Node A", "NodeB"]
    assert nodes[0]["scheme"] == "http" and nodes[0]["username"] == "user"
    assert nodes[1]["scheme"] == "socks5"
    assert failed == ["line 4: invalid proxy URI"]


@pytest.mark.parametrize("text", [
    base64.b64encode(URI_LIST.encode()).decode(),
    '{"proxies": [{"name": "J1", "type": "http", "server": "j.test", "port": 3128}]}',
    'proxies:\n  - {name: "SG-02", type: socks5, server: sg.test, port: 1080}',
])
def test_subscription_formats_are_not_parsed(text):
    nodes, failed = parse_proxy_uris(text)
    assert nodes == []
    assert failed


def test_empty_lines_comments_and_bom():
    assert parse_proxy_uris("\ufeff \n# comment\r\n\t\r\n") == ([], [])
    nodes, failed = parse_proxy_uris("\ufeffhttps://example.test:443\r\n # comment\n")
    assert len(nodes) == 1 and failed == []
    assert nodes[0]["name"] == ""


def test_invalid_uri_credentials_are_never_echoed():
    nodes, failed = parse_proxy_uris(
        "\n# comment\nhttp://sensitive-user:secret-password@proxy.test:bad\n")
    assert nodes == []
    assert failed == ["line 3: invalid proxy URI"]
    assert "sensitive-user" not in str(failed)
    assert "secret-password" not in str(failed)


@pytest.mark.parametrize("scheme", ["http", "https", "socks5", "socks", "socks5h"])
def test_supported_uri_schemes(scheme):
    config = proxy_from_uri(f"{scheme}://proxy.test:1080#%E6%97%A5%E6%9C%AC")
    assert config["name"] == "日本"
    assert config["scheme"] == ("socks5" if scheme.startswith("socks") else scheme)


@pytest.mark.parametrize("uri", [
    "proxy.test:1080", "direct", "ftp://proxy.test:1080", "http://proxy.test",
    "http://proxy.test:0", "http://proxy.test:65536", "http://proxy.test:-1",
    "http://proxy.test:abc", "http://:1080", "http://[broken]:1080",
    "http://proxy.test:1080/subscription", "http://proxy.test:1080?token=secret",
    "http://user:p%00ss@proxy.test:1080", "http://bad host:8080",
    "http://proxy.\ntest:1080", "http://proxy.test:8080 socks5://other.test:1080",
])
def test_invalid_uri_is_rejected(uri):
    assert proxy_from_uri(uri) is None


def test_identity_ignores_name_and_id_but_not_endpoint():
    config = proxy_from_uri("http://u:p@proxy.test:1080#first")
    assert proxy_identity(config) == proxy_identity({**config, "name": "renamed", "id": "id"})
    assert proxy_identity(config) != proxy_identity({**config, "password": "changed"})


def test_proxy_url_quoting():
    config = {"scheme": "socks5", "host": "h.example.com", "port": 1080,
              "username": "u@x", "password": "p:w"}
    assert proxy_url(config) == "socks5://u%40x:p%3Aw@h.example.com:1080"


def test_ipv6_and_credentials_round_trip():
    uri = "https://%20u%40x%20:p%3A%2F%40%23%25@[2001:db8::1]:443"
    config = proxy_from_uri(uri)
    assert config["host"] == "2001:db8::1"
    assert config["username"] == " u@x "
    assert config["password"] == "p:/@#%"
    assert proxy_url(config) == uri


def test_manual_blank_name_and_credentials_are_preserved():
    config = proxy_node_value({"scheme": "http", "host": "proxy.test", "port": "8080",
                               "name": "", "username": " u ", "password": " p "})
    assert config["name"] == ""
    assert config["username"] == " u "
    assert config["password"] == " p "
    assert config["port"] == 8080


@pytest.mark.parametrize("changes", [
    {"host": ""}, {"host": "h/path"}, {"host": "h:8080"}, {"host": "h@other"},
    {"host": "h?x"}, {"host": "h#x"}, {"host": "h\\other"}, {"host": "h\x00"},
    {"host": None}, {"scheme": "vmess"}, {"port": 0}, {"port": 65536},
    {"port": True}, {"port": 1.5}, {"port": None}, {"password": "\r"},
])
def test_invalid_manual_endpoint(changes):
    with pytest.raises(RelayError) as raised:
        proxy_node_value({"scheme": "http", "host": "proxy.test", "port": 8080, **changes})
    assert raised.value.status == 400


def test_direct_url_is_explicit_and_missing_config_fails_closed():
    assert proxy_url(DIRECT) is None
    assert proxy_url(None) is None
    for config in ("", {}, {"host": "h", "port": 8080},
                   {"host": "h", "port": 8080, "scheme": "invalid"}):
        with pytest.raises(RelayError) as raised:
            proxy_url(config)
        assert raised.value.status == 503


def test_payload_summary_redacts_content():
    from mirofish.upstream import _payload_summary, _rejection_detail

    payload = {
        "model": "claude-fable-5", "max_tokens": 4096, "temperature": 1.3,
        "system": "TOP SECRET SYSTEM PROMPT",
        "messages": [
            {"role": "user", "content": "my secret question"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": [
                {"type": "text", "text": "hidden text"},
                {"type": "tool_result", "tool_use_id": "call_1", "content": "42"},
            ]},
        ],
    }
    summary = _payload_summary(payload)
    for secret in ("SECRET", "secret question", "hidden text", "42"):
        assert secret not in summary
    assert "model=claude-fable-5" in summary
    assert "temperature=1.3" in summary
    assert "assistant:EMPTY" in summary
    assert "tool_result" in summary

    detail = _rejection_detail({"error": {"type": "invalid_request_error",
                                          "message": "The request was rejected as invalid."}})
    assert detail == "invalid_request_error: The request was rejected as invalid."
