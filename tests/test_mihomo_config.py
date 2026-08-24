"""The generated Mihomo config must carry the subscription's own dns section:
some providers publish node servers under private domains that only their DNS
(nameserver-policy) resolves; public resolvers answer with placeholders."""

import httpx
import pytest
import respx
import yaml

from mirofish.config import Settings
from mirofish.mihomo_config import dns_from_subscription, write_mihomo_config

SUB_WITH_DNS = """\
dns:
  enable: true
  nameserver:
    - 223.5.5.5
  nameserver-policy:
    "+.entry.example.qpon": tcp://private-dns.example:8080
proxies:
  - {name: node-a, type: http, server: a.entry.example.qpon, port: 8080}
"""

SUB_WITHOUT_DNS = """\
proxies:
  - {name: node-a, type: http, server: a.example, port: 8080}
"""


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MIROFISH_MASTER_KEY", "unit-test-master-key")
    monkeypatch.delenv("MIROFISH_PROXY_SUBSCRIPTION_URL", raising=False)
    monkeypatch.delenv("MIROFISH_PROXY_SUBSCRIPTION_FILE", raising=False)
    return Settings(auth_base="https://auth.test", relay_base="https://relay.test",
                    data_dir=tmp_path / "data", cred_backend="file")


def _generate(tmp_path):
    output = tmp_path / "mihomo" / "config.yaml"
    return output


def test_dns_from_subscription_extracts_mapping():
    assert dns_from_subscription(SUB_WITH_DNS.encode())["nameserver-policy"] == {
        "+.entry.example.qpon": "tcp://private-dns.example:8080"}
    assert dns_from_subscription(SUB_WITHOUT_DNS.encode()) is None
    assert dns_from_subscription(b"not: [valid") is None
    assert dns_from_subscription(b"- just\n- a list\n") is None


def test_file_subscription_dns_is_copied(tmp_path, settings, monkeypatch):
    source = tmp_path / "sub.yaml"
    source.write_text(SUB_WITH_DNS)
    monkeypatch.setenv("MIROFISH_PROXY_SUBSCRIPTION_FILE", str(source))
    output = _generate(tmp_path)
    write_mihomo_config(output, settings)
    config = yaml.safe_load(output.read_text())
    assert config["dns"]["nameserver-policy"] == {
        "+.entry.example.qpon": "tcp://private-dns.example:8080"}


def test_file_subscription_without_dns_stays_clean(tmp_path, settings, monkeypatch):
    source = tmp_path / "sub.yaml"
    source.write_text(SUB_WITHOUT_DNS)
    monkeypatch.setenv("MIROFISH_PROXY_SUBSCRIPTION_FILE", str(source))
    output = _generate(tmp_path)
    write_mihomo_config(output, settings)
    assert "dns" not in yaml.safe_load(output.read_text())


@respx.mock
def test_url_subscription_dns_is_fetched(tmp_path, settings, monkeypatch):
    url = "https://sub.test/link?clash=3&extend=1"
    respx.get(url).mock(return_value=httpx.Response(200, text=SUB_WITH_DNS))
    monkeypatch.setenv("MIROFISH_PROXY_SUBSCRIPTION_URL", url)
    output = _generate(tmp_path)
    write_mihomo_config(output, settings)
    config = yaml.safe_load(output.read_text())
    assert config["dns"]["enable"] is True
    assert "+.entry.example.qpon" in config["dns"]["nameserver-policy"]
    # The provider block itself is unchanged.
    assert config["proxy-providers"]["mirofish"]["url"] == url


@respx.mock
def test_url_subscription_fetch_failure_is_non_fatal(tmp_path, settings, monkeypatch):
    url = "https://sub.test/link"
    respx.get(url).mock(side_effect=httpx.ConnectError("boom"))
    monkeypatch.setenv("MIROFISH_PROXY_SUBSCRIPTION_URL", url)
    output = _generate(tmp_path)
    write_mihomo_config(output, settings)
    config = yaml.safe_load(output.read_text())
    assert "dns" not in config
    assert config["proxy-providers"]["mirofish"]["url"] == url


def test_node_exclude_filter_reaches_provider(tmp_path, settings, monkeypatch):
    source = tmp_path / "sub.yaml"
    source.write_text(SUB_WITHOUT_DNS)
    monkeypatch.setenv("MIROFISH_PROXY_SUBSCRIPTION_FILE", str(source))
    settings.proxy_node_exclude = "香港|HK|🇭🇰"
    output = _generate(tmp_path)
    write_mihomo_config(output, settings)
    config = yaml.safe_load(output.read_text())
    provider = config["proxy-providers"]["mirofish"]
    assert provider["exclude-filter"] == "香港|HK|🇭🇰"


def test_invalid_node_exclude_regex_fails_loudly(tmp_path, settings, monkeypatch):
    from mirofish.errors import RelayError

    source = tmp_path / "sub.yaml"
    source.write_text(SUB_WITHOUT_DNS)
    monkeypatch.setenv("MIROFISH_PROXY_SUBSCRIPTION_FILE", str(source))
    settings.proxy_node_exclude = "香港|(unclosed"
    with pytest.raises(RelayError):
        write_mihomo_config(_generate(tmp_path), settings)
