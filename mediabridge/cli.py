"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .errors import MediaBridgeError
from .logging_setup import setup_logging

log = logging.getLogger("mediabridge")

DEFAULT_CONFIG = "config.yaml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mediabridge",
        description="Automatically repost openly-licensed media to AcFun.",
    )
    parser.add_argument("--version", action="version", version=f"mediabridge {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG, help=f"config file (default: {DEFAULT_CONFIG})"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="discover, download and publish")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be published without downloading or submitting anything",
    )
    run.add_argument(
        "--download",
        action="store_true",
        help="with --dry-run, also exercise the download path (no submission)",
    )
    run.add_argument("--source", action="append", dest="sources", help="only run this source (repeatable)")
    run.add_argument("--max-items", type=int, help="override limits.max_items_per_run")

    refresh = sub.add_parser(
        "refresh",
        help="re-render already-published items and re-submit them in place (articles only)",
    )
    refresh.add_argument("--dry-run", action="store_true", help="report what would be updated")
    refresh.add_argument(
        "--download", action="store_true", help="with --dry-run, also exercise the fetch path"
    )
    refresh.add_argument(
        "--source", action="append", dest="sources", help="only refresh this source (repeatable)"
    )
    refresh.add_argument("--max-items", type=int, help="stop after this many updates")

    sub.add_parser("login-check", help="verify the stored AcFun session and report its expiry")

    channels = sub.add_parser("channels", help="list AcFun partition and realm IDs")
    channels.add_argument(
        "--articles", action="store_true", help="show article realms instead of video partitions"
    )

    sub.add_parser("sources", help="list registered source types, including plugins")
    sub.add_parser("check-config", help="validate the config file without doing anything")

    return parser


def _load(args: argparse.Namespace):
    from .config import load_config

    return load_config(args.config)


def _acfun_client(config):
    from .publishers.acfun.auth import load_credentials
    from .publishers.acfun.client import AcFunClient
    from .utils.http import build_session

    credentials = load_credentials(cookie_env=config.acfun.cookie_env, cookie_file=config.acfun.cookie_file)
    return credentials, AcFunClient(
        credentials,
        session=build_session(),
        timeout=config.acfun.request_timeout,
        upload_timeout=config.acfun.upload_timeout,
    )


def cmd_run(args: argparse.Namespace) -> int:
    from .orchestrator import Orchestrator

    config = _load(args)
    if args.dry_run:
        log.info(
            "Dry run: nothing will be submitted to AcFun%s.",
            "" if args.download else " and nothing will be downloaded (pass --download to rehearse that too)",
        )

    report = Orchestrator(config, dry_run=args.dry_run, fetch_on_dry_run=args.download).run(
        only=args.sources, max_items=args.max_items
    )

    _write_job_summary(report)
    for failure in report.failures:
        log.error("failure: %s", failure)
    return report.exit_code


def cmd_refresh(args: argparse.Namespace) -> int:
    from .orchestrator import Orchestrator

    config = _load(args)
    log.info("Refreshing already-published items; nothing new will be posted.")

    report = Orchestrator(config, dry_run=args.dry_run, fetch_on_dry_run=args.download).refresh(
        only=args.sources, max_items=args.max_items
    )

    for failure in report.failures:
        log.error("failure: %s", failure)
    return report.exit_code


def cmd_login_check(args: argparse.Namespace) -> int:
    from .publishers.acfun.auth import verify

    config = _load(args)
    credentials, client = _acfun_client(config)

    log.info("Credentials loaded from %s", credentials.source)
    log.info("Cookies present: %s", ", ".join(sorted(credentials.cookies)))
    credentials.check_expiry()

    info = verify(client.session, credentials, timeout=config.acfun.request_timeout)
    log.info(
        "Session is valid (uid=%s, membership=%s, signed-in-today=%s)",
        credentials.user_id,
        info.get("membershipState"),
        info.get("signIn"),
    )
    return 0


def cmd_channels(args: argparse.Namespace) -> int:
    from .publishers.acfun.channels import describe_channels

    config = _load(args)
    _credentials, client = _acfun_client(config)
    print(describe_channels(client, articles=args.articles))
    return 0


def cmd_sources(_args: argparse.Namespace) -> int:
    from .sources.registry import available_types

    print("Registered source types:\n")
    for name, description in sorted(available_types().items()):
        print(f"  {name:<14} {description}")
    print("\nAdd your own by registering a mediabridge.sources entry point -- see docs/EXTENDING.md.")
    return 0


def cmd_check_config(args: argparse.Namespace) -> int:
    from .sources.registry import get_source_class

    config = _load(args)
    log.info(
        "Config %s is valid: %d source(s), %d enabled",
        args.config,
        len(config.sources),
        len(config.enabled_sources()),
    )

    problems = 0
    for source_config in config.sources:
        try:
            source_class = get_source_class(source_config.type)
            source_class(source_config.name, source_config.options)
        except MediaBridgeError as exc:
            log.error("source '%s': %s", source_config.name, exc)
            problems += 1
            continue

        target = source_config.publish.target
        try:
            from .publishers.base import get_publisher_class

            get_publisher_class(target)
        except MediaBridgeError as exc:
            log.error("source '%s': %s", source_config.name, exc)
            problems += 1

    if problems:
        log.error("%d configuration problem(s) found", problems)
        return 1
    log.info("All sources and publish targets resolve correctly.")
    return 0


def _write_job_summary(report) -> None:
    """Emit a GitHub Actions job summary when running in CI."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    lines = [
        "## MediaBridge run",
        "",
        f"- discovered: {report.discovered}",
        f"- published: {report.published}",
        f"- duplicates skipped: {report.skipped_duplicate}",
        f"- filtered out: {report.skipped_filtered}",
        f"- failed: {report.failed}",
    ]
    if report.published_urls:
        lines += ["", "### Published", *(f"- {url}" for url in report.published_urls)]
    if report.failures:
        lines += ["", "### Failures", *(f"- {failure}" for failure in report.failures)]

    try:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        log.debug("Could not write the job summary", exc_info=True)


_COMMANDS = {
    "run": cmd_run,
    "refresh": cmd_refresh,
    "login-check": cmd_login_check,
    "channels": cmd_channels,
    "sources": cmd_sources,
    "check-config": cmd_check_config,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logging(args.verbose)

    try:
        return _COMMANDS[args.command](args)
    except MediaBridgeError as exc:
        log.error("%s", exc)
        if exc.hint:
            log.error("hint: %s", exc.hint)
        return 2
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
