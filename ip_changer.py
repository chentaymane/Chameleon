# -*- coding: utf-8 -*-
"""
Anti-Detect Suite
-----------------
Smart IP rotation through verified proxies + anti-detect guide.

Key behaviour:
  - Background threads fetch & test proxies continuously from 7 sources.
  - The queue ALWAYS has tested, working proxies ready.
  - When the timer fires: if the next proxy is ready → rotate instantly.
    If not ready yet → show "waiting…" and hold until one is verified.
  - On close: Windows proxy is restored to direct connection.

Affects: Chrome, Edge, Internet Explorer, and any app that follows
Windows system proxy. Firefox has its own proxy setting (see Guide tab).
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import threading
import queue
import random
import time
import winreg
import ctypes
import requests
import json
import string
import hashlib
import base64
import uuid
from datetime import datetime, timedelta

# ── Tuning constants ──────────────────────────────────────────────────────────

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=anonymous",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=elite",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
]

# Plain HTTP — free proxies almost never support HTTPS tunneling (CONNECT)
TEST_URL        = "http://ip-api.com/json/"
TEST_TIMEOUT    = 7      # seconds per proxy test
TEST_WORKERS    = 14     # concurrent test threads
QUEUE_LOW_MARK  = 5      # refetch when queue drops below this
QUEUE_HIGH_MARK = 30     # stop testing when queue reaches this
MAX_TEST_BATCH  = 350    # proxies tested per fetch cycle

# Multiple HTTP-only IP check endpoints tried in order until one responds
IP_CHECK_ENDPOINTS = [
    # (url, extractor_fn)
    ("http://ip-api.com/json/",              lambda r: r.json().get("query", "")),
    ("http://api.ipify.org?format=json",     lambda r: r.json().get("ip", "")),
    ("http://checkip.amazonaws.com/",        lambda r: r.text.strip()),
    ("http://ifconfig.me/ip",               lambda r: r.text.strip()),
    ("http://ipecho.net/plain",             lambda r: r.text.strip()),
]

# ── Colour palette ────────────────────────────────────────────────────────────

C = dict(
    bg     = "#1e1e2e",
    panel  = "#313244",
    muted  = "#45475a",
    text   = "#cdd6f4",
    dim    = "#a6adc8",
    hint   = "#6c7086",
    blue   = "#89b4fa",
    red    = "#f38ba8",
    green  = "#a6e3a1",
    yellow = "#f9e2af",
    orange = "#fab387",
)

# ── Windows proxy helpers ─────────────────────────────────────────────────────

def _win_key(write: bool = False):
    flag = winreg.KEY_WRITE if write else winreg.KEY_READ
    return winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        0, flag,
    )

def _notify_wininet():
    """Tell WinInet to pick up the new proxy settings immediately."""
    ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
    ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH

def enable_proxy(host: str, port: str) -> bool:
    try:
        key = _win_key(write=True)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"{host}:{port}")
        winreg.CloseKey(key)
        _notify_wininet()
        return True
    except Exception:
        return False

def disable_proxy():
    try:
        key = _win_key(write=True)
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
        _notify_wininet()
    except Exception:
        pass

# ── Background proxy manager ──────────────────────────────────────────────────

class ProxyManager:
    """
    Runs a background fetch-loop that continuously replenishes a queue of
    verified working proxies. Completely separate from the rotation timer.
    """

    def __init__(self, on_log, on_count_change):
        self._log   = on_log
        self._count = on_count_change
        self._q: queue.Queue[str] = queue.Queue()
        self._seen: set[str]      = set()
        self._seen_lock           = threading.Lock()
        self._sem                 = threading.Semaphore(TEST_WORKERS)
        self._active              = False

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def ready(self) -> int:
        return self._q.qsize()

    def start(self):
        if not self._active:
            self._active = True
            threading.Thread(target=self._fetch_loop, daemon=True,
                             name="proxy-fetcher").start()

    def stop(self):
        self._active = False

    def get_next(self) -> str | None:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_loop(self):
        while self._active:
            if self._q.qsize() < QUEUE_LOW_MARK:
                self._fetch_batch()
            time.sleep(8)

    def _fetch_batch(self):
        raw: set[str] = set()
        for url in PROXY_SOURCES:
            if not self._active:
                return
            try:
                r = requests.get(url, timeout=14)
                for line in r.text.splitlines():
                    line = line.strip()
                    if ":" in line and len(line) < 24 and " " not in line:
                        raw.add(line)
            except Exception:
                pass

        with self._seen_lock:
            new = [p for p in raw if p not in self._seen]
        random.shuffle(new)
        self._log(f"Fetched {len(raw)} proxies, testing {min(len(new), MAX_TEST_BATCH)} new ones…")

        for proxy in new[:MAX_TEST_BATCH]:
            if not self._active or self._q.qsize() >= QUEUE_HIGH_MARK:
                break
            threading.Thread(target=self._test_one, args=(proxy,),
                             daemon=True).start()

    def _test_one(self, proxy: str):
        with self._sem:
            if self._q.qsize() >= QUEUE_HIGH_MARK:
                return
            try:
                p = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
                r = requests.get(TEST_URL, proxies=p, timeout=TEST_TIMEOUT)
                if r.status_code != 200:
                    return
                # Validate the response actually contains an IP address
                data = r.json()
                ip = data.get("query", "")
                if not ip or "." not in ip:
                    return
                should_add = False
                with self._seen_lock:
                    if proxy not in self._seen:
                        self._seen.add(proxy)
                        should_add = True
                if should_add:
                    self._q.put(proxy)
                    n = self._q.qsize()
                    self._log(f"✓  {proxy}  →  {ip}   (queue: {n})")
                    self._count(n)
            except Exception:
                pass

# ── Cookie Randomizer ────────────────────────────────────────────────────────

class CookieRandomizer:
    """Generates realistic random browser cookies for multi-browser / anti-detect setups."""

    _SAMESITES = ["Lax", "Strict", "None"]

    # Templates: (name_pattern, value_factory, httpOnly, secure)
    _ANALYTICS = [
        ("_ga",       lambda: f"GA1.{random.randint(1,3)}.{random.randint(10**9,10**10-1)}.{random.randint(10**9,10**10-1)}", False, False),
        ("_gid",      lambda: f"GA1.{random.randint(1,3)}.{random.randint(10**9,10**10-1)}.{random.randint(10**9,10**10-1)}", False, False),
        ("_gat",      lambda: "1",                                                                                              False, False),
        ("_fbp",      lambda: f"fb.1.{int(time.time()*1000)}.{random.randint(10**8,10**9-1)}",                                 False, False),
        ("_fbc",      lambda: f"fb.1.{int(time.time()*1000)}.IwAR{CookieRandomizer._b64token(24)}",                           False, False),
        ("__utma",    lambda: f"{random.randint(10**8,10**9-1)}.{random.randint(10**9,10**10-1)}.{int(time.time())}.{int(time.time())}.{int(time.time())}.{random.randint(1,30)}", False, False),
        ("__utmb",    lambda: f"{random.randint(10**8,10**9-1)}.1.10.{int(time.time())}",                                      False, False),
        ("__utmz",    lambda: f"{random.randint(10**8,10**9-1)}.{int(time.time())}.1.1.utmcsr=(direct)|utmccn=(direct)|utmcmd=(none)", False, False),
        ("_hjid",     lambda: str(uuid.uuid4()),                                                                               False, False),
        ("_hjFirstSeen", lambda: "1",                                                                                          False, False),
    ]

    _SESSION = [
        ("sessionid",    lambda: CookieRandomizer._hextoken(32),  True,  True),
        ("session_token",lambda: CookieRandomizer._hextoken(40),  True,  True),
        ("PHPSESSID",    lambda: CookieRandomizer._hextoken(26),  True,  False),
        ("JSESSIONID",   lambda: CookieRandomizer._hextoken(32).upper(), True, False),
        ("ASP.NET_SessionId", lambda: CookieRandomizer._alphanum(24), True, False),
        ("connect.sid",  lambda: f"s%3A{CookieRandomizer._hextoken(40)}.{CookieRandomizer._hextoken(20)}", True, True),
        ("sid",          lambda: CookieRandomizer._hextoken(32),  True,  True),
    ]

    _AUTH = [
        ("auth_token",   lambda: CookieRandomizer._hextoken(40),  True,  True),
        ("access_token", lambda: CookieRandomizer._jwt_like(),     True,  True),
        ("refresh_token",lambda: CookieRandomizer._hextoken(64),  True,  True),
        ("remember_token",lambda: CookieRandomizer._hextoken(40), True,  True),
        ("_csrf",        lambda: CookieRandomizer._b64token(32),   False, True),
        ("csrftoken",    lambda: CookieRandomizer._alphanum(64),   False, False),
        ("XSRF-TOKEN",   lambda: CookieRandomizer._b64token(32),   False, True),
    ]

    _TRACKING = [
        ("__cf_bm",     lambda: f"{CookieRandomizer._b64token(40)}-{int(time.time())}-{random.randint(0,9)}", False, True),
        ("cf_clearance",lambda: f"{CookieRandomizer._hextoken(40)}-{int(time.time())}-0-{random.randint(100,999)}", False, True),
        ("_dd_s",       lambda: f"rum=0&expire={int(time.time()*1000)+3600000}", False, False),
        ("OptanonConsent", lambda: f"isIABGlobal=false&datestamp={datetime.utcnow().strftime('%a+%b+%d+%Y+%H%%3A%M%%3A%S+GMT%%2B0000')}&version=6.{random.randint(10,39)}.0", False, False),
        ("CookieConsent", lambda: f"{{stamp:'{CookieRandomizer._alphanum(28)}',necessary:true,preferences:true,statistics:true,marketing:false}}", False, False),
        ("euconsent-v2", lambda: CookieRandomizer._b64token(80),  False, False),
    ]

    _PREFS = [
        ("theme",       lambda: random.choice(["light","dark","system"]), False, False),
        ("lang",        lambda: random.choice(["en","en-US","fr","de","es","pt","it","ja","ko","zh"]), False, False),
        ("locale",      lambda: random.choice(["en_US","fr_FR","de_DE","es_ES","pt_BR"]), False, False),
        ("currency",    lambda: random.choice(["USD","EUR","GBP","JPY","CAD"]), False, False),
        ("timezone",    lambda: random.choice(["UTC","America/New_York","Europe/London","Asia/Tokyo","America/Los_Angeles"]).replace("/","%2F"), False, False),
    ]

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _hextoken(n: int) -> str:
        return ''.join(random.choices('0123456789abcdef', k=n))

    @staticmethod
    def _alphanum(n: int) -> str:
        return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

    @staticmethod
    def _b64token(n: int) -> str:
        raw = bytes(random.randint(0, 255) for _ in range(n))
        return base64.urlsafe_b64encode(raw).decode()[:n]

    @staticmethod
    def _jwt_like() -> str:
        header  = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": str(random.randint(10**5, 10**9)),
                        "iat": int(time.time()),
                        "exp": int(time.time()) + 3600}).encode()
        ).decode().rstrip("=")
        sig = CookieRandomizer._b64token(43)
        return f"{header}.{payload}.{sig}"

    # ── Public API ────────────────────────────────────────────────────────────

    def generate(self, domain: str, count: int, categories: list[str]) -> list[dict]:
        pool: list[tuple] = []
        if "analytics" in categories:  pool += self._ANALYTICS
        if "session"   in categories:  pool += self._SESSION
        if "auth"      in categories:  pool += self._AUTH
        if "tracking"  in categories:  pool += self._TRACKING
        if "prefs"     in categories:  pool += self._PREFS

        if not pool:
            return []

        now = datetime.utcnow()
        cookies = []
        used_names: set[str] = set()

        for _ in range(count):
            template = random.choice(pool)
            name, value_fn, http_only, secure = template
            # avoid duplicate names in the same batch
            suffix = f"_{CookieRandomizer._alphanum(4)}" if name in used_names else ""
            used_names.add(name)
            name_final = name + suffix

            expires_ts = int((now + timedelta(days=random.randint(1, 730))).timestamp())

            cookie = {
                "name":     name_final,
                "value":    value_fn(),
                "domain":   domain if domain.startswith(".") else f".{domain}" if domain else ".example.com",
                "path":     "/",
                "expires":  expires_ts,
                "httpOnly": http_only,
                "secure":   secure,
                "sameSite": "None" if (secure and random.random() < 0.3) else random.choice(["Lax", "Strict"]),
            }
            cookies.append(cookie)

        return cookies


# ── GUI ───────────────────────────────────────────────────────────────────────

class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Anti-Detect Suite")
        self.root.geometry("740x620")
        self.root.resizable(False, False)
        self.root.configure(bg=C["bg"])

        self._running        = False
        self._current_proxy  = None
        self._rotation_thread: threading.Thread | None = None
        self._cookie_gen     = CookieRandomizer()

        self._manager = ProxyManager(
            on_log          = lambda msg: self.root.after(0, self._log, msg),
            on_count_change = lambda n:   self.root.after(0, self._update_queue_label, n),
        )

        self._build_ui()
        self._apply_style()

    # ── UI skeleton ───────────────────────────────────────────────────────────

    def _build_ui(self):
        # App title strip
        bar = tk.Frame(self.root, bg=C["panel"])
        bar.pack(fill="x")
        tk.Label(bar, text="Anti-Detect Suite", font=("Segoe UI", 13, "bold"),
                 bg=C["panel"], fg=C["text"], pady=8).pack(side="left", padx=16)
        tk.Label(bar, text="smart IP rotation + fingerprint guide",
                 font=("Segoe UI", 9), bg=C["panel"], fg=C["hint"]).pack(side="left")

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=16, pady=12)

        self._build_rotator_tab(nb)
        self._build_cookie_tab(nb)
        self._build_guide_tab(nb)

    # ── Rotator tab ───────────────────────────────────────────────────────────

    def _build_rotator_tab(self, nb):
        tab = tk.Frame(nb, bg=C["bg"])
        nb.add(tab, text="  IP Rotator  ")

        # ── Current IP ──
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

        # ── Queue status ──
        q_row = tk.Frame(tab, bg=C["bg"])
        q_row.pack(fill="x", pady=(4, 0))

        tk.Label(q_row, text="Proxy queue:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left")

        self._queue_var = tk.StringVar(value="idle")
        tk.Label(q_row, textvariable=self._queue_var, bg=C["bg"], fg=C["yellow"],
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=6)

        self._wait_var = tk.StringVar(value="")
        tk.Label(q_row, textvariable=self._wait_var, bg=C["bg"], fg=C["orange"],
                 font=("Segoe UI", 9, "italic")).pack(side="left")

        # ── Interval control ──
        ctrl = tk.Frame(tab, bg=C["bg"])
        ctrl.pack(fill="x", pady=8)

        tk.Label(ctrl, text="Rotate every:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 10)).pack(side="left")

        self._interval_var = tk.StringVar(value="30")
        self._interval_cb = ttk.Combobox(
            ctrl, textvariable=self._interval_var,
            values=["15", "30", "45", "60", "90", "120"],
            width=5, font=("Segoe UI", 10), state="readonly",
        )
        self._interval_cb.pack(side="left", padx=6)

        tk.Label(ctrl,
                 text="seconds   |   waits automatically if no proxy is ready yet",
                 bg=C["bg"], fg=C["hint"], font=("Segoe UI", 9)).pack(side="left")

        # ── Buttons ──
        btn_row = tk.Frame(tab, bg=C["bg"])
        btn_row.pack(pady=4)

        self._start_btn = tk.Button(
            btn_row, text="▶  Start Rotation", command=self._toggle,
            bg=C["blue"], fg="#1e1e2e", relief="flat", cursor="hand2",
            font=("Segoe UI", 10, "bold"), padx=18, pady=7,
        )
        self._start_btn.pack(side="left", padx=5)

        self._next_btn = tk.Button(
            btn_row, text="Skip → Next IP", command=self._force_rotate,
            bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
            font=("Segoe UI", 10), padx=12, pady=7, state="disabled",
        )
        self._next_btn.pack(side="left", padx=5)

        tk.Button(
            btn_row, text="Disable Proxy", command=self._disable_proxy_and_refresh,
            bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
            font=("Segoe UI", 10), padx=12, pady=7,
        ).pack(side="left", padx=5)

        # ── Countdown ──
        cd_frame = tk.Frame(tab, bg=C["bg"])
        cd_frame.pack(fill="x", pady=(4, 2))

        self._cd_var = tk.StringVar(value="")
        tk.Label(cd_frame, textvariable=self._cd_var, bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(anchor="w")

        self._progress = ttk.Progressbar(cd_frame, mode="determinate", length=666)
        self._progress.pack(fill="x", pady=2)

        # ── Log ──
        tk.Label(tab, text="Activity log", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(6, 1))

        self._log_widget = scrolledtext.ScrolledText(
            tab, height=9, bg="#181825", fg=C["text"],
            font=("Consolas", 9), relief="flat", bd=0,
            insertbackground=C["text"], state="disabled",
        )
        self._log_widget.pack(fill="both", expand=True)

    # ── Cookie Randomizer tab ─────────────────────────────────────────────────

    def _build_cookie_tab(self, nb):
        tab = tk.Frame(nb, bg=C["bg"])
        nb.add(tab, text="  Cookie Randomizer  ")

        # ── Header ──
        hdr = tk.Frame(tab, bg=C["panel"])
        hdr.pack(fill="x")
        tk.Label(hdr, text="Cookie Randomizer", font=("Segoe UI", 11, "bold"),
                 bg=C["panel"], fg=C["text"], pady=7).pack(side="left", padx=14)
        tk.Label(hdr, text="generate random cookies for multi-browser / anti-detect profiles",
                 font=("Segoe UI", 9), bg=C["panel"], fg=C["hint"]).pack(side="left")

        body = tk.Frame(tab, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=10)

        # ── Row 1: domain + count ──
        row1 = tk.Frame(body, bg=C["bg"])
        row1.pack(fill="x", pady=(0, 6))

        tk.Label(row1, text="Domain:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left")
        self._ck_domain = tk.Entry(row1, width=24, bg=C["panel"], fg=C["text"],
                                   insertbackground=C["text"], relief="flat",
                                   font=("Segoe UI", 10))
        self._ck_domain.insert(0, "example.com")
        self._ck_domain.pack(side="left", padx=(4, 18), ipady=4)

        tk.Label(row1, text="Count:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left")
        self._ck_count = ttk.Combobox(row1, values=["5","10","15","20","30","50"],
                                      width=5, font=("Segoe UI", 10), state="readonly")
        self._ck_count.set("10")
        self._ck_count.pack(side="left", padx=4)

        # ── Row 2: category checkboxes ──
        row2 = tk.Frame(body, bg=C["bg"])
        row2.pack(fill="x", pady=(0, 8))

        tk.Label(row2, text="Categories:", bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(side="left")

        self._ck_cats = {}
        cat_defs = [
            ("analytics", "Analytics (_ga, _fbp…)"),
            ("session",   "Session (PHPSESSID…)"),
            ("auth",      "Auth (tokens, CSRF…)"),
            ("tracking",  "Tracking (Cloudflare…)"),
            ("prefs",     "Preferences"),
        ]
        for key, label in cat_defs:
            var = tk.BooleanVar(value=True)
            self._ck_cats[key] = var
            tk.Checkbutton(row2, text=label, variable=var,
                           bg=C["bg"], fg=C["text"], selectcolor=C["panel"],
                           activebackground=C["bg"], activeforeground=C["blue"],
                           font=("Segoe UI", 9), cursor="hand2").pack(side="left", padx=5)

        # ── Row 3: action buttons ──
        row3 = tk.Frame(body, bg=C["bg"])
        row3.pack(fill="x", pady=(0, 8))

        tk.Button(row3, text="⚄  Generate", command=self._generate_cookies,
                  bg=C["blue"], fg="#1e1e2e", relief="flat", cursor="hand2",
                  font=("Segoe UI", 10, "bold"), padx=16, pady=6).pack(side="left", padx=(0, 6))

        tk.Button(row3, text="Copy JSON", command=self._copy_cookies,
                  bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
                  font=("Segoe UI", 10), padx=12, pady=6).pack(side="left", padx=4)

        tk.Button(row3, text="Save to File…", command=self._save_cookies,
                  bg=C["muted"], fg=C["text"], relief="flat", cursor="hand2",
                  font=("Segoe UI", 10), padx=12, pady=6).pack(side="left", padx=4)

        tk.Button(row3, text="Clear", command=self._clear_cookies,
                  bg=C["muted"], fg=C["red"], relief="flat", cursor="hand2",
                  font=("Segoe UI", 10), padx=12, pady=6).pack(side="left", padx=4)

        self._ck_status = tk.StringVar(value="")
        tk.Label(row3, textvariable=self._ck_status, bg=C["bg"], fg=C["green"],
                 font=("Segoe UI", 9, "italic")).pack(side="left", padx=10)

        # ── JSON output area ──
        tk.Label(body, text="JSON output  (compatible with Selenium, Playwright, GoLogin, AdsPower, Puppeteer)",
                 bg=C["bg"], fg=C["dim"], font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(2, 2))

        self._ck_output = scrolledtext.ScrolledText(
            body, bg="#181825", fg=C["text"], font=("Consolas", 9),
            relief="flat", bd=0, insertbackground=C["text"],
        )
        self._ck_output.pack(fill="both", expand=True)

    def _generate_cookies(self):
        domain = self._ck_domain.get().strip()
        try:
            count = int(self._ck_count.get())
        except ValueError:
            count = 10

        selected = [k for k, v in self._ck_cats.items() if v.get()]
        if not selected:
            self._ck_status.set("Select at least one category.")
            return

        cookies = self._cookie_gen.generate(domain, count, selected)
        output  = json.dumps(cookies, indent=2)

        self._ck_output.config(state="normal")
        self._ck_output.delete("1.0", "end")
        self._ck_output.insert("end", output)
        self._ck_output.config(state="normal")

        self._ck_status.set(f"Generated {len(cookies)} cookies.")

    def _copy_cookies(self):
        text = self._ck_output.get("1.0", "end").strip()
        if not text:
            self._ck_status.set("Nothing to copy — generate first.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._ck_status.set("Copied to clipboard!")

    def _save_cookies(self):
        text = self._ck_output.get("1.0", "end").strip()
        if not text:
            self._ck_status.set("Nothing to save — generate first.")
            return
        domain = self._ck_domain.get().strip().replace(".", "_") or "cookies"
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile=f"{domain}_cookies.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._ck_status.set(f"Saved → {path}")

    def _clear_cookies(self):
        self._ck_output.config(state="normal")
        self._ck_output.delete("1.0", "end")
        self._ck_status.set("")

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

        # Bind mousewheel
        def _scroll(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _scroll)

        def h(text, color=C["blue"]):
            tk.Label(inner, text=text, bg=C["bg"], fg=color,
                     font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(14, 3))

        def p(text, indent=False):
            pad = 28 if indent else 14
            tk.Label(inner, text=text, bg=C["bg"],
                     fg=C["dim"] if indent else C["text"],
                     font=("Segoe UI", 9), justify="left",
                     wraplength=610).pack(anchor="w", padx=pad, pady=1)

        def div():
            tk.Frame(inner, bg=C["muted"], height=1).pack(fill="x", padx=14, pady=8)

        h("How websites detect returning users", C["yellow"])
        p("① IP address           — changes with a proxy or VPN (this app handles this)")
        p("② Browser fingerprint  — Canvas, WebGL GPU hash, fonts, screen size, timezone,")
        p("                          installed plugins, CPU cores, memory → unique device hash", indent=True)
        p("③ Cookies & storage    — session tokens saved in your browser across visits")
        p("④ Behavioural signals  — mouse movement, typing speed, scroll patterns (ML-based)")
        p("⑤ Email + payment      — permanently linked to your identity in their database")

        div()

        h("Layer 1 — IP rotation  (this app)", C["green"])
        p("✔  7 proxy sources, 14 concurrent testers running in background.")
        p("✔  Only verified, responsive proxies enter the queue.")
        p("✔  Timer fires → if next proxy is ready, rotate instantly.")
        p("✔  If not ready → pauses with 'waiting…' until one passes the test.")
        p("✔  On Stop or window close: proxy is disabled, direct connection restored.")
        p("⚠  Only affects Chrome, Edge, and WinInet apps. Firefox: set manually.", indent=True)

        div()

        h("Layer 2 — Browser fingerprint  (do this manually!)", C["red"])
        p("This is what catches most people. Changing IP alone is not enough.")
        p("")
        p("Option A — GoLogin (recommended free option, 3 profiles)")
        p("  Download: gologin.com  |  Spoofs Canvas, WebGL, fonts, User-Agent", indent=True)
        p("Option B — AdsPower (2 free profiles)")
        p("  Download: adspower.com  |  50+ spoofable parameters", indent=True)
        p("Option C — Brave Browser")
        p("  Settings → Privacy → 'Randomize fingerprint'  |  Fast, no install", indent=True)
        p("")
        p("Test your fingerprint BEFORE visiting the site:")
        p("  coveryourtracks.eff.org      — EFF tracker test", indent=True)
        p("  browserleaks.com             — detailed fingerprint breakdown", indent=True)
        p("  pixelscan.net                — shows if you look like a bot", indent=True)

        div()

        h("Layer 3 — Cookies & storage")
        p("Never open your new account in the same browser profile as the old one.")
        p("GoLogin/AdsPower create fully isolated profiles — this is automatic.")
        p("Without them: use a fresh incognito window + clear all site data first.")

        div()

        h("Layer 4 — Email")
        p("Use a brand-new email never associated with the platform before.")
        p("  ProtonMail (proton.me)         — no phone number required", indent=True)
        p("  SimpleLogin / AnonAddy         — alias forwarding for privacy", indent=True)

        div()

        h("Layer 5 — Payment")
        p("Card numbers are permanently linked to your real identity.")
        p("  Privacy.com (US only)          — generates virtual card per merchant", indent=True)
        p("  Revolut / Wise                 — prepaid virtual cards", indent=True)
        p("  PayPal on a separate account   — separate email required", indent=True)

        div()

        h("Complete free stack (all 5 layers covered)", C["green"])
        p("1. This app + Proton VPN free tier          → clean IP")
        p("2. GoLogin (3 free profiles)                → fingerprint + cookie isolation")
        p("3. ProtonMail new address                   → clean email")
        p("4. Privacy.com virtual card                 → clean payment")
        p("5. Test at browserleaks.com before signing up")

        div()

        h("First thing to try: appeal the ban", C["yellow"])
        p("Email:  legal@teepublic.com")
        p("Many wrongly-banned accounts are reinstated quickly.")
        p("State clearly that you were not spamming and ask which rule was violated.")

        tk.Frame(inner, bg=C["bg"], height=20).pack()

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_widget.config(state="normal")
        self._log_widget.insert("end", f"[{ts}] {text}\n")
        self._log_widget.see("end")
        self._log_widget.config(state="disabled")

    def _update_queue_label(self, n: int):
        self._queue_var.set(f"{n} ready")

    # ── IP refresh ────────────────────────────────────────────────────────────

    def _refresh_ip_async(self):
        threading.Thread(target=self._do_fetch_ip, daemon=True).start()

    def _do_fetch_ip(self):
        kw: dict = {}
        if self._current_proxy:
            h, p = self._current_proxy.rsplit(":", 1)
            # Use http:// only — free proxies rarely support HTTPS tunneling
            kw["proxies"] = {"http": f"http://{h}:{p}"}

        for url, extractor in IP_CHECK_ENDPOINTS:
            try:
                r = requests.get(url, timeout=10, **kw)
                ip = extractor(r)
                if ip and "." in ip:
                    self.root.after(0, self._ip_var.set, ip)
                    self.root.after(0, self._dot.config, {"fg": C["green"]})
                    return
            except Exception:
                continue

        # All endpoints failed
        label = "proxy unreachable" if self._current_proxy else "no internet?"
        self.root.after(0, self._ip_var.set, label)
        self.root.after(0, self._dot.config, {"fg": C["red"]})

    def _disable_proxy_and_refresh(self):
        disable_proxy()
        self._current_proxy = None
        self._log("Proxy disabled — using direct connection.")
        self._refresh_ip_async()

    # ── Rotation control ──────────────────────────────────────────────────────

    def _toggle(self):
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self):
        self._running = True
        self._start_btn.config(text="■  Stop Rotation", bg=C["red"], fg="#1e1e2e")
        self._next_btn.config(state="normal")
        self._interval_cb.config(state="disabled")
        self._log("Rotation started — background proxy fetcher running.")
        self._manager.start()
        self._rotation_thread = threading.Thread(
            target=self._rotation_loop, daemon=True, name="rotator")
        self._rotation_thread.start()

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
        disable_proxy()
        self._current_proxy = None
        self._log("Stopped — proxy disabled, direct connection restored.")
        self._refresh_ip_async()

    def _apply_proxy(self, proxy: str):
        host, port = proxy.rsplit(":", 1)
        if enable_proxy(host, port):
            self._current_proxy = proxy
            self._log(f"→  Active proxy: {proxy}")
            self._refresh_ip_async()
        else:
            self._log(f"✗  Could not set proxy {proxy} (registry error)")

    def _force_rotate(self):
        proxy = self._manager.get_next()
        if proxy:
            self._apply_proxy(proxy)
            self.root.after(0, self._wait_var.set, "")
        else:
            self._log("No verified proxy in queue yet — still fetching…")

    # ── Rotation loop (runs in background thread) ─────────────────────────────

    def _rotation_loop(self):
        # First rotation immediately on start
        self._do_wait_and_rotate()

        while self._running:
            interval = int(self._interval_var.get())

            # Countdown
            for remaining in range(interval, 0, -1):
                if not self._running:
                    break
                pct = (1 - remaining / interval) * 100
                self.root.after(0, self._set_countdown, remaining, pct)
                time.sleep(1)

            if self._running:
                self._do_wait_and_rotate()

        # Cleanup
        self.root.after(0, self._cd_var.set, "")
        self.root.after(0, self._wait_var.set, "")
        self.root.after(0, self._progress.__setitem__, "value", 0)

    def _do_wait_and_rotate(self):
        """
        Get the next proxy from the queue.
        If empty, switch the progress bar to indeterminate and block here
        until a proxy becomes available. Never skips a rotation.
        """
        proxy = self._manager.get_next()

        if proxy is None:
            self._log("Timer fired — no proxy ready yet, waiting…")
            self.root.after(0, self._wait_var.set, "⏳ waiting for proxy…")
            self.root.after(0, self._cd_var.set, "Holding — waiting for a verified proxy…")
            self.root.after(0, self._progress.config, {"mode": "indeterminate"})
            self.root.after(0, self._progress.start, 12)

            while self._running:
                proxy = self._manager.get_next()
                if proxy:
                    break
                time.sleep(0.5)

            self.root.after(0, self._progress.stop)
            self.root.after(0, self._progress.config, {"mode": "determinate"})
            self.root.after(0, self._wait_var.set, "")

        if proxy and self._running:
            self._apply_proxy(proxy)

    def _set_countdown(self, remaining: int, pct: float):
        self._cd_var.set(f"Next rotation in {remaining}s")
        self._progress["value"] = pct

    # ── Style ──────────────────────────────────────────────────────────────────

    def _apply_style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("TNotebook",        background=C["bg"],    borderwidth=0)
        s.configure("TNotebook.Tab",    background=C["panel"], foreground=C["dim"],
                    padding=[12, 5],    font=("Segoe UI", 9))
        s.map("TNotebook.Tab",
              background=[("selected", C["blue"])],
              foreground=[("selected", "#1e1e2e")])
        s.configure("TCombobox",
                    fieldbackground=C["panel"], background=C["panel"],
                    foreground=C["text"], selectbackground=C["panel"],
                    selectforeground=C["text"])
        s.configure("TProgressbar",
                    troughcolor=C["panel"], background=C["blue"], borderwidth=0)
        s.configure("TScrollbar",
                    background=C["muted"], troughcolor=C["bg"], borderwidth=0)

    # ── Entry point ───────────────────────────────────────────────────────────

    def _on_close(self):
        self._running = False
        self._manager.stop()
        disable_proxy()
        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_ip_async()
        self._log("Ready. Click 'Start Rotation' — proxy fetcher starts automatically.")
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
