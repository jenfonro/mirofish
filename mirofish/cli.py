"""CLI: account management, the relay server.

Subcommands match the legacy single-file relay so existing docs and muscle
memory keep working: add / list / status / models / remove /
serve.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import pathlib
import sys
from typing import Any

from .accounts import public_status
from .api import create_app
from .api.state import AppState
from .config import DEFAULT_DATA_DIR, Settings
from .errors import RelayError
from .proxy import DIRECT


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
    status = commands.add_parser("status", help="仅刷新账号套餐资料")
    status.add_argument("alias")
    status.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    models = commands.add_parser("models", help="读取账号模型目录，不发送模型调用")
    models.add_argument("alias")
    remove = commands.add_parser("remove", help="删除本地账号及凭证")
    remove.add_argument("alias")
    serve = commands.add_parser("serve", help="启动仅监听 localhost 的中转")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--default-account",
                       default=os.environ.get("MIROFISH_DEFAULT_ACCOUNT"))
    serve.add_argument("--proxy-key")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _proxy_key_notice(state: AppState) -> str:
    """Safe startup notice: never place the bearer secret in process logs."""
    return ("本地代理密钥已加载；完整值不会写入日志。默认密钥文件："
            + str(state.store.proxy_key_path))


async def _cmd_add(state: AppState, alias: str, email: str) -> None:
    try:
        proxy_id = state.store.row(alias)["proxy_id"]
    except RelayError as exc:
        if exc.status != 404:
            raise
        proxy_id = DIRECT
    proxy = state.pool.by_id(proxy_id)
    await state.with_fixed_proxy(
        alias, proxy, lambda url: state.accounts.start_login(alias, email, proxy_url=url))
    print("验证码已发送。")
    code = getpass.getpass("输入 6 位验证码（不会回显）：").strip()
    result = await state.with_fixed_proxy(
        alias, proxy,
        lambda url: state.accounts.finish_login(
            alias, email, code, proxy_url=url,
            proxy_id=proxy_id))
    state.reset_account_runtime(alias)
    for kind in ("profile_refusal", "limits_refusal"):
        refusal = result.pop(kind, None)
        if refusal is not None:
            status, body = refusal
            state.note_account_error(alias, RelayError(kind, status, body))
            result[kind.replace("refusal", "error")] = {"status": status}
    result.update(public_status(state.store.row(alias), proxy=state.pool.account_public(alias)))
    _print(result)


def _cmd_remove(state: AppState, alias: str) -> None:
    state.remove_account(alias)
    print("已删除本地账号：" + alias)


async def _cmd_status(state: AppState, alias: str, probe: bool) -> None:
    try:
        result = await state.with_proxy(
            alias, lambda url: state.accounts.fetch_status(alias, proxy_url=url))
    except RelayError as exc:
        state.note_account_error(alias, exc)
        raise
    result["proxy"] = state.pool.account_public(alias)
    _print(result)


async def _cmd_models(state: AppState, alias: str) -> None:
    result = await state.with_proxy(
        alias, lambda url: state.accounts.model_list(alias, proxy_url=url))
    _print(result)


def _run(coroutine) -> None:
    asyncio.run(coroutine)


def _configure_logging() -> None:
    """Give the package's own logger a handler, at INFO.

    Nothing else installs one: uvicorn configures only its own loggers, so a
    ``logger.info`` call anywhere in ``mirofish.*`` used to reach just
    logging's last-resort handler — which starts at WARNING — and be dropped.
    That is why operationally useful lines had to be raised to WARNING to show
    up in ``docker logs`` at all.

    Root keeps WARNING so httpx and httpcore do not narrate every upstream
    request, and an operator who already configured logging keeps their
    handlers: only the package level is set unconditionally, because that is
    the part this relay depends on.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.WARNING)
    logging.getLogger("mirofish").setLevel(logging.INFO)


def main() -> int:
    args = make_parser().parse_args()
    try:
        settings = Settings.from_env()
        settings.data_dir = args.data_dir
        settings.timeout = args.timeout
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
                _run(_cmd_models(state, args.alias))
            elif args.command == "remove":
                if input("确认删除本地账号和凭证？输入 DELETE：") == "DELETE":
                    _cmd_remove(state, args.alias)
            elif args.command == "serve":
                if state.default_account:
                    state.store.row(state.default_account)
                import uvicorn
                app = create_app(state)
                _configure_logging()
                print(f"中转地址：http://{args.host}:{args.port}")
                print(_proxy_key_notice(state))
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
