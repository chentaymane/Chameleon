"""Discovery, HTTPS verification and ranking for Chameleon's proxy pool."""

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from ipaddress import ip_address
import re
import threading
import time
from urllib.parse import quote, unquote, urlsplit

import requests

PROXY_SOURCES = (
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt",
    "https://raw.githubusercontent.com/proxyscrape/free-proxy-list/main/proxies/all/data.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
)
PROTOCOLS = ("http", "socks4", "socks5")
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
TEST_TIMEOUT = 5
TEST_WORKERS = 24
QUEUE_LOW_MARK = 8
QUEUE_HIGH_MARK = 40
MAX_TEST_BATCH = 400
SOURCE_REFRESH = 120
RETRY_AFTER = 180
VERIFIED_TTL = 180


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

    if "@" not in line and not line.startswith("[") and line.count(":") >= 3:
        host, port, user, password = line.split(":", 3)
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
    except (requests.RequestException, ValueError):
        return None
    country = fields.get("loc", "")
    if not re.fullmatch(r"[A-Z]{2}", country) or country in {"XX", "T1"}:
        country = "??"
    return str(ip), country, round((time.monotonic() - started) * 1000)


def fetch_source(url):
    """Accept only public IP literals from untrusted feeds."""
    text = get_text(url, 2_000_000, 8)
    candidates = {}
    for line in text.splitlines():
        if "://" not in line:
            continue
        entry = parse_proxy_line(line)
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

    def __init__(self, on_log, on_change):
        self._log = on_log
        self._change = on_change
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = None
        self._thread = None
        self._pool = {}
        self._custom = {}
        self._attempted = {}
        self._sources = []
        self._refresh_at = 0
        self._protocol = "all"
        self._latency = 0
        self._country = "Any"
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

    def configure(self, protocol, latency, country):
        with self._lock:
            if (protocol, latency, country) == (self._protocol, self._latency, self._country):
                return
            self._protocol, self._latency, self._country = protocol, latency, country
            self._wake.set()

    def add_custom(self, entries):
        with self._lock:
            before = len(self._custom)
            self._custom.update(dict.fromkeys(entries))
            self._wake.set()
            return len(self._custom) - before

    def _eligible(self, proxy):
        return ((self._protocol == "all" or self._protocol == proxy.protocol)
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

    def get_next(self, cc="Any"):
        with self._lock:
            self._prune(time.monotonic())
            key = min((key for key, proxy in self._pool.items() if cc in ("Any", proxy.cc)),
                      key=lambda entry: rank(self._pool[entry]), default=None)
            self._wake.set()
            return self._pool.pop(key, None)

    def _ready_count(self):
        return sum(self._country in ("Any", proxy.cc) for proxy in self._pool.values())

    def _run(self, stop):
        try:
            with ThreadPoolExecutor(max_workers=TEST_WORKERS,
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
        if time.monotonic() < self._refresh_at:
            return
        downloads = [workers.submit(fetch_source, url) for url in PROXY_SOURCES]
        groups = {}
        successes = 0
        for download in downloads:
            try:
                entries = download.result()
            except (requests.RequestException, ValueError):
                continue
            if entries:
                successes += 1
            for entry in entries:
                groups.setdefault(entry[1], {})[entry] = None
        # Interleave protocols so large HTTP feeds cannot starve SOCKS.
        ordered = []
        sources = [iter(group) for group in groups.values()]
        while sources:
            for source in sources[:]:
                entry = next(source, None)
                if entry is None:
                    sources.remove(source)
                else:
                    ordered.append(entry)
        with self._lock:
            if stop.is_set():
                return
            self._refresh_at = time.monotonic() + (SOURCE_REFRESH if successes else 15)
            if successes:
                self._sources = ordered
        self._log(f"Proxy feeds: {successes}/{len(PROXY_SOURCES)} available, {len(ordered)} unique candidates.")

    def _probe(self, key, custom, timeout):
        proxy = Proxy(*key, ip="", cc="??", latency=0, checked_at=0, custom=custom)
        result = check_connection(proxy, timeout)
        if result is None:
            return None
        proxy.ip, proxy.cc, proxy.latency = result
        proxy.checked_at = time.monotonic()
        return proxy

    def scan(self, workers, stop, budget=MAX_TEST_BATCH):
        """Scan at most budget candidates, keeping only one wave of work in flight."""
        self._refresh_sources(workers, stop)
        with self._lock:
            now = time.monotonic()
            self._attempted = {key: stamp for key, stamp in self._attempted.items()
                               if now - stamp < RETRY_AFTER}
            candidates = [
                (key, key in self._custom)
                for key in dict.fromkeys([*self._custom, *self._sources])
                if key not in self._pool and key not in self._attempted
                and self._protocol in ("all", key[1])
            ][:budget]
            protocol, latency = self._protocol, self._latency
            timeout = min(TEST_TIMEOUT, latency / 1000) if latency else TEST_TIMEOUT
        candidates = iter(candidates)
        pending = {}
        while not stop.is_set():
            while len(pending) < TEST_WORKERS:
                with self._lock:
                    if (stop.is_set() or self._ready_count() >= QUEUE_HIGH_MARK
                            or (protocol, latency) != (self._protocol, self._latency)):
                        break
                    candidate = next(candidates, None)
                    if candidate is None:
                        break
                    key, custom = candidate
                    self._attempted[key] = time.monotonic()
                    self.tested_total += 1
                pending[workers.submit(self._probe, key, custom, timeout)] = key
            if not pending:
                return
            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for future in done:
                key = pending.pop(future)
                proxy = future.result()
                if proxy is None:
                    continue
                with self._lock:
                    if stop.is_set() or not self._eligible(proxy):
                        continue
                    self.verified_total += 1
                    self._pool[key] = proxy
                    if len(self._pool) > QUEUE_HIGH_MARK:
                        worst = max(self._pool, key=lambda item: (
                            self._country not in ("Any", self._pool[item].cc), rank(self._pool[item])
                        ))
                        del self._pool[worst]
                self._change()
