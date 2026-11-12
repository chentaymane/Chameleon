from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

import proxy_pool as pool
import find_proxies


class ProxyPoolTests(unittest.TestCase):
    def test_imports_exported_rows_and_normalizes_credentials(self):
        self.assertEqual(
            pool.parse_proxy_line("socks5://user:p%40ss@[2606:4700::1]:1080  US  120"),
            ("user:p%40ss@[2606:4700::1]:1080", "socks5"),
        )
        self.assertEqual(
            pool.parse_proxy_line("PROXY.EXAMPLE:8080:user:p@ss"),
            ("user:p%40ss@proxy.example:8080", "http"),
        )
        for invalid in ("gopher://1.1.1.1:80", "host:65536", "host:0", "http://host:80/path"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(pool.parse_proxy_line(invalid))

    def test_feed_rejects_nonpublic_hosts_and_deduplicates_by_protocol(self):
        content = """http://127.0.0.1:80
http://example.org:80
http://169.254.169.254:80
http://224.0.0.1:80
http://[::ffff:127.0.0.1]:80
http://user:pass@1.1.1.1:80
http://1.1.1.1:80
http://1.1.1.1:80
socks5://1.1.1.1:80
"""
        with patch.object(pool, "get_text", return_value=content):
            self.assertEqual(pool.fetch_source(pool.Feed("test", "https://example.org")), [
                ("1.1.1.1:80", "http"), ("1.1.1.1:80", "socks5"),
            ])

    def test_https_probe_requires_a_valid_public_exit(self):
        proxy = pool.Proxy("1.1.1.1:1080", "socks5", "", "??", 0, 0)
        valid = "ip=2606:4700::1\nloc=US\nvisit_scheme=https\n"
        with patch.object(pool, "get_text", return_value=valid) as get:
            self.assertEqual(pool.check_connection(proxy)[:2], ("2606:4700::1", "US"))
            self.assertEqual(get.call_args.args[-1]["https"], "socks5h://1.1.1.1:1080")
        for body in ("<html>200 OK</html>", valid.replace("https", "http"),
                     valid.replace("2606:4700::1", "192.168.1.1")):
            with self.subTest(body=body), patch.object(pool, "get_text", return_value=body):
                self.assertIsNone(pool.check_connection(proxy))

    def test_scan_caps_queue_and_prioritizes_selected_country_and_fastest_results(self):
        manager = pool.ProxyManager(lambda _: None, lambda: None, verify_site=False)
        manager.configure("http", 2500, "FR")
        entries = [(f"8.8.8.{i}:80", "http") for i in range(1, 70)]
        entries.append(("8.8.4.4:1080", "socks5"))

        def check(proxy, timeout):
            index = int(proxy.endpoint.split(".")[-1].split(":")[0])
            return "1.1.1.1", "US" if index < 41 else "FR", 100 - index

        with patch.object(pool, "fetch_source", return_value=entries), \
                patch.object(pool, "check_connection", side_effect=check), \
                ThreadPoolExecutor(max_workers=pool.TEST_WORKERS) as workers:
            manager.scan(workers, Event())

        self.assertEqual(manager.tested_total, 69)
        self.assertEqual(len(manager.snapshot()), pool.QUEUE_HIGH_MARK)
        self.assertEqual(manager.countries()["FR"], 29)
        self.assertEqual(manager.get_next("FR").endpoint, "8.8.8.69:80")
        manager.configure("http", 32, "FR")
        self.assertEqual([p.latency for p in manager.snapshot()], [32])
        with patch.object(pool.time, "monotonic", return_value=time.monotonic() + pool.VERIFIED_TTL):
            self.assertEqual(manager.snapshot(), [])

    def test_consumed_proxy_can_be_rechecked_after_cooldown(self):
        manager = pool.ProxyManager(lambda _: None, lambda: None, verify_site=False)
        entry = ("8.8.8.8:80", "http")
        with patch.object(pool, "fetch_source", return_value=[entry]), \
                patch.object(pool, "check_connection", return_value=("1.1.1.1", "US", 40)), \
                ThreadPoolExecutor(max_workers=pool.TEST_WORKERS) as workers:
            manager.scan(workers, Event())
            self.assertIsNotNone(manager.get_next())
            manager.scan(workers, Event())
            self.assertEqual(manager.tested_total, 1)
            later = time.monotonic() + pool.RETRY_AFTER + 1
            with patch.object(pool.time, "monotonic", return_value=later):
                manager.scan(workers, Event())
                self.assertIsNotNone(manager.get_next())
            self.assertEqual(manager.tested_total, 2)

    def test_claim_selected_proxy_respects_current_country_and_expiry(self):
        manager = pool.ProxyManager(lambda _: None, lambda: None, sources=(), verify_site=False)
        first = ("8.8.8.8:80", "http")
        second = ("8.8.4.4:1080", "socks5")
        manager.add_custom([first, second])
        with patch.object(pool, "check_connection", return_value=("1.1.1.1", "US", 40)), \
                ThreadPoolExecutor(max_workers=2) as workers:
            manager.scan(workers, Event())
        manager.configure("all", 0, "DE")
        self.assertIsNone(manager.take_proxy(*second))
        manager.configure("all", 0, "US")
        self.assertEqual(manager.take_proxy(*second).endpoint, second[0])
        self.assertIsNone(manager.take_proxy(*second))
        with patch.object(pool.time, "monotonic", return_value=time.monotonic() + pool.VERIFIED_TTL):
            self.assertIsNone(manager.take_proxy(*first))

    def test_targeted_feeds_and_other_protocols_share_the_scan_budget(self):
        manager = pool.ProxyManager(lambda _: None, lambda: None,
                                    sources=("proxyscrape-api", "monosans"), workers=1,
                                    verify_site=False)
        manager.configure("all", 0, "US, DE")
        entries = {
            "proxyscrape-api/DE": [("8.8.8.1:80", "http"), ("8.8.8.2:80", "http")],
            "proxyscrape-api/US": [("8.8.8.3:80", "http")],
            "monosans": [("8.8.8.4:80", "http"), ("8.8.8.5:1080", "socks5")],
        }
        checked = []

        def fetch(feed):
            if feed.name.startswith("proxyscrape-api/"):
                self.assertIn("country=" + feed.name[-2:].lower(), feed.url)
            return entries[feed.name]

        def check(proxy, _timeout):
            checked.append(proxy.endpoint)
            return None

        with patch.object(pool, "fetch_source", side_effect=fetch), \
                patch.object(pool, "check_connection", side_effect=check), \
                ThreadPoolExecutor(max_workers=1) as workers:
            manager.scan(workers, Event(), budget=4)
        self.assertEqual(checked, ["8.8.8.1:80", "8.8.8.4:80", "8.8.8.3:80", "8.8.8.5:1080"])

    def test_cli_import_only_enforces_dual_check_country_and_unique_exits(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "proxies.csv"
            path.write_text("proxy,country\nhttp://1.1.1.1:80,US\n"
                            "socks5://1.1.1.2:1080,DE\nhttp://1.1.1.3:80,US\n"
                            "http://1.1.1.4:80,GB\n", encoding="utf-8")
            output = StringIO()
            traces = {
                "1.1.1.1:80": ("8.8.8.8", "US", 100),
                "1.1.1.2:1080": ("8.8.8.8", "US", 20),
                "1.1.1.3:80": ("8.8.4.4", "DE", 10),
                "1.1.1.4:80": ("1.0.0.1", "GB", 15),
            }
            with patch.object(pool, "check_connection",
                              side_effect=lambda proxy, _: traces[proxy.endpoint]), \
                    patch.object(pool, "verify_destination",
                                 side_effect=lambda proxy, _: None if proxy.cc == "DE" else 40), \
                    redirect_stdout(output), redirect_stderr(StringIO()):
                status = find_proxies.main(["--no-public", "--input", str(path), "--format", "json",
                                            "--country", "US,DE", "--protocol", "http,socks5",
                                            "--unique-exits", "--limit", "2"])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue()), [
                {"proxy": "socks5://1.1.1.2:1080", "exit_ip": "8.8.8.8",
                 "country": "US", "latency_ms": 40},
            ])


if __name__ == "__main__":
    unittest.main()
