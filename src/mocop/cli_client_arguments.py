"""The subcommands that talk to a running service: ``api`` and ``brief``.

Both reach the listener named by the configuration with the capability from
the private access-token file beside it, so an operator or an agent never
spells the address or the Bearer header.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path


def add_client_commands(
    commands: argparse._SubParsersAction,  # type: ignore[type-arg]
    *,
    json_flag: Callable[[argparse.ArgumentParser], None],
) -> None:
    api_parser = commands.add_parser(
        "api",
        help=(
            "GET one public or authenticated route from the running service, "
            "or POST a JSON body to a writer route, and write the response to "
            "stdout"
        ),
        description=(
            "Talk to the running monitor without spelling the listen address or "
            "the Bearer header: the listener comes from the configuration and "
            "the capability from the private access-token file beside it. "
            "Writer routes (maintenance windows, incident actions, hosts, "
            "collector settings, probes, restart, update) take --data with the "
            "JSON body the manifest describes; reader routes are refused with "
            "DASHBOARD_ONLY because their marker changes the collection "
            "cadence. /api/events streams until interrupted. Exit 0 on a 2xx, "
            "1 on any other HTTP status or an unreachable service, 2 on a usage "
            "or configuration problem; a non-zero exit always leaves a JSON "
            "error envelope on stdout."
        ),
    )
    api_parser.add_argument(
        "--data",
        metavar="JSON",
        default=None,
        help=(
            "JSON body to POST to a writer route; @FILE reads the body from a "
            "file and @- from stdin ('{}' for routes whose body is empty)"
        ),
    )
    api_parser.add_argument(
        "path",
        metavar="PATH",
        help="absolute API path with optional query, e.g. /api/capacity?gpus=2",
    )
    _add_client_arguments(api_parser)

    brief_parser = commands.add_parser(
        "brief",
        help=(
            "print the situation brief of the running service: what needs a "
            "person, what changed, where capacity is, whose GPUs sit idle"
        ),
        description=(
            "The operator's scan of the dashboard as one screen of text, read "
            "from GET /api/brief of the running service: fleet status, the "
            "actionable conditions worst first with their correlations, "
            "openings and recoveries over the window with the conditions that "
            "keep coming back, idle GPUs by host, and per-owner GPU-hours with "
            "the share their reservation sat idle. Exit codes follow `mocop api`."
        ),
    )
    brief_parser.add_argument(
        "--hours",
        type=_window_hours,
        default=24,
        metavar="N",
        help="look-back window for changes and usage, 1-168 (default: 24)",
    )
    json_flag(brief_parser)
    _add_client_arguments(brief_parser)


def _add_client_arguments(parser: argparse.ArgumentParser) -> None:
    """How a command reaches the running service: listener and capability."""
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="configuration naming the listener and the access-token location",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="capability file (default: the access-token file beside the config)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="socket timeout for the request (default: 10)",
    )


def _window_hours(value: str) -> int:
    try:
        hours = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a whole number of hours"
        ) from None
    if not 1 <= hours <= 168:
        raise argparse.ArgumentTypeError("hours must be between 1 and 168")
    return hours
