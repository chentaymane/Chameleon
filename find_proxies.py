"""Find currently working free proxies without opening the GUI."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import sys
import threading

from proxy_pool import MAX_TEST_BATCH, PROTOCOLS, QUEUE_HIGH_MARK, TEST_WORKERS, ProxyManager


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=("all", *PROTOCOLS), default="all")
    parser.add_argument("--country", type=str.upper, metavar="CC",
                        help="Require a matching exit country (two-letter code)")
    parser.add_argument("--max-latency", type=int, default=2500, metavar="MS",
                        help="Maximum HTTPS latency in milliseconds (0 for no cap)")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of results")
    parser.add_argument("--budget", type=int, default=MAX_TEST_BATCH,
                        help="Maximum number of candidates to verify")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= QUEUE_HIGH_MARK or args.budget < 1 or args.max_latency < 0:
        parser.error(f"limit must be 1–{QUEUE_HIGH_MARK}, budget positive, and latency nonnegative")
    if args.country and (len(args.country) != 2 or not args.country.isascii()
                         or not args.country.isalpha()):
        parser.error("country must be a two-letter country code")

    def report(message):
        print(message, file=sys.stderr, flush=True)

    manager = ProxyManager(report, lambda: None)
    manager.configure(args.protocol, args.max_latency, args.country or "Any")
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=TEST_WORKERS) as workers:
        try:
            manager.scan(workers, stop, args.budget)
        except KeyboardInterrupt:
            stop.set()
            return 130

    matches = [proxy for proxy in manager.snapshot()
               if not args.country or proxy.cc == args.country]
    report(f"Tested {manager.tested_total} candidates. {len(matches)} verified matches ready.")
    for proxy in matches[:args.limit]:
        print(f"{proxy.url}  {proxy.cc}  {proxy.latency}")
    return 0 if matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
