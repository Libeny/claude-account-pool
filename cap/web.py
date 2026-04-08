"""
CAP Web UI — 轻量 HTTP 服务.

GET  /                  → 前端页面
GET  /api/accounts      → 账号列表 + 用量
POST /api/switch        → 切换账号 { "name": "..." }
POST /api/add/start     → 开始添加账号 { "name": "..." }，启动 claude auth login
GET  /api/add/status    → 登录进度（URL、状态）
POST /api/remove        → 删除账号 { "name": "..." }
GET  /api/active        → 当前生效账号
GET  /api/alerts        → 报警日志
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from cap.pool import AccountPool
from cap.switcher import switch_to, get_active_account
from cap.credentials import read_credentials
from cap.account_meta import extract_meta_from_claude_json, email_to_dirname, write_meta, read_meta
from cap.types import AccountMeta

logger = logging.getLogger("cap.web")

# Global state
_pool: Optional[AccountPool] = None
_pool_dir: str = ""
_alerts: list[dict] = []
_login_sessions: dict[str, dict] = {}  # name -> { status, url, error, process }
_auto_switch: bool = True


def _alert_handler(event: str, acct) -> None:
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "account": acct.name,
        "usage": {
            "five_hour": acct.usage.five_hour.utilization if acct.usage else None,
            "seven_day_sonnet": acct.usage.seven_day_sonnet.utilization if acct.usage else None,
        },
    }
    _alerts.append(entry)
    if len(_alerts) > 200:
        _alerts.pop(0)


def _daemon_loop(interval: int) -> None:
    while True:
        time.sleep(interval)
        try:
            _pool.check_all()
            # Auto-switch: if current account is overloaded, pick a better one
            if _auto_switch:
                _try_auto_switch()
        except Exception as e:
            logger.error("daemon check failed: %s", e)


def _try_auto_switch() -> None:
    """If current active account is ≥90% on any key dimension, auto-switch."""
    active_name = get_active_account(_pool_dir)
    if not active_name:
        return

    active = next((a for a in _pool.list() if a.name == active_name), None)
    if not active or not active.usage:
        return

    eff = max(active.usage.five_hour.utilization, active.usage.seven_day_sonnet.utilization)
    if eff < 90:
        return

    # Need to switch
    picked = _pool.pick()  # no affinity — we want a different account
    if not picked or picked.name == active_name:
        return

    try:
        switch_to(_pool_dir, picked.name)
        _alert_handler("auto_switch", picked)
        logger.info("auto-switched from %s to %s", active_name, picked.name)
    except Exception as e:
        logger.error("auto-switch failed: %s", e)


def _start_login(name: str, pool_dir: str) -> dict:
    """Start claude auth login in a subprocess, capture the login URL.

    Flow:
      1. spawn `claude auth login` with stdin=PIPE
      2. background thread reads stdout, captures the authorization URL
      3. frontend shows URL → user opens in browser → gets authorization code
      4. user pastes code into frontend → POST /api/add/code → writes to stdin
      5. subprocess completes → credentials created
    """
    acct_dir = Path(pool_dir) / name
    if acct_dir.exists():
        return {"status": "error", "error": f"账号「{name}」已存在"}

    acct_dir.mkdir(parents=True)

    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(acct_dir)

    claude_bin = os.environ.get("CLAUDE_EXECUTABLE", "claude")

    proc = subprocess.Popen(
        [claude_bin, "auth", "login"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    session = {
        "status": "waiting",
        "url": None,
        "error": None,
        "process": proc,
        "acct_dir": str(acct_dir),
    }
    _login_sessions[name] = session

    def _watch():
        output_lines = []
        for line in proc.stdout:
            line = line.strip()
            output_lines.append(line)
            logger.info("login[%s]: %s", name, line)

            # Capture authorization URL
            if "http" in line.lower() and session["url"] is None:
                for word in line.split():
                    if word.startswith("http"):
                        session["url"] = word
                        session["status"] = "waiting_for_code"
                        break

            # Detect "Login successful" or similar success message
            lower = line.lower()
            if "login successful" in lower or "successfully" in lower:
                session["status"] = "completing"

        proc.wait()

        # Check if credentials were created
        cred_file = acct_dir / ".credentials.json"
        if cred_file.exists():
            target = acct_dir / "creds.json"
            cred_file.rename(target)
            os.chmod(str(target), 0o600)

            # Extract meta (email etc.)
            claude_json = acct_dir / ".claude.json"
            if claude_json.exists():
                meta = extract_meta_from_claude_json(claude_json)
            else:
                meta = AccountMeta(display_name=name)

            # Duplicate check: does this email already exist in the pool?
            if meta.email:
                for existing_dir in Path(pool_dir).iterdir():
                    if not existing_dir.is_dir() or existing_dir.name == name:
                        continue
                    existing_meta = read_meta(str(existing_dir))
                    if existing_meta.email == meta.email:
                        session["status"] = "failed"
                        session["error"] = f"邮箱 {meta.email} 已在账号「{existing_dir.name}」中存在"
                        # Clean up duplicate
                        import shutil
                        shutil.rmtree(str(acct_dir), ignore_errors=True)
                        logger.warning("login[%s]: duplicate email %s (exists in %s)", name, meta.email, existing_dir.name)
                        return

            write_meta(str(acct_dir), meta)
            session["status"] = "success"
            logger.info("login[%s]: success (%s)", name, meta.email)
        else:
            session["status"] = "failed"
            session["error"] = "\n".join(output_lines[-5:]) or "登录未完成"
            # Clean up: remove empty account directory on failure
            import shutil
            shutil.rmtree(str(acct_dir), ignore_errors=True)
            logger.error("login[%s]: failed, cleaned up — %s", name, session["error"])

    t = threading.Thread(target=_watch, daemon=True)
    t.start()

    return {"status": "started", "name": name}


def _submit_login_code(name: str, code: str) -> dict:
    """Write the authorization code to the login subprocess's stdin."""
    session = _login_sessions.get(name)
    if not session:
        return {"ok": False, "error": f"没有找到「{name}」的登录会话"}

    proc = session.get("process")
    if not proc or proc.stdin is None or proc.stdin.closed:
        return {"ok": False, "error": "登录进程已结束"}

    try:
        proc.stdin.write(code.strip() + "\n")
        proc.stdin.flush()
        session["status"] = "code_submitted"
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default access logs

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/api/accounts":
            active = get_active_account(_pool_dir)
            accounts = []
            for a in _pool.list():
                u = a.usage
                accounts.append({
                    "name": a.name,
                    "status": a.status.value,
                    "active": a.name == active,
                    "email": a.meta.email,
                    "display_name": a.meta.display_name,
                    "org_name": a.meta.org_name,
                    "token_expires_at": a.token_expires_at,
                    "token_hours_left": max(0, (a.token_expires_at / 1000 - time.time()) / 3600) if a.token_expires_at else 0,
                    "usage": {
                        "five_hour": u.five_hour.utilization if u else None,
                        "five_hour_resets_at": u.five_hour.resets_at if u else None,
                        "seven_day": u.seven_day.utilization if u else None,
                        "seven_day_resets_at": u.seven_day.resets_at if u else None,
                        "seven_day_sonnet": u.seven_day_sonnet.utilization if u else None,
                        "seven_day_sonnet_resets_at": u.seven_day_sonnet.resets_at if u else None,
                    } if u else None,
                    "auto_switch": _auto_switch,
                })
            self._json_response({"accounts": accounts, "auto_switch": _auto_switch})

        elif self.path == "/api/active":
            active = get_active_account(_pool_dir)
            self._json_response({"active": active})

        elif self.path == "/api/alerts":
            self._json_response({"alerts": _alerts[-50:]})

        elif self.path.startswith("/api/add/status"):
            statuses = {}
            for name, s in _login_sessions.items():
                statuses[name] = {
                    "status": s["status"],
                    "url": s["url"],
                    "error": s["error"],
                }
            self._json_response({"sessions": statuses})

        elif self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_path = Path(__file__).parent / "dashboard.html"
            self.wfile.write(html_path.read_bytes())

        else:
            self.send_error(404)

    def do_POST(self) -> None:
        global _auto_switch
        if self.path == "/api/switch":
            body = self._read_body()
            name = body.get("name", "")
            try:
                switch_to(_pool_dir, name)
                self._json_response({"ok": True, "switched_to": name})
            except FileNotFoundError as e:
                self._json_response({"ok": False, "error": str(e)}, 400)

        elif self.path == "/api/add/start":
            body = self._read_body()
            name = body.get("name", "").strip()
            if not name:
                self._json_response({"ok": False, "error": "请输入账号名称"}, 400)
                return
            result = _start_login(name, _pool_dir)
            self._json_response(result)

        elif self.path == "/api/add/code":
            body = self._read_body()
            name = body.get("name", "").strip()
            code = body.get("code", "").strip()
            if not name or not code:
                self._json_response({"ok": False, "error": "缺少参数"}, 400)
                return
            result = _submit_login_code(name, code)
            self._json_response(result)

        elif self.path == "/api/remove":
            body = self._read_body()
            name = body.get("name", "")
            if not name:
                self._json_response({"ok": False, "error": "缺少账号名称"}, 400)
                return
            target = Path(_pool_dir) / name
            if not target.exists():
                self._json_response({"ok": False, "error": f"账号「{name}」不存在"}, 404)
                return
            # Don't allow removing the last active account
            active = get_active_account(_pool_dir)
            if active == name:
                others = [p for p in Path(_pool_dir).iterdir() if p.is_dir() and p.name != name and not p.name.startswith(".")]
                if not others:
                    self._json_response({"ok": False, "error": "不能删除唯一的活跃账号"}, 400)
                    return
                link = Path.home() / ".claude" / ".credentials.json"
                if link.is_symlink():
                    link.unlink()
            import shutil
            shutil.rmtree(str(target), ignore_errors=True)
            self._json_response({"ok": True, "removed": name})

        elif self.path == "/api/check":
            _pool.check_all()
            if _auto_switch:
                _try_auto_switch()
            self._json_response({"ok": True})

        elif self.path == "/api/config":
            body = self._read_body()
            if "auto_switch" in body:
                _auto_switch = bool(body["auto_switch"])
                logger.info("auto_switch set to %s", _auto_switch)
            self._json_response({"ok": True, "auto_switch": _auto_switch})

        else:
            self.send_error(404)


def start_server(pool_dir: str, host: str = "0.0.0.0", port: int = 8210) -> None:
    global _pool, _pool_dir
    _pool_dir = pool_dir

    _pool = AccountPool(pool_dir, on_alert=_alert_handler)
    _pool.init()  # bootstrap_local happens inside init()

    # Start daemon thread
    t = threading.Thread(target=_daemon_loop, args=(20 * 60,), daemon=True)
    t.start()

    server = HTTPServer((host, port), Handler)
    print(f"CAP Web UI: http://{host}:{port}")
    print(f"Accounts dir: {pool_dir}")
    print(f"Daemon: checking every 20 min")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        server.server_close()
