from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from .config import ConfigError, load_config, resolve_config_path
from .discovery import create_host_source
from .lifecycle import (
    LifecycleError,
    UserServiceManager,
    initialize_config,
    user_config_path,
    user_unit_path,
)
from .probe import create_probe
from .service import MonitorService, StateStore
from .web import MonitorHttpServer


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="mocop: AI-native GPU cluster monitor over OpenSSH."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "JSON config path (otherwise use MOCOP_CONFIG, the user config "
            "directory, ./config/mocop.json, or the bundled safe default)"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="collect one snapshot, write it as JSON, and exit",
    )
    commands = parser.add_subparsers(dest="command")

    init_parser = commands.add_parser(
        "init", help="create a safe user configuration without overwriting one"
    )
    init_parser.add_argument(
        "--config", type=Path, default=None, help="configuration path to create"
    )
    init_parser.add_argument(
        "--host",
        dest="hosts",
        action="append",
        default=[],
        metavar="SSH_ALIAS",
        help="SSH host alias to monitor; repeat for multiple servers",
    )

    service_parser = commands.add_parser(
        "service", help="manage the user-level systemd service"
    )
    service_actions = service_parser.add_subparsers(dest="action", required=True)
    for action in ("install", "status", "uninstall"):
        action_parser = service_actions.add_parser(action)
        action_parser.add_argument(
            "--config",
            type=Path,
            default=None,
            help="configuration used by the service",
        )
    return parser.parse_args(argv)


def _run_monitor(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    state = StateStore(
        config.poll_interval_seconds,
        config.thresholds,
        history_points=config.history_points,
        incident_history_points=config.incident_history_points,
        collection_stale_cycles=config.collection_stale_cycles,
    )
    monitor = MonitorService(
        config=config,
        host_source=create_host_source("openssh-config"),
        probe=create_probe("openssh-linux-v2"),
        state=state,
    )
    if args.once:
        import json

        monitor.poll_once()
        print(json.dumps(state.snapshot(), ensure_ascii=False, indent=2))
        return 0

    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        server = MonitorHttpServer((config.listen_host, config.listen_port), state)
    except OSError as exc:
        print(
            f"Cannot listen on {config.listen_host}:{config.listen_port}: {exc}",
            file=sys.stderr,
        )
        return 1
    server.timeout = 0.5

    collector = threading.Thread(
        target=monitor.run,
        args=(stop_event,),
        name="mocop-collector",
        daemon=True,
    )
    collector.start()
    print(f"Configuration: {config_path}")
    print(f"Mocop: http://{config.listen_host}:{config.listen_port}")
    try:
        while not stop_event.is_set():
            server.handle_request()
    finally:
        stop_event.set()
        server.server_close()
        collector.join(timeout=config.probe_timeout_seconds + 1)
    return 0


def _run_lifecycle(args: argparse.Namespace) -> int:
    if args.command == "init":
        path = (args.config or user_config_path()).expanduser().resolve()
        created = initialize_config(path, args.hosts)
        print(f"Created configuration: {created}")
        if not args.hosts:
            print("Add SSH host aliases to the hosts list before starting mocop.")
        print("Next: mocop service install")
        return 0

    config_path = (args.config or user_config_path()).expanduser().resolve()
    manager = UserServiceManager(
        config_path=config_path,
        unit_path=user_unit_path(),
        python_executable=Path(sys.executable),
    )
    if args.action == "install":
        manager.install()
        print(f"Installed and started {manager.unit_path}")
        return 0
    if args.action == "status":
        return manager.status()
    manager.uninstall()
    print(f"Stopped and removed {manager.unit_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.command is None:
        return _run_monitor(args)
    try:
        return _run_lifecycle(args)
    except LifecycleError as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
