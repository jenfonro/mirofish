# CLAUDE.md

## Project overview

This repository contains the Mirofish relay, centered on `scripts/general/mirofish_relay.py`. It provides multi-account credential management, email-code login, Anthropic-compatible model relay endpoints, OpenAI chat-completions translation, a built-in Chinese WebUI, and Docker deployment.

## Key files

- `scripts/general/mirofish_relay.py`: Main Python implementation and CLI.
- `deploy/mirofish-relay/Dockerfile`: Python 3.12 slim container image.
- `deploy/mirofish-relay/docker-compose.yml`: Local relay service and persistent data volume.
- `deploy/mirofish-relay/.env.example`: Required master-key configuration.
- `deploy/mirofish-relay/README.md`: Deployment and API usage documentation.

## Architecture

- Uses only the Python standard library.
- Stores account metadata in SQLite.
- Stores credentials in macOS Keychain on the host or an encrypted `secrets.enc` file in containers.
- Automatically refreshes access tokens after an upstream HTTP 401 response.
- Docker deployment runs a Mihomo sidecar for subscription-native proxy protocols and persistently binds each account to one selected node, rotating only after proxy network failure.
- Requires `X-Mirofish-Proxy-Key` on local API requests.
- Selects accounts by `X-Mirofish-Account`, configured default account, or round-robin fallback.
- Proxies Anthropic `/v1/messages` requests and exposes OpenAI-compatible chat-completions translation.
- Embeds the WebUI HTML, CSS, and JavaScript directly in the Python module.

## Common commands

Run the host CLI from the repository root:

```bash
python3 scripts/general/mirofish_relay.py add <alias> --email <email>
python3 scripts/general/mirofish_relay.py list
python3 scripts/general/mirofish_relay.py status <alias>
python3 scripts/general/mirofish_relay.py status <alias> --probe
python3 scripts/general/mirofish_relay.py models <alias>
python3 scripts/general/mirofish_relay.py models <alias> --scan
python3 scripts/general/mirofish_relay.py remove <alias>
python3 scripts/general/mirofish_relay.py serve --host 127.0.0.1 --port 8787
```

Run with Docker:

```bash
cd deploy/mirofish-relay
cp .env.example .env
# Set MIROFISH_MASTER_KEY to at least 16 characters.
docker compose up -d --build
docker compose logs -f mirofish-relay
```

## Development guidance

- Preserve compatibility with Python 3.12 and the standard library unless dependency management is intentionally introduced.
- Never log, expose, commit, or place access tokens, refresh tokens, proxy keys, verification codes, or `MIROFISH_MASTER_KEY` in source files.
- Keep upstream caller authorization isolated. The relay must use credentials selected from its own credential store.
- Validate aliases, email addresses, verification codes, request sizes, and JSON payloads at trust boundaries.
- Maintain both credential backends when changing credential persistence.
- Keep SQLite limited to metadata. Credentials belong in Keychain or the encrypted file vault.
- Treat probe and model-scan operations as billable upstream requests and document that behavior.
- Proxy subscription URLs and node credentials must not enter source control; SQLite stores only proxy metadata and account-to-node IDs. Docker writes the sidecar's runtime config to its private volume.
- Preserve the explicit-account, default-account, then round-robin selection order.
- When changing API behavior, update the embedded WebUI and `deploy/mirofish-relay/README.md` together.
- Bind locally by default. Any public exposure requires separate reverse-proxy authentication and transport security.

## Validation

At minimum, syntax-check the main module after changes:

```bash
python3 -m py_compile scripts/general/mirofish_relay.py
```

For container-related changes, also build the deployment:

```bash
docker compose -f deploy/mirofish-relay/docker-compose.yml build
```

Avoid live login, probe, scan, or model requests during routine validation unless explicitly requested, because they contact upstream services and probe operations may consume account quota.
