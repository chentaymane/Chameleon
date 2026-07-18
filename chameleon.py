# -*- coding: utf-8 -*-
"""
Chameleon — cross-platform smart IP rotation.

Blend into a new location on demand: fetch, test and geo-tag free/paid proxies
(HTTP · SOCKS4 · SOCKS5), then rotate the OS system proxy on a timer or manually,
optionally pinned to a chosen country.

Highlights
  - Background workers keep a queue of verified, latency-measured proxies ready.
  - Every proxy is geo-tagged; pick the country for the next rotation.
  - Load your own paid proxy list (host:port:user:pass supported).
  - Optional health check re-tests the active proxy and auto-rotates on failure.
  - Live session stats: queue depth, proxies verified, rotations, per-country mix.
  - On stop / close: the system proxy is restored to a direct connection.

Platforms
  - Windows : WinINET system proxy (Chrome, Edge, IE, WinINET apps).
  - Linux   : GNOME gsettings proxy (apps that follow it).
  - macOS   : networksetup on active network services.
On any platform you can also copy the active proxy string into an anti-detect
browser profile (Multilogin / AdsPower / GoLogin).
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog
import threading
import queue
import platform
import subprocess
import time
import random
import requests
from collections import Counter, deque
from datetime import datetime

# ── Platform detection & guarded imports ──────────────────────────────────────

_SYS       = platform.system()
IS_WINDOWS = _SYS == "Windows"
IS_LINUX   = _SYS == "Linux"
IS_MAC     = _SYS == "Darwin"

if IS_WINDOWS:
    import winreg
    import ctypes

# ── Tuning constants ──────────────────────────────────────────────────────────

# (url, protocol) — protocol is how the listed proxies must be dialled.
PROXY_SOURCES = [
    ("https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=anonymous", "http"),
    ("https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=elite", "http"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt", "http"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt", "http"),
    ("https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks4&timeout=10000&country=all", "socks4"),
    ("https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5&timeout=10000&country=all", "socks5"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
]

# Step-1 test: proxy's own IP + geolocation (returns the country fields we need).
GEO_TEST_URL       = "http://ip-api.com/json/?fields=status,country,countryCode,query"
# Step-2 test: prove the proxy can open an HTTPS CONNECT tunnel — the exact
# thing a browser needs. Plain-HTTP proxies that pass step 1 but can't tunnel
# HTTPS are what cause ERR_TUNNEL_CONNECTION_FAILED in the browser, so this
# step is deliberately an https:// URL to filter them out.
SECONDARY_TEST_URL = "https://www.google.com/generate_204"

TEST_TIMEOUT    = 7      # seconds per proxy test
TEST_WORKERS    = 16     # concurrent test threads
QUEUE_LOW_MARK  = 8      # refetch when queue drops below this
QUEUE_HIGH_MARK = 40     # stop testing when queue reaches this
MAX_TEST_BATCH  = 400    # proxies tested per fetch cycle
HEALTH_INTERVAL = 20     # seconds between active-proxy health checks

# All of the app's own outbound requests (fetching proxy lists, validating
# proxies, IP checks, health checks) go through this session. trust_env=False
# makes it ignore any ambient/system proxy env vars (HTTP_PROXY, ALL_PROXY,
# GNOME/desktop-exported proxies). Otherwise, if the OS session already has a
# proxy configured — including a stale/dead one left by a previous run — every
# source fetch would be tunnelled through it and return nothing ("Fetched 0").
# Explicit per-request `proxies=` still applies (that's how we test proxies).
HTTP = requests.Session()
HTTP.trust_env = False

# HTTP-only IP-check endpoints, tried in order until one responds.
IP_CHECK_ENDPOINTS = [
    ("http://ip-api.com/json/?fields=query,country,countryCode", lambda r: r.json().get("query", "")),
    ("http://api.ipify.org?format=json",                          lambda r: r.json().get("ip", "")),
    ("http://checkip.amazonaws.com/",                             lambda r: r.text.strip()),
    ("http://ifconfig.me/ip",                                     lambda r: r.text.strip()),
    ("http://ipecho.net/plain",                                   lambda r: r.text.strip()),
]

# ── Colour palette & fonts ─────────────────────────────────────────────────────

C = dict(
    bg="#181825", surface="#1e1e2e", panel="#313244", muted="#45475a",
    text="#cdd6f4", dim="#a6adc8", hint="#6c7086", blue="#89b4fa",
    red="#f38ba8", green="#a6e3a1", yellow="#f9e2af", orange="#fab387",
    mauve="#cba6f7", teal="#94e2d5",
)
F = dict(
    h1=("Segoe UI Semibold", 15), h2=("Segoe UI Semibold", 10),
    body=("Segoe UI", 9), small=("Segoe UI", 8), mono=("Consolas", 9),
    stat=("Segoe UI Semibold", 14), btn=("Segoe UI Semibold", 10),
)


# ── Proxy model ───────────────────────────────────────────────────────────────

class Proxy:
    """A single verified proxy with its measured metadata."""
    __slots__ = ("addr", "protocol", "cc", "country", "latency")

    def __init__(self, addr, protocol, cc, country, latency):
        self.addr     = addr        # "host:port" (may include user:pass@)
        self.protocol = protocol    # http | socks4 | socks5
        self.cc       = cc or "??"  # ISO country code, e.g. "US"
        self.country  = country or "Unknown"
        self.latency  = latency     # ms (int)

    def requests_dict(self):
        scheme = {"http": "http", "socks4": "socks4", "socks5": "socks5h"}[self.protocol]
        url = f"{scheme}://{self.addr}"
        return {"http": url, "https": url}

    def __str__(self):
        return f"{self.addr}  ·  {self.protocol}  ·  {self.cc}  ·  {self.latency} ms"


# ── System proxy control (cross-platform) ─────────────────────────────────────

class SystemProxy:
    """Applies/removes the OS-level proxy so browsers follow it."""

    # --- Windows (WinINET registry) ---
    @staticmethod
    def _win_key(write=False):
        flag = winreg.KEY_WRITE if write else winreg.KEY_READ
        return winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, flag,
        )

    @staticmethod
    def _win_notify():
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH

    @classmethod
    def _win_enable(cls, host, port, protocol):
        value = f"socks={host}:{port}" if protocol in ("socks4", "socks5") else f"{host}:{port}"
        key = cls._win_key(write=True)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        cls._win_notify()

    @classmethod
    def _win_disable(cls):
        key = cls._win_key(write=True)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
        cls._win_notify()

    # --- Linux (GNOME gsettings) ---
    @staticmethod
    def _gset(*args):
        subprocess.run(["gsettings", *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def _linux_enable(cls, host, port, protocol):
        cls._gset("set", "org.gnome.system.proxy", "mode", "manual")
        if protocol in ("socks4", "socks5"):
            cls._gset("set", "org.gnome.system.proxy.socks", "host", host)
            cls._gset("set", "org.gnome.system.proxy.socks", "port", str(port))
        else:
            for scheme in ("http", "https"):
                cls._gset("set", f"org.gnome.system.proxy.{scheme}", "host", host)
                cls._gset("set", f"org.gnome.system.proxy.{scheme}", "port", str(port))

    @classmethod
    def _linux_disable(cls):
        cls._gset("set", "org.gnome.system.proxy", "mode", "none")

    # --- macOS (networksetup) ---
    @staticmethod
    def _mac_services():
        out = subprocess.run(["networksetup", "-listallnetworkservices"],
                             capture_output=True, text=True)
        return [s for s in out.stdout.splitlines()[1:] if s and not s.startswith("*")]

    @classmethod
    def _mac_enable(cls, host, port, protocol):
        socks = protocol in ("socks4", "socks5")
        for svc in cls._mac_services():
            if socks:
                subprocess.run(["networksetup", "-setsocksfirewallproxy", svc, host, str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                for cmd in ("-setwebproxy", "-setsecurewebproxy"):
                    subprocess.run(["networksetup", cmd, svc, host, str(port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def _mac_disable(cls):
        for svc in cls._mac_services():
            for cmd in ("-setwebproxystate", "-setsecurewebproxystate", "-setsocksfirewallproxystate"):
                subprocess.run(["networksetup", cmd, svc, "off"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- Public API ---
    @classmethod
    def enable(cls, host, port, protocol) -> bool:
        try:
            if IS_WINDOWS:  cls._win_enable(host, port, protocol)
            elif IS_LINUX:  cls._linux_enable(host, port, protocol)
            elif IS_MAC:    cls._mac_enable(host, port, protocol)
            else:           return False
            return True
        except Exception:
            return False

    @classmethod
    def disable(cls):
        try:
            if IS_WINDOWS:  cls._win_disable()
            elif IS_LINUX:  cls._linux_disable()
            elif IS_MAC:    cls._mac_disable()
        except Exception:
            pass

    @staticmethod
    def platform_note() -> str:
        if IS_WINDOWS: return "Windows system proxy (Chrome, Edge, WinINET apps)."
        if IS_LINUX:   return "GNOME gsettings proxy (apps that follow it)."
        if IS_MAC:     return "macOS networksetup proxy on active services."
        return "Unsupported OS — copy the proxy into your browser manually."


# ── Background proxy manager ──────────────────────────────────────────────────

class ProxyManager:
    """
    Continuously fetches, tests and geo-tags proxies into a country-aware pool.
    Thread-safe. The rotation loop pulls from it, optionally filtered by country.
    """

    def __init__(self, on_log, on_change):
        self._log       = on_log
        self._change    = on_change
        self._pool: deque[Proxy] = deque()
        self._lock      = threading.Lock()
        self._seen      = set()
        self._seen_lock = threading.Lock()
        self._sem       = threading.Semaphore(TEST_WORKERS)
        self._active    = False
        self._custom: list[tuple[str, str]] = []

        # Live filters (set from the UI thread; read by workers).
        self.protocol_filter = "all"   # all | http | socks4 | socks5
        self.max_latency     = 0       # ms; 0 = no cap

        # Session counters.
        self.verified_total = 0        # proxies that ever passed testing
        self.tested_total   = 0        # proxies that were attempted

    # ── Introspection ──
    @property
    def ready(self) -> int:
        with self._lock:
            return len(self._pool)

    def countries(self) -> Counter:
        with self._lock:
            return Counter(p.cc for p in self._pool)

    def snapshot(self):
        with self._lock:
            return list(self._pool)

    # ── Lifecycle ──
    def start(self):
        if not self._active:
            self._active = True
            threading.Thread(target=self._fetch_loop, daemon=True,
                             name="proxy-fetcher").start()

    def stop(self):
        self._active = False

    def add_custom(self, entries):
        added = 0
        with self._lock:
            existing = {a for a, _ in self._custom}
            for addr, proto in entries:
                if addr not in existing:
                    self._custom.append((addr, proto))
                    existing.add(addr)
                    added += 1
        return added

    def get_next(self, cc=None) -> Proxy | None:
        with self._lock:
            if not self._pool:
                return None
            if cc and cc != "Any":
                for i, p in enumerate(self._pool):
                    if p.cc == cc:
                        del self._pool[i]
                        return p
                return None
            return self._pool.popleft()

    # ── Internal ──
    def _sources(self):
        pf = self.protocol_filter
        for url, proto in PROXY_SOURCES:
            if pf == "all" or pf == proto:
                yield url, proto

    def _fetch_loop(self):
        while self._active:
            if self.ready < QUEUE_LOW_MARK:
                self._fetch_batch()
            time.sleep(6)

    def _fetch_batch(self):
        raw: list[tuple[str, str]] = []
        pf = self.protocol_filter

        # Custom proxies first — the user paid for these; keep retrying them.
        for addr, proto in list(self._custom):
            if pf == "all" or pf == proto:
                raw.append((addr, proto))

        errors: list[str] = []
        sources_ok = 0
        for url, proto in self._sources():
            if not self._active:
                return
            try:
                r = HTTP.get(url, timeout=14)
                sources_ok += 1
                for line in r.text.splitlines():
                    line = line.strip()
                    if ":" in line and len(line) < 24 and " " not in line:
                        raw.append((line, proto))
            except Exception as e:
                errors.append(type(e).__name__)

        with self._seen_lock:
            custom_addrs = {a for a, _ in self._custom}
            new = [(a, pr) for a, pr in raw if a in custom_addrs or a not in self._seen]
        random.shuffle(new)
        batch = new[:MAX_TEST_BATCH]
        self._log(f"Fetched {len(raw)} candidates — testing {len(batch)}…")
        if not raw and errors:
            uniq = ", ".join(sorted(set(errors)))
            self._log(f"⚠  Could not reach any proxy source ({sources_ok} ok, "
                      f"{len(errors)} failed: {uniq}). Check your internet / "
                      f"disable any system VPN or proxy, then it'll retry.")

        for addr, proto in batch:
            if not self._active or self.ready >= QUEUE_HIGH_MARK:
                break
            threading.Thread(target=self._test_one, args=(addr, proto),
                             daemon=True).start()

    def _test_one(self, addr, proto):
        with self._sem:
            if self.ready >= QUEUE_HIGH_MARK:
                return
            self.tested_total += 1
            proxy = Proxy(addr, proto, None, None, 0)
            p = proxy.requests_dict()
            try:
                t0 = time.time()
                r = HTTP.get(GEO_TEST_URL, proxies=p, timeout=TEST_TIMEOUT)
                latency = int((time.time() - t0) * 1000)
                if r.status_code != 200:
                    return
                data = r.json()
                if data.get("status") != "success":
                    return
                ip = data.get("query", "")
                if not ip or "." not in ip:
                    return
                # Early reject on the cheap HTTP hop before paying for the tunnel.
                if self.max_latency and latency > self.max_latency:
                    return
                t1 = time.time()
                r2 = HTTP.get(SECONDARY_TEST_URL, proxies=p, timeout=TEST_TIMEOUT)
                if r2.status_code not in (200, 204, 301, 302):
                    return
                # Browsing is HTTPS, so the tunnel hop is the honest speed
                # measure — keep the slower of the two so the cap means what
                # the user expects.
                latency = max(latency, int((time.time() - t1) * 1000))
                if self.max_latency and latency > self.max_latency:
                    return
                proxy.cc      = data.get("countryCode") or "??"
                proxy.country = data.get("country") or "Unknown"
                proxy.latency = latency
            except Exception:
                return

            with self._seen_lock:
                if addr in self._seen:
                    return
                self._seen.add(addr)
            with self._lock:
                self._pool.append(proxy)
                n = len(self._pool)
            self.verified_total += 1
            self._log(f"✓  {addr}  [{proto}]  {proxy.cc} {ip}  {latency} ms   (queue: {n})")
            self._change()


# ── Proxy list parsing ────────────────────────────────────────────────────────

def parse_proxy_line(line, default_proto="http"):
    """
    Accepts common formats and returns (addr, protocol) or None:
      host:port
      protocol://host:port
      host:port:user:pass
      protocol://user:pass@host:port
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    proto = default_proto
    if "://" in line:
        proto, line = line.split("://", 1)
        proto = proto.lower()
        if proto not in ("http", "https", "socks4", "socks5"):
            proto = default_proto
        if proto == "https":
            proto = "http"
    if "@" in line:
        return (line, proto)
    parts = line.split(":")
    if len(parts) == 2:
        return (line, proto)
    if len(parts) == 4:
        host, port, user, pw = parts
        return (f"{user}:{pw}@{host}:{port}", proto)
    return None


# ── GUI ───────────────────────────────────────────────────────────────────────

class App:
    APP_NAME = "Chameleon"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(self.APP_NAME)
        self.root.geometry("760x720")
        self.root.minsize(700, 660)
        self.root.configure(bg=C["bg"])

        self._running = False
        self._current: Proxy | None = None
        self._rotations = 0
        self._sel_country_code = "Any"

        self._ui_queue: queue.Queue = queue.Queue()

        self._manager = ProxyManager(
            on_log    = lambda msg: self._ui(self._log, msg),
            on_change = lambda: self._ui(self._refresh_status),
        )

        self._apply_style()
        self._build_ui()
        self.root.after(100, self._drain_ui_queue)
        self.root.after(1200, self._tick_status)

    # ── Small UI helpers ──────────────────────────────────────────────────────
    def _card(self, parent, title=None):
        """A rounded-looking surface panel with optional header."""
        outer = tk.Frame(parent, bg=C["surface"], highlightthickness=1,
                         highlightbackground=C["panel"])
        outer.pack(fill="x", pady=6)
        if title:
            tk.Label(outer, text=title.upper(), bg=C["surface"], fg=C["hint"],
                     font=F["small"]).pack(anchor="w", padx=14, pady=(10, 0))
        body = tk.Frame(outer, bg=C["surface"])
        body.pack(fill="x", padx=14, pady=(6, 12))
        return body

    def _btn(self, parent, text, cmd, kind="ghost", **kw):
        styles = {
            "primary": dict(bg=C["blue"], fg="#11111b"),
            "danger":  dict(bg=C["red"],  fg="#11111b"),
            "ghost":   dict(bg=C["panel"], fg=C["text"]),
        }
        opts = dict(relief="flat", cursor="hand2", bd=0,
                    activebackground=C["muted"], activeforeground=C["text"],
                    font=F["btn"], padx=14, pady=7)
        opts.update(styles[kind])
        opts.update(kw)                       # caller overrides win
        return tk.Button(parent, text=text, command=cmd, **opts)

    def _combo(self, parent, var, values, width, on_change=None):
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=width,
                          font=F["body"], state="readonly")
        if on_change:
            cb.bind("<<ComboboxSelected>>", on_change)
        return cb

    # ── UI skeleton ───────────────────────────────────────────────────────────
    def _build_ui(self):
        # Header
        bar = tk.Frame(self.root, bg=C["panel"])
        bar.pack(fill="x")
        tk.Label(bar, text="🦎  " + self.APP_NAME, font=F["h1"],
                 bg=C["panel"], fg=C["text"], pady=10).pack(side="left", padx=16)
        tk.Label(bar, text=f"smart IP rotation  ·  {_SYS}", font=F["body"],
                 bg=C["panel"], fg=C["hint"]).pack(side="left")

        root = tk.Frame(self.root, bg=C["bg"])
        root.pack(fill="both", expand=True, padx=16, pady=(10, 12))

        self._build_ip_card(root)
        self._build_stats_card(root)
        self._build_controls_card(root)
        self._build_action_row(root)
        self._build_log(root)

    # ── Current-IP card ─────────────────────────────────────────────────────
    def _build_ip_card(self, parent):
        body = self._card(parent, "Current connection")
        top = tk.Frame(body, bg=C["surface"])
        top.pack(fill="x")

        self._dot = tk.Label(top, text="●", bg=C["surface"], fg=C["hint"], font=("Segoe UI", 13))
        self._dot.pack(side="left")
        self._ip_var = tk.StringVar(value="fetching…")
        tk.Label(top, textvariable=self._ip_var, bg=C["surface"], fg=C["blue"],
                 font=F["stat"]).pack(side="left", padx=8)
        self._btn(top, "Refresh", self._refresh_ip_async, "ghost",
                  padx=10, pady=4, font=F["body"]).pack(side="right")

        self._active_var = tk.StringVar(value="Proxy: none  ·  direct connection")
        row = tk.Frame(body, bg=C["surface"])
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, textvariable=self._active_var, bg=C["surface"], fg=C["dim"],
                 font=F["mono"]).pack(side="left")
        self._btn(row, "Copy", self._copy_active, "ghost",
                  padx=8, pady=2, font=F["small"]).pack(side="right")

    # ── Stats card ────────────────────────────────────────────────────────────
    def _build_stats_card(self, parent):
        body = self._card(parent, "Session")
        grid = tk.Frame(body, bg=C["surface"])
        grid.pack(fill="x")

        self._stat_queue = self._stat_cell(grid, 0, "Queue ready", C["yellow"])
        self._stat_verif = self._stat_cell(grid, 1, "Verified", C["green"])
        self._stat_rot   = self._stat_cell(grid, 2, "Rotations", C["mauve"])
        self._stat_tested= self._stat_cell(grid, 3, "Tested", C["teal"])
        for i in range(4):
            grid.columnconfigure(i, weight=1)

        self._countries_var = tk.StringVar(value="No countries discovered yet")
        tk.Label(body, textvariable=self._countries_var, bg=C["surface"], fg=C["dim"],
                 font=F["small"], anchor="w").pack(fill="x", pady=(10, 0))

    def _stat_cell(self, parent, col, label, color):
        cell = tk.Frame(parent, bg=C["surface"])
        cell.grid(row=0, column=col, sticky="ew")
        var = tk.StringVar(value="0")
        tk.Label(cell, textvariable=var, bg=C["surface"], fg=color,
                 font=F["stat"]).pack()
        tk.Label(cell, text=label, bg=C["surface"], fg=C["hint"],
                 font=F["small"]).pack()
        return var

    # ── Controls card ─────────────────────────────────────────────────────────
    def _build_controls_card(self, parent):
        body = self._card(parent, "Rotation settings")

        r1 = tk.Frame(body, bg=C["surface"])
        r1.pack(fill="x", pady=(0, 8))
        tk.Label(r1, text="Rotate every", bg=C["surface"], fg=C["dim"],
                 font=F["body"]).pack(side="left")
        self._interval_var = tk.StringVar(value="30")
        self._interval_cb = self._combo(r1, self._interval_var,
            ["Manual", "15", "30", "45", "60", "90", "120", "300"], 8)
        self._interval_cb.pack(side="left", padx=(6, 2))
        tk.Label(r1, text="sec", bg=C["surface"], fg=C["hint"],
                 font=F["small"]).pack(side="left")

        tk.Label(r1, text="Protocol", bg=C["surface"], fg=C["dim"],
                 font=F["body"]).pack(side="left", padx=(18, 0))
        self._proto_var = tk.StringVar(value="all")
        self._combo(r1, self._proto_var, ["all", "http", "socks4", "socks5"], 7,
                    self._on_filters_change).pack(side="left", padx=6)

        tk.Label(r1, text="Max latency", bg=C["surface"], fg=C["dim"],
                 font=F["body"]).pack(side="left", padx=(18, 0))
        # Default to a real cap: proxies slower than ~2.5 s are effectively
        # unusable for browsing (pages hang / time out), so don't queue them
        # unless the user deliberately widens this to "Any".
        self._lat_var = tk.StringVar(value="2500")
        self._combo(r1, self._lat_var, ["1000", "1500", "2000", "2500", "3000", "5000", "Any"], 7,
                    self._on_filters_change).pack(side="left", padx=6)
        tk.Label(r1, text="ms", bg=C["surface"], fg=C["hint"],
                 font=F["small"]).pack(side="left")

        r2 = tk.Frame(body, bg=C["surface"])
        r2.pack(fill="x")
        tk.Label(r2, text="Country for next proxy", bg=C["surface"], fg=C["mauve"],
                 font=F["h2"]).pack(side="left")
        self._country_var = tk.StringVar(value="Any")
        self._country_cb = self._combo(r2, self._country_var, ["Any"], 26,
                                       lambda _e: self._selected_country())
        self._country_cb.pack(side="left", padx=8)

        self._health_var = tk.BooleanVar(value=True)
        tk.Checkbutton(r2, text="Auto-recheck active proxy", variable=self._health_var,
                       bg=C["surface"], fg=C["dim"], selectcolor=C["panel"],
                       activebackground=C["surface"], activeforeground=C["text"],
                       font=F["body"], bd=0, highlightthickness=0).pack(side="right")

    # ── Action row + countdown ────────────────────────────────────────────────
    def _build_action_row(self, parent):
        wrap = tk.Frame(parent, bg=C["bg"])
        wrap.pack(fill="x", pady=(2, 0))

        self._start_btn = self._btn(wrap, "▶  Start", self._toggle, "primary")
        self._start_btn.pack(side="left", padx=(0, 6))
        self._next_btn = self._btn(wrap, "→ Next Proxy", self._force_rotate, "ghost",
                                   state="disabled")
        self._next_btn.pack(side="left", padx=6)
        self._btn(wrap, "Disable", self._disable_proxy_and_refresh, "ghost").pack(side="left", padx=6)
        self._btn(wrap, "Export", self._export_working, "ghost",
                  padx=10, font=F["body"]).pack(side="right")
        self._btn(wrap, "Load list…", self._load_custom, "ghost",
                  padx=10, font=F["body"]).pack(side="right", padx=6)

        cd = tk.Frame(parent, bg=C["bg"])
        cd.pack(fill="x", pady=(10, 0))
        self._cd_var = tk.StringVar(value="")
        tk.Label(cd, textvariable=self._cd_var, bg=C["bg"], fg=C["dim"],
                 font=F["small"]).pack(anchor="w")
        self._progress = ttk.Progressbar(cd, mode="determinate")
        self._progress.pack(fill="x", pady=(2, 0))
        self._wait_var = tk.StringVar(value="")
        tk.Label(cd, textvariable=self._wait_var, bg=C["bg"], fg=C["orange"],
                 font=F["small"]).pack(anchor="w")

    # ── Log ─────────────────────────────────────────────────────────────────
    def _build_log(self, parent):
        hdr = tk.Frame(parent, bg=C["bg"])
        hdr.pack(fill="x", pady=(8, 2))
        tk.Label(hdr, text="ACTIVITY LOG", bg=C["bg"], fg=C["hint"],
                 font=F["small"]).pack(side="left")
        self._btn(hdr, "Clear", self._clear_log, "ghost",
                  padx=8, pady=1, font=F["small"]).pack(side="right")
        self._log_widget = scrolledtext.ScrolledText(
            parent, height=9, bg="#11111b", fg=C["text"], font=F["mono"],
            relief="flat", bd=0, insertbackground=C["text"], state="disabled")
        self._log_widget.pack(fill="both", expand=True)

    # ── Filters ───────────────────────────────────────────────────────────────
    def _on_filters_change(self, _evt=None):
        self._manager.protocol_filter = self._proto_var.get()
        lat = self._lat_var.get()
        self._manager.max_latency = 0 if lat == "Any" else int(lat)
        self._log(f"Filters — protocol: {self._proto_var.get()}, max latency: {lat}")

    # ── Status refresh ────────────────────────────────────────────────────────
    def _refresh_status(self):
        counts = self._manager.countries()
        total = sum(counts.values())
        self._stat_queue.set(str(total))
        self._stat_verif.set(str(self._manager.verified_total))
        self._stat_tested.set(str(self._manager.tested_total))
        self._stat_rot.set(str(self._rotations))

        if counts:
            mix = "   ".join(f"{cc} {n}" for cc, n in counts.most_common(8))
            self._countries_var.set("In queue:   " + mix)
        else:
            self._countries_var.set("No countries discovered yet")

        values = ["Any"] + [f"{cc}  ({n})" for cc, n in counts.most_common()]
        codes  = ["Any"] + [cc for cc, _ in counts.most_common()]
        self._country_cb["values"] = values
        self._country_codes = codes
        if self._sel_country_code in codes:
            self._country_var.set(values[codes.index(self._sel_country_code)])

    def _tick_status(self):
        self._refresh_status()
        self.root.after(1200, self._tick_status)

    def _selected_country(self):
        val = self._country_var.get()
        self._sel_country_code = val.split(" ")[0] if val else "Any"
        return self._sel_country_code

    # ── Logging ───────────────────────────────────────────────────────────────
    def _ui(self, fn, *args):
        """Run a Tk-touching callable on the main thread.

        Tkinter is not thread-safe; calling widget methods from a background
        thread segfaults the whole process on Linux. Worse, on some Tk builds
        (e.g. the one uv ships on Linux) `root.after` from a background thread
        silently drops the callback. So marshal through a queue that the main
        thread drains on a short timer — the only mechanism that works
        everywhere.
        """
        if threading.current_thread() is threading.main_thread():
            fn(*args)
        else:
            self._ui_queue.put((fn, args))

    def _drain_ui_queue(self):
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def _log(self, text):
        self._ui(self._log_impl, text)

    def _log_impl(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_widget.config(state="normal")
        self._log_widget.insert("end", f"[{ts}] {text}\n")
        self._log_widget.see("end")
        self._log_widget.config(state="disabled")

    def _clear_log(self):
        self._log_widget.config(state="normal")
        self._log_widget.delete("1.0", "end")
        self._log_widget.config(state="disabled")

    # ── IP refresh ────────────────────────────────────────────────────────────
    def _refresh_ip_async(self):
        threading.Thread(target=self._do_fetch_ip, daemon=True).start()

    def _do_fetch_ip(self):
        kw = {}
        if self._current:
            kw["proxies"] = self._current.requests_dict()
        for url, extractor in IP_CHECK_ENDPOINTS:
            try:
                r = HTTP.get(url, timeout=10, **kw)
                ip = extractor(r)
                if ip and "." in ip:
                    tag = f"   {self._current.cc}" if self._current else ""
                    self._ui(self._ip_var.set, ip + tag)
                    self._ui(self._dot.config, {"fg": C["green"]})
                    return
            except Exception:
                continue
        label = "proxy unreachable" if self._current else "no internet?"
        self._ui(self._ip_var.set, label)
        self._ui(self._dot.config, {"fg": C["red"]})

    def _disable_proxy_and_refresh(self):
        SystemProxy.disable()
        self._current = None
        self._active_var.set("Proxy: none  ·  direct connection")
        self._log("Proxy disabled — using direct connection.")
        self._refresh_ip_async()

    # ── Custom list / export ──────────────────────────────────────────────────
    def _load_custom(self):
        path = filedialog.askopenfilename(
            title="Load proxy list",
            filetypes=[("Text/CSV", "*.txt *.csv"), ("All files", "*.*")])
        if not path:
            return
        default = self._proto_var.get()
        if default == "all":
            default = "http"
        entries = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parsed = parse_proxy_line(line, default)
                    if parsed:
                        entries.append(parsed)
        except Exception as e:
            self._log(f"✗  Could not read list: {e}")
            return
        added = self._manager.add_custom(entries)
        self._log(f"Loaded {added} custom proxies (default protocol: {default}). "
                  "They'll be tested and prioritised.")
        if not self._running:
            self._log("Tip: click Start to begin testing them.")

    def _export_working(self):
        pool = self._manager.snapshot()
        if not pool:
            self._log("Nothing to export yet — queue is empty.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile="working_proxies.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write("# protocol://host:port  country  latency_ms\n")
            for p in sorted(pool, key=lambda x: x.latency):
                f.write(f"{p.protocol}://{p.addr}  {p.cc}  {p.latency}\n")
        self._log(f"Exported {len(pool)} working proxies → {path}")

    def _copy_active(self):
        if not self._current:
            self._log("No active proxy to copy.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(f"{self._current.protocol}://{self._current.addr}")
        self._log("Active proxy copied to clipboard.")

    # ── Rotation control ──────────────────────────────────────────────────────
    def _toggle(self):
        self._stop() if self._running else self._start()

    def _start(self):
        self._running = True
        self._on_filters_change()
        manual = self._interval_var.get() == "Manual"
        self._start_btn.config(text="■  Stop", bg=C["red"])
        self._next_btn.config(state="normal")
        self._interval_cb.config(state="disabled")
        self._log("Manual mode — use '→ Next Proxy' to rotate." if manual
                  else "Rotation started — background fetcher running.")
        self._manager.start()
        threading.Thread(target=self._rotation_loop, daemon=True, name="rotator").start()
        if self._health_var.get():
            threading.Thread(target=self._health_loop, daemon=True, name="health").start()

    def _stop(self):
        self._running = False
        self._manager.stop()
        self._start_btn.config(text="▶  Start", bg=C["blue"])
        self._next_btn.config(state="disabled")
        self._interval_cb.config(state="readonly")
        self._cd_var.set("")
        self._wait_var.set("")
        self._progress.stop()
        self._progress.config(mode="determinate")
        self._progress["value"] = 0
        SystemProxy.disable()
        self._current = None
        self._active_var.set("Proxy: none  ·  direct connection")
        self._log("Stopped — proxy disabled, direct connection restored.")
        self._refresh_ip_async()

    def _apply_proxy(self, proxy: Proxy):
        host, port = proxy.addr.rsplit(":", 1)
        host = host.split("@")[-1]      # strip credentials for the OS host field
        if SystemProxy.enable(host, port, proxy.protocol):
            self._current = proxy
            self._rotations += 1
            self._ui(self._active_var.set, f"Proxy: {proxy}")
            self._log(f"→  Active: {proxy}")
            self._refresh_ip_async()
        else:
            self._log(f"✗  Could not set proxy {proxy.addr}")

    def _force_rotate(self):
        cc = self._selected_country()
        proxy = self._manager.get_next(cc)
        if proxy:
            self._apply_proxy(proxy)
            self._wait_var.set("")
        else:
            where = "" if cc == "Any" else f" from {cc}"
            self._log(f"No verified proxy{where} in queue yet — still fetching…")

    def _rotation_loop(self):
        self._do_wait_and_rotate()
        while self._running:
            if self._interval_var.get() == "Manual":
                self._ui(self._cd_var.set,
                                "Manual mode — click '→ Next Proxy' to rotate")
                self._ui(self._progress.__setitem__, "value", 0)
                time.sleep(1)
                continue
            interval = int(self._interval_var.get())
            for remaining in range(interval, 0, -1):
                if not self._running or self._interval_var.get() == "Manual":
                    break
                pct = (1 - remaining / interval) * 100
                self._ui(self._set_countdown, remaining, pct)
                time.sleep(1)
            if self._running and self._interval_var.get() != "Manual":
                self._do_wait_and_rotate()
        self._ui(self._cd_var.set, "")
        self._ui(self._wait_var.set, "")
        self._ui(self._progress.__setitem__, "value", 0)

    def _do_wait_and_rotate(self):
        cc = self._selected_country()
        proxy = self._manager.get_next(cc)
        if proxy is None:
            where = "" if cc == "Any" else f" ({cc})"
            self._log(f"Timer fired — no proxy{where} ready, waiting…")
            self._ui(self._wait_var.set, f"⏳ waiting for proxy{where}…")
            self._ui(self._cd_var.set, "Holding — waiting for a verified proxy…")
            self._ui(self._progress.config, {"mode": "indeterminate"})
            self._ui(self._progress.start, 12)
            while self._running:
                proxy = self._manager.get_next(self._selected_country())
                if proxy:
                    break
                time.sleep(0.5)
            self._ui(self._progress.stop)
            self._ui(self._progress.config, {"mode": "determinate"})
            self._ui(self._wait_var.set, "")
        if proxy and self._running:
            self._apply_proxy(proxy)

    def _set_countdown(self, remaining, pct):
        self._cd_var.set(f"Next rotation in {remaining}s")
        self._progress["value"] = pct

    def _health_loop(self):
        while self._running:
            for _ in range(HEALTH_INTERVAL):
                if not self._running:
                    return
                time.sleep(1)
            if not self._health_var.get() or not self._current:
                continue
            try:
                r = HTTP.get(GEO_TEST_URL, proxies=self._current.requests_dict(),
                                 timeout=TEST_TIMEOUT)
                ok = r.status_code == 200 and r.json().get("status") == "success"
            except Exception:
                ok = False
            if not ok and self._running:
                self._ui(self._log,
                                f"⚠  Active proxy {self._current.addr} failed health check — rotating.")
                self._do_wait_and_rotate()

    # ── Style ─────────────────────────────────────────────────────────────────
    def _apply_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TCombobox", fieldbackground=C["panel"], background=C["panel"],
                    foreground=C["text"], selectbackground=C["panel"],
                    selectforeground=C["text"], arrowcolor=C["dim"], bordercolor=C["muted"])
        s.map("TCombobox", fieldbackground=[("readonly", C["panel"])],
              foreground=[("readonly", C["text"])])
        s.configure("TProgressbar", troughcolor=C["panel"], background=C["blue"],
                    borderwidth=0, thickness=8)
        s.configure("Vertical.TScrollbar", background=C["muted"], troughcolor=C["bg"],
                    borderwidth=0, arrowcolor=C["dim"])

    # ── Entry point ───────────────────────────────────────────────────────────
    def _on_close(self):
        self._running = False
        self._manager.stop()
        SystemProxy.disable()
        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_ip_async()
        self._log(f"{self.APP_NAME} ready on {_SYS}. {SystemProxy.platform_note()}")
        self._log("Click 'Start' — the proxy fetcher runs automatically.")
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
