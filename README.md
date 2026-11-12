<h1 align="center">🦎 Chameleon</h1>

<p align="center">
  <b>Cross-platform smart IP rotation through verified, geo-tagged proxies.</b><br>
  Blend into a new location on demand — pick a country, rotate on a timer or manually,
  and keep only proxies that actually work.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-blue">
  <img src="https://img.shields.io/badge/UI-tkinter-informational">
  <img src="https://img.shields.io/badge/License-MIT-green">
</p>

---

## Table of contents

- [What Chameleon does](#what-chameleon-does)
- [Why "just change my IP" is not enough](#why-just-change-my-ip-is-not-enough)
- [How it works under the hood](#how-it-works-under-the-hood)
- [Install & run](#install--run)
- [The interface, panel by panel](#the-interface-panel-by-panel)
- [Choosing a country](#choosing-a-country)
- [Using your own (paid) proxies](#using-your-own-paid-proxies)
- [Every setting explained](#every-setting-explained)
- [How the system proxy is applied per OS](#how-the-system-proxy-is-applied-per-os)
- [Tuning constants](#tuning-constants)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Project structure](#project-structure)
- [Disclaimer & license](#disclaimer--license)

---

## What Chameleon does

Chameleon is a single-window desktop app that:

1. **Collects proxies** continuously in the background from many free public
   sources (HTTP, SOCKS4 and SOCKS5) — plus any list you load yourself.
2. **Verifies candidates over HTTPS**, measures their latency, and records the
   country reported for their public exit IP.
3. **Rotates your operating-system proxy** to those verified proxies, either
   automatically on a timer or manually with a button — optionally **pinned to a
   country you choose**.
4. **Watches the active proxy** and, if it dies, automatically switches to the
   next good one.
5. **Disables the system proxy it applied** when you stop or close the app.

Because it drives the **OS-level proxy**, it affects normal browsers and any app
that follows the system proxy — or you can just **copy** the active proxy string
into an anti-detect browser profile (Multilogin / AdsPower / GoLogin).

---

## Why "just change my IP" is not enough

Websites recognise returning visitors across **five independent layers**.
Rotating your IP only defeats the first one. Chameleon handles Layer 1 properly
and is designed to sit alongside tools that cover the rest:

| Layer | What the site collects | What defeats it |
|------:|------------------------|-----------------|
| **1. IP address** | Your ISP-assigned IP + its geolocation | **Proxy / VPN ← Chameleon** |
| **2. Browser fingerprint** | Canvas hash, WebGL GPU, fonts, screen, timezone, CPU/RAM | Anti-detect browser (GoLogin, AdsPower) or Brave "randomize fingerprint" |
| **3. Cookies & storage** | Session tokens, tracking IDs | Isolated browser profile / clearing all site data |
| **4. Email** | Address tied to account history | A fresh email (ProtonMail, SimpleLogin) |
| **5. Payment** | Card / PayPal tied to identity | Virtual card (Privacy.com, Revolut, Wise) |

> **Consistency matters more than novelty.** A "US" account that suddenly logs in
> from five countries in one hour is a bigger red flag than one that stays in the
> US. Use Chameleon's **country selector** to keep your IP consistent with the
> email / address / payment country on the account.

---

## How it works under the hood

Chameleon fetches [monosans](https://github.com/monosans/proxy-list),
[ProxyScrape](https://github.com/ProxyScrape/free-proxy-list), and
[Proxifly](https://github.com/proxifly/free-proxy-list) concurrently. It deduplicates
public endpoints by address and protocol, gives imported proxies priority, and
interleaves HTTP, SOCKS4, and SOCKS5 candidates.

Each of up to 24 workers makes a TLS-verified request to
[Cloudflare's connection trace endpoint](https://developers.cloudflare.com/fundamentals/reference/cdn-cgi-endpoint/).
The HTTPS request measures latency and checks the exit IP and reported country.
The pool holds up to 40 results for three minutes. Rotation chooses the fastest
eligible proxy, with imported proxies ranked first. If a country is pinned, the
pool keeps scanning when fewer than eight proxies from that country are ready.

When enabled, the health check repeats the HTTPS probe every 20 seconds. If the
active proxy fails, Chameleon clears it and waits for an eligible replacement.

---

## Install & run

### Requirements

- **Python 3.10+** with **tkinter**
  - Windows / macOS: tkinter ships with the official Python installer.
  - Linux: install it separately —
    - Debian/Ubuntu: `sudo apt install python3 python3-tk`
    - Fedora: `sudo dnf install python3-tkinter`
    - Arch: `sudo pacman -S tk`
- **`requests[socks]`** — installs `requests` plus the SOCKS support it needs
  (handled automatically by the launcher).

### Windows

```bat
git clone https://github.com/chentaymane/Chameleon.git
cd Chameleon
run.bat
```

`run.bat` installs dependencies and launches the app. (Or run
`pip install -r requirements.txt` then `python chameleon.py`.)

### Linux / macOS

```bash
git clone https://github.com/chentaymane/Chameleon.git
cd Chameleon
chmod +x run.sh
./run.sh
```

### Find live proxies from the command line

```sh
python -m pip install -r requirements.txt
python find_proxies.py --limit 10
python find_proxies.py --protocol socks5 --country US --max-latency 3000 --budget 800
python find_proxies.py > working_proxies.txt
```

Results are ordered by measured latency and can be imported with **Load list**.
Each row contains the proxy URL, exit country, and latency in milliseconds.
Progress is written to stderr. Exit status 1 means no matching proxy passed.
A passing result reflects connectivity from the machine running the check.

---

## The interface, panel by panel

Chameleon is a single window with four stacked cards plus an action row and log.

**Current connection**
- The big value is your **current public IP** (with its country code once a proxy
  is active). The dot is **green** when reachable, **red** when not.
- **Proxy:** the active proxy line — `host:port · protocol · country · latency`.
- **Refresh** re-checks your IP now. **Copy** puts `protocol://host:port` on the
  clipboard for pasting into an anti-detect browser.

**Session** — live counters for the current run:
- **Queue ready** — verified proxies waiting to be used.
- **Verified** — total proxies that have passed testing this session.
- **Rotations** — how many times Chameleon has switched proxy.
- **Tested** — total proxies attempted.
- **In queue:** a per-country breakdown of what's currently available.

**Rotation settings** — every filter and the country selector (see below).

**Action row**
- **Start / Stop** — begins/ends rotation and the background fetcher.
- **→ Next Proxy** — rotate immediately (respects the country selector).
- **Disable** — stop rotation and turn off the proxy Chameleon applied.
- **Load list…** — import your own proxies.
- **Export** — save all currently-working proxies to a file.

Below that: a **countdown + progress bar** to the next rotation, and a scrollable
**activity log** (with a **Clear** button).

---

## Choosing a country

This is the headline feature. Every verified proxy is geo-tagged, so:

1. Start Chameleon and let the **Queue ready** counter climb — as proxies are
   verified, the **Country for next proxy** dropdown fills with the countries
   that are actually available, each showing a live count, e.g. `US (5)`.
2. Pick a country. From then on **every** rotation — automatic *and* manual —
   will only use a proxy from that country.
3. If no proxy from the chosen country is ready when the timer fires, Chameleon
   shows **"waiting…"** and holds until one is verified, rather than jumping to a
   different country.
4. Switch back to **Any** to allow any country again.

The dropdown label shows counts, but your choice is remembered by country **code**,
so it won't get lost as the counts change in the background.

---

## Using your own (paid) proxies

Free public proxies are unstable by nature — they appear and vanish constantly.
For real reliability, load your own. Click **Load list…** and select a `.txt`/`.csv`
file. Chameleon accepts all of these formats, one per line (blank lines and lines
starting with `#` are ignored):

```
185.23.44.10:8080                       # host:port          (uses the Protocol dropdown default)
http://185.23.44.10:8080                # protocol://host:port
socks5://185.23.44.10:1080              # protocol://host:port
185.23.44.10:8080:myuser:mypass         # host:port:user:pass (auth)
socks5://myuser:mypass@185.23.44.10:1080# protocol://user:pass@host:port
```

Notes:
- The **Protocol** dropdown sets the default protocol for lines that don't specify
  one (`https` is treated as `http` for dialling).
- Imported proxies are checked before public candidates and can be retried after
  a three-minute cooldown.
- Authenticated proxies can be used through **Copy**. The system settings used
  by Chameleon do not apply imported proxy credentials.

---

## Every setting explained

| Control | What it does |
|---|---|
| **Rotate every** | Seconds between automatic rotations, or **Manual** (never auto-rotate — you drive it with *Next Proxy*). |
| **Protocol** | Which proxy types to fetch and use: `all`, `http`, `socks4`, or `socks5`. Narrowing this can improve quality if one type works better on your network. |
| **Max latency** | Reject any proxy slower than this many milliseconds during testing. Great for dropping sluggish/half-dead proxies. `Any` = no cap. |
| **Country for next proxy** | Pin all rotations to one country, or `Any`. Fills in live as countries are discovered. |
| **Auto-recheck active proxy** | When on, the active proxy is re-tested every 20 s and Chameleon auto-rotates the moment it stops working. |

---

## How the system proxy is applied per OS

| OS | Mechanism | Affects |
|---|---|---|
| **Windows** | WinINET registry keys under `Internet Settings` + a live settings-changed notification | Chrome, Edge, Internet Explorer, and any WinINET app |
| **Linux** | GNOME `gsettings` (`org.gnome.system.proxy` → manual) | Apps that follow the GNOME proxy (GNOME/Cinnamon/Unity) |
| **macOS** | `networksetup` web/secure/SOCKS proxy on each active network service | System-wide network services |

On **stop** or **window close**, Chameleon disables any system proxy it applied.

> **Firefox and non-GNOME Linux desktops** don't follow these system settings.
> There, use the **Copy** button and paste the proxy into the app's own proxy
> configuration (or your anti-detect browser profile).

---

## Tuning constants

All near the top of [`chameleon.py`](chameleon.py) if you want to tweak behaviour:

| Constant | Default | Meaning |
|---|---:|---|
| `TEST_TIMEOUT` | `5` | Maximum connect and read timeout in seconds for a probe. |
| `TEST_WORKERS` | `24` | Max concurrent test threads. |
| `QUEUE_LOW_MARK` | `8` | Refetch when the queue drops below this. |
| `QUEUE_HIGH_MARK` | `40` | Maximum verified proxies retained. |
| `MAX_TEST_BATCH` | `400` | Proxies tested per fetch cycle. |
| `HEALTH_INTERVAL` | `20` | Seconds between active-proxy health checks. |
| `PROXY_SOURCES` | — | URLs for feeds with one protocol-prefixed proxy per line. |

---

## Troubleshooting

- **"waiting…" forever / queue stays at 0** — free sources may be down or your
  network blocks them. Lower filters (`Protocol: all`, `Max latency: Any`) or
  **Load list…** with your own proxies.
- **IP shows "proxy unreachable"** — that proxy died between testing and use
  (common with free proxies). With **Auto-recheck** on, Chameleon rotates away
  automatically; otherwise click **→ Next Proxy**.
- **Browser ignores the proxy (Linux)** — you're likely not on a GNOME-based
  desktop, or the app has its own proxy setting. Use **Copy** + paste it in.
- **`ModuleNotFoundError: socks`** — run `pip install "requests[socks]"`.
- **`No module named tkinter` (Linux)** — install the tk package for your distro
  (see [Requirements](#requirements)).
- **Nothing changes system-wide** — some corporate machines lock proxy settings;
  Chameleon can't override policy. Use the Copy-into-app workflow.

---

## FAQ

**Is this a VPN?** No. It routes traffic through third-party HTTP/SOCKS proxies.
Public proxies are **not** private — never send sensitive data through them. For
privacy, load trusted paid proxies or use a real VPN.

**Does it hide my browser fingerprint?** No — that's Layer 2. Chameleon only
handles the IP layer. Pair it with an anti-detect browser (see the table above).

**Will it work on Wayland / KDE?** The GUI runs anywhere tkinter runs. System-proxy
control uses GNOME `gsettings`; on KDE/others use the **Copy** workflow.

**Can I keep the same country the whole session?** Yes — pick it in *Country for
next proxy* and every rotation stays in that country.

---

## Project structure

```
chameleon/
├── chameleon.py      # desktop app and system proxy integration
├── proxy_pool.py     # discovery, HTTPS validation and ranking
├── find_proxies.py   # command-line discovery
├── test_proxy_pool.py
├── requirements.txt  # requests[socks]
├── run.bat           # Windows launcher
├── run.sh            # Linux / macOS launcher
├── .gitignore
└── README.md
```

Run the regression tests with `python -m unittest -v`.

---

## Disclaimer & license

Published for **educational and privacy-research purposes**. It demonstrates how
IP rotation, proxy testing, and geolocation filtering work.

- Using this tool to violate any website's Terms of Service is your own
  responsibility.
- Free public proxies are operated by unknown third parties — **do not send
  sensitive data through them**.
- The authors are not responsible for how this software is used.

**License:** MIT — free to use, modify, and distribute.
