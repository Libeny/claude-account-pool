"""
Account pool: health check + selection strategy.

Health check (every 20 min):
  - accessToken 距离过期 < 1h → refresh
  - refresh 失败 → mark needs_login
  - fetch usage (3 dimensions)

Selection strategy (Sonnet workload):
  三档漏斗:
    healthy:   7d_sonnet < 90% 且 5h < 90%
    week_ok:   7d_sonnet < 90% 但 5h ≥ 90%（短期忙，长期有钱）
    exhausted: 7d_sonnet ≥ 90%（周废）

  全军覆没时: 取 5h 最低的 exhausted 账号 + 触发报警
"""

from __future__ import annotations

import json
import os
import shutil
import time
import logging
from pathlib import Path
from typing import Callable, Optional

from cap.types import AccountState, AccountStatus, CredentialsFile, UsageSnapshot
from cap.credentials import read_credentials, write_credentials, sync_active_to_keychain, read_keychain
from cap.token_refresher import refresh_token, RefreshOk
from cap.usage_monitor import fetch_usage
from cap.account_meta import read_meta, write_meta, extract_meta_from_claude_json, email_to_dirname
from cap.switcher import get_active_account, switch_to, CRED_LINK, CLAUDE_HOME

logger = logging.getLogger("cap")

REFRESH_THRESHOLD_S = 3600  # 1 hour
CHECK_INTERVAL_S = 20 * 60  # 20 minutes
THRESHOLD_WARN = 80
THRESHOLD_SWITCH = 90


def _effective_load(usage: Optional[UsageSnapshot]) -> int:
    """Sonnet workload: bottleneck = max(5h, 7d_sonnet)."""
    if not usage:
        return 0
    return max(usage.five_hour.utilization, usage.seven_day_sonnet.utilization)


class AccountPool:
    def __init__(
        self,
        accounts_dir: str,
        check_interval_s: int = CHECK_INTERVAL_S,
        on_alert: Optional[Callable[[str, AccountState], None]] = None,
    ):
        self.accounts_dir = accounts_dir
        self.check_interval_s = check_interval_s
        self.on_alert = on_alert or self._default_alert
        self._accounts: list[AccountState] = []

    # ── Lifecycle ──

    def init(self) -> None:
        self._bootstrap_local()
        self._scan()
        names = ", ".join(a.name for a in self._accounts) or "无"
        logger.info("📦 已加载 %d 个账号: %s", len(self._accounts), names)
        self.check_all()

    def _bootstrap_local(self) -> None:
        """首次启动: 把本地 ~/.claude/.credentials.json 迁移到账号池。"""
        pool_path = Path(self.accounts_dir)
        pool_path.mkdir(parents=True, exist_ok=True)

        # 已有包含 creds.json 的账号 → 不重复导入
        existing = [
            p for p in pool_path.iterdir()
            if p.is_dir() and not p.name.startswith(".") and (p / "creds.json").exists()
        ]
        if existing:
            logger.info("⏭️ 账号池已有 %d 个账号，跳过自动导入", len(existing))
            return

        # 清理之前失败留下的空目录
        for p in pool_path.iterdir():
            if p.is_dir() and not p.name.startswith(".") and not (p / "creds.json").exists():
                logger.info("🧹 清理空目录: %s", p.name)
                shutil.rmtree(str(p), ignore_errors=True)

        # 已经是 symlink → 说明已经迁移过
        if CRED_LINK.is_symlink():
            logger.info("⏭️ .credentials.json 已是软链接，跳过自动导入")
            return

        logger.info("🔍 开始自动导入本地 Claude 账号...")
        logger.info("   📄 凭证路径: %s (存在: %s)", CRED_LINK, CRED_LINK.exists())
        logger.info("   📄 配置路径: %s (存在: %s)", CLAUDE_HOME / ".claude.json",
                     (Path.home() / ".claude.json").exists())

        # 提取 meta 获取邮箱
        meta = extract_meta_from_claude_json()
        dir_name = email_to_dirname(meta.email) if meta.email else "local"
        logger.info("   👤 邮箱: %s → 目录名: %s", meta.email or "未知", dir_name)

        acct_dir = pool_path / dir_name
        acct_dir.mkdir(parents=True, exist_ok=True)

        # 读凭证: 直接读 ~/.claude/.credentials.json，macOS fallback 到 Keychain
        creds: CredentialsFile | None = None
        if CRED_LINK.exists():
            try:
                raw = json.loads(CRED_LINK.read_text())
                oauth = raw.get("claudeAiOauth", {})
                if oauth.get("accessToken") and oauth.get("refreshToken"):
                    creds = CredentialsFile(
                        access_token=oauth["accessToken"],
                        refresh_token=oauth["refreshToken"],
                        expires_at=int(oauth.get("expiresAt", 0)),
                        scopes=oauth.get("scopes", []),
                    )
                    logger.info("   🔑 从文件读取凭证成功")
                else:
                    logger.warning("   ⚠️ 凭证文件存在但缺少 accessToken 或 refreshToken")
            except Exception as e:
                logger.warning("   ⚠️ 读取凭证文件失败: %s", e)

        if not creds:
            creds = read_keychain()
            if creds:
                logger.info("   🔑 从 Keychain 读取凭证成功")

        if not creds:
            logger.warning("❌ 未发现本地凭证，跳过自动导入")
            shutil.rmtree(str(acct_dir), ignore_errors=True)
            return

        # 写 creds.json 到账号目录
        write_credentials(str(acct_dir), creds)

        # 保存 meta
        write_meta(str(acct_dir), meta)

        # 替换原文件为 symlink
        if CRED_LINK.exists():
            CRED_LINK.unlink()
        CRED_LINK.symlink_to((acct_dir / "creds.json").resolve())

        # macOS: 保持 Keychain 一致
        sync_active_to_keychain(creds)

        logger.info("🎉 自动导入完成！账号「%s」(%s) 已加入池中", dir_name, meta.email or "未知邮箱")
        logger.info("   📁 %s → %s", CRED_LINK, (acct_dir / "creds.json").resolve())

    def check_all(self) -> None:
        self._scan()
        for acct in self._accounts:
            try:
                self._check_one(acct)
            except Exception as e:
                logger.error("❌ %s: 检查异常: %s", acct.name, e)

        self._log_summary()

    # ── Selection ──

    def pick(self, affinity_name: Optional[str] = None) -> Optional[AccountState]:
        active = [a for a in self._accounts if a.status == AccountStatus.ACTIVE]
        if not active:
            return None

        healthy: list[AccountState] = []
        week_ok: list[AccountState] = []
        exhausted: list[AccountState] = []

        for a in active:
            s7d = a.usage.seven_day_sonnet.utilization if a.usage else 0
            h5 = a.usage.five_hour.utilization if a.usage else 0

            if s7d >= THRESHOLD_SWITCH:
                exhausted.append(a)
            elif h5 >= THRESHOLD_SWITCH:
                week_ok.append(a)
            else:
                healthy.append(a)

        if affinity_name:
            for a in healthy:
                if a.name == affinity_name:
                    return a

        if healthy:
            return min(healthy, key=lambda a: _effective_load(a.usage))

        if week_ok:
            return min(week_ok, key=lambda a: a.usage.five_hour.utilization if a.usage else 0)

        if exhausted:
            best = min(exhausted, key=lambda a: a.usage.five_hour.utilization if a.usage else 0)
            self.on_alert("all_exhausted", best)
            return best

        return None

    def list(self) -> list[AccountState]:
        return list(self._accounts)

    # ── Health check (single account) ──

    def _check_one(self, acct: AccountState) -> None:
        creds = read_credentials(acct.config_dir)
        if not creds:
            acct.status = AccountStatus.NEEDS_LOGIN
            logger.warning("🔴 %s: 未找到凭证", acct.name)
            return

        acct.token_expires_at = creds.expires_at

        # Token 续期: 距过期不足 1 小时时刷新
        time_to_expiry_s = (creds.expires_at - time.time() * 1000) / 1000
        if time_to_expiry_s < REFRESH_THRESHOLD_S:
            logger.info("🔄 %s: Token 还剩 %d 分钟，正在刷新...", acct.name, int(time_to_expiry_s / 60))
            result = refresh_token(creds)
            if isinstance(result, RefreshOk):
                write_credentials(acct.config_dir, result.credentials)
                active_name = get_active_account(self.accounts_dir)
                if acct.name == active_name:
                    sync_active_to_keychain(result.credentials)
                acct.token_expires_at = result.credentials.expires_at
                acct.last_refresh_at = time.time()
                logger.info("✅ %s: Token 已续期，新过期时间 %s UTC", acct.name, time.strftime("%Y-%m-%d %H:%M", time.gmtime(acct.token_expires_at / 1000)))
                creds = result.credentials
            else:
                acct.status = AccountStatus.NEEDS_LOGIN
                self.on_alert("refresh_failed", acct)
                logger.error("❌ %s: Token 刷新失败 — %s", acct.name, result.error)
                return

        # 拉取用量
        usage = fetch_usage(creds.access_token)
        if usage:
            acct.previous_usage = acct.usage
            acct.usage = usage

            eff = _effective_load(usage)
            if THRESHOLD_WARN <= eff < THRESHOLD_SWITCH:
                self.on_alert("usage_warning", acct)

        if acct.status != AccountStatus.DISABLED:
            acct.status = AccountStatus.ACTIVE

    # ── Internals ──

    def _scan(self) -> None:
        d = Path(self.accounts_dir)
        d.mkdir(parents=True, exist_ok=True)

        dirs = sorted(
            p.name for p in d.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

        existing = {a.name: a for a in self._accounts}
        self._accounts = []
        for name in dirs:
            if name in existing:
                self._accounts.append(existing[name])
            else:
                acct = AccountState(name=name, config_dir=str(d / name))
                acct.meta = read_meta(acct.config_dir)
                self._accounts.append(acct)

    def _log_summary(self) -> None:
        parts = []
        for a in self._accounts:
            u = a.usage
            icon = {"active": "🟢", "needs_login": "🔴", "disabled": "⚫"}.get(a.status.value, "⚪")
            if u:
                parts.append(
                    f"{icon} {a.name} "
                    f"5h:{u.five_hour.utilization}% "
                    f"7d:{u.seven_day.utilization}% "
                    f"S7d:{u.seven_day_sonnet.utilization}%"
                )
            else:
                parts.append(f"{icon} {a.name} 暂无用量")
        logger.info("📊 健康检查: %s", " │ ".join(parts))

    @staticmethod
    def _default_alert(event: str, acct: AccountState) -> None:
        icons = {
            "all_exhausted": "🚨",
            "refresh_failed": "❌",
            "usage_warning": "⚠️",
            "auto_switch": "🔄",
        }
        icon = icons.get(event, "⚠️")
        usage_str = (f"5h:{acct.usage.five_hour.utilization}% S7d:{acct.usage.seven_day_sonnet.utilization}%"
                     if acct.usage else "无数据")
        logger.warning("%s [CAP 报警] %s — 账号: %s — 用量: %s", icon, event, acct.name, usage_str)
