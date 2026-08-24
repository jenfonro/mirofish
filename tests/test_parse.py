import base64

import pytest

from mirofish.errors import RelayError
from mirofish.proxy.parse import parse_proxy_subscription, proxy_identity, proxy_url

URI_LIST = """
http://user:pass@proxy-a.example.com:8080#Node%20A
socks5://proxy-b.example.com:1080#NodeB
vmess://ignored-encrypted-node
"""

YAML_DOC = """
proxies:
  - name: "HK-01"
    type: http
    server: hk.example.com
    port: 8080
    username: u1
    password: p1
  - name: "SS-01"
    type: ss
    server: ss.example.com
    port: 8388
    cipher: aes-256-gcm
  - {name: "SG-02", type: socks5, server: sg.example.com, port: 1080}
"""


def test_uri_list():
    nodes, skipped = parse_proxy_subscription(URI_LIST.encode())
    assert [n["name"] for n in nodes] == ["Node A", "NodeB"]
    assert nodes[0]["scheme"] == "http" and nodes[0]["username"] == "user"
    assert nodes[1]["scheme"] == "socks5"
    assert skipped == 1  # the vmess line


def test_base64_wrapped_uri_list():
    encoded = base64.b64encode(URI_LIST.strip().encode())
    nodes, _ = parse_proxy_subscription(encoded)
    assert len(nodes) == 2


def test_mihomo_yaml():
    nodes, skipped = parse_proxy_subscription(YAML_DOC.encode())
    assert [n["name"] for n in nodes] == ["HK-01", "SG-02"]
    assert nodes[0]["port"] == 8080 and nodes[0]["password"] == "p1"
    assert skipped == 1  # the ss node


def test_json_subscription():
    doc = b'{"proxies": [{"name": "J1", "type": "http", "server": "j.example.com", "port": 3128}]}'
    nodes, _ = parse_proxy_subscription(doc)
    assert nodes[0]["name"] == "J1"


def test_unsupported_only_raises():
    with pytest.raises(RelayError):
        parse_proxy_subscription(b"vmess://only-encrypted-nodes")


def test_identity_matches_legacy_algorithm():
    import hashlib
    import json as jsonlib
    config = {"name": "N", "scheme": "http", "host": "h", "port": 1,
              "username": "", "password": ""}
    canonical = jsonlib.dumps(config, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))
    assert proxy_identity(config) == hashlib.sha256(canonical.encode()).hexdigest()[:32]


def test_proxy_url_quoting():
    config = {"scheme": "socks5", "host": "h.example.com", "port": 1080,
              "username": "u@x", "password": "p:w"}
    assert proxy_url(config) == "socks5://u%40x:p%3Aw@h.example.com:1080"


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
