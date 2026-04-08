"""
CAP CLI — Claude Account Pool 命令行工具.

用法:
  cap add <name>              从当前已登录的 Claude 账号导入凭证
  cap add <name> --from <dir> 从指定 config 目录导入凭证
  cap remove <name>           删除账号
  cap list                    列出所有账号 + 用量
  cap check                   运行一次健康检查（刷新 token + 拉取用量）
  cap pick [--affinity NAME]  选择最佳账号
  cap switch <name>           切换当前生效的账号（改 symlink）
  cap active                  显示当前生效的账号
  cap serve                   启动 Web UI + daemon
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from cap.pool import AccountPool
from cap.credentials import read_credentials
from cap.switcher import switch_to, get_active_account

DEFAULT_POOL_DIR = os.environ.get("CAP_POOL_DIR", os.path.expanduser("~/.cap/accounts"))
CLAUDE_HOME = os.path.expanduser("~/.claude")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)


def cmd_add(args: argparse.Namespace) -> None:
    """新增账号: 从 source dir 拷贝 .credentials.json 到 pool 的 creds.json."""
    dest = Path(args.pool_dir) / args.name
    if dest.exists():
        print(f"account '{args.name}' already exists at {dest}")
        sys.exit(1)

    source = Path(args.source) if args.source else Path(CLAUDE_HOME)
    cred_file = source / ".credentials.json"

    # If it's a symlink, read the actual content
    if cred_file.is_symlink():
        cred_file = cred_file.resolve()

    if not cred_file.exists():
        print(f"no .credentials.json found in {source}")
        print("hint: run `claude /login` first, or use --from <dir>")
        sys.exit(1)

    dest.mkdir(parents=True)
    shutil.copy2(str(cred_file), str(dest / "creds.json"))
    os.chmod(str(dest / "creds.json"), 0o600)

    creds = read_credentials(str(dest))
    if creds:
        expires = time.strftime("%Y-%m-%d %H:%M", time.gmtime(creds.expires_at / 1000))
        print(f"added '{args.name}' (token expires {expires} UTC)")
    else:
        print(f"added '{args.name}' (warning: credentials may be invalid)")


def cmd_remove(args: argparse.Namespace) -> None:
    target = Path(args.pool_dir) / args.name
    if not target.exists():
        print(f"account '{args.name}' not found")
        sys.exit(1)

    # Check if this is the active account
    active = get_active_account(args.pool_dir)
    if active == args.name:
        print(f"warning: '{args.name}' is currently active, removing symlink too")
        link = Path(CLAUDE_HOME) / ".credentials.json"
        if link.is_symlink():
            link.unlink()

    shutil.rmtree(target)
    print(f"removed '{args.name}'")


def cmd_list(args: argparse.Namespace) -> None:
    pool = AccountPool(args.pool_dir)
    pool.init()

    if not pool.list():
        print("no accounts. use `cap add <name>` to add one.")
        return

    active = get_active_account(args.pool_dir)

    for a in pool.list():
        u = a.usage
        expires_s = (a.token_expires_at / 1000 - time.time()) if a.token_expires_at else 0
        expires_h = max(0, expires_s / 3600)
        marker = " ← active" if a.name == active else ""

        line = f"  {a.name:15s} [{a.status.value:11s}] token:{expires_h:.1f}h"
        if u:
            line += f"  |  5h:{u.five_hour.utilization:3d}%  7d:{u.seven_day.utilization:3d}%  S7d:{u.seven_day_sonnet.utilization:3d}%"
        line += marker
        print(line)


def cmd_check(args: argparse.Namespace) -> None:
    pool = AccountPool(args.pool_dir)
    pool.init()


def cmd_pick(args: argparse.Namespace) -> None:
    pool = AccountPool(args.pool_dir)
    pool.init()

    picked = pool.pick(affinity_name=args.affinity)
    if picked:
        print(picked.name)
    else:
        print("no usable account", file=sys.stderr)
        sys.exit(1)


def cmd_switch(args: argparse.Namespace) -> None:
    """切换: 把 ~/.claude/.credentials.json symlink 指向目标账号."""
    try:
        name = switch_to(args.pool_dir, args.name)
        print(f"switched to '{name}'")
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)


def cmd_active(args: argparse.Namespace) -> None:
    active = get_active_account(args.pool_dir)
    if active:
        print(active)
    else:
        print("no active account (not a symlink)")


def cmd_serve(args: argparse.Namespace) -> None:
    from cap.web import start_server
    start_server(args.pool_dir, host=args.host, port=args.port)


def main() -> None:
    parser = argparse.ArgumentParser(prog="cap", description="Claude Account Pool")
    parser.add_argument("--pool-dir", default=DEFAULT_POOL_DIR, help="accounts directory")
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="add account")
    p_add.add_argument("name")
    p_add.add_argument("--from", dest="source", help="source config dir (default: ~/.claude)")

    p_rm = sub.add_parser("remove", help="remove account")
    p_rm.add_argument("name")

    sub.add_parser("list", help="list accounts + usage")
    sub.add_parser("check", help="run one health check")
    sub.add_parser("active", help="show active account")

    p_pick = sub.add_parser("pick", help="pick best account")
    p_pick.add_argument("--affinity", help="prefer this account if healthy")

    p_sw = sub.add_parser("switch", help="switch active account")
    p_sw.add_argument("name")

    p_serve = sub.add_parser("serve", help="start web UI + daemon")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=9210)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "add": cmd_add,
        "remove": cmd_remove,
        "list": cmd_list,
        "check": cmd_check,
        "active": cmd_active,
        "pick": cmd_pick,
        "switch": cmd_switch,
        "serve": cmd_serve,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
