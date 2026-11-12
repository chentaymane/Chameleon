# -*- coding: utf-8 -*-
"""Discover, inspect and rotate verified proxies in a Tkinter dashboard."""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox, simpledialog
import csv
import threading
import queue
import platform
import subprocess
import time
from datetime import datetime

from proxy_pool import (
    BUILTIN_FEEDS,
    PROTOCOLS,
    SOURCE_NAMES,
    TEST_TIMEOUT,
    Proxy,
    ProxyManager,
    check_connection,
    country_codes,
    parse_feed,
    read_proxy_list,
    verify_destination,
)

# ── Platform detection & guarded imports ──────────────────────────────────────

_SYS       = platform.system()
IS_WINDOWS = _SYS == "Windows"
IS_LINUX   = _SYS == "Linux"
IS_MAC     = _SYS == "Darwin"

if IS_WINDOWS:
    import winreg
    import ctypes

HEALTH_INTERVAL = 20
SOURCE_OPTIONS = {
    "All public": SOURCE_NAMES,
    "Live API": ("proxyscrape-api",),
    "Text feeds": tuple(BUILTIN_FEEDS),
    "Imported only": (),
}


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


# ── GUI ───────────────────────────────────────────────────────────────────────

class App:
    APP_NAME = "Chameleon"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(self.APP_NAME)
        self.root.geometry("880x740")
        self.root.minsize(740, 640)
        self.root.configure(bg=C["bg"])

        self._running = False
        self._rotating = False
        self._current: Proxy | None = None
        self._rotations = 0
        self._selected_countries = frozenset()
        self._rotation_due = None
        self._interval = 0
        self._health_in_flight = False
        self._next_health_check = 0
        self._ip_request = 0
        self._system_proxy_active = False
        self._result_rows = {}
        self._results_notice_until = 0

        self._ui_queue: queue.Queue = queue.Queue()

        self._manager = ProxyManager(
            on_log    = lambda msg: self._ui(self._log, msg),
            on_change = lambda: None,
        )

        self._apply_style()
        self._build_ui()
        self.root.after(100, self._drain_ui_queue)
        self.root.after(400, self._tick_status)
        self.root.after(250, self._tick_rotation)

    # ── Small UI helpers ──────────────────────────────────────────────────────
    def _card(self, parent, title=None):
        """A rounded-looking surface panel with optional header."""
        outer = tk.Frame(parent, bg=C["surface"], highlightthickness=1,
                         highlightbackground=C["panel"])
        outer.pack(fill="x", pady=4)
        if title:
            tk.Label(outer, text=title.upper(), bg=C["surface"], fg=C["hint"],
                     font=F["small"]).pack(anchor="w", padx=14, pady=(6, 0))
        body = tk.Frame(outer, bg=C["surface"])
        body.pack(fill="x", padx=14, pady=(3, 7))
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
                 bg=C["panel"], fg=C["text"], pady=7).pack(side="left", padx=16)
        tk.Label(bar, text=f"smart IP rotation  ·  {_SYS}", font=F["body"],
                 bg=C["panel"], fg=C["hint"]).pack(side="left")

        root = tk.Frame(self.root, bg=C["bg"])
        root.pack(fill="both", expand=True, padx=16, pady=(10, 12))

        summary = tk.Frame(root, bg=C["bg"])
        summary.pack(fill="x")
        summary.columnconfigure(0, weight=3, uniform="summary")
        summary.columnconfigure(1, weight=2, uniform="summary")
        connection = tk.Frame(summary, bg=C["bg"])
        connection.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        stats = tk.Frame(summary, bg=C["bg"])
        stats.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self._build_ip_card(connection)
        self._build_stats_card(stats)
        self._build_controls_card(root)
        self._build_action_row(root)
        self._build_workspace(root)

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
                 font=F["mono"], wraplength=300, justify="left").pack(side="left")
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
        body = self._card(parent, "Discovery and rotation")

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
        self._combo(r1, self._proto_var,
                    ["all", *PROTOCOLS, "http,socks4", "http,socks5", "socks4,socks5"],
                    13, self._on_filters_change).pack(side="left", padx=6)

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
        tk.Label(r2, text="Exit countries", bg=C["surface"], fg=C["mauve"],
                 font=F["h2"]).pack(side="left")
        self._country_var = tk.StringVar(value="Any")
        self._country_cb = self._combo(r2, self._country_var, ["Any"], 14,
                                       self._on_filters_change)
        self._country_cb.config(state="normal")
        self._country_cb.bind("<Return>", self._on_filters_change)
        self._country_cb.pack(side="left", padx=8)
        self._btn(r2, "Apply", self._on_filters_change, padx=8, pady=2).pack(side="left")

        self._health_var = tk.BooleanVar(value=True)
        tk.Checkbutton(r2, text="Auto-recheck active proxy", variable=self._health_var,
                       bg=C["surface"], fg=C["dim"], selectcolor=C["panel"],
                       activebackground=C["surface"], activeforeground=C["text"],
                       font=F["body"], bd=0, highlightthickness=0).pack(side="right")

        r3 = tk.Frame(body, bg=C["surface"])
        r3.pack(fill="x", pady=(8, 0))
        tk.Label(r3, text="Sources", bg=C["surface"], fg=C["dim"],
                 font=F["body"]).pack(side="left")
        self._source_var = tk.StringVar(value="All public")
        self._combo(r3, self._source_var, list(SOURCE_OPTIONS), 15,
                    self._on_filters_change).pack(side="left", padx=8)
        self._dual_var = tk.BooleanVar(value=True)
        tk.Checkbutton(r3, text="Verify two HTTPS sites", variable=self._dual_var,
                       command=self._on_filters_change, bg=C["surface"], fg=C["dim"],
                       selectcolor=C["panel"], activebackground=C["surface"],
                       activeforeground=C["text"], font=F["body"], bd=0,
                       highlightthickness=0).pack(side="right")

    # ── Action row + countdown ────────────────────────────────────────────────
    def _build_action_row(self, parent):
        wrap = tk.Frame(parent, bg=C["bg"])
        wrap.pack(fill="x", pady=(2, 0))

        self._start_btn = self._btn(wrap, "Find proxies", self._toggle, "primary", padx=10)
        self._start_btn.pack(side="left", padx=(0, 6))
        self._rotation_btn = self._btn(wrap, "Start rotation", self._toggle_rotation, padx=10)
        self._rotation_btn.pack(side="left", padx=6)
        self._next_btn = self._btn(wrap, "Next proxy", self._force_rotate, padx=10, state="disabled")
        self._next_btn.pack(side="left", padx=6)
        self._btn(wrap, "Disable proxy", self._disable_proxy_and_refresh, padx=10).pack(side="left", padx=6)
        self._btn(wrap, "Load list…", self._load_custom, padx=10,
                  font=F["body"]).pack(side="right", padx=(6, 0))
        self._btn(wrap, "Add feed…", self._add_feed, padx=10,
                  font=F["body"]).pack(side="right", padx=(6, 0))
        self._btn(wrap, "Export…", self._export_working, padx=10,
                  font=F["body"]).pack(side="right")

        cd = tk.Frame(parent, bg=C["bg"])
        cd.pack(fill="x", pady=(6, 0))
        labels = tk.Frame(cd, bg=C["bg"])
        labels.pack(fill="x")
        self._cd_var = tk.StringVar(value="")
        tk.Label(labels, textvariable=self._cd_var, bg=C["bg"], fg=C["dim"],
                 font=F["small"]).pack(side="left")
        self._wait_var = tk.StringVar(value="")
        tk.Label(labels, textvariable=self._wait_var, bg=C["bg"], fg=C["orange"],
                 font=F["small"]).pack(side="left")
        self._progress = ttk.Progressbar(cd, mode="determinate")
        self._progress.pack(fill="x", pady=(2, 0))

    # ── Results and activity ──────────────────────────────────────────────────
    def _build_workspace(self, parent):
        tabs = ttk.Notebook(parent)
        tabs.pack(fill="both", expand=True, pady=(8, 0))
        results = tk.Frame(tabs, bg=C["surface"], padx=8, pady=8)
        activity = tk.Frame(tabs, bg=C["bg"], padx=8, pady=4)
        tabs.add(results, text="Verified proxies")
        tabs.add(activity, text="Activity")

        actions = tk.Frame(results, bg=C["surface"])
        actions.pack(side="bottom", fill="x", pady=(8, 0))
        self._results_status = tk.StringVar(value="Click Find proxies to start scanning.")
        tk.Label(actions, textvariable=self._results_status, bg=C["surface"], fg=C["dim"],
                 font=F["body"]).pack(side="left")
        self._copy_selected_btn = self._btn(actions, "Copy selected", self._copy_selected,
                                            padx=10, pady=4, state="disabled")
        self._copy_selected_btn.pack(side="right")
        self._use_selected_btn = self._btn(actions, "Use selected", self._use_selected,
                                           padx=10, pady=4, state="disabled")
        self._use_selected_btn.pack(side="right", padx=6)

        columns = (
            ("endpoint", "Proxy endpoint", 190, "w"),
            ("protocol", "Protocol", 70, "center"),
            ("country", "Country", 65, "center"),
            ("exit", "Exit IP", 165, "w"),
            ("latency", "Latency", 70, "e"),
            ("source", "Source", 75, "center"),
        )
        self._results = ttk.Treeview(results, columns=[name for name, *_ in columns],
                                     show="headings", selectmode="browse", height=5)
        for name, title, width, anchor in columns:
            self._results.heading(name, text=title, anchor=anchor)
            self._results.column(name, width=width, minwidth=55, anchor=anchor)
        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self._results.yview)
        scrollbar.pack(side="right", fill="y")
        self._results.configure(yscrollcommand=scrollbar.set)
        self._results.pack(fill="both", expand=True)
        self._results.bind("<<TreeviewSelect>>", self._update_result_actions)
        self._build_log(activity)

    def _ready_proxies(self):
        return [proxy for proxy in self._manager.snapshot()
                if not self._selected_countries or proxy.cc in self._selected_countries]

    def _refresh_results(self, proxies):
        rows = {proxy.url: proxy for proxy in proxies}
        for key in self._result_rows.keys() - rows.keys():
            self._results.delete(key)
        for index, (key, proxy) in enumerate(rows.items()):
            values = (proxy.endpoint, proxy.protocol.upper(), proxy.cc, proxy.ip,
                      f"{proxy.latency} ms", "Imported" if proxy.custom else "Public")
            if key not in self._result_rows:
                self._results.insert("", index, iid=key, values=values)
            else:
                if tuple(self._results.item(key, "values")) != values:
                    self._results.item(key, values=values)
                if self._results.index(key) != index:
                    self._results.move(key, "", index)
        self._result_rows = rows
        self._update_result_actions()
        if proxies:
            label = f"{len(proxies)} ready  ·  fastest {min(proxy.latency for proxy in proxies)} ms"
        elif self._running:
            label = "Scanning for matching proxies…"
        else:
            label = "Click Find proxies to start scanning."
        if time.monotonic() >= self._results_notice_until:
            self._results_status.set(label)

    def _notice_result(self, message):
        self._results_notice_until = time.monotonic() + 3
        self._results_status.set(message)

    def _selected_result(self):
        selected = self._results.selection()
        return self._result_rows.get(selected[0]) if selected else None

    def _update_result_actions(self, _event=None):
        proxy = self._selected_result()
        self._copy_selected_btn.config(state="normal" if proxy else "disabled")
        self._use_selected_btn.config(
            state="normal" if proxy and proxy.addr == proxy.endpoint else "disabled"
        )

    def _copy_selected(self):
        proxy = self._selected_result()
        if proxy is None:
            return
        if all(candidate.url != proxy.url for candidate in self._ready_proxies()):
            self._notice_result("That result has expired. Select another proxy.")
            return
        self._copy_url(proxy.url)
        self._notice_result("Selected proxy copied.")

    def _use_selected(self):
        proxy = self._selected_result()
        if proxy is None or proxy.addr != proxy.endpoint:
            return
        proxy = self._manager.take_proxy(proxy.addr, proxy.protocol)
        if proxy is None:
            self._refresh_status()
            self._notice_result("That result has expired. Select another proxy.")
        elif self._activate_proxy(proxy):
            self._refresh_status()
        else:
            self._refresh_status()
            self._notice_result("Could not apply the system proxy. See Activity.")

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
        lat = self._lat_var.get()
        try:
            self._selected_countries = country_codes(self._country_var.get())
        except ValueError as exc:
            self._log(str(exc))
            self._country_var.set(", ".join(sorted(self._selected_countries)) or "Any")
        protocol = self._proto_var.get()
        protocols = PROTOCOLS if protocol == "all" else protocol.split(",")
        self._manager.configure(protocols, 0 if lat == "Any" else int(lat),
                                self._selected_countries,
                                sources=SOURCE_OPTIONS[self._source_var.get()],
                                verify_site=self._dual_var.get())
        self._refresh_status()

    # ── Status refresh ────────────────────────────────────────────────────────
    def _refresh_status(self):
        proxies = self._manager.snapshot()
        countries = {proxy.cc for proxy in proxies if proxy.cc != "??"}
        matching = [proxy for proxy in proxies
                    if not self._selected_countries or proxy.cc in self._selected_countries]
        self._stat_queue.set(str(len(matching)))
        self._next_btn.config(state="normal" if matching else "disabled")
        self._refresh_results(matching)
        self._stat_verif.set(str(self._manager.verified_total))
        self._stat_tested.set(str(self._manager.tested_total))
        self._stat_rot.set(str(self._rotations))

        self._country_cb["values"] = ["Any", *sorted(countries)]

    def _tick_status(self):
        self._refresh_status()
        self.root.after(400, self._tick_status)

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
        self._ip_request += 1
        threading.Thread(target=self._do_fetch_ip, args=(self._ip_request, self._current),
                         daemon=True).start()

    def _do_fetch_ip(self, request_id, proxy):
        result = check_connection(proxy)
        self._ui(self._show_ip, request_id, proxy, result)

    def _show_ip(self, request_id, proxy, result):
        if request_id != self._ip_request:
            return
        if result:
            ip, cc, _ = result
            tag = f"   {cc}" if proxy else ""
            self._ip_var.set(ip + tag)
            self._dot.config(fg=C["green"])
        else:
            self._ip_var.set("Proxy unreachable" if proxy else "Direct IP unavailable")
            self._dot.config(fg=C["red"])

    def _clear_active_proxy(self):
        if self._system_proxy_active:
            SystemProxy.disable()
            self._system_proxy_active = False
        self._current = None
        self._active_var.set("Proxy: none  ·  direct connection")
        self._refresh_ip_async()

    def _disable_proxy_and_refresh(self):
        self._stop_rotation()
        self._clear_active_proxy()

    # ── Custom list / export ──────────────────────────────────────────────────
    def _add_feed(self):
        value = simpledialog.askstring(
            "Add proxy feed",
            "Enter an HTTPS text-feed URL. For bare addresses, prefix it with "
            "http=, socks4=, or socks5=.",
            parent=self.root,
        )
        if not value:
            return
        try:
            feed = parse_feed(value)
        except ValueError as exc:
            messagebox.showerror("Invalid feed", str(exc), parent=self.root)
            return
        if self._manager.add_feed(feed):
            self._log(f"Added feed {feed.name}.")
            self._notice_result("Feed added." if self._running else "Feed added. Click Find proxies.")
        else:
            self._notice_result("That feed is already enabled.")

    def _load_custom(self):
        path = filedialog.askopenfilename(
            title="Load proxy list",
            filetypes=[("Proxy lists", "*.txt *.csv *.json"), ("All files", "*.*")])
        if not path:
            return
        default = self._proto_var.get()
        if default not in PROTOCOLS:
            default = "http"
        try:
            entries = read_proxy_list(path, default)
        except (OSError, ValueError, csv.Error) as e:
            self._log(f"✗  Could not read list: {e}")
            return
        added = self._manager.add_custom(entries)
        self._log(f"Loaded {added} custom proxies (default protocol: {default}). "
                  "They'll be tested and prioritised.")
        if not self._running:
            self._log("Click Find proxies to test the imported list.")

    def _export_working(self):
        pool = self._ready_proxies()
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
            for p in pool:
                f.write(f"{p.url}  {p.cc}  {p.latency}\n")
        self._log(f"Exported {len(pool)} working proxies → {path}")

    def _copy_active(self):
        if not self._current:
            self._log("No active proxy to copy.")
            return
        self._copy_url(self._current.url)

    def _copy_url(self, url):
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._log("Proxy copied to clipboard.")

    # ── Rotation control ──────────────────────────────────────────────────────
    def _toggle(self):
        self._stop() if self._running else self._start()

    def _start(self):
        self._running = True
        self._on_filters_change()
        self._start_btn.config(text="Stop scan", bg=C["red"])
        self._log("Proxy discovery started.")
        self._manager.start()

    def _stop(self):
        self._running = False
        self._manager.stop()
        self._stop_rotation()
        self._start_btn.config(text="Find proxies", bg=C["blue"])
        self._log("Proxy discovery stopped.")
        self._refresh_status()

    def _toggle_rotation(self):
        if self._rotating:
            self._stop_rotation()
            return
        interval = self._interval_var.get()
        if interval == "Manual":
            self._cd_var.set("Manual mode. Use Next proxy or select a result.")
            return
        if not self._running:
            self._start()
        self._interval = int(interval)
        self._rotating = True
        self._rotation_due = time.monotonic()
        self._rotation_btn.config(text="Stop rotation")
        self._interval_cb.config(state="disabled")

    def _stop_rotation(self):
        self._rotating = False
        self._rotation_due = None
        self._rotation_btn.config(text="Start rotation")
        self._interval_cb.config(state="readonly")
        self._cd_var.set("")
        self._wait_var.set("")
        self._progress["value"] = 0

    def _apply_proxy(self, proxy: Proxy):
        host, port = proxy.endpoint.rsplit(":", 1)
        if proxy.addr != proxy.endpoint:
            if self._system_proxy_active:
                SystemProxy.disable()
                self._system_proxy_active = False
            label = "Copy to use"
        elif SystemProxy.enable(host, port, proxy.protocol):
            self._system_proxy_active = True
            label = "Proxy"
        else:
            self._log(f"✗  Could not set system proxy {proxy.endpoint}")
            return False
        self._current = proxy
        self._rotations += 1
        self._next_health_check = time.monotonic() + HEALTH_INTERVAL
        self._active_var.set(f"{label}: {proxy}")
        self._log(f"→  {label}: {proxy}")
        self._ip_request += 1
        self._show_ip(self._ip_request, proxy, (proxy.ip, proxy.cc, proxy.latency))
        return True

    def _force_rotate(self):
        if not self._try_rotate():
            countries = ", ".join(sorted(self._selected_countries))
            where = f" from {countries}" if countries else ""
            self._log(f"No usable proxy{where} is ready.")

    def _try_rotate(self):
        previous_ip = self._current.ip if self._current else None
        proxy = self._manager.get_next(self._selected_countries, previous_ip)
        return proxy is not None and self._activate_proxy(proxy)

    def _activate_proxy(self, proxy):
        if not self._apply_proxy(proxy):
            return False
        if self._rotating:
            self._rotation_due = time.monotonic() + self._interval
        self._wait_var.set("")
        return True

    def _tick_rotation(self):
        now = time.monotonic()
        if self._rotating:
            remaining = self._rotation_due - now
            if remaining <= 0:
                if not self._try_rotate():
                    countries = ", ".join(sorted(self._selected_countries))
                    where = f" ({countries})" if countries else ""
                    self._wait_var.set(f"Waiting for a verified proxy{where}…")
                    self._cd_var.set("")
                    self._progress["value"] = 0
            else:
                self._cd_var.set(f"Next rotation in {int(remaining + 0.999)}s")
                self._progress["value"] = 100 * (1 - remaining / self._interval)
        if (self._current and self._health_var.get() and not self._health_in_flight
                and now >= self._next_health_check):
            self._health_in_flight = True
            threading.Thread(target=self._check_active, args=(self._current, self._dual_var.get()),
                             daemon=True).start()
        self.root.after(250, self._tick_rotation)

    def _check_active(self, proxy, dual):
        result = check_connection(proxy)
        if result and dual and verify_destination(proxy, TEST_TIMEOUT) is None:
            result = None
        self._ui(self._finish_health_check, proxy, result)

    def _finish_health_check(self, proxy, result):
        self._health_in_flight = False
        if proxy is not self._current:
            return
        self._next_health_check = time.monotonic() + HEALTH_INTERVAL
        if result is None and self._health_var.get():
            self._log(f"⚠  {proxy.endpoint} failed the HTTPS health check.")
            self._clear_active_proxy()
            if self._rotating:
                self._rotation_due = time.monotonic()
        elif result:
            proxy.ip = result[0]
            self._ip_request += 1
            self._show_ip(self._ip_request, proxy, result)

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
        s.configure("TNotebook", background=C["bg"], borderwidth=0)
        s.configure("TNotebook.Tab", background=C["panel"], foreground=C["dim"],
                    padding=(12, 5), font=F["body"])
        s.map("TNotebook.Tab", background=[("selected", C["surface"])],
              foreground=[("selected", C["text"])])
        s.configure("Treeview", background=C["bg"], fieldbackground=C["bg"],
                    foreground=C["text"], rowheight=23, borderwidth=0, font=F["body"])
        s.configure("Treeview.Heading", background=C["panel"], foreground=C["text"], font=F["h2"])
        s.map("Treeview", background=[("selected", C["blue"])],
              foreground=[("selected", C["bg"])])
        s.configure("Vertical.TScrollbar", background=C["muted"], troughcolor=C["bg"],
                    borderwidth=0, arrowcolor=C["dim"])

    # ── Entry point ───────────────────────────────────────────────────────────
    def _on_close(self):
        self._running = False
        self._manager.stop()
        if self._system_proxy_active:
            SystemProxy.disable()
        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_ip_async()
        self._log(f"{self.APP_NAME} ready on {_SYS}. {SystemProxy.platform_note()}")
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
