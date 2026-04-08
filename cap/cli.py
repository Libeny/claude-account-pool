"""
CAP CLI — Claude Account Pool 命令行工具 🎩

用法:
  cap add <name>              从当前已登录的 Claude 账号导入凭证
  cap add <name> --from <dir> 从指定目录导入凭证
  cap remove <name>           删除账号
  cap list                    列出所有账号 + 用量
  cap check                   运行一次健康检查
  cap pick [--affinity NAME]  选择最佳账号
  cap switch <name>           切换当前生效的账号
  cap active                  显示当前生效的账号
  cap serve                   启动 Web UI + 守护进程
  cap serve -d                后台运行
  cap stop                    停止后台守护进程
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
    format="%(asctime)s 🎩 [CAP] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)


def cmd_add(args: argparse.Namespace) -> None:
    dest = Path(args.pool_dir) / args.name
    if dest.exists():
        print(f"❌ [CAP] 账号「{args.name}」已存在: {dest}")
        sys.exit(1)

    source = Path(args.source) if args.source else Path(CLAUDE_HOME)
    cred_file = source / ".credentials.json"

    if cred_file.is_symlink():
        cred_file = cred_file.resolve()

    if not cred_file.exists():
        print(f"❌ [CAP] 在 {source} 下未找到 .credentials.json")
        print(f"💡 [CAP] 提示: 先执行 `claude auth login` 登录，或使用 --from 指定目录")
        sys.exit(1)

    dest.mkdir(parents=True)
    shutil.copy2(str(cred_file), str(dest / "creds.json"))
    os.chmod(str(dest / "creds.json"), 0o600)

    creds = read_credentials(str(dest))
    if creds:
        expires = time.strftime("%Y-%m-%d %H:%M", time.gmtime(creds.expires_at / 1000))
        print(f"✅ [CAP] 账号「{args.name}」添加成功 (Token 过期: {expires} UTC)")
    else:
        print(f"⚠️ [CAP] 账号「{args.name}」已添加，但凭证可能无效")


def cmd_remove(args: argparse.Namespace) -> None:
    target = Path(args.pool_dir) / args.name
    if not target.exists():
        print(f"❌ [CAP] 账号「{args.name}」不存在")
        sys.exit(1)

    active = get_active_account(args.pool_dir)
    if active == args.name:
        print(f"⚠️ [CAP] 「{args.name}」是当前生效账号，将同时移除软链接")
        link = Path(CLAUDE_HOME) / ".credentials.json"
        if link.is_symlink():
            link.unlink()

    shutil.rmtree(target)
    print(f"🗑️ [CAP] 账号「{args.name}」已删除")


def cmd_list(args: argparse.Namespace) -> None:
    pool = AccountPool(args.pool_dir)
    pool.init()

    if not pool.list():
        print("📭 [CAP] 暂无账号，使用 `cap add <名称>` 添加第一个账号")
        return

    active = get_active_account(args.pool_dir)

    print()
    print("🎩 [CAP] 账号列表")
    print("─" * 70)
    for a in pool.list():
        u = a.usage
        expires_s = (a.token_expires_at / 1000 - time.time()) if a.token_expires_at else 0
        expires_h = max(0, expires_s / 3600)
        marker = " ⬅️ 当前生效" if a.name == active else ""

        status_icon = {"active": "🟢", "needs_login": "🔴", "disabled": "⚫"}.get(a.status.value, "⚪")
        line = f"  {status_icon} {a.name:15s} Token 剩余 {expires_h:.1f}h"
        if u:
            line += f"  │  5h:{u.five_hour.utilization:3d}%  7d:{u.seven_day.utilization:3d}%  S7d:{u.seven_day_sonnet.utilization:3d}%"
        line += marker
        print(line)
    print("─" * 70)
    print()


def cmd_check(args: argparse.Namespace) -> None:
    print("🔍 [CAP] 正在执行健康检查...")
    pool = AccountPool(args.pool_dir)
    pool.init()
    print("✅ [CAP] 健康检查完成")


def cmd_pick(args: argparse.Namespace) -> None:
    pool = AccountPool(args.pool_dir)
    pool.init()

    picked = pool.pick(affinity_name=args.affinity)
    if picked:
        print(f"🎯 [CAP] 推荐账号: {picked.name}")
    else:
        print("❌ [CAP] 没有可用账号", file=sys.stderr)
        sys.exit(1)


def cmd_switch(args: argparse.Namespace) -> None:
    try:
        name = switch_to(args.pool_dir, args.name)
        print(f"🔄 [CAP] 已切换到「{name}」")
    except FileNotFoundError as e:
        print(f"❌ [CAP] {e}")
        sys.exit(1)


def cmd_active(args: argparse.Namespace) -> None:
    active = get_active_account(args.pool_dir)
    if active:
        print(f"👉 [CAP] 当前生效账号: {active}")
    else:
        print("⚠️ [CAP] 没有生效账号（.credentials.json 不是软链接）")


def cmd_serve(args: argparse.Namespace) -> None:
    if args.foreground:
        from cap.web import start_server
        start_server(args.pool_dir, host=args.host, port=args.port)
    else:
        _daemonize(args)


def _daemonize(args: argparse.Namespace) -> None:
    import subprocess
    log_file = os.path.expanduser("~/.cap/cap.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    cmd = ["cap", "--pool-dir", args.pool_dir, "serve", "--foreground", "--host", args.host, "--port", str(args.port)]
    with open(log_file, "a") as f:
        proc = subprocess.Popen(
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    pid_file = os.path.expanduser("~/.cap/cap.pid")
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    # 获取本机 IP 用于显示
    display_host = args.host
    if display_host == "0.0.0.0":
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            display_host = s.getsockname()[0]
            s.close()
        except Exception:
            display_host = "127.0.0.1"

    print()
    print(f"  🎩 [CAP] 守护进程已启动！")
    print(f"     🌐 面板地址:  http://{display_host}:{args.port}")
    print(f"     📋 日志文件:  {log_file}")
    print(f"     🔢 进程 PID:  {proc.pid}")
    print(f"     🛑 停止命令:  cap stop")
    print()


def cmd_stop(args: argparse.Namespace) -> None:
    import signal
    pid_file = os.path.expanduser("~/.cap/cap.pid")
    if not os.path.exists(pid_file):
        print("⚠️ [CAP] 没有正在运行的守护进程")
        return
    try:
        pid = int(open(pid_file).read().strip())
        os.kill(pid, signal.SIGTERM)
        os.unlink(pid_file)
        print(f"🛑 [CAP] 守护进程已停止 (PID {pid})")
    except ProcessLookupError:
        os.unlink(pid_file)
        print("⚠️ [CAP] 进程已不存在，已清理 PID 文件")
    except Exception as e:
        print(f"❌ [CAP] 停止失败: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cap",
        description="🎩 CAP — Claude Account Pool · Claude 账号池管理器",
    )
    parser.add_argument("--pool-dir", default=DEFAULT_POOL_DIR, help="账号存储目录")
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="新增账号")
    p_add.add_argument("name", help="账号名称")
    p_add.add_argument("--from", dest="source", help="凭证来源目录（默认 ~/.claude）")

    p_rm = sub.add_parser("remove", help="删除账号")
    p_rm.add_argument("name", help="账号名称")

    sub.add_parser("list", help="列出所有账号和用量")
    sub.add_parser("check", help="执行一次健康检查")
    sub.add_parser("active", help="显示当前生效账号")

    p_pick = sub.add_parser("pick", help="选择最佳可用账号")
    p_pick.add_argument("--affinity", help="优先选择指定账号（KV Cache 亲和）")

    p_sw = sub.add_parser("switch", help="切换生效账号")
    p_sw.add_argument("name", help="目标账号名称")

    p_serve = sub.add_parser("serve", help="启动 Web 面板 + 守护进程（默认后台运行）")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8210)
    p_serve.add_argument("-f", "--foreground", action="store_true", help="前台运行（调试用）")

    sub.add_parser("stop", help="停止后台守护进程")

    args = parser.parse_args()

    if not args.command:
        print()
        print("  🎩 CAP — Claude Account Pool")
        print("  ─────────────────────────────")
        print("  Claude 多账号池管理器")
        print()
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
        "stop": cmd_stop,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
