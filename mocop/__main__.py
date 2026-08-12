from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from .config import ConfigError, load_config, resolve_config_path
from .discovery import create_host_source
from .doctor import run_doctor
from .inventory import ConfigInventory
from .lifecycle import (
    LifecycleError,
    UserServiceManager,
    initialize_config,
    user_config_path,
    user_unit_path,
)
from .notifications import (
    DisabledNotificationSink,
    NotificationError,
    create_notification_sink,
)
from .persistence import (
    DisabledPersistence,
    PersistenceError,
    create_persistence,
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
    parser.add_argument(
        "--managed-service",
        action="store_true",
        help=argparse.SUPPRESS,
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

    doctor_parser = commands.add_parser(
        "doctor",
        help="diagnose SSH reachability and connection reuse for monitored aliases",
    )
    doctor_parser.add_argument(
        "--config", type=Path, default=None, help="configuration path to diagnose"
    )
    doctor_parser.add_argument(
        "--host",
        dest="hosts",
        action="append",
        default=[],
        metavar="SSH_ALIAS",
        help="limit the diagnosis to this monitored alias; repeat for multiple",
    )
    doctor_parser.add_argument(
        "--no-connect",
        action="store_true",
        help="inspect configuration only; skip live connection tests",
    )
    doctor_parser.add_argument(
        "--profile",
        action="store_true",
        help=(
            "decompose collection latency per alias into transport, fixed "
            "script, and NVIDIA query stages"
        ),
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="write a machine-readable report"
    )
    return parser.parse_args(argv)


def _run_monitor(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    persistence = DisabledPersistence()
    restored = persistence.load(config.history_points, config.incident_history_points)
    if not args.once:
        try:
            persistence = create_persistence(config.persistence)
            restored = persistence.load(
                config.history_points, config.incident_history_points
            )
        except PersistenceError as exc:
            persistence.close()
            print(f"Persistence error: {exc}", file=sys.stderr)
            return 2

    notifications = DisabledNotificationSink()
    if not args.once:
        try:
            notifications = create_notification_sink(config.webhooks)
        except NotificationError as exc:
            persistence.close()
            print(f"Notification error: {exc}", file=sys.stderr)
            return 2

    state = StateStore(
        config.poll_interval_seconds,
        config.thresholds,
        history_points=config.history_points,
        incident_history_points=config.incident_history_points,
        collection_stale_cycles=config.collection_stale_cycles,
        expected_gpu_counts=config.expected_gpu_counts,
        incidents=config.incidents,
        incident_actions=config.incident_actions,
        host_incident_overrides=config.host_incident_overrides,
        group_incident_overrides=config.group_incident_overrides,
        maintenance_windows=config.maintenance_windows,
        host_groups=config.host_groups,
        host_display_names=config.host_display_names(),
        persistence=persistence,
        restored=restored,
        topology=config.topology,
        notifications=notifications,
    )
    host_source = create_host_source("openssh-config")
    monitor = MonitorService(
        config=config,
        host_source=host_source,
        probe=create_probe("openssh-linux-v6"),
        state=state,
    )
    if args.once:
        import json

        monitor.poll_once()
        print(json.dumps(state.snapshot(), ensure_ascii=False, indent=2))
        return 0

    stop_event = threading.Event()
    restart_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        inventory = ConfigInventory(config_path, host_source, monitor.update_config)
        server = MonitorHttpServer(
            (config.listen_host, config.listen_port),
            state,
            inventory,
            restart_event.set if args.managed_service else None,
            monitor,
            trusted_hosts=config.trusted_web_hosts,
        )
    except OSError as exc:
        print(
            f"Cannot listen on {config.listen_host}:{config.listen_port}: {exc}",
            file=sys.stderr,
        )
        persistence.close()
        notifications.close()
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
    collector_failed = False
    try:
        while not stop_event.is_set() and not restart_event.is_set():
            server.handle_request()
            if (
                not collector.is_alive()
                and not stop_event.is_set()
                and not restart_event.is_set()
            ):
                print("Collector scheduler stopped unexpectedly", file=sys.stderr)
                collector_failed = True
                break
    finally:
        stop_event.set()
        monitor.stop()
        server.server_close()
        collector.join(timeout=monitor.shutdown_timeout_seconds())
        if collector.is_alive():
            print(
                "Collector thread did not stop within the shutdown budget; "
                "flushing persistence and notifications regardless",
                file=sys.stderr,
            )
        persistence.close()
        notifications.close()
    if restart_event.is_set():
        return 75  # EX_TEMPFAIL: systemd Restart=on-failure starts the new process.
    return 1 if collector_failed else 0


def _run_doctor(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return run_doctor(
        config,
        host_filter=tuple(args.hosts),
        probe_connection=not args.no_connect,
        profile=args.profile,
        as_json=args.json,
    )


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
    if args.command == "doctor":
        return _run_doctor(args)
    try:
        return _run_lifecycle(args)
    except LifecycleError as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
