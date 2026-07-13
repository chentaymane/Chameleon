# Anti-Detect Suite

Cross-platform smart IP rotation through verified, geo-tagged proxies + a built-in anti-detect guide — all in one desktop app.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)
![License](https://img.shields.io/badge/License-MIT-green)

---

## What it does

Most IP-changer apps swap your IP and call it done. That is not enough — modern websites fingerprint your **browser**, not just your IP. This app handles the IP layer properly, lets you pin rotations to a **specific country**, and teaches you how to cover every other layer.

### IP Rotator
- Pulls proxies from many free **HTTP, SOCKS4 and SOCKS5** sources at once
- Tests them with concurrent workers — only verified, working proxies enter the queue
- **Geo-tags every proxy** and measures its latency, so you can:
  - pick the **country for the next proxy** (manual *and* auto mode)
  - filter by **protocol** and cap **max latency** to drop slow/dead proxies
- **Load your own list** — supports `host:port`, `protocol://host:port`, `host:port:user:pass`, and `protocol://user:pass@host:port` (great for paid proxies with auth)
- **Auto-recheck**: an optional health check re-tests the active proxy and rotates automatically if it dies
- **Copy** the active proxy or **export** all working ones to a file
- **Smart wait logic**: if the timer fires and no matching proxy is ready, it waits rather than applying a broken one
- Restores your direct connection automatically when you stop or close the app

### Cross-platform proxy control

| OS | How the system proxy is applied |
|---|---|
| **Windows** | WinINET registry — affects Chrome, Edge, IE, and any WinINET app |
| **Linux** | GNOME `gsettings` — affects apps that follow the GNOME proxy |
| **macOS** | `networksetup` on active network services |

On any OS you can also just **Copy** the active proxy string and paste it into an anti-detect browser profile (Multilogin / AdsPower / GoLogin).

### Anti-Detect Guide (built-in tab)
A scrollable guide explaining every tracking layer websites use and the free tools to defeat each one.

---

## Requirements

- Windows 10/11, a Linux desktop (GNOME-based for system-proxy control), or macOS
- Python 3.10 or newer, with **tkinter**
  - Windows/macOS: bundled with the standard Python installer
  - Linux: install separately, e.g. `sudo apt install python3-tk` (Debian/Ubuntu), `sudo dnf install python3-tkinter` (Fedora), `sudo pacman -S tk` (Arch)
- `requests[socks]` (installed by the launcher / `pip install -r requirements.txt`)

---

## Installation & running

```bash
git clone https://github.com/YOUR_USERNAME/anti-detect-suite.git
cd anti-detect-suite
pip install -r requirements.txt
python ip_changer.py
```

- **Windows**: double-click **`run.bat`** — it installs dependencies and launches the app.
- **Linux / macOS**: `chmod +x run.sh && ./run.sh`

---

## Usage

1. Launch the app.
2. Your real IP is shown at the top — click **Refresh** any time.
3. (Optional) Set filters:
   - **Rotate every** — interval in seconds, or **Manual** (rotate only on demand)
   - **Max latency** — reject proxies slower than this
   - **Protocol** — all / http / socks4 / socks5
   - **Country for next proxy** — the list fills in as proxies are verified; pick one to pin the next rotation to that country, or leave it on **Any**
4. (Optional) **Load list…** to add your own proxies, then **Export working** later to save the ones that passed.
5. Click **▶ Start Rotation**. The fetcher begins immediately; the first verified proxy is applied, and each rotation picks the next one matching your filters.
6. Use **→ Next Proxy** to rotate immediately (respects the country selector).
7. Click **■ Stop** — your direct connection is restored automatically.

---

## Why a plain proxy is not enough

Websites track returning users across **5 separate layers**. Changing your IP only defeats the first one.

| Layer | What is collected | Defeated by |
|---|---|---|
| **1. IP address** | Your ISP-assigned IP | Proxy / VPN ← this app |
| **2. Browser fingerprint** | Canvas hash, WebGL GPU, fonts, screen, timezone, CPU, RAM | Anti-detect browser (GoLogin, AdsPower, Brave) |
| **3. Cookies & storage** | Session tokens, tracking IDs | Isolated browser profile / clearing site data |
| **4. Email address** | Linked to account history | New email (ProtonMail, SimpleLogin) |
| **5. Payment method** | Card/PayPal linked to identity | Virtual card (Privacy.com, Revolut) |

**Tip:** match the proxy country to the account's email/address/payment country. A "US" account logging in from five countries in an hour is an obvious flag — use the country selector to stay consistent.

---

## Proxy quality note

Free public proxies are unstable — they appear and disappear quickly. This app mitigates that by testing before use, running the fetcher continuously, capping latency, and re-checking the active proxy while running. For real reliability, **load your own paid proxies** (`Load list…`) or use a VPN such as [Proton VPN](https://protonvpn.com) (free) or [Windscribe](https://windscribe.com) (10 GB/month free).

---

## Project structure

```
anti-detect-suite/
├── ip_changer.py     # main application (single file, no frameworks)
├── requirements.txt  # dependency: requests[socks]
├── run.bat           # Windows launcher
├── run.sh            # Linux / macOS launcher
└── README.md
```

---

## Disclaimer

This tool is published for **educational and privacy research purposes**. It demonstrates how IP rotation, proxy testing, and browser fingerprinting work.

- Using this tool to violate any website's Terms of Service is your own responsibility
- Free public proxies are operated by third parties — do not send sensitive data through them
- The authors are not responsible for how this software is used

---

## License

MIT — free to use, modify, and distribute.
