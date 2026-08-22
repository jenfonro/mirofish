"""CLI: account management, sidecar config generation, and the relay server.

Subcommands match the legacy single-file relay so existing docs and muscle
memory keep working: add / list / status / models / remove / mihomo-config /
serve.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import pathlib
import sys
from typing import Any

from .accounts import public_status
from .api import create_app
from .api.state import AppState
from .config import DEFAULT_DATA_DIR, Settings
from .errors import RelayError
from .mihomo_config import write_mihomo_config


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mirofish",
                                     description="本机多账号 Mirofish Anthropic-compatible 中转")
    parser.add_argument("--data-dir", type=pathlib.Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--timeout", type=float, default=30.0)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="登录并持久化一个账号")
    add.add_argument("alias")
    add.add_argument("--email")
    commands.add_parser("list", help="列出本地账号状态")
    status = commands.add_parser("status", help="刷新账号套餐和配额状态")
    status.add_argument("alias")
    status.add_argument("--probe", action="store_true",
                        help="发送一次 1-token 探测，会产生模型调用")
    models = commands.add_parser("models", help="探测 relay 支持的模型列表")
    models.add_argument("alias")
    models.add_argument("--scan", action="store_true",
                        help="额外用 1-token 探测候选模型名；会产生少量模型调用费用")
    models.add_argument("--max-scan", type=int, default=0,
                        help="--scan 时最多探测的候选模型数（默认全部）")
    remove = commands.add_parser("remove", help="删除本地账号及凭证")
    remove.add_argument("alias")
    mihomo = commands.add_parser("mihomo-config", help="生成 Docker Mihomo sidecar 配置")
    mihomo.add_argument("--output", type=pathlib.Path, required=True)
    serve = commands.add_parser("serve", help="启动仅监听 localhost 的中转")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--default-account",
                       default=os.environ.get("MIROFISH_DEFAULT_ACCOUNT"))
    serve.add_argument("--proxy-key")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def _cmd_add(state: AppState, alias: str, email: str) -> None:
    proxy, _ = await state.with_pending_proxy(
        alias, lambda url: state.accounts.start_login(alias, email, proxy_url=url))
    print("验证码已发送。")
    code = getpass.getpass("输入 6 位验证码（不会回显）：").strip()
    result = await state.with_fixed_proxy(
        alias, proxy,
        lambda url: state.accounts.finish_login(
            alias, email, code, proxy_url=url,
            proxy_id=str(proxy["id"]) if proxy and proxy.get("id") else None))
    _print(result)


async def _cmd_status(state: AppState, alias: str, probe: bool) -> None:
    result = await state.with_proxy(
        alias, lambda url: state.accounts.fetch_status(alias, probe, proxy_url=url))
    result["proxy"] = state.pool.account_public(alias)
    _print(result)


async def _cmd_models(state: AppState, alias: str, scan: bool, max_scan: int) -> None:
    result = await state.with_proxy(
        alias, lambda url: state.accounts.model_list(alias, proxy_url=url))
    if scan:
        result["probe_scan"] = await state.with_proxy(
            alias, lambda url: state.accounts.scan_models(alias, max_scan, proxy_url=url))
    _print(result)


def _run(coroutine) -> None:
    asyncio.run(coroutine)


def main() -> int:
    args = make_parser().parse_args()
    try:
        settings = Settings.from_env()
        settings.data_dir = args.data_dir
        settings.timeout = args.timeout
        if args.command == "mihomo-config":
            write_mihomo_config(args.output, settings)
            print("已生成 Mihomo 配置：" + str(args.output))
            return 0
        if args.command == "serve" and args.default_account:
            settings.default_account = args.default_account
        state = AppState(settings,
                         proxy_key=getattr(args, "proxy_key", None)
                         if args.command == "serve" else None)
        try:
            if args.command == "add":
                _run(_cmd_add(state, args.alias, args.email or input("邮箱：")))
            elif args.command == "list":
                _print({"accounts": [public_status(state.store.row(alias),
                                                   proxy=state.pool.account_public(alias))
                                     for alias in state.store.aliases()]})
            elif args.command == "status":
                _run(_cmd_status(state, args.alias, args.probe))
            elif args.command == "models":
                _run(_cmd_models(state, args.alias, args.scan, args.max_scan))
            elif args.command == "remove":
                if input("确认删除本地账号和凭证？输入 DELETE：") == "DELETE":
                    state.store.remove(args.alias)
                    print("已删除本地账号：" + args.alias)
            elif args.command == "serve":
                if state.default_account:
                    state.store.row(state.default_account)
                import uvicorn
                app = create_app(state)
                print(f"中转地址：http://{args.host}:{args.port}")
                print("本地代理密钥（仅显示一次）：" + state.proxy_key)
                print("账号选择头：X-Mirofish-Account")
                uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        finally:
            if args.command != "serve":
                _run(state.aclose())
        return 0
    except RelayError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
