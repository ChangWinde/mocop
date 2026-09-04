from __future__ import annotations

import argparse
import os
import secrets
import shlex
import signal
import socket
import sys
import threading
from pathlib import Path

from .config import ConfigError, load_config, load_private_config, resolve_config_path
from .discovery import OpenSshConfigHostSource
from .doctor import run_doctor
from .inventory import ConfigInventory
from .lifecycle import (
    LifecycleError,
    UserServiceManager,
    access_token_path,
    initialize_config,
    read_access_token,
    user_config_path,
    user_unit_path,
)
from .migration import migrate_config
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
from .probe import OpenSshLinuxResourceProbe
from .service import MonitorService, StateStore
from .updates import UpdateManager
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
        "--strict",
        action="store_true",
        help=(
            "with --once, exit 1 unless every configured host produced an online sample"
        ),
    )
    parser.add_argument(
        "--managed-service",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--access-token-file",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    commands = parser.add_subparsers(dest="command")

    init_parser = commands.add_parser(
        "init", help="create a safe user configuration without overwriting one"
    )
    init_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="configuration path to create",
    )
    init_parser.add_argument(
        "--host",
        dest="hosts",
        action="append",
        default=[],
        metavar="SSH_ALIAS",
        help="SSH host alias to monitor; repeat for multiple servers",
    )

    deploy_parser = commands.add_parser(
        "deploy", help="configure and start Mocop on a fresh monitoring server"
    )
    deploy_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="new configuration path; it must not already exist",
    )
    deploy_parser.add_argument(
        "--host",
        dest="hosts",
        action="append",
        default=[],
        metavar="SSH_ALIAS",
        help="explicit SSH alias to monitor; repeat for multiple servers",
    )
    deploy_identity = deploy_parser.add_mutually_exclusive_group()
    deploy_identity.add_argument("--local-host", metavar="ALIAS")
    deploy_identity.add_argument(
        "--no-local", action="store_true", help="do not monitor this server locally"
    )
    deploy_parser.add_argument("--display-name")
    deploy_parser.add_argument("--ssh-config", default="~/.ssh/config")
    deploy_admission = deploy_parser.add_mutually_exclusive_group()
    deploy_admission.add_argument(
        "--auto-discover", dest="auto_discover", action="store_true"
    )
    deploy_admission.add_argument(
        "--no-auto-discover", dest="auto_discover", action="store_false"
    )
    deploy_parser.set_defaults(auto_discover=True)

    migrate_parser = commands.add_parser(
        "migrate", help="generate a new private config from another installation"
    )
    migrate_parser.add_argument("--from-config", type=Path, required=True)
    migrate_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="new configuration path; it must not already exist",
    )
    local_identity = migrate_parser.add_mutually_exclusive_group()
    local_identity.add_argument("--local-host", metavar="ALIAS")
    local_identity.add_argument("--drop-local-host", action="store_true")
    migrate_parser.add_argument("--display-name")
    migrate_parser.add_argument("--ssh-config", default="~/.ssh/config")
    admission = migrate_parser.add_mutually_exclusive_group()
    admission.add_argument("--auto-discover", dest="auto_discover", action="store_true")
    admission.add_argument(
        "--no-auto-discover", dest="auto_discover", action="store_false"
    )
    migrate_parser.set_defaults(auto_discover=None)

    config_parser = commands.add_parser(
        "config", help="inspect the monitor configuration"
    )
    config_actions = config_parser.add_subparsers(dest="action", required=True)
    check_parser = config_actions.add_parser(
        "check",
        help=(
            "parse and validate the configuration without starting the web "
            "server or opening SSH connections"
        ),
    )
    check_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="configuration path to validate",
    )

    service_parser = commands.add_parser(
        "service", help="manage the user-level systemd service"
    )
    service_actions = service_parser.add_subparsers(dest="action", required=True)
    install_parser = service_actions.add_parser("install")
    install_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="configuration used by the service",
    )
    # status and uninstall operate on the fixed unit; they take no --config.
    service_actions.add_parser("status")
    service_actions.add_parser("uninstall")

    doctor_parser = commands.add_parser(
        "doctor",
        help="diagnose SSH reachability and connection reuse for monitored aliases",
    )
    doctor_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="configuration path to diagnose",
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
        "--probe",
        action="store_true",
        help=(
            "run one production collection per alias and report status, "
            "latency, GPU and process counts, and workload coverage"
        ),
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="write a machine-readable report"
    )
    return parser.parse_args(argv)


def _run_monitor(args: argparse.Namespace) -> int:
    if args.managed_service and (args.config is None or args.access_token_file is None):
        # The generated unit always passes both. A unit predating the
        # capability (0.8.x) must be regenerated with `mocop service install`
        # rather than silently minting a token nobody was shown.
        print(
            "Configuration error: --managed-service requires --config and "
            "--access-token-file; re-run `mocop service install`",
            file=sys.stderr,
        )
        return 2
    try:
        config_path = (
            Path(os.path.abspath(args.config.expanduser()))
            if args.managed_service
            else resolve_config_path(args.config)
        )
        config = (
            load_private_config(config_path)
            if args.managed_service
            else load_config(config_path)
        )
    except (ConfigError, RuntimeError, UnicodeError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.access_token_file is not None:
        try:
            access_token = read_access_token(args.access_token_file)
        except LifecycleError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
    else:
        # Foreground servers receive an ephemeral per-process capability. It
        # is printed only in the operator's terminal and is never persisted.
        # (--once never starts the server, so the value is simply unused.)
        access_token = secrets.token_urlsafe(32)

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
    host_source = OpenSshConfigHostSource()
    monitor = MonitorService(
        config=config,
        host_source=host_source,
        probe=OpenSshLinuxResourceProbe(),
        state=state,
    )
    if args.once:
        import json

        monitor.poll_once()
        snapshot = state.snapshot()
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        if args.strict:
            servers = snapshot.get("servers", [])
            failed = sorted(
                str(server.get("host"))
                for server in servers
                if server.get("status") != "online"
            )
            if not servers or failed:
                detail = ", ".join(failed) if failed else "no configured hosts"
                print(f"strict: not fully online: {detail}", file=sys.stderr)
                return 1
        return 0

    stop_event = threading.Event()
    restart_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    updates = UpdateManager(
        config.updates,
        restart=restart_event.set if args.managed_service else None,
    )
    try:
        inventory = ConfigInventory(config_path, host_source, monitor.update_config)
        server = MonitorHttpServer(
            (config.listen_host, config.listen_port),
            state,
            inventory,
            restart_event.set if args.managed_service else None,
            monitor,
            trusted_hosts=config.trusted_web_hosts,
            access_token=access_token,
            updates=updates,
        )
    except (OSError, ValueError, UnicodeError) as exc:
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
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    collector_failed = False
    shutdown_failed = False
    try:
        collector.start()
        updates.start()
        print(f"Configuration: {config_path}")
        dashboard_url = _http_url(config.listen_host, config.listen_port)
        if not args.managed_service:
            dashboard_url += f"#access_token={access_token}"
        print(f"Mocop: {dashboard_url}")
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
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        stop_event.set()
        updates.stop()
        monitor.stop()
        server.server_close()
        collector.join(timeout=monitor.shutdown_timeout_seconds())
        if collector.is_alive():
            print(
                "Collector thread did not stop within the shutdown budget; "
                "flushing persistence and notifications regardless",
                file=sys.stderr,
            )
            shutdown_failed = True
        persistence.close()
        notifications.close()
    if restart_event.is_set():
        return 75  # EX_TEMPFAIL: systemd Restart=on-failure starts the new process.
    return 1 if collector_failed or shutdown_failed else 0


def _run_doctor(args: argparse.Namespace) -> int:
    try:
        config_path = resolve_config_path(args.config)
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return run_doctor(
        config,
        host_filter=tuple(args.hosts),
        probe_connection=not args.no_connect,
        profile=args.profile,
        collect=args.probe,
        as_json=args.json,
    )


def _environment_state(name: str) -> str:
    """Report whether a referenced environment variable is set, never its value."""
    return "set" if os.environ.get(name) else "unset"


def _run_config_check(args: argparse.Namespace) -> int:
    """Parse and validate only: no web server, no SSH connections."""
    try:
        config_path = resolve_config_path(args.config)
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    print(f"configuration OK: {config_path}")
    local_note = f" (local: {config.local_host})" if config.local_host else ""
    print(f"hosts: {len(config.hosts)}{local_note}")
    print(
        "ssh discovery: "
        f"{config.ssh_discovery.mode} "
        f"(refresh {config.ssh_discovery.refresh_seconds}s, "
        f"resolve timeout {config.ssh_discovery.resolve_timeout_seconds:g}s)"
    )
    print(f"persistence: {'enabled' if config.persistence.enabled else 'disabled'}")
    print(f"workloads: {config.workloads.mode}")
    if config.topology is None:
        print(
            "topology: resolved from SSH at runtime"
            if config.ssh_discovery.mode == "topology"
            else "topology: none"
        )
    else:
        print(f"topology: configured ({len(config.topology.links)} links)")
    if not config.webhooks:
        print("webhooks: none")
        return 0
    print(f"webhooks: {len(config.webhooks)}")
    for webhook in config.webhooks:
        references = [
            f"url_env {webhook.url_env} ({_environment_state(webhook.url_env)})"
        ]
        if webhook.secret_env is not None:
            references.append(
                f"secret_env {webhook.secret_env} "
                f"({_environment_state(webhook.secret_env)})"
            )
        print(f"  {webhook.name}: {', '.join(references)}")
    return 0


def _install_service(config_path: Path) -> int:
    manager = UserServiceManager(
        config_path=config_path,
        unit_path=user_unit_path(),
        python_executable=Path(sys.executable),
    )
    config = manager.install()
    try:
        active = manager.wait_until_active()
        token = read_access_token(manager.access_token_path) if active else None
        healthy = (
            manager.wait_until_healthy(
                config.listen_host,
                config.listen_port,
            )
            if token is not None
            else False
        )
    except BaseException as verification_error:
        try:
            manager.rollback_install()
        except LifecycleError as rollback_error:
            raise LifecycleError(
                "service verification failed and rollback is incomplete: "
                f"{rollback_error}"
            ) from verification_error
        raise
    if not active or not healthy:
        manager.rollback_install()
        print("Service did not become healthy; the previous unit was restored")
        print("Inspect it with: systemctl --user status mocop")
        print("Logs: journalctl --user -u mocop -f")
        return 1
    assert token is not None
    manager.commit_install()
    print(f"Installed and started {manager.unit_path}")
    print(
        f"Dashboard: {_http_url(config.listen_host, config.listen_port)}"
        f"#access_token={token}"
    )
    print("Logs: journalctl --user -u mocop -f")
    return 0


def _run_lifecycle(args: argparse.Namespace) -> int:
    if args.command == "migrate":
        target = args.config or user_config_path()
        result = migrate_config(
            args.from_config,
            target,
            current_hostname=socket.gethostname(),
            local_host=args.local_host,
            drop_local_host=args.drop_local_host,
            display_name=args.display_name,
            ssh_config=args.ssh_config,
            auto_discover=args.auto_discover,
        )
        print(f"Migrated configuration: {result.target}")
        print(f"Source preserved: {result.source}")
        if result.old_local_host or result.new_local_host:
            print(
                f"Local host: {result.old_local_host or 'none'} -> "
                f"{result.new_local_host or 'none'}"
            )
        print(
            "Automatic host discovery: "
            f"{'enabled' if result.auto_discover else 'disabled'}"
        )
        if result.dropped_fields:
            print(f"Dropped old-machine metadata: {', '.join(result.dropped_fields)}")
        print("No capability, secrets, service unit, or history was copied.")
        target_argument = shlex.quote(str(result.target))
        print(f"Next: mocop config check --config {target_argument}")
        print(f"Then: mocop doctor --no-connect --config {target_argument}")
        print(f"Then: mocop service install --config {target_argument}")
        return 0

    if args.command == "init":
        path = args.config or user_config_path()
        created = initialize_config(path, args.hosts)
        print(f"Created configuration: {created}")
        if not args.hosts:
            print("Add SSH host aliases to the hosts list before starting mocop.")
        print("Next: mocop doctor (verifies SSH reachability)")
        print("Then: mocop service install")
        return 0

    if args.command == "deploy":
        target = args.config or user_config_path()
        sibling_paths = (
            access_token_path(target),
            access_token_path(target).with_name("environment"),
        )
        existing_siblings = [
            path.name for path in sibling_paths if os.path.lexists(path)
        ]
        if existing_siblings:
            raise LifecycleError(
                "fresh deployment found existing installation state "
                f"({', '.join(existing_siblings)}); use a clean target directory "
                "or run service install for an existing setup"
            )
        local_host = None
        if not args.no_local:
            local_host = args.local_host or socket.gethostname()
        created = initialize_config(
            target,
            args.hosts,
            local_host=local_host,
            display_name=args.display_name,
            ssh_config=args.ssh_config,
            auto_discover=args.auto_discover,
        )
        print(f"Fresh deployment configuration: {created}")
        result = _install_service(created)
        if result != 0:
            print(f"Configuration retained for diagnosis: {created}")
        return result

    config_path = args.config or user_config_path()
    if args.action == "install":
        return _install_service(config_path)
    manager = UserServiceManager(
        config_path=config_path,
        unit_path=user_unit_path(),
        python_executable=Path(sys.executable),
    )
    if args.action == "status":
        return manager.status()
    manager.uninstall()
    print(f"Stopped and removed {manager.unit_path}")
    return 0


def _http_url(host: str, port: int) -> str:
    authority = f"[{host.replace('%', '%25')}]" if ":" in host else host
    return f"http://{authority}:{port}/"


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.strict and not args.once:
        print("--strict requires --once", file=sys.stderr)
        return 2
    if args.command is None:
        return _run_monitor(args)
    if args.command == "config":
        return _run_config_check(args)
    if args.command == "doctor":
        return _run_doctor(args)
    try:
        return _run_lifecycle(args)
    except LifecycleError as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
