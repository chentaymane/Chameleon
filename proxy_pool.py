"""Discovery, HTTPS verification and ranking for Chameleon's proxy pool."""

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import csv
from dataclasses import dataclass
from ipaddress import ip_address
from itertools import islice, zip_longest
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import quote, unquote, urlencode, urlsplit

import requests
from requests.exceptions import RequestException

PROTOCOLS = ("http", "socks4", "socks5")
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
VERIFY_URL = "https://example.com/"
TEST_TIMEOUT = 5
TEST_WORKERS = 24
QUEUE_LOW_MARK = 8
QUEUE_HIGH_MARK = 40
MAX_TEST_BATCH = 400
SOURCE_REFRESH = 120
RETRY_AFTER = 180
VERIFIED_TTL = 180


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    protocol: str | None = None


BUILTIN_FEEDS = {
    "monosans": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt",
    "proxyscrape": "https://raw.githubusercontent.com/proxyscrape/free-proxy-list/main/proxies/all/data.txt",
    "proxifly": "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "proxy-scraper": "https://proxy-scraper.de/all.txt",
}
SOURCE_NAMES = (*BUILTIN_FEEDS, "proxyscrape-api")


def country_codes(value):
    if value.strip().lower() in ("", "any"):
        return frozenset()
    codes = frozenset(part.strip().upper() for part in value.split(","))
    if any(not re.fullmatch(r"[A-Z]{2}", code) for code in codes):
        raise ValueError("Enter two-letter country codes separated by commas, or Any.")
    return codes


def discovery_feeds(names, countries):
    if "proxyscrape-api" in names:
        for country in sorted(countries) or [""]:
            params = dict(request="display_proxies", proxy_format="protocolipport", format="text")
            if country:
                params["country"] = country.lower()
            url = "https://api.proxyscrape.com/v4/free-proxy-list/get?" + urlencode(params)
            label = f"proxyscrape-api/{country}" if country else "proxyscrape-api"
            yield Feed(label, url)
    for name in names:
        if name in BUILTIN_FEEDS:
            yield Feed(name, BUILTIN_FEEDS[name])


def interleave(groups):
    return (item for row in zip_longest(*groups) for item in row if item is not None)


def parse_proxy_line(line, default_proto="http"):
    """Read a bare address, proxy URL, or host:port:user:password entry."""
    fields = line.strip().split()
    if not fields or fields[0].startswith("#"):
        return None
    line = fields[0]
    protocol = default_proto
    if "://" in line:
        protocol, line = line.split("://", 1)
    protocol = {"https": "http", "socks4a": "socks4", "socks5h": "socks5"}.get(
        protocol.lower(), protocol.lower()
    )
    if protocol not in PROTOCOLS:
        return None

    legacy = line.split(":", 3)
    if not line.startswith("[") and len(legacy) == 4 and legacy[1].isdigit():
        host, port, user, password = legacy
        line = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    try:
        parsed = urlsplit(f"{protocol}://{line}")
        host, port = parsed.hostname, parsed.port
        if not host or not port or parsed.path or parsed.query or parsed.fragment:
            return None
        if ":" in host:
            host = f"[{ip_address(host)}]"
        elif not re.fullmatch(r"[a-zA-Z0-9._-]+", host):
            return None
    except ValueError:
        return None

    addr = f"{host}:{port}"
    if parsed.username is not None:
        user = quote(unquote(parsed.username), safe="")
        password = quote(unquote(parsed.password or ""), safe="")
        addr = f"{user}:{password}@{addr}"
    return addr, protocol


def read_proxy_list(path, default_proto="http"):
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as source:
        if path.suffix.lower() == ".json":
            rows = json.load(source)
            if not isinstance(rows, list):
                raise ValueError("Expected a JSON array of proxy records.")
            lines = (row.get("proxy") if isinstance(row, dict) else row for row in rows)
        elif path.suffix.lower() == ".csv":
            rows = csv.DictReader(source)
            if rows.fieldnames and "proxy" in rows.fieldnames:
                lines = (row["proxy"] for row in rows)
            else:
                source.seek(0)
                lines = source
        else:
            lines = source
        return [entry for line in lines if isinstance(line, str)
                if (entry := parse_proxy_line(line, default_proto))]


@dataclass
class Proxy:
    addr: str
    protocol: str
    ip: str
    cc: str
    latency: int
    checked_at: float
    custom: bool = False

    @property
    def url(self):
        return f"{self.protocol}://{self.addr}"

    @property
    def endpoint(self):
        return self.addr.rsplit("@", 1)[-1]

    def requests_dict(self):
        scheme = {"socks4": "socks4a", "socks5": "socks5h"}.get(self.protocol, self.protocol)
        url = f"{scheme}://{self.addr}"
        return {"http": url, "https": url}

    def __str__(self):
        return f"{self.endpoint}  ·  {self.protocol}  ·  {self.cc}  ·  {self.latency} ms"


def get_text(url, limit, timeout, proxies=None):
    # Ignore desktop and environment proxies, including Chameleon's current route.
    with requests.Session() as session:
        session.trust_env = False
        with session.get(url, proxies=proxies, timeout=timeout, stream=True,
                         allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError(f"HTTP {response.status_code}")
            body = bytearray()
            for chunk in response.iter_content(4096):
                body.extend(chunk)
                if len(body) > limit:
                    raise ValueError("Response exceeded size limit")
    return body.decode("utf-8")


def check_connection(proxy=None, timeout=TEST_TIMEOUT):
    """Verify TLS and read the exit IP and country from the same HTTPS request."""
    started = time.monotonic()
    try:
        trace = get_text(TRACE_URL, 4096, timeout, proxy.requests_dict() if proxy else None)
        fields = dict(line.split("=", 1) for line in trace.splitlines() if "=" in line)
        ip = ip_address(fields.get("ip", ""))
        if not ip.is_global or fields.get("visit_scheme") != "https":
            return None
    except (RequestException, ValueError):
        return None
    country = fields.get("loc", "")
    if not re.fullmatch(r"[A-Z]{2}", country) or country in {"XX", "T1"}:
        country = "??"
    return str(ip), country, round((time.monotonic() - started) * 1000)


def verify_destination(proxy, timeout):
    started = time.monotonic()
    try:
        page = get_text(VERIFY_URL, 16_384, timeout, proxy.requests_dict())
    except (RequestException, ValueError):
        return None
    if "<title>Example Domain</title>" not in page:
        return None
    return round((time.monotonic() - started) * 1000)


def fetch_source(feed):
    """Accept only public IP literals from untrusted feeds."""
    text = get_text(feed.url, 2_000_000, 8)
    candidates = {}
    for line in text.splitlines():
        if "://" not in line and feed.protocol is None:
            continue
        entry = parse_proxy_line(line, feed.protocol or "http")
        if entry is None or "@" in entry[0]:
            continue
        host = entry[0].rsplit(":", 1)[0].strip("[]")
        try:
            address = ip_address(host)
        except ValueError:
            continue
        mapped = getattr(address, "ipv4_mapped", None)
        if not (mapped or address).is_global or address.is_multicast:
            continue
        candidates[entry] = None
    return list(candidates)


def rank(proxy):
    return not proxy.custom, proxy.latency


class ProxyManager:
    """Keep a fresh, bounded pool of the fastest matching verified proxies."""

    def __init__(self, on_log, on_change, *, sources=SOURCE_NAMES, extra_feeds=(),
                 workers=TEST_WORKERS, timeout=TEST_TIMEOUT, verify_site=True,
                 capacity=QUEUE_HIGH_MARK, distinct_exits=False):
        self._log = on_log
        self._change = on_change
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = None
        self._thread = None
        self._pool = {}
        self._custom = {}
        self._attempted = {}
        self._feeds = {}
        self._source_names = tuple(sources)
        self._extra_feeds = tuple(extra_feeds)
        self._workers = workers
        self._timeout = timeout
        self._verify_site = verify_site
        self._capacity = capacity
        self._distinct_exits = distinct_exits
        self._protocols = frozenset(PROTOCOLS)
        self._latency = 0
        self._countries = frozenset()
        self.verified_total = 0
        self.tested_total = 0

    def start(self):
        with self._lock:
            if self._stop is not None and not self._stop.is_set():
                return
            self._stop = threading.Event()
            if self._thread is None:
                self._start_worker()

    def _start_worker(self):
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True,
                                        name="proxy-discovery")
        self._thread.start()

    def stop(self):
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            self._wake.set()

    def configure(self, protocol, latency, country, *, sources=None, verify_site=None):
        protocols = frozenset(PROTOCOLS if protocol == "all" else
                              (protocol,) if isinstance(protocol, str) else protocol)
        countries = country_codes(country) if isinstance(country, str) else frozenset(country)
        with self._lock:
            if sources is not None and tuple(sources) != self._source_names:
                self._source_names = tuple(sources)
                self._pool = {key: proxy for key, proxy in self._pool.items() if proxy.custom}
                self._attempted.clear()
            if verify_site is not None and verify_site != self._verify_site:
                self._verify_site = verify_site
                self._pool.clear()
                self._attempted.clear()
            if (protocols != self._protocols or latency > self._latency > 0
                    or not latency and self._latency):
                self._attempted.clear()
            self._protocols, self._latency, self._countries = protocols, latency, countries
            self._wake.set()

    def add_custom(self, entries):
        with self._lock:
            before = len(self._custom)
            for key in entries:
                if key in self._custom:
                    continue
                self._custom[key] = None
                self._attempted.pop(key, None)
                if key in self._pool:
                    self._pool[key].custom = True
            self._wake.set()
            return len(self._custom) - before

    def _eligible(self, proxy):
        return (proxy.protocol in self._protocols
                and (not self._latency or proxy.latency <= self._latency))

    def _prune(self, now):
        for key, proxy in list(self._pool.items()):
            if now - proxy.checked_at >= VERIFIED_TTL or not self._eligible(proxy):
                del self._pool[key]

    def snapshot(self):
        with self._lock:
            self._prune(time.monotonic())
            return sorted(self._pool.values(), key=rank)

    def countries(self):
        return Counter(proxy.cc for proxy in self.snapshot())

    def get_next(self, cc="Any", previous_ip=None):
        countries = country_codes(cc) if isinstance(cc, str) else cc
        with self._lock:
            self._prune(time.monotonic())
            eligible = (proxy for proxy in self._pool.values()
                        if not countries or proxy.cc in countries)
            proxy = min(eligible, key=lambda item: (item.ip == previous_ip, rank(item)), default=None)
            self._wake.set()
            return self._pool.pop((proxy.addr, proxy.protocol)) if proxy else None

    def _ready_count(self):
        return sum(not self._countries or proxy.cc in self._countries for proxy in self._pool.values())

    def _pool_rank(self, proxy):
        return bool(self._countries and proxy.cc not in self._countries), rank(proxy)

    def _run(self, stop):
        try:
            with ThreadPoolExecutor(max_workers=self._workers,
                                    thread_name_prefix="proxy-check") as workers:
                while not stop.is_set():
                    self._wake.clear()
                    with self._lock:
                        self._prune(time.monotonic())
                        refill = self._ready_count() < QUEUE_LOW_MARK
                    if refill:
                        self.scan(workers, stop)
                    self._wake.wait(3)
        finally:
            with self._lock:
                self._thread = None
                if self._stop is not stop and not self._stop.is_set():
                    self._start_worker()

    def _refresh_sources(self, workers, stop):
        with self._lock:
            feeds = [*discovery_feeds(self._source_names, self._countries), *self._extra_feeds]
            configuration = self._configuration()
        self._feeds = {feed: self._feeds[feed] for feed in feeds if feed in self._feeds}
        now = time.monotonic()
        downloads = {workers.submit(fetch_source, feed): feed for feed in feeds
                     if feed not in self._feeds or self._feeds[feed][0] <= now}
        for download, feed in downloads.items():
            try:
                entries = download.result()
            except (RequestException, ValueError):
                entries = []
            checked = time.monotonic()
            if entries:
                self._feeds[feed] = (checked + SOURCE_REFRESH, checked, entries)
            else:
                _, last_good, cached = self._feeds.get(feed, (0, 0, []))
                self._feeds[feed] = (checked + 30, last_good, cached)
        groups = {True: [], False: []}
        available = []
        for feed in feeds:
            _, last_good, entries = self._feeds[feed]
            if not entries or time.monotonic() - last_good > 5 * SOURCE_REFRESH:
                continue
            available.append(feed.name)
            protocols = {}
            for entry in entries:
                if entry[1] in configuration[0]:
                    protocols.setdefault(entry[1], []).append(entry)
            groups[feed.name.startswith("proxyscrape-api/")].append(interleave(protocols.values()))
        ordered = dict.fromkeys(interleave(interleave(group) for group in groups.values()))
        if downloads and not stop.is_set():
            names = ", ".join(available) or "none"
            self._log(f"{len(ordered)} unique candidates from {len(available)}/{len(feeds)} feeds: {names}.")
        return configuration, ordered

    def _configuration(self):
        return self._protocols, self._latency, self._countries, self._source_names, self._verify_site

    def _probe(self, key, timeout, verify_site, latency_cap):
        proxy = Proxy(*key, ip="", cc="??", latency=0, checked_at=0)
        result = check_connection(proxy, timeout)
        if result is None:
            return None
        proxy.ip, proxy.cc, proxy.latency = result
        if latency_cap and proxy.latency > latency_cap:
            return None
        if verify_site:
            latency = verify_destination(proxy, timeout)
            if latency is None:
                return None
            proxy.latency = max(proxy.latency, latency)
        proxy.checked_at = time.monotonic()
        return proxy

    def scan(self, workers, stop, budget=MAX_TEST_BATCH, *, exhaustive=False):
        """Scan at most budget candidates, keeping only one wave of work in flight."""
        configuration, sources = self._refresh_sources(workers, stop)
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._attempted = {key: stamp for key, stamp in self._attempted.items()
                               if now - stamp < RETRY_AFTER}
            candidates = list(islice((
                key for key in dict.fromkeys([*self._custom, *sources])
                if key not in self._pool and key not in self._attempted
                and key[1] in configuration[0]
            ), budget))
            latency, verify_site = configuration[1], configuration[4]
            timeout = min(self._timeout, latency / 1000) if latency else self._timeout
        candidates = iter(candidates)
        pending = {}
        while not stop.is_set():
            while len(pending) < self._workers:
                with self._lock:
                    if (stop.is_set() or configuration != self._configuration()
                            or not exhaustive and self._ready_count() >= self._capacity):
                        break
                    key = next(candidates, None)
                    if key is None:
                        break
                    self._attempted[key] = time.monotonic()
                    self.tested_total += 1
                pending[workers.submit(self._probe, key, timeout, verify_site, latency)] = key
            if not pending:
                return
            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for future in done:
                key = pending.pop(future)
                proxy = future.result()
                if proxy is None:
                    continue
                with self._lock:
                    if (stop.is_set() or configuration != self._configuration()
                            or not self._eligible(proxy)):
                        continue
                    self._prune(time.monotonic())
                    proxy.custom = key in self._custom
                    self.verified_total += 1
                    if self._distinct_exits:
                        same_exit = next((item for item in self._pool.values()
                                          if item.ip == proxy.ip), None)
                        if same_exit and self._pool_rank(same_exit) <= self._pool_rank(proxy):
                            continue
                        if same_exit:
                            del self._pool[same_exit.addr, same_exit.protocol]
                    self._pool[key] = proxy
                    if len(self._pool) > self._capacity:
                        worst = max(self._pool, key=lambda item: self._pool_rank(self._pool[item]))
                        del self._pool[worst]
                self._change()
