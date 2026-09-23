"""Command-line entry point for the exporter."""

from __future__ import annotations

import argparse
import logging
import re
import signal
import sys
from pathlib import Path

import httpx
from prometheus_client import CollectorRegistry, generate_latest, start_http_server

from . import __version__
from .metrics import QuotaCollector
from .poller import Poller, ProviderState
from .providers import PROVIDERS
from .providers.openai_codex import OpenAICodexProvider

log = logging.getLogger(__name__)

DEFAULT_PORT = 9184
DEFAULT_INTERVAL = 300.0
USER_AGENT = f"llm-quota-exporter/{__version__}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-quota-exporter",
        description="Prometheus exporter for LLM subscription usage and quota windows",
    )
    parser.add_argument("--listen-address", default="0.0.0.0", help="address to bind (default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to bind (default: %(default)s)")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="seconds between upstream polls (default: %(default)s)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="home directory containing CLI credential files (default: %(default)s)",
    )
    parser.add_argument(
        "--providers",
        default="all",
        help=f"comma-separated subset of providers (default: all of {','.join(sorted(PROVIDERS))})",
    )
    parser.add_argument(
        "--openai-account",
        action="append",
        type=parse_openai_account,
        default=[],
        metavar="NAME=CODEX_HOME",
        help="named account and credential directory; repeat for multiple accounts, replacing default openai",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll every provider once, print metrics to stdout and exit",
    )
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    parser.add_argument("--version", action="version", version=USER_AGENT)
    return parser


def parse_openai_account(value: str) -> tuple[str, Path]:
    name, sep, directory = value.partition("=")
    if not sep or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not directory:
        raise argparse.ArgumentTypeError("expected NAME=CODEX_HOME with a lowercase alphanumeric/hyphen name")
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("CODEX_HOME must be an absolute directory path")
    return name, path


def select_providers(spec: str) -> list[str]:
    if spec.strip().lower() == "all":
        return sorted(PROVIDERS)
    names = [name.strip().lower() for name in spec.split(",") if name.strip()]
    unknown = sorted(set(names) - set(PROVIDERS))
    if unknown:
        raise SystemExit(f"unknown providers: {', '.join(unknown)} (available: {', '.join(sorted(PROVIDERS))})")
    return list(dict.fromkeys(names))  # de-dupe, preserve order


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    names = select_providers(args.providers)
    if args.openai_account and "openai" not in names:
        parser.error("--openai-account requires openai in --providers")
    accounts = dict(args.openai_account)
    if len(accounts) != len(args.openai_account):
        parser.error("--openai-account names must be unique")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with httpx.Client(
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        states = []
        for name in names:
            if name == "openai" and accounts:
                states.extend(
                    ProviderState(provider=OpenAICodexProvider(
                        home=args.home, client=client, account=account, codex_home=directory
                    ))
                    for account, directory in accounts.items()
                )
            else:
                states.append(ProviderState(provider=PROVIDERS[name](home=args.home, client=client)))
        poller = Poller(states=states, interval=args.interval)

        registry = CollectorRegistry()
        registry.register(QuotaCollector(poller))

        if args.once:
            poller.poll_once()
            sys.stdout.write(generate_latest(registry).decode())
            return 0 if any(state.snapshot for state in states) else 1

        start_http_server(args.port, addr=args.listen_address, registry=registry)
        log.info(
            "listening on %s:%d, polling %s every %.0fs",
            args.listen_address,
            args.port,
            ", ".join(state.provider.name for state in states),
            args.interval,
        )
        signal.signal(signal.SIGTERM, lambda *_: poller.stop())
        signal.signal(signal.SIGINT, lambda *_: poller.stop())
        poller.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
