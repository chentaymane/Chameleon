from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import unittest
from unittest.mock import patch

import proxy_pool as pool


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
            self.assertEqual(pool.fetch_source("feed"), [
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
        manager = pool.ProxyManager(lambda _: None, lambda: None)
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
        manager = pool.ProxyManager(lambda _: None, lambda: None)
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


if __name__ == "__main__":
    unittest.main()
