# Anti-Detect Suite

Smart IP rotation through verified proxies + a built-in anti-detect guide — all in one desktop app.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![Platform](https://img.shields.io/badge/Platform-Windows-blue?logo=windows)
![License](https://img.shields.io/badge/License-MIT-green)

---

## What it does

Most IP-changer apps swap your IP and call it done. That is not enough — modern websites fingerprint your **browser**, not just your IP. This app handles the IP layer properly and teaches you how to cover every other layer.

### IP Rotator (Layer 1)
- Pulls proxies from **7 free public sources** simultaneously
- Tests them with **14 concurrent threads** — only verified, working proxies enter the queue
- Rotates the **Windows system proxy** on a configurable timer (15 s – 2 min)
- **Smart wait logic**: if the timer fires and no tested proxy is ready yet, it pauses and waits — it never leaves you with a broken or untested connection
- Restores your direct connection automatically when you stop or close the app
- Affects: **Chrome, Edge, and any app that follows Windows system proxy**

### Anti-Detect Guide (built-in tab)
A scrollable guide explaining every tracking layer websites use and the free tools to defeat each one.

---

## Screenshots

> IP Rotator tab — shows current IP, queue depth, countdown, and activity log  
> Anti-Detect Guide tab — explains all 5 tracking layers and free tools for each

---

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- `requests` library (installed automatically by `run.bat`)

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/anti-detect-suite.git
cd anti-detect-suite
pip install -r requirements.txt
python ip_changer.py
```

Or just double-click **`run.bat`** — it installs dependencies and launches the app.

---

## Usage

1. Launch the app (`run.bat` or `python ip_changer.py`)
2. Your real IP is shown at the top — click **Refresh** any time
3. Click **▶ Start Rotation**
   - The background fetcher starts immediately, downloading and testing proxies
   - As soon as the first proxy passes its test it is activated
   - The countdown timer runs; when it hits zero the next proxy in the queue is applied
   - If no proxy is ready at rotation time: the app shows "waiting…" and applies the next one the moment it passes the test
4. Use **Skip → Next IP** to rotate immediately without waiting for the timer
5. Click **■ Stop Rotation** to stop — your direct connection is restored automatically

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

---

## The complete free stack (all 5 layers)

| Tool | Layer | Cost |
|---|---|---|
| This app | IP rotation | Free |
| [Proton VPN](https://protonvpn.com) | Better-quality IPs | Free tier |
| [GoLogin](https://gologin.com) | Browser fingerprint + cookie isolation | Free (3 profiles) |
| [ProtonMail](https://proton.me) | Clean email, no phone required | Free |
| [Privacy.com](https://privacy.com) | Virtual card per merchant (US only) | Free |

### Test your fingerprint before visiting any site
- [coveryourtracks.eff.org](https://coveryourtracks.eff.org) — EFF tracker test
- [browserleaks.com](https://browserleaks.com) — detailed breakdown of every signal
- [pixelscan.net](https://pixelscan.net) — bot/human detection test

---

## How browser fingerprinting works

When you load a page, JavaScript runs silently and collects:

- **Canvas fingerprint** — draws a hidden shape; your GPU renders it with microscopic differences unique to your machine
- **WebGL fingerprint** — queries your GPU model and driver version
- **Font list** — checks which fonts are installed
- **Screen** — resolution, colour depth, pixel ratio
- **Timezone + language** — reveals your location even behind a VPN
- **Navigator** — CPU core count, RAM size, browser version, platform
- **Behaviour** — mouse movement curves, typing rhythm, scroll speed

All of these are combined into a single hash. That hash identifies your device across sites, across sessions, even after you clear cookies and change your IP.

An **anti-detect browser** (GoLogin, AdsPower, Brave with fingerprint randomisation) spoofs every one of these signals, making you look like a completely different device each session.

---

## Proxy quality note

Free public proxies are unstable — they appear and disappear quickly. This app mitigates that by:
- Testing before using (only verified proxies enter the queue)
- Running the fetcher continuously in the background so the queue is always replenished
- Waiting for a fresh proxy rather than applying an untested one

For higher reliability and faster speeds, consider [Proton VPN](https://protonvpn.com) (free) or [Windscribe](https://windscribe.com) (10 GB/month free) instead of public proxies.

---

## Project structure

```
anti-detect-suite/
├── ip_changer.py     # main application (single file, no frameworks)
├── requirements.txt  # only dependency: requests
├── run.bat           # Windows launcher (installs deps + starts app)
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
