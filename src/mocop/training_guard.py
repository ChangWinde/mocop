"""Standalone, once-per-minute loopback watchdog with bounded Mac recovery."""

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if __package__:
    from .platform_origin import read_origin
    from .training_health import (
        GUARD_STATE,
        POLICY_PATH,
        evaluate_health,
        load_policy,
        private_json,
    )
else:
    from platform_origin import read_origin
    from training_health import (
        GUARD_STATE,
        POLICY_PATH,
        evaluate_health,
        load_policy,
        private_json,
    )

TOKEN_PATH = Path.home() / ".config/mocop/access-token"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_snapshot(policy, token_path=TOKEN_PATH):
    try:
        info = token_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 4096
        ):
            raise ValueError("private capability unavailable")
        token = token_path.read_text().strip()
    except OSError as error:
        raise ValueError("private capability unavailable") from error
    if not token or any(ord(c) <= 32 for c in token):
        raise ValueError("invalid private capability")
    request = Request(
        policy["url"].rstrip("/") + "/api/training-jobs",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    with build_opener(ProxyHandler({}), NoRedirect()).open(
        request, timeout=15
    ) as response:
        payload = response.read(16_000_001)
    if len(payload) > 16_000_000:
        raise ValueError("snapshot too large")
    snapshot = json.loads(payload)
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("jobs"), list)
        or not isinstance(snapshot.get("overview"), dict)
    ):
        raise ValueError("training snapshot schema changed")
    return snapshot


def failure_health(code, message, now, restart=False):
    return {
        "state": "critical",
        "checked_at": now,
        "issues": [{"code": code, "severity": "critical", "message": message}],
        "restart_recommended": restart,
        "browser_open_recommended": False,
    }


def assess(policy, *, now, reader=read_snapshot):
    try:
        snapshot = reader(policy)
        health = evaluate_health(snapshot, policy, now=now)
        if "health" not in snapshot:
            health["issues"].append(
                {
                    "code": "GUARD_EXTENSION_MISSING",
                    "severity": "critical",
                    "message": "训练监控扩展缺失，可能被升级覆盖，需要核验安装",
                }
            )
            health["state"] = "critical"
            health["restart_recommended"] = False
        return health
    except HTTPError as error:
        code = (
            "GUARD_EXTENSION_MISSING"
            if error.code == 404
            else "LOCAL_API_AUTH_REQUIRED"
            if error.code in (401, 403)
            else "LOCAL_API_ERROR"
        )
        return failure_health(code, "本机训练监管接口异常，需要检查服务或授权", now)
    except (ValueError, TypeError, KeyError):
        return failure_health(
            "GUARD_INPUT_INVALID", "监管配置、能力文件或返回格式异常，需要核验", now
        )
    except (OSError, TimeoutError):
        return failure_health(
            "MONITOR_UNREACHABLE",
            "本机监管服务不可达，独立巡检将按限额尝试恢复",
            now,
            restart=True,
        )


def plan_actions(previous, health, policy, *, now):
    state = dict(previous)
    state.update(
        checked_at=now, state=health["state"], issues=health["issues"], last_action=None
    )
    failures = previous.get("consecutive_failures", 0) + 1 if health["issues"] else 0
    state["consecutive_failures"] = failures
    restarts = [
        at
        for at in previous.get("restarts", [])
        if type(at) in (int, float) and 0 <= now - at < 86400000
    ]
    state["restarts"] = restarts
    actions = []
    confirmed = failures >= policy["confirm_checks"]

    def cooled(key, seconds):
        at = previous.get(key)
        return at is None or (type(at) in (int, float) and now - at >= seconds * 1000)

    if (
        confirmed
        and policy["auto_restart_monitor"]
        and health["restart_recommended"]
        and len(restarts) < policy["max_restarts_per_day"]
        and cooled("last_restart_at", policy["recovery_cooldown_seconds"])
    ):
        actions.append("restart_monitor")
        state["last_restart_at"] = now
        state["restarts"] = [*restarts, now]
    browser_opens = [
        at
        for at in previous.get("browser_opens", [])
        if type(at) in (int, float) and 0 <= now - at < 86400000
    ]
    state["browser_opens"] = browser_opens
    if (
        confirmed
        and policy["auto_open_browser"]
        and health["browser_open_recommended"]
        and len(browser_opens) < policy["max_browser_opens_per_day"]
        and cooled("last_browser_open_at", policy["recovery_cooldown_seconds"])
    ):
        actions.append("open_platform")
        state["last_browser_open_at"] = now
        state["browser_opens"] = [*browser_opens, now]
    incident = bool(previous.get("incident_active"))
    if confirmed:
        state["incident_active"] = True
        if policy["desktop_notifications"] and cooled(
            "last_notification_at", policy["notification_cooldown_seconds"]
        ):
            actions.append("notify_problem")
            state["last_notification_at"] = now
    elif not health["issues"]:
        state["incident_active"] = False
        if incident and policy["desktop_notifications"]:
            actions.append("notify_recovery")
    return state, actions


def perform(action, policy, health):
    if sys.platform != "darwin":
        return "unsupported"
    if action == "restart_monitor":
        label = policy["service_label"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{2,120}", label):
            return "invalid_service_label"
        target = f"gui/{os.getuid()}/{label}"
        result = subprocess.run(
            ["launchctl", "kill", "SIGTERM", target], capture_output=True, timeout=15
        )
        if result.returncode:
            result = subprocess.run(
                ["launchctl", "kickstart", target], capture_output=True, timeout=15
            )
        return "requested" if result.returncode == 0 else "failed"
    if action == "open_platform":
        origin = read_origin()
        if origin is None or urlsplit(origin).scheme not in ("http", "https"):
            return "origin_unavailable"
        browser = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        profile = Path.home() / "Library/Application Support/Google/Chrome"
        subprocess.Popen(
            [
                browser,
                "--profile-directory=Default",
                "--user-data-dir=" + str(profile),
                origin + "/my/jobs",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "requested"
    text = (
        "训练监管已恢复，任务与指标已重新同步。"
        if action == "notify_recovery"
        else "训练监管需要检查："
        + "；".join(i["message"] for i in health["issues"][:3])
    )
    # argv crosses into AppleScript as data; no upstream string becomes script source.
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "Mocop 训练监管"\nend run'
    result = subprocess.run(
        ["osascript", "-e", script, text], capture_output=True, timeout=15
    )
    return "accepted" if result.returncode == 0 else "failed"


def run_once(
    policy_path=POLICY_PATH,
    state_path=GUARD_STATE,
    *,
    now=None,
    reader=read_snapshot,
    executor=perform,
):
    now = time.time() * 1000 if now is None else now
    policy = load_policy(policy_path)
    try:
        previous = private_json(state_path)
    except (OSError, ValueError, UnicodeError):
        previous = {}
    if not policy["enabled"]:
        state = {
            "checked_at": now,
            "state": "disabled",
            "issues": [],
            "configuration_error": policy.get("configuration_error", False),
        }
        atomic_json(state_path, state)
        return state
    health = assess(policy, now=now, reader=reader)
    state, actions = plan_actions(previous, health, policy, now=now)
    # Persist action reservations before side effects, including failed attempts.
    atomic_json(state_path, state)
    outcomes = []
    for action in actions:
        try:
            outcome = executor(action, policy, health)
        except (OSError, subprocess.SubprocessError):
            outcome = "failed"
        outcomes.append({"action": action, "result": outcome, "at": now})
    state["last_action"] = outcomes or previous.get("last_action")
    state["events"] = [*(previous.get("events") or []), *outcomes][-20:]
    atomic_json(state_path, state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=POLICY_PATH)
    parser.add_argument("--state", type=Path, default=GUARD_STATE)
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        args.state.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        state = run_once(args.policy, args.state)
        print(
            json.dumps(
                {
                    "checked_at": state["checked_at"],
                    "state": state["state"],
                    "issues": [i["code"] for i in state["issues"]],
                    "last_action": state.get("last_action"),
                }
            )
        )


if __name__ == "__main__":
    main()
