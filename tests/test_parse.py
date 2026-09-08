"""Parsing manually entered proxy endpoints."""

import pytest

from mirofish.errors import RelayError
from mirofish.proxy.parse import proxy_from_uri, proxy_identity, proxy_url
from mirofish.validate import proxy_node_value


@pytest.mark.parametrize("line,expected", [
    ("socks5://user:pass@198.51.100.24:1080",
     {"scheme": "socks5", "host": "198.51.100.24", "port": 1080,
      "username": "user", "password": "pass"}),
    ("http://1.2.3.4:3128",
     {"scheme": "http", "host": "1.2.3.4", "port": 3128,
      "username": "", "password": ""}),
    ("socks5h://h.example.com:1080",
     {"scheme": "socks5", "host": "h.example.com", "port": 1080,
      "username": "", "password": ""}),
])
def test_a_pasted_uri_becomes_a_node(line, expected):
    node = proxy_from_uri(line)
    for key, value in expected.items():
        assert node[key] == value


def test_a_name_comes_from_the_fragment_or_the_endpoint():
    assert proxy_from_uri("socks5://h.example.com:1080#Tokyo%201")["name"] == "Tokyo 1"
    # No fragment: the endpoint is the only honest label available.
    assert proxy_from_uri("socks5://h.example.com:1080")["name"] == "h.example.com:1080"


@pytest.mark.parametrize("line", [
    "", "not-a-proxy", "vmess://encrypted-node", "socks5://h.example.com",
    "ftp://h.example.com:21",
])
def test_unusable_lines_are_rejected(line):
    """A bulk import reports these per line rather than failing the batch."""
    assert proxy_from_uri(line) is None


def test_identity_is_the_endpoint_not_the_name():
    """Renaming must not re-identify a node: accounts are pinned by id, so a
    rename that changed it would silently unpin every account on that exit."""
    base = {"scheme": "socks5", "host": "h", "port": 1080,
            "username": "u", "password": "p"}
    assert proxy_identity({**base, "name": "Tokyo"}) \
        == proxy_identity({**base, "name": "Osaka"})
    # A different endpoint is a different node.
    assert proxy_identity({**base, "port": 1081}) != proxy_identity(base)
    assert proxy_identity({**base, "password": "q"}) != proxy_identity(base)


def test_proxy_url_quoting():
    config = {"scheme": "socks5", "host": "h.example.com", "port": 1080,
              "username": "u@x", "password": "p:w"}
    assert proxy_url(config) == "socks5://u%40x:p%3Aw@h.example.com:1080"


def test_a_node_needs_a_host_a_port_and_a_dialable_scheme():
    with pytest.raises(RelayError):
        proxy_node_value({"host": "", "port": 1080})
    with pytest.raises(RelayError):
        proxy_node_value({"host": "h", "port": 0})
    with pytest.raises(RelayError):
        proxy_node_value({"host": "h", "port": 1080, "scheme": "vmess"})


def test_a_blank_name_falls_back_to_the_endpoint():
    """A pasted endpoint usually has no meaningful name, and demanding one
    would only produce placeholders."""
    node = proxy_node_value({"host": "h.example.com", "port": 1080})
    assert node["name"] == "h.example.com:1080"
    assert node["scheme"] == "socks5"


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
