"""The ``mocop`` command line: every subcommand, flag, and help text.

``__main__`` runs the parsed arguments; this module only describes them, so
the help an operator or an agent reads is one file and the runtime stays a
separate concern.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__
from .cli_client_arguments import add_client_commands


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


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
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

    add_client_commands(commands, json_flag=_add_json_flag)
    return parser.parse_args(argv)
