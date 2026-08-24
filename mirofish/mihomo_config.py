"""Generate the private Mihomo sidecar config (Docker init container).

Besides the legacy MirofishPool selector on the shared mixed port, the config
now defines N slot selector groups, each with its own mixed listener port, so
the relay can pin accounts to independent exits and stop serializing all
proxied traffic behind one global selector switch.
"""

from __future__ import annotations

import logging
import os
import pathlib
import stat
from typing import Any, Optional

import httpx
import yaml

logger = logging.getLogger("mirofish.mihomo_config")

from .config import Settings
from .errors import RelayError
from .proxy.mihomo import slot_group_name
from .validate import (node_exclude_pattern, proxy_subscription_file_value,
                       proxy_subscription_value)


def _write_private(path: pathlib.Path, content: bytes) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
        os.replace(temp_path, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if fd != -1:
            os.close(fd)


def dns_from_subscription(raw: bytes) -> Optional[dict[str, Any]]:
    """The subscription's own top-level `dns:` section, if it carries one.

    Some providers publish node servers under private domains that only their
    own DNS can resolve (public resolvers answer with placeholder addresses),
    declared via `nameserver-policy` in the subscription's full Clash config.
    Mihomo only reads `proxies` from a provider file, so the dns section must
    be copied into the root config or those nodes never connect."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    dns = data.get("dns")
    return dns if isinstance(dns, dict) and dns else None


def _fetch_subscription_dns(url: str, settings: Settings) -> Optional[dict[str, Any]]:
    """Best effort: a failure only means the generated config has no dns
    section, exactly what was generated before."""
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(url, headers={
                "User-Agent": settings.proxy_subscription_user_agent})
            response.raise_for_status()
            if len(response.content) > settings.proxy_fetch_max_bytes:
                logger.warning("subscription too large to inspect for a dns section")
                return None
            return dns_from_subscription(response.content)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("could not inspect the subscription for a dns section: %s",
                       type(exc).__name__)
        return None


def write_mihomo_config(output_path: pathlib.Path, settings: Settings) -> None:
    subscription = os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_URL", "").strip()
    subscription_file = os.environ.get("MIROFISH_PROXY_SUBSCRIPTION_FILE", "").strip()
    if subscription and subscription_file:
        raise RelayError("configure either MIROFISH_PROXY_SUBSCRIPTION_URL or "
                         "MIROFISH_PROXY_SUBSCRIPTION_FILE, not both", 500)
    if not subscription and not subscription_file:
        raise RelayError("MIROFISH_PROXY_SUBSCRIPTION_URL or "
                         "MIROFISH_PROXY_SUBSCRIPTION_FILE is required for Mihomo", 500)
    if subscription:
        subscription = proxy_subscription_value(subscription)
    else:
        subscription_file = proxy_subscription_file_value(subscription_file)

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    (output_path.parent / "providers").mkdir(parents=True, exist_ok=True)

    provider: dict[str, Any]
    dns: Optional[dict[str, Any]] = None
    if subscription:
        provider = {"type": "http", "url": subscription,
                    "path": "./providers/mirofish.yaml",
                    "interval": int(settings.proxy_refresh_seconds),
                    "header": {"User-Agent": [settings.proxy_subscription_user_agent]}}
        dns = _fetch_subscription_dns(subscription, settings)
    else:
        # Mihomo restricts file providers to its home/safe path. Copy the
        # read-only host bind mount into the named /config volume first.
        source_path = pathlib.Path(subscription_file)
        try:
            if not source_path.is_file():
                raise RelayError("static proxy subscription file does not exist", 500)
            source_bytes = source_path.read_bytes()
        except OSError as exc:
            raise RelayError("cannot read static proxy subscription file", 500) from exc
        if len(source_bytes) > settings.proxy_fetch_max_bytes:
            raise RelayError("static proxy subscription file is too large", 413)
        provider_file = output_path.parent / "subscription.yaml"
        _write_private(provider_file, source_bytes)
        provider = {"type": "file", "path": str(provider_file)}
        dns = dns_from_subscription(source_bytes)

    if node_exclude_pattern(settings.proxy_node_exclude) is not None:
        # Filtered at the provider, so neither the selector groups nor the
        # relay's node list ever see the excluded exits.
        provider["exclude-filter"] = settings.proxy_node_exclude

    groups: list[dict[str, Any]] = [{"name": settings.mihomo_selector, "type": "select",
                                     "use": [settings.mihomo_provider]}]
    listeners: list[dict[str, Any]] = []
    for index in range(settings.mihomo_slots):
        group = slot_group_name(index)
        groups.append({"name": group, "type": "select",
                       "use": [settings.mihomo_provider]})
        listeners.append({"name": f"mirofish-slot-{index}", "type": "mixed",
                          "port": settings.mihomo_slot_base_port + index,
                          "listen": "0.0.0.0", "proxy": group})

    config: dict[str, Any] = {
        "mixed-port": 7890,
        # The sidecar is only reachable inside the compose network (no host
        # ports are published); the relay container is a "LAN" peer.
        "allow-lan": True,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": "0.0.0.0:9090",
        "proxy-providers": {settings.mihomo_provider: provider},
        "proxy-groups": groups,
        "listeners": listeners,
        # Provider pulls follow the rules; MATCH,DIRECT keeps the subscription
        # download off the pool (routing it through a dead cached node would
        # deadlock the refresh). Relay traffic enters via the slot listeners,
        # each pinned to its own selector group, so it never hits the rules.
        "rules": ["MATCH,DIRECT"],
    }
    if dns:
        # Copied verbatim from the subscription so provider-private node
        # domains (nameserver-policy) resolve the way the provider requires.
        config["dns"] = dns
    _write_private(output_path,
                   yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode("utf-8"))
