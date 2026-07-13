# -*- coding: utf-8 -*-
"""
Anti-Detect Suite — cross-platform smart IP rotation.

Key behaviour:
  - Background threads fetch & test proxies continuously from public sources
    (HTTP / SOCKS4 / SOCKS5) plus any custom list you load.
  - The queue ALWAYS holds tested, working proxies, each tagged with its
    country and measured latency.
  - When the timer fires: if a proxy matching your filters is ready → rotate
    instantly. If not → show "waiting…" and hold until one is verified.
  - Manual mode: pick the country for the *next* proxy, then click Next Proxy.
  - Optional health check re-tests the active proxy and auto-rotates if it dies.
  - On stop / close: the system proxy is restored to a direct connection.

Platforms:
  - Windows : WinINET system proxy (Chrome, Edge, IE, and WinINET apps).
  - Linux   : GNOME gsettings proxy (GNOME/Cinnamon/Unity apps that follow it).
  - macOS   : networksetup on active network services.
On any platform you can also just copy the active proxy string and paste it
into an anti-detect browser (Multilogin / AdsPower / GoLogin) profile.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog
import threading
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

# Step-1 test: proxy's own IP + geolocation (returns country fields we need).
GEO_TEST_URL       = "http://ip-api.com/json/?fields=status,country,countryCode,query"
# Step-2 test: prove the proxy reaches a real site, not just whitelisted APIs.
SECONDARY_TEST_URL = "http://example.com"

TEST_TIMEOUT    = 7      # seconds per proxy test
TEST_WORKERS    = 16     # concurrent test threads
QUEUE_LOW_MARK  = 8      # refetch when queue drops below this
QUEUE_HIGH_MARK = 40     # stop testing when queue reaches this
MAX_TEST_BATCH  = 400    # proxies tested per fetch cycle
HEALTH_INTERVAL = 20     # seconds between active-proxy health checks

# HTTP-only IP-check endpoints, tried in order until one responds.
IP_CHECK_ENDPOINTS = [
    ("http://ip-api.com/json/?fields=query,country,countryCode", lambda r: r.json().get("query", "")),
    ("http://api.ipify.org?format=json",                          lambda r: r.json().get("ip", "")),
    ("http://checkip.amazonaws.com/",                             lambda r: r.text.strip()),
    ("http://ifconfig.me/ip",                                     lambda r: r.text.strip()),
    ("http://ipecho.net/plain",                                   lambda r: r.text.strip()),
]

# ── Colour palette ────────────────────────────────────────────────────────────

C = dict(
    bg="#1e1e2e", panel="#313244", muted="#45475a", text="#cdd6f4",
    dim="#a6adc8", hint="#6c7086", blue="#89b4fa", red="#f38ba8",
    green="#a6e3a1", yellow="#f9e2af", orange="#fab387", mauve="#cba6f7",
)


# ── Proxy model ───────────────────────────────────────────────────────────────

class Proxy:
    """A single verified proxy with its measured metadata."""
    __slots__ = ("addr", "protocol", "cc", "country", "latency")

    def __init__(self, addr, protocol, cc, country, latency):
        self.addr     = addr        # "host:port"
        self.protocol = protocol    # http | socks4 | socks5
        self.cc       = cc or "??"  # ISO country code, e.g. "US"
        self.country  = country or "Unknown"
        self.latency  = latency     # ms (int)

    def requests_dict(self):
        scheme = {"http": "http", "socks4": "socks4", "socks5": "socks5h"}[self.protocol]
        url = f"{scheme}://{self.addr}"
        return {"http": url, "https": url}

    def __str__(self):
        return f"{self.addr} [{self.protocol}] {self.cc} {self.latency}ms"


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
        self._change    = on_change              # called with () after pool changes
        self._pool: deque[Proxy] = deque()
        self._lock      = threading.Lock()
        self._seen      = set()
        self._seen_lock = threading.Lock()
        self._sem       = threading.Semaphore(TEST_WORKERS)
        self._active    = False
        self._custom: list[tuple[str, str]] = []   # (addr, protocol) user-provided

        # Live filters (set from the UI thread; read by workers).
        self.protocol_filter = "all"   # all | http | socks4 | socks5
        self.max_latency     = 0       # ms; 0 = no cap

    # ── Introspection ──
    @property
    def ready(self) -> int:
        with self._lock:
            return len(self._pool)

    def countries(self) -> Counter:
        with self._lock:
            return Counter(p.cc for p in self._pool)

    # ── Lifecycle ──
    def start(self):
        if not self._active:
            self._active = True
            threading.Thread(target=self._fetch_loop, daemon=True,
                             name="proxy-fetcher").start()

    def stop(self):
        self._active = False

    def add_custom(self, entries):
        """entries: list of (addr, protocol). Retried every cycle until working."""
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
        """Pop the next proxy, optionally the first one matching a country code."""
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

        # Custom proxies first — the user paid for these; keep retrying them.
        pf = self.protocol_filter
        for addr, proto in list(self._custom):
            if pf == "all" or pf == proto:
                raw.append((addr, proto))

        for url, proto in self._sources():
            if not self._active:
                return
            try:
                r = requests.get(url, timeout=14)
                for line in r.text.splitlines():
                    line = line.strip()
                    if ":" in line and len(line) < 24 and " " not in line:
                        raw.append((line, proto))
            except Exception:
                pass

        with self._seen_lock:
            custom_addrs = {a for a, _ in self._custom}
            new = [(a, pr) for a, pr in raw if a in custom_addrs or a not in self._seen]
        random.shuffle(new)
        batch = new[:MAX_TEST_BATCH]
        self._log(f"Fetched {len(raw)} candidates — testing {len(batch)}…")

        for addr, proto in batch:
            if not self._active or self.ready >= QUEUE_HIGH_MARK:
                break
            threading.Thread(target=self._test_one, args=(addr, proto),
                             daemon=True).start()

    def _test_one(self, addr, proto):
        with self._sem:
            if self.ready >= QUEUE_HIGH_MARK:
                return
            proxy = Proxy(addr, proto, None, None, 0)
            p = proxy.requests_dict()
            try:
                # Step 1: geolocation + latency.
                t0 = time.time()
                r = requests.get(GEO_TEST_URL, proxies=p, timeout=TEST_TIMEOUT)
                latency = int((time.time() - t0) * 1000)
                if r.status_code != 200:
                    return
                data = r.json()
                if data.get("status") != "success":
                    return
                ip = data.get("query", "")
                if not ip or "." not in ip:
                    return

                if self.max_latency and latency > self.max_latency:
                    return

                # Step 2: reach a real website, not just the IP-lookup API.
                r2 = requests.get(SECONDARY_TEST_URL, proxies=p, timeout=TEST_TIMEOUT)
                if r2.status_code not in (200, 301, 302):
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
            self._log(f"✓  {addr}  [{proto}]  {proxy.cc} {ip}  {latency}ms   (queue: {n})")
            self._change()


# ── Proxy list parsing ────────────────────────────────────────────────────────

def parse_proxy_line(line, default_proto="http"):
    """
    Accepts several common formats and returns (addr, protocol) or None:
      host:port
      protocol://host:port
      host:port:user:pass          (auth folded into addr for requests)
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

    if "@" in line:                        # user:pass@host:port
        return (line, proto)

    parts = line.split(":")
    if len(parts) == 2:
        return (line, proto)
    if len(parts) == 4:                    # host:port:user:pass
        host, port, user, pw = parts
        return (f"{user}:{pw}@{host}:{port}", proto)
    return None


# ── GUI ───────────────────────────────────────────────────────────────────────

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Anti-Detect Suite")
        self.root.geometry("760x680")
        self.root.minsize(720, 620)
        self.root.configure(bg=C["bg"])

        self._running       = False
        self._current: Proxy | None = None
        self._rotation_thread: threading.Thread | None = None

        self._manager = ProxyManager(
            on_log    = lambda msg: self.root.after(0, self._log, msg),
            on_change = lambda: self.root.after(0, self._refresh_status),
        )

        self._build_ui()
        self._apply_style()
        self.root.after(1500, self._tick_status)   # periodic country/queue refresh

    # ── UI skeleton ───────────────────────────────────────────────────────────
    def _build_ui(self):
        bar = tk.Frame(self.root, bg=C["panel"])
        bar.pack(fill="x")
        tk.Label(bar, text="Anti-Detect Suite", font=("Segoe UI", 13, "bold"),
                 bg=C["panel"], fg=C["text"], pady=8).pack(side="left", padx=16)
        tk.Label(bar, text=f"smart IP rotation · {_SYS}",
                 font=("Segoe UI", 9), bg=C["panel"], fg=C["hint"]).pack(side="left")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=16, pady=12)
        self._build_rotator_tab(nb)
        self._build_guide_tab(nb)

    # ── Rotator tab ───────────────────────────────────────────────────────────
    def _build_rotator_tab(self, nb):
        tab = tk.Frame(nb, bg=C["bg"])
        nb.add(tab, text="  IP Rotator  ")

        # Current IP card
        ip_card = tk.Frame(tab, bg=C["panel"])
        ip_card.pack(fill="x", pady=(8, 4))
        tk.Label(ip_card, text="Current IP", bg=C["panel"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left", padx=14, pady=10)
        self._ip_var = tk.StringVar(value="fetching…")
        tk.Label(ip_card, textvariable=self._ip_var, bg=C["panel"], fg=C["blue"],
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=6)
        self._dot = tk.Label(ip_card, text="●", bg=C["panel"], fg=C["hint"],
                             font=("Segoe UI", 11))
        self._dot.pack(side="left", padx=3)
        tk.Button(ip_card, text="Refresh", command=self._refresh_ip_async,
                  bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
                  font=("Segoe UI", 9), padx=9, pady=4).pack(side="right", padx=12)

        # Active proxy + queue status
        st = tk.Frame(tab, bg=C["bg"])
        st.pack(fill="x", pady=(4, 0))
        tk.Label(st, text="Active proxy:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        self._active_var = tk.StringVar(value="none")
        tk.Label(st, textvariable=self._active_var, bg=C["bg"], fg=C["green"],
                 font=("Consolas", 9)).grid(row=0, column=1, sticky="w", padx=6)
        tk.Button(st, text="Copy", command=self._copy_active,
                  bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
                  font=("Segoe UI", 8), padx=6).grid(row=0, column=2, padx=4)

        tk.Label(st, text="Queue:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(3, 0))
        self._queue_var = tk.StringVar(value="idle")
        tk.Label(st, textvariable=self._queue_var, bg=C["bg"], fg=C["yellow"],
                 font=("Segoe UI", 9, "bold")).grid(row=1, column=1, sticky="w",
                                                    padx=6, pady=(3, 0))
        self._wait_var = tk.StringVar(value="")
        tk.Label(st, textvariable=self._wait_var, bg=C["bg"], fg=C["orange"],
                 font=("Segoe UI", 9, "italic")).grid(row=1, column=3, sticky="w",
                                                      pady=(3, 0))

        # Options row 1: interval / latency / protocol
        opt1 = tk.Frame(tab, bg=C["bg"])
        opt1.pack(fill="x", pady=(10, 2))
        tk.Label(opt1, text="Rotate every:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left")
        self._interval_var = tk.StringVar(value="30")
        self._interval_cb = ttk.Combobox(
            opt1, textvariable=self._interval_var,
            values=["Manual", "15", "30", "45", "60", "90", "120", "300"],
            width=7, font=("Segoe UI", 9), state="readonly")
        self._interval_cb.pack(side="left", padx=6)
        tk.Label(opt1, text="s", bg=C["bg"], fg=C["hint"],
                 font=("Segoe UI", 9)).pack(side="left")

        tk.Label(opt1, text="Max latency:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(18, 0))
        self._lat_var = tk.StringVar(value="Any")
        lat_cb = ttk.Combobox(opt1, textvariable=self._lat_var,
                              values=["Any", "1000", "2000", "3000", "5000"],
                              width=6, font=("Segoe UI", 9), state="readonly")
        lat_cb.pack(side="left", padx=6)
        lat_cb.bind("<<ComboboxSelected>>", self._on_filters_change)

        tk.Label(opt1, text="Protocol:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left", padx=(18, 0))
        self._proto_var = tk.StringVar(value="all")
        proto_cb = ttk.Combobox(opt1, textvariable=self._proto_var,
                                values=["all", "http", "socks4", "socks5"],
                                width=7, font=("Segoe UI", 9), state="readonly")
        proto_cb.pack(side="left", padx=6)
        proto_cb.bind("<<ComboboxSelected>>", self._on_filters_change)

        # Options row 2: country + health check
        opt2 = tk.Frame(tab, bg=C["bg"])
        opt2.pack(fill="x", pady=(2, 2))
        tk.Label(opt2, text="Country for next proxy:", bg=C["bg"], fg=C["mauve"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self._country_var = tk.StringVar(value="Any")
        self._country_cb = ttk.Combobox(opt2, textvariable=self._country_var,
                                        values=["Any"], width=22,
                                        font=("Segoe UI", 9), state="readonly")
        self._country_cb.pack(side="left", padx=6)
        self._country_cb.bind("<<ComboboxSelected>>",
                              lambda _e: self._selected_country())

        self._health_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opt2, text="Auto-recheck active proxy", variable=self._health_var,
                       bg=C["bg"], fg=C["dim"], selectcolor=C["panel"],
                       activebackground=C["bg"], activeforeground=C["text"],
                       font=("Segoe UI", 9)).pack(side="left", padx=(18, 0))

        # Buttons
        btn = tk.Frame(tab, bg=C["bg"])
        btn.pack(pady=8)
        self._start_btn = tk.Button(btn, text="▶  Start Rotation", command=self._toggle,
                                    bg=C["blue"], fg="#1e1e2e", relief="flat", cursor="hand2",
                                    font=("Segoe UI", 10, "bold"), padx=16, pady=7)
        self._start_btn.pack(side="left", padx=4)
        self._next_btn = tk.Button(btn, text="→ Next Proxy", command=self._force_rotate,
                                   bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
                                   font=("Segoe UI", 10), padx=12, pady=7, state="disabled")
        self._next_btn.pack(side="left", padx=4)
        tk.Button(btn, text="Disable Proxy", command=self._disable_proxy_and_refresh,
                  bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
                  font=("Segoe UI", 10), padx=12, pady=7).pack(side="left", padx=4)
        tk.Button(btn, text="Load list…", command=self._load_custom,
                  bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
                  font=("Segoe UI", 10), padx=12, pady=7).pack(side="left", padx=4)
        tk.Button(btn, text="Export working", command=self._export_working,
                  bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
                  font=("Segoe UI", 10), padx=12, pady=7).pack(side="left", padx=4)

        # Countdown / progress
        cd = tk.Frame(tab, bg=C["bg"])
        cd.pack(fill="x", pady=(4, 2))
        self._cd_var = tk.StringVar(value="")
        tk.Label(cd, textvariable=self._cd_var, bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._progress = ttk.Progressbar(cd, mode="determinate")
        self._progress.pack(fill="x", pady=2)

        # Log
        tk.Label(tab, text="Activity log", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(6, 1))
        self._log_widget = scrolledtext.ScrolledText(
            tab, height=10, bg="#181825", fg=C["text"], font=("Consolas", 9),
            relief="flat", bd=0, insertbackground=C["text"], state="disabled")
        self._log_widget.pack(fill="both", expand=True)

    # ── Guide tab ─────────────────────────────────────────────────────────────
    def _build_guide_tab(self, nb):
        tab = tk.Frame(nb, bg=C["bg"])
        nb.add(tab, text="  Anti-Detect Guide  ")

        canvas = tk.Canvas(tab, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=C["bg"])
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        def h(text, color=C["blue"]):
            tk.Label(inner, text=text, bg=C["bg"], fg=color,
                     font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(14, 3))

        def p(text, indent=False):
            tk.Label(inner, text=text, bg=C["bg"],
                     fg=C["dim"] if indent else C["text"], font=("Segoe UI", 9),
                     justify="left", wraplength=630).pack(anchor="w",
                     padx=28 if indent else 14, pady=1)

        def div():
            tk.Frame(inner, bg=C["muted"], height=1).pack(fill="x", padx=14, pady=8)

        h("How websites detect returning users", C["yellow"])
        p("① IP address           — changes with a proxy or VPN (this app handles this)")
        p("② Browser fingerprint  — Canvas, WebGL GPU hash, fonts, screen, timezone,")
        p("                          plugins, CPU cores, memory → unique device hash", indent=True)
        p("③ Cookies & storage    — session tokens saved across visits")
        p("④ Behavioural signals  — mouse movement, typing speed, scroll patterns")
        p("⑤ Email + payment      — permanently linked to your identity")
        div()

        h("Layer 1 — IP rotation  (this app)", C["green"])
        p(f"✔  Runs on {_SYS}: {SystemProxy.platform_note()}")
        p("✔  HTTP + SOCKS4 + SOCKS5 sources, tested with concurrent workers.")
        p("✔  Each proxy is geo-tagged — pick the country for the next rotation.")
        p("✔  Load your own paid proxy list (host:port:user:pass supported).")
        p("✔  Optional health check auto-rotates when the active proxy dies.")
        p("✔  On Stop / close: proxy disabled, direct connection restored.")
        p("⚠  Firefox uses its own proxy setting — or paste the proxy into your", indent=True)
        p("   anti-detect browser profile directly (Copy button).", indent=True)
        div()

        h("Layer 2 — Browser fingerprint  (do this manually!)", C["red"])
        p("Changing IP alone is not enough. This is what catches most people.")
        p("Option A — GoLogin (gologin.com, 3 free profiles): spoofs Canvas, WebGL, fonts, UA")
        p("Option B — AdsPower (adspower.com, 2 free profiles): 50+ spoofable parameters")
        p("Option C — Brave: Settings → Privacy → 'Randomize fingerprint'")
        p("Test before visiting: coveryourtracks.eff.org · browserleaks.com · pixelscan.net", indent=True)
        div()

        h("Layer 3 — Cookies & storage")
        p("Never open a new account in the same browser profile as the old one.")
        p("GoLogin/AdsPower create fully isolated profiles — this is automatic.")
        p("Without them: a fresh incognito window + clear all site data first.")
        div()

        h("Layer 4 — Email")
        p("Use a brand-new email never associated with the platform.")
        p("ProtonMail (proton.me) — no phone · SimpleLogin / AnonAddy — aliases", indent=True)
        div()

        h("Layer 5 — Payment")
        p("Card numbers are permanently linked to your real identity.")
        p("Privacy.com (US) · Revolut / Wise prepaid virtual cards", indent=True)
        div()

        h("Match the proxy country to your identity", C["mauve"])
        p("Use the 'Country for next proxy' selector to keep your IP consistent with")
        p("the email, address, and payment details on the account. A US account logging")
        p("in from five countries in an hour is an obvious flag.")
        div()

        h("If an account gets banned", C["yellow"])
        p("Try appealing first — many wrongly-flagged accounts are reinstated.")
        p("If final: make sure every layer (IP, fingerprint, cookies, email, payment)")
        p("is fresh before the next attempt. A new IP alone is not enough.")
        tk.Frame(inner, bg=C["bg"], height=20).pack()

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
        top = "  ".join(f"{cc}:{n}" for cc, n in counts.most_common(6))
        self._queue_var.set(f"{total} ready" + (f"   ({top})" if top else ""))

        # Keep country dropdown in sync without disturbing the current selection.
        current = self._country_var.get()
        values = ["Any"] + [f"{cc} ({n})" for cc, n in counts.most_common()]
        codes  = ["Any"] + [cc for cc, _ in counts.most_common()]
        self._country_cb["values"] = values
        self._country_codes = codes
        # Preserve selection by code even as counts change the label text.
        sel_code = getattr(self, "_sel_country_code", "Any")
        if sel_code in codes:
            self._country_var.set(values[codes.index(sel_code)])

    def _tick_status(self):
        self._refresh_status()
        self.root.after(1500, self._tick_status)

    def _selected_country(self):
        """Resolve the dropdown selection back to a bare country code."""
        val = self._country_var.get()
        code = val.split(" ")[0] if val else "Any"
        self._sel_country_code = code
        return code

    # ── Logging ───────────────────────────────────────────────────────────────
    def _log(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_widget.config(state="normal")
        self._log_widget.insert("end", f"[{ts}] {text}\n")
        self._log_widget.see("end")
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
                r = requests.get(url, timeout=10, **kw)
                ip = extractor(r)
                if ip and "." in ip:
                    tag = f"  ({self._current.cc})" if self._current else ""
                    self.root.after(0, self._ip_var.set, ip + tag)
                    self.root.after(0, self._dot.config, {"fg": C["green"]})
                    return
            except Exception:
                continue
        label = "proxy unreachable" if self._current else "no internet?"
        self.root.after(0, self._ip_var.set, label)
        self.root.after(0, self._dot.config, {"fg": C["red"]})

    def _disable_proxy_and_refresh(self):
        SystemProxy.disable()
        self._current = None
        self._active_var.set("none")
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
                  f"They'll be tested and prioritised.")
        if not self._running:
            self._log("Tip: click Start Rotation to begin testing them.")

    def _export_working(self):
        with self._manager._lock:
            pool = list(self._manager._pool)
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
        self._start_btn.config(text="■  Stop", bg=C["red"], fg="#1e1e2e")
        self._next_btn.config(state="normal")
        self._interval_cb.config(state="disabled")
        self._log("Manual mode — use '→ Next Proxy' to rotate." if manual
                  else "Rotation started — background fetcher running.")
        self._manager.start()
        self._rotation_thread = threading.Thread(target=self._rotation_loop,
                                                 daemon=True, name="rotator")
        self._rotation_thread.start()
        if self._health_var.get():
            threading.Thread(target=self._health_loop, daemon=True,
                             name="health").start()

    def _stop(self):
        self._running = False
        self._manager.stop()
        self._start_btn.config(text="▶  Start Rotation", bg=C["blue"], fg="#1e1e2e")
        self._next_btn.config(state="disabled")
        self._interval_cb.config(state="readonly")
        self._cd_var.set("")
        self._wait_var.set("")
        self._progress.stop()
        self._progress.config(mode="determinate")
        self._progress["value"] = 0
        SystemProxy.disable()
        self._current = None
        self._active_var.set("none")
        self._log("Stopped — proxy disabled, direct connection restored.")
        self._refresh_ip_async()

    def _apply_proxy(self, proxy: Proxy):
        host, port = proxy.addr.rsplit(":", 1)
        # Strip any embedded credentials for the OS proxy host field.
        host = host.split("@")[-1]
        if SystemProxy.enable(host, port, proxy.protocol):
            self._current = proxy
            self._active_var.set(str(proxy))
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
                self.root.after(0, self._cd_var.set,
                                "Manual mode — click '→ Next Proxy' to rotate")
                self.root.after(0, self._progress.__setitem__, "value", 0)
                time.sleep(1)
                continue
            interval = int(self._interval_var.get())
            for remaining in range(interval, 0, -1):
                if not self._running or self._interval_var.get() == "Manual":
                    break
                pct = (1 - remaining / interval) * 100
                self.root.after(0, self._set_countdown, remaining, pct)
                time.sleep(1)
            if self._running and self._interval_var.get() != "Manual":
                self._do_wait_and_rotate()
        self.root.after(0, self._cd_var.set, "")
        self.root.after(0, self._wait_var.set, "")
        self.root.after(0, self._progress.__setitem__, "value", 0)

    def _do_wait_and_rotate(self):
        cc = self._selected_country()
        proxy = self._manager.get_next(cc)
        if proxy is None:
            where = "" if cc == "Any" else f" ({cc})"
            self._log(f"Timer fired — no proxy{where} ready, waiting…")
            self.root.after(0, self._wait_var.set, f"⏳ waiting for proxy{where}…")
            self.root.after(0, self._cd_var.set, "Holding — waiting for a verified proxy…")
            self.root.after(0, self._progress.config, {"mode": "indeterminate"})
            self.root.after(0, self._progress.start, 12)
            while self._running:
                proxy = self._manager.get_next(self._selected_country())
                if proxy:
                    break
                time.sleep(0.5)
            self.root.after(0, self._progress.stop)
            self.root.after(0, self._progress.config, {"mode": "determinate"})
            self.root.after(0, self._wait_var.set, "")
        if proxy and self._running:
            self._apply_proxy(proxy)

    def _set_countdown(self, remaining, pct):
        self._cd_var.set(f"Next rotation in {remaining}s")
        self._progress["value"] = pct

    def _health_loop(self):
        """Periodically verify the active proxy still works; rotate if it dies."""
        while self._running:
            for _ in range(HEALTH_INTERVAL):
                if not self._running:
                    return
                time.sleep(1)
            if not self._health_var.get() or not self._current:
                continue
            try:
                r = requests.get(GEO_TEST_URL, proxies=self._current.requests_dict(),
                                 timeout=TEST_TIMEOUT)
                ok = r.status_code == 200 and r.json().get("status") == "success"
            except Exception:
                ok = False
            if not ok and self._running:
                self.root.after(0, self._log,
                                f"⚠  Active proxy {self._current.addr} failed health check — rotating.")
                self._do_wait_and_rotate()

    # ── Style ─────────────────────────────────────────────────────────────────
    def _apply_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TNotebook", background=C["bg"], borderwidth=0)
        s.configure("TNotebook.Tab", background=C["panel"], foreground=C["dim"],
                    padding=[12, 5], font=("Segoe UI", 9))
        s.map("TNotebook.Tab", background=[("selected", C["blue"])],
              foreground=[("selected", "#1e1e2e")])
        s.configure("TCombobox", fieldbackground=C["panel"], background=C["panel"],
                    foreground=C["text"], selectbackground=C["panel"],
                    selectforeground=C["text"])
        s.configure("TProgressbar", troughcolor=C["panel"], background=C["blue"],
                    borderwidth=0)
        s.configure("TScrollbar", background=C["muted"], troughcolor=C["bg"],
                    borderwidth=0)

    # ── Entry point ───────────────────────────────────────────────────────────
    def _on_close(self):
        self._running = False
        self._manager.stop()
        SystemProxy.disable()
        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_ip_async()
        self._log(f"Ready on {_SYS}. {SystemProxy.platform_note()}")
        self._log("Click 'Start Rotation' — the proxy fetcher starts automatically.")
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
