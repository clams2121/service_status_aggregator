"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from service_status_aggregator import __version__
from service_status_aggregator.config import (
    ConfigError,
    find_config_path,
    load_config,
    load_registration_token,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CONFIG = 2
EXIT_BIND = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="service-status-aggregator",
        description="Read-only dashboard of self-registering home services.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="path to config.toml (default: $SSA_CONFIG or /etc/...)",
    )
    parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version and exit", parents=[common])
    sub.add_parser(
        "check-config",
        help="validate the configuration and report every problem",
        parents=[common],
    )
    sub.add_parser("run", help="run the aggregator", parents=[common])

    rm = sub.add_parser(
        "remove", help="remove a decommissioned service from the database", parents=[common]
    )
    rm.add_argument("name")
    rm.add_argument("host")
    return parser


def cmd_check_config(config_arg: str | None) -> int:
    path = find_config_path(config_arg)
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    _token, source = load_registration_token()
    print(f"config OK: {path}")
    print(f"  bind={cfg.server.bind} port={cfg.server.port}")
    print(f"  db={cfg.storage.db_path}")
    print(f"  log={cfg.logging.path} level={cfg.logging.level}")
    print(f"  poll every {cfg.polling.interval_seconds}s, timeout {cfg.polling.timeout_seconds}s")
    print(f"  staleness {cfg.registration.staleness_seconds}s")
    print(f"  registration token: {'enabled' if cfg.token_enabled else 'DISABLED'} ({source})")
    print(f"  allowed poll targets: {', '.join(map(str, cfg.registration.allowed_target_cidrs))}")
    if not cfg.token_enabled:
        print("  WARNING: anyone who can reach this service can register entries", file=sys.stderr)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return EXIT_OK
    if args.command == "check-config":
        return cmd_check_config(args.config)
    if args.command == "run":
        from service_status_aggregator.runtime import cmd_run

        return cmd_run(args.config)
    if args.command == "remove":
        from service_status_aggregator.runtime import cmd_remove

        return cmd_remove(args.config, args.name, args.host)
    parser.print_usage(sys.stderr)
    return EXIT_USAGE
