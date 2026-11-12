# Chameleon

Chameleon finds free HTTP, SOCKS4, and SOCKS5 proxies, verifies them from your
machine, and ranks them by measured latency. The Python desktop dashboard
lets you scan, filter, copy, export, and apply verified proxies.

## Quick start

Install Python 3.10 or newer, then run:

```sh
git clone https://github.com/chentaymane/Chameleon.git
cd Chameleon
python -m pip install -r requirements.txt
python chameleon.py
```

The dashboard requires Tkinter, which
comes with the standard Windows and macOS Python installers. On Linux, install
your distribution's Tkinter package if needed, such as `python3-tk` on Ubuntu
or `python3-tkinter` on Fedora. Windows can also use `run.bat`; Linux and macOS
can use `bash run.sh`.

## Use the dashboard

1. Choose one or more proxy types, a latency cap, and a source group. Enter
   countries such as `US, DE` in **Exit countries** and press Enter or click
   **Apply**. Use `Any` to allow all countries.
2. Click **Find proxies** to scan. Scanning leaves your system proxy alone.
   The **Verified proxies** table shows the endpoint, type, exit country,
   exit IP, latency, and whether the endpoint came from an imported file.
3. Select a result and click **Copy selected** to get its proxy URL or
   **Use selected** to apply it to the system. **Next proxy** picks the
   next matching result. **Export** saves the current results to a text file.
4. To rotate automatically, choose an interval and click **Start rotation**.
   **Stop rotation** pauses the timer while scanning continues. **Stop scan**
   stops discovery and the rotation timer. Use **Disable proxy** to clear
   the active system proxy. Closing Chameleon also clears the proxy it applied.

Use **Load list** for local text, CSV, or JSON files and **Add feed** for an HTTPS
text feed. Prefix a feed with `http=`, `socks4=`, or `socks5=` if it lists bare
addresses. **Imported only** skips the built-in providers and uses the files
and feeds you added. Open **Activity** for feed counts and status messages.

**Auto-recheck active proxy** tests the active route about every 20 seconds.
On failure, it clears the route and looks for a replacement if timed rotation
is enabled.

| OS | System settings used |
| --- | --- |
| Windows | WinINET settings |
| Linux | GNOME `gsettings` |
| macOS | `networksetup` |

System proxy support depends on the OS and the application using it.
Authenticated proxies can be copied into applications that support credentials.
Text inputs accept `host:port`, `socks5://host:port`,
`host:port:user:password`, and `http://user:password@host:port`.

## Optional command-line finder

```sh
# Find up to 15 distinct exits in the US, Germany, or France.
python find_proxies.py --country US,DE,FR --protocol http,socks5 --limit 15 --unique-exits

# Combine two sources and save structured results.
python find_proxies.py --source proxyscrape-api --source monosans --budget 800 --format json > proxies.json

# Recheck a saved list and a custom feed of bare SOCKS5 addresses.
python find_proxies.py --no-public --input proxies.json --feed socks5=https://example.org/list.txt
```

| Option | What it controls |
| --- | --- |
| `--country US,DE` | Allowed exit countries. Repeat the flag or use commas. Country comes from the live HTTPS check. |
| `--protocol http,socks5` | Allowed proxy types. Select HTTP, SOCKS4, SOCKS5, or `all`. |
| `--source NAME` | Choose one or more built-in providers. Omitting it enables all five. |
| `--feed [TYPE=]HTTPS_URL` | Add a remote text feed. Use a type prefix if the lines are bare addresses. |
| `--input PATH` | Import a text list or Chameleon's CSV or JSON output. Repeat for multiple files. |
| `--no-public` | Use only explicitly imported files and feeds. |
| `--input-protocol TYPE` | Protocol for bare addresses in local lists. The default is HTTP. |
| `--verify dual` | Require both HTTPS destinations. Choose `trace` for a faster, single-site check. |
| `--max-latency 2500` | Maximum measured latency in milliseconds per site. Use `0` to disable the cap. |
| `--budget 400` | Maximum number of candidates to test. A larger budget gives the finder more chances to find a match. |
| `--workers 24` / `--timeout 5` | Tune concurrency and connect/read timeouts. |
| `--limit 10` / `--unique-exits` | Set the result count and optionally keep only the best endpoint per exit IP. |
| `--format text` | Choose text, CSV, or JSON. CSV and JSON also include the observed exit IP. |

Run `python find_proxies.py --help` for the full argument list. Progress goes
to stderr. Exit code 0 means at least one match, 1 means no matches, and 2
means invalid arguments or an unreadable input file.

## Sources and ranking

| Source name | How candidates are retrieved |
| --- | --- |
| `proxyscrape-api` | Live text API with a country query when one or more countries are selected. [ProxyScrape](https://github.com/ProxyScrape/free-proxy-list#want-the-live-api-instead) |
| `proxyscrape` | Public text mirror of the same provider. [ProxyScrape mirror](https://github.com/ProxyScrape/free-proxy-list) |
| `monosans` | Public text list ordered by upstream checks. [monosans](https://github.com/monosans/proxy-list) |
| `proxifly` | Public mixed-protocol text list. [Proxifly](https://github.com/proxifly/free-proxy-list) |
| `proxy-scraper` | Public mixed-protocol text list on the provider's own domain. [Proxy Scraper](https://github.com/maximilianfeix/free-proxy-list) |

The desktop app refreshes each feed every two minutes when the queue needs
candidates. It interleaves feeds and protocols so one large list cannot consume
the scan budget. Imported endpoints are checked first. Public endpoints are
deduplicated by address and protocol.

Chameleon opens a TLS-verified connection to Cloudflare's trace endpoint to
read the exit IP and country. The default dual mode also fetches Example
Domain and checks its page title. The slower of the two requests sets the
latency used for ranking. Imported endpoints rank first, then the fastest
passing endpoints take priority. In the desktop app, rotation prefers a
different exit IP when one is ready.

HTTP entries are dialed as HTTP CONNECT proxies for HTTPS traffic. SOCKS4 and
SOCKS5 checks resolve the destination through the proxy. Results reflect
connectivity from your machine at scan time. Free proxies can change quickly,
and a passing test does not establish anonymity. Keep credentials and sensitive
traffic off public proxies. Exports from imported authenticated lists may
contain credentials.

## Development

```sh
python -m unittest -v
```

- `chameleon.py` contains the desktop app and system proxy integration.
- `proxy_pool.py` implements source fetching, TLS checks, and the verified pool.
- `find_proxies.py` is the headless finder and exporter.
