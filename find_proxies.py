"""Find and rank currently working free proxies without opening the GUI."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

from proxy_pool import (
    MAX_TEST_BATCH,
    PROTOCOLS,
    SOURCE_NAMES,
    TEST_TIMEOUT,
    TEST_WORKERS,
    Feed,
    ProxyManager,
    country_codes,
    read_proxy_list,
)


def custom_feed(value):
    prefix, _, remainder = value.partition("=")
    protocol, url = (prefix, remainder) if prefix in PROTOCOLS else (None, value)
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname and parsed.username is None:
            return Feed(f"custom/{parsed.hostname}", url, protocol)
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use an HTTPS URL, optionally prefixed with protocol=")


def write_results(proxies, output_format):
    rows = [
        {"proxy": proxy.url, "exit_ip": proxy.ip, "country": proxy.cc, "latency_ms": proxy.latency}
        for proxy in proxies
    ]
    if output_format == "json":
        print(json.dumps(rows, indent=2))
    elif output_format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=["proxy", "exit_ip", "country", "latency_ms"],
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    else:
        for row in rows:
            print(f"{row['proxy']}  {row['country']}  {row['latency_ms']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", action="append", metavar="TYPE[,TYPE]",
                        help="http, socks4, socks5, or all (default)")
    parser.add_argument("--country", action="append", metavar="CC[,CC]",
                        help="Filter by observed exit country. Repeat or use comma-separated codes.")
    parser.add_argument("--max-latency", type=int, default=2500, metavar="MS",
                        help="Maximum measured HTTPS latency per site (0 for no cap, default 2500)")
    parser.add_argument("--limit", type=int, default=10, help="Maximum results (default 10, up to 200)")
    parser.add_argument("--budget", type=int, default=MAX_TEST_BATCH,
                        help="Maximum candidate checks (default %(default)s)")
    parser.add_argument("--workers", type=int, default=TEST_WORKERS,
                        help="Concurrent candidate checks, 1–64 (default %(default)s)")
    parser.add_argument("--timeout", type=float, default=TEST_TIMEOUT,
                        help="Connect and read timeout in seconds (default %(default)s)")
    parser.add_argument("--verify", choices=("dual", "trace"), default="dual",
                        help="Use both HTTPS destinations, or only the exit-IP probe")
    parser.add_argument("--unique-exits", action="store_true",
                        help="Keep the best endpoint for each observed exit IP")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--source", action="append", choices=SOURCE_NAMES,
                         help="Select a built-in provider. Repeat to combine providers.")
    sources.add_argument("--no-public", action="store_true", help="Skip all built-in providers")
    parser.add_argument("--feed", action="append", type=custom_feed, default=[],
                        metavar="[TYPE=]HTTPS_URL", help="Add a remote plain-text feed")
    parser.add_argument("--input", action="append", type=Path, default=[],
                        metavar="PATH", help="Import a local list, including saved finder output")
    parser.add_argument("--input-protocol", choices=PROTOCOLS, default="http",
                        help="Protocol for bare addresses in local lists")
    parser.add_argument("--format", choices=("text", "json", "csv"), default="text")
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= 200 or args.budget < 1 or args.max_latency < 0:
        parser.error("limit must be 1–200, budget positive, and latency nonnegative")
    if not 1 <= args.workers <= 64 or not 0 < args.timeout <= 60:
        parser.error("workers must be 1–64 and timeout between 0 and 60 seconds")
    protocols = {part.strip().lower() for value in args.protocol or ["all"]
                 for part in value.split(",")}
    if protocols == {"all"}:
        protocols = PROTOCOLS
    elif not protocols <= set(PROTOCOLS):
        parser.error("choose all or one or more of http, socks4, socks5")
    try:
        countries = country_codes(",".join(args.country or []))
    except ValueError as exc:
        parser.error(str(exc))
    if args.no_public and not (args.input or args.feed):
        parser.error("--no-public requires at least one --input or --feed")

    def report(message):
        print(message, file=sys.stderr, flush=True)

    enabled = () if args.no_public else tuple(dict.fromkeys(args.source or SOURCE_NAMES))
    manager = ProxyManager(report, lambda: None, sources=enabled, extra_feeds=args.feed,
                           workers=args.workers, timeout=args.timeout, verify_site=args.verify == "dual",
                           capacity=args.limit, distinct_exits=args.unique_exits)
    manager.configure(protocols, args.max_latency, countries)
    for path in args.input:
        try:
            entries = read_proxy_list(path, args.input_protocol)
        except (OSError, ValueError, csv.Error) as exc:
            parser.error(f"could not read {path}: {exc}")
        report(f"Imported {manager.add_custom(entries)} candidates from {path}.")
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=args.workers) as workers:
        try:
            manager.scan(workers, stop, args.budget, exhaustive=True)
        except KeyboardInterrupt:
            stop.set()
            return 130

    matches = [proxy for proxy in manager.snapshot() if not countries or proxy.cc in countries]
    report(f"Tested {manager.tested_total} candidates. {len(matches)} verified matches retained.")
    write_results(matches, args.format)
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
