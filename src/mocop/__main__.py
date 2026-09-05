from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import signal
import socket
import sys
import threading
from pathlib import Path

from . import __version__
from . import client as api_client
from .config import (
    ConfigError,
    MonitorConfig,
    load_config,
    load_private_config,
    resolve_config_path,
)
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


def _add_target_identity_arguments(
    parser: argparse.ArgumentParser,
    *,
    local_host_help: str,
    without_local_flag: str,
    without_local_help: str,
    auto_discover_default: bool | None,
) -> None:
    """Options shared by ``deploy`` and ``migrate`` for the new machine's identity."""
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--local-host", metavar="ALIAS", help=local_host_help)
    identity.add_argument(
        without_local_flag, action="store_true", help=without_local_help
    )
    parser.add_argument(
        "--display-name",
        help="dashboard label for the local host; presentation only",
    )
    parser.add_argument(
        "--ssh-config",
        default="~/.ssh/config",
        help="OpenSSH client configuration to scan for aliases (default: %(default)s)",
    )
    admission = parser.add_mutually_exclusive_group()
    admission.add_argument(
        "--auto-discover",
        dest="auto_discover",
        action="store_true",
        help="admit safe aliases from the SSH config automatically",
    )
    admission.add_argument(
        "--no-auto-discover",
        dest="auto_discover",
        action="store_false",
        help="monitor only the explicitly listed hosts",
    )
    parser.set_defaults(auto_discover=auto_discover_default)


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="write a machine-readable report"
    )


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cli_failure(
    message: str,
    *,
    as_json: bool,
    code: str,
    prefix: str,
) -> int:
    if as_json:
        _emit_json({"ok": False, "code": code, "error": message})
    else:
        print(f"{prefix}: {message}", file=sys.stderr)
    return 2


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mocop",
        description=(
            "mocop: AI-native GPU cluster monitor over OpenSSH. "
            "HTTP contract: GET /api/meta."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
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
    _add_json_flag(init_parser)

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
    _add_target_identity_arguments(
        deploy_parser,
        local_host_help=(
            "safe alias that identifies this machine in the inventory "
            "(default: the current hostname)"
        ),
        without_local_flag="--no-local",
        without_local_help="do not monitor this server locally",
        auto_discover_default=True,
    )
    _add_json_flag(deploy_parser)

    migrate_parser = commands.add_parser(
        "migrate", help="generate a new private config from another installation"
    )
    migrate_parser.add_argument(
        "--from-config",
        type=Path,
        required=True,
        help="existing configuration to migrate; it is read, never modified",
    )
    migrate_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="new configuration path; it must not already exist",
    )
    # None keeps the source installation's auto_discover policy.
    _add_target_identity_arguments(
        migrate_parser,
        local_host_help=(
            "safe alias for this machine when the source monitored itself "
            "(default: the current hostname)"
        ),
        without_local_flag="--drop-local-host",
        without_local_help="the new monitor must not collect from itself",
        auto_discover_default=None,
    )
    _add_json_flag(migrate_parser)

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
    _add_json_flag(check_parser)

    service_parser = commands.add_parser(
        "service", help="manage the user-level systemd service"
    )
    service_actions = service_parser.add_subparsers(dest="action", required=True)
    install_parser = service_actions.add_parser(
        "install",
        help=(
            "generate, enable, start, and verify the user unit, then print the "
            "dashboard capability URL"
        ),
    )
    install_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="configuration used by the service",
    )
    _add_json_flag(install_parser)
    # status and uninstall operate on the fixed unit; they take no --config.
    status_parser = service_actions.add_parser(
        "status", help="show systemd status for the generated unit"
    )
    _add_json_flag(status_parser)
    uninstall_parser = service_actions.add_parser(
        "uninstall", help="stop and remove the generated unit only"
    )
    _add_json_flag(uninstall_parser)

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
    _add_json_flag(doctor_parser)

    api_parser = commands.add_parser(
        "api",
        help=(
            "GET one public or authenticated route from the running service "
            "and write the response body to stdout"
        ),
        description=(
            "Read the running monitor without spelling the listen address or "
            "the Bearer header: the listener comes from the configuration and "
            "the capability from the private access-token file beside it. "
            "Routes the dashboard reserves for itself (reader and writer tiers) "
            "are refused with DASHBOARD_ONLY. /api/events streams until "
            "interrupted. Exit 0 on a 2xx, 1 on any other HTTP status or an "
            "unreachable service, 2 on a usage or configuration problem; a "
            "non-zero exit always leaves a JSON error envelope on stdout."
        ),
    )
    api_parser.add_argument(
        "path",
        metavar="PATH",
        help="absolute API path with optional query, e.g. /api/capacity?gpus=2",
    )
    api_parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="configuration naming the listener and the access-token location",
    )
    api_parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="capability file (default: the access-token file beside the config)",
    )
    api_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="socket timeout for the request (default: 10)",
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
    access_token = ""
    if args.access_token_file is not None:
        try:
            access_token = read_access_token(args.access_token_file)
        except LifecycleError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            return 2
    elif not args.once:
        # Foreground servers receive an ephemeral per-process capability. It
        # is printed only in the operator's terminal and is never persisted.
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
        return _cli_failure(
            str(exc),
            as_json=args.json,
            code=exc.code,
            prefix="Configuration error",
        )
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


def _config_check_report(config_path: Path, config: MonitorConfig) -> dict[str, object]:
    """One report feeds both renderers; it names environment variables, never values."""
    if config.topology is not None:
        topology: dict[str, object] = {
            "source": "configured",
            "links": len(config.topology.links),
        }
    elif config.ssh_discovery.mode == "topology":
        topology = {"source": "resolved", "links": None}
    else:
        topology = {"source": "none", "links": None}
    return {
        "configPath": str(config_path),
        "hosts": len(config.hosts),
        "localHost": config.local_host,
        "sshDiscovery": {
            "mode": config.ssh_discovery.mode,
            "refreshSeconds": config.ssh_discovery.refresh_seconds,
            "resolveTimeoutSeconds": config.ssh_discovery.resolve_timeout_seconds,
        },
        "persistence": config.persistence.enabled,
        "workloads": config.workloads.mode,
        "topology": topology,
        "updates": config.updates.mode,
        "listen": {"host": config.listen_host, "port": config.listen_port},
        "webhooks": [
            {
                "name": webhook.name,
                "urlEnv": webhook.url_env,
                "urlEnvState": _environment_state(webhook.url_env),
                "secretEnv": webhook.secret_env,
                "secretEnvState": (
                    _environment_state(webhook.secret_env)
                    if webhook.secret_env is not None
                    else None
                ),
            }
            for webhook in config.webhooks
        ],
    }


def _print_config_check_report(report: dict[str, object]) -> None:
    print(f"configuration OK: {report['configPath']}")
    local_note = f" (local: {report['localHost']})" if report["localHost"] else ""
    print(f"hosts: {report['hosts']}{local_note}")
    discovery = report["sshDiscovery"]
    assert isinstance(discovery, dict)
    print(
        f"ssh discovery: {discovery['mode']} "
        f"(refresh {discovery['refreshSeconds']}s, "
        f"resolve timeout {discovery['resolveTimeoutSeconds']:g}s)"
    )
    print(f"persistence: {'enabled' if report['persistence'] else 'disabled'}")
    print(f"workloads: {report['workloads']}")
    print(f"updates: {report['updates']}")
    topology = report["topology"]
    assert isinstance(topology, dict)
    if topology["source"] == "configured":
        print(f"topology: configured ({topology['links']} links)")
    elif topology["source"] == "resolved":
        print("topology: resolved from SSH at runtime")
    else:
        print("topology: none")
    webhooks = report["webhooks"]
    assert isinstance(webhooks, list)
    if not webhooks:
        print("webhooks: none")
        return
    print(f"webhooks: {len(webhooks)}")
    for webhook in webhooks:
        references = [f"url_env {webhook['urlEnv']} ({webhook['urlEnvState']})"]
        if webhook["secretEnv"] is not None:
            references.append(
                f"secret_env {webhook['secretEnv']} ({webhook['secretEnvState']})"
            )
        print(f"  {webhook['name']}: {', '.join(references)}")


def _run_api(args: argparse.Namespace) -> int:
    """Forward one GET to the running service; the body is the whole output."""
    try:
        response = api_client.request(
            args.path,
            config_path=args.config,
            token_file=args.token_file,
            timeout=args.timeout,
        )
    except api_client.ApiClientError as exc:
        _emit_json({"error": str(exc), "code": exc.code})
        return exc.exit_code
    try:
        api_client.write_response(response, sys.stdout.buffer)
    except KeyboardInterrupt:
        # Ctrl-C on an event stream is the normal way to stop following it.
        return 0
    except BrokenPipeError:
        # `mocop api ... | head` closed the pipe; hand stdout to /dev/null so
        # the interpreter's final flush cannot raise the same error again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    return 0 if 200 <= response.status < 300 else 1


def _run_config_check(args: argparse.Namespace) -> int:
    """Parse and validate only: no web server, no SSH connections."""
    try:
        config_path = resolve_config_path(args.config)
        config = load_config(config_path)
    except ConfigError as exc:
        return _cli_failure(
            str(exc),
            as_json=args.json,
            code=exc.code,
            prefix="Configuration error",
        )
    report = _config_check_report(config_path, config)
    if args.json:
        _emit_json({"ok": True, **report})
    else:
        _print_config_check_report(report)
    return 0


def _install_service(config_path: Path, *, as_json: bool) -> int:
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
        if as_json:
            _emit_json(
                {
                    "ok": False,
                    "code": "SERVICE_UNHEALTHY",
                    "error": (
                        "Service did not become healthy; the previous unit was restored"
                    ),
                    "configPath": str(config_path),
                }
            )
        else:
            print("Service did not become healthy; the previous unit was restored")
            print("Inspect it with: systemctl --user status mocop")
            print("Logs: journalctl --user -u mocop -f")
        return 1
    assert token is not None
    manager.commit_install()
    dashboard_url = (
        f"{_http_url(config.listen_host, config.listen_port)}#access_token={token}"
    )
    if as_json:
        _emit_json(
            {
                "ok": True,
                "unitPath": str(manager.unit_path),
                "dashboardUrl": dashboard_url,
                "configPath": str(config_path),
            }
        )
        return 0
    print(f"Installed and started {manager.unit_path}")
    print(f"Dashboard: {dashboard_url}")
    print("Logs: journalctl --user -u mocop -f")
    return 0


def _run_lifecycle(args: argparse.Namespace) -> int:
    as_json = args.json
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
        target_argument = shlex.quote(str(result.target))
        next_steps = (
            f"mocop config check --config {target_argument}",
            f"mocop doctor --no-connect --config {target_argument}",
            f"mocop service install --config {target_argument}",
        )
        if as_json:
            _emit_json(
                {
                    "ok": True,
                    "target": str(result.target),
                    "source": str(result.source),
                    "oldLocalHost": result.old_local_host,
                    "newLocalHost": result.new_local_host,
                    "autoDiscover": result.auto_discover,
                    "droppedFields": list(result.dropped_fields),
                    "next": list(next_steps),
                }
            )
            return 0
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
        print(f"Next: {next_steps[0]}")
        print(f"Then: {next_steps[1]}")
        print(f"Then: {next_steps[2]}")
        return 0

    if args.command == "init":
        path = args.config or user_config_path()
        created = initialize_config(path, args.hosts)
        next_steps = ("mocop doctor", "mocop service install")
        if as_json:
            _emit_json(
                {
                    "ok": True,
                    "configPath": str(created),
                    "hostsAdded": len(args.hosts),
                    "next": list(next_steps),
                }
            )
            return 0
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
        if not as_json:
            print(f"Fresh deployment configuration: {created}")
        result = _install_service(created, as_json=as_json)
        if result != 0 and not as_json:
            print(f"Configuration retained for diagnosis: {created}")
        return result

    config_path = args.config or user_config_path()
    if args.action == "install":
        return _install_service(config_path, as_json=as_json)
    manager = UserServiceManager(
        config_path=config_path,
        unit_path=user_unit_path(),
        python_executable=Path(sys.executable),
    )
    if args.action == "status":
        if as_json:
            report = manager.inspect()
            _emit_json({"ok": True, **report})
            return 0 if report["active"] else 1
        return manager.status()
    manager.uninstall()
    if as_json:
        _emit_json({"ok": True, "unitPath": str(manager.unit_path)})
        return 0
    print(f"Stopped and removed {manager.unit_path}")
    return 0


def _http_url(host: str, port: int) -> str:
    authority = f"[{host.replace('%', '%25')}]" if ":" in host else host
    return f"http://{authority}:{port}/"


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.command is not None and (args.once or args.strict):
        print(
            "--once and --strict apply only to the default monitor command",
            file=sys.stderr,
        )
        return 2
    if args.strict and not args.once:
        print("--strict requires --once", file=sys.stderr)
        return 2
    if args.command is None:
        return _run_monitor(args)
    if args.command == "api":
        return _run_api(args)
    if args.command == "config":
        return _run_config_check(args)
    if args.command == "doctor":
        return _run_doctor(args)
    try:
        return _run_lifecycle(args)
    except LifecycleError as exc:
        return _cli_failure(
            str(exc), as_json=args.json, code=exc.code, prefix="Setup error"
        )


if __name__ == "__main__":
    raise SystemExit(main())
