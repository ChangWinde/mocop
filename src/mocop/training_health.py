"""Pure training freshness checks shared by the server and independent guard."""

import json
import math
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit

POLICY_PATH = Path.home() / ".config/mocop/training-guard.json"
GUARD_STATE = Path.home() / ".local/state/mocop/training-guard.json"
DEFAULTS = {
    "enabled": False,
    "url": "http://127.0.0.1:8787",
    "service_label": "",
    "list_stale_seconds": 120,
    "worker_stale_seconds": 300,
    "browser_stale_seconds": 180,
    "guard_stale_seconds": 180,
    "startup_grace_seconds": 180,
    "confirm_checks": 2,
    "recovery_cooldown_seconds": 900,
    "notification_cooldown_seconds": 1800,
    "max_restarts_per_day": 3,
    "max_browser_opens_per_day": 3,
    "browser_required": False,
    "auto_restart_monitor": False,
    "auto_open_browser": False,
    "desktop_notifications": False,
}


def private_json(path, maximum=65536):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size > maximum
    ):
        raise ValueError("invalid private state")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected private JSON object")
    return value


def load_policy(path=POLICY_PATH):
    try:
        supplied = private_json(path, 8192)
        if supplied.keys() - DEFAULTS.keys():
            raise ValueError("unknown guard policy")
        policy = {**DEFAULTS, **supplied}
        for key, default in DEFAULTS.items():
            value = policy[key]
            if type(value) is not type(default):
                raise ValueError("invalid guard policy type")
            if type(default) is int and not 1 <= value <= (
                86400 if key.endswith("seconds") else 20
            ):
                raise ValueError("invalid guard threshold")
        parts = urlsplit(policy["url"])
        if (
            parts.scheme != "http"
            or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("guard reads only the configured loopback listener")
        _ = parts.port
        return policy
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (OSError, ValueError, UnicodeError):
        return {**DEFAULTS, "configuration_error": True}


def guard_status(path=GUARD_STATE):
    try:
        state = private_json(path)
        return {
            key: state.get(key)
            for key in (
                "checked_at",
                "state",
                "last_action",
                "last_notification_at",
                "consecutive_failures",
            )
        }
    except (OSError, ValueError, UnicodeError):
        return {}


def age_seconds(value, now):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return max(0, (now - value) / 1000)


def evaluate_health(snapshot, policy=None, *, now=None, include_guard=False):
    policy = DEFAULTS if policy is None else policy
    now = time.time() * 1000 if now is None else now
    issues = []

    def issue(code, message, severity="warning"):
        issues.append({"code": code, "message": message, "severity": severity})

    started_age = age_seconds(snapshot.get("monitor_started_at"), now)
    warming = (
        snapshot.get("sampled_at") is None
        and started_age is not None
        and started_age < policy["startup_grace_seconds"]
    )
    error = snapshot.get("error_status")
    list_age = age_seconds(snapshot.get("list_sampled_at"), now)
    worker_age = age_seconds(snapshot.get("attempted_at"), now)
    if policy.get("configuration_error"):
        issue("GUARD_POLICY_INVALID", "独立巡检配置无效，自动恢复已禁用", "critical")
    if error in (301, 302, 307, 308):
        issue("PLATFORM_MOVED", "平台地址或 API 已变化，需要核验配置", "critical")
    elif error in (401, 403):
        issue(
            "PLATFORM_AUTH_REQUIRED", "平台令牌失效或权限不足，需要重新授权", "critical"
        )
    elif error == 429:
        issue("PLATFORM_RATE_LIMITED", "平台限流中，采集器正在等待后重试")
    elif error:
        issue("PLATFORM_UNAVAILABLE", "平台暂不可用，采集器正在自动重试")
    if not warming and (list_age is None or list_age > policy["list_stale_seconds"]):
        issue("TASK_LIST_STALE", "任务列表超过同步时限，不能代表当前用卡", "critical")
    blocked = error in (301, 302, 307, 308, 401, 403, 429)
    stalled = (
        not warming
        and not blocked
        and (worker_age is None or worker_age > policy["worker_stale_seconds"])
    )
    if stalled:
        issue("COLLECTOR_STALLED", "训练采集器长时间没有开始新一轮采样", "critical")
    if snapshot.get("complete") is False and not warming:
        issue("HISTORY_INCOMPLETE", "任务历史尚未完整载入，统计仅含已读取部分")
    overview = snapshot.get("overview") or {}
    running, observed = (
        overview.get("running_jobs", 0),
        overview.get("utilization_jobs", 0),
    )
    if running > observed and not warming:
        issue(
            "GPU_METRICS_MISSING", f"{running - observed} 个运行任务缺少近 3 分钟利用率"
        )
    missing = overview.get(
        "unknown_active_card_jobs", overview.get("unknown_card_jobs", 0)
    )
    if missing and not warming:
        issue("GPU_SPECS_MISSING", f"{missing} 个活动任务尚未读到卡型或卡数")
    browser = snapshot.get("browser") or {}
    browser_age = age_seconds(browser.get("sampled_at"), now)
    required = policy["browser_required"] or missing > 0
    if required and (
        browser.get("status") != "ok"
        or browser_age is None
        or browser_age > policy["browser_stale_seconds"]
    ):
        issue(
            "BROWSER_OBSERVATION_UNAVAILABLE",
            "网页登录规格观测已中断，新任务的卡型和卡数可能缺失",
        )
    if include_guard and policy["enabled"]:
        checked_age = age_seconds((snapshot.get("guard") or {}).get("checked_at"), now)
        if checked_age is None or checked_age > policy["guard_stale_seconds"]:
            issue(
                "INDEPENDENT_GUARD_STALE",
                "独立巡检未按时更新，需要检查 Mac 守护任务",
                "critical",
            )
    level = (
        "critical"
        if any(i["severity"] == "critical" for i in issues)
        else "warning"
        if issues
        else "warming"
        if warming
        else "healthy"
    )
    return {
        "state": level,
        "checked_at": now,
        "issues": issues,
        "list_age_seconds": list_age,
        "restart_recommended": stalled,
        "browser_open_recommended": required
        and not blocked
        and browser.get("status") == "platform_tab_closed",
        "running_jobs": running,
        "jobs_with_fresh_metrics": observed,
    }
