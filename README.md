# proxy-checker
Fast threaded proxy checker with real transfer tests, GeoIP lookup, multi-format outputs, and optional Discord notifications with rich embeds.

<!-- PROXY_STATUS:START -->
| 🌐 Updated (UTC) | ✅ Alive | 📄 TXT | 📜 JSONL | 📦 Size (TXT/JSONL) |
|---|---:|---|---|---|
| 2025-11-12 08:36 UTC | 5079 | [proxies/alive_proxies.txt](proxies/alive_proxies.txt) | [proxies/alive_proxies.jsonl](proxies/alive_proxies.jsonl) | 96.4KB / 977.2KB |
<!-- PROXY_STATUS:END -->

## Features
- Harvests proxies from many public lists (HTTP + HTTPS)
- Verifies real data transfer (>= 200KB) over both HTTP and HTTPS
- Optional GeoIP via ip-api.com through the proxy itself
- Outputs in `proxies/`: `alive_proxies.txt`, `alive_proxies.jsonl`, `alive_proxies.json`
- Discord webhook notifications:
	- Per-hit embed for each alive proxy (flag, ISP, supports, mobile/proxy/hosting flags)
	- Optional final summary (alive count, total processed, duration, top countries)
 - Protocols: HTTP and HTTPS only for now. SOCKS4 and SOCKS5 planned for a future version.

## Usage

Basic run:

```bash
python app.py
```

Key options:

- `--timeout <sec>`: per-proxy hard budget (default 2.0)
- `--workers <n>`: concurrent checks (default 1000, clamped by system caps)
- `--no-geo`: disable GeoIP lookups (faster)
- `--only http|https`: only harvest one protocol list
- `--min-bytes <n>`: required bytes to pass (default 204800)
- `--webhook-url <url>`: Discord webhook for notifications (or set env `DISCORD_WEBHOOK_URL`)
- `--webhook-username <name>`: custom webhook username (optional)
- `--webhook-summary`: also send a final completion summary embed
- `--no-git-push`: disable auto-commit and push of updated `proxies/*` outputs (enabled by default)

Example with Discord:

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/XXX/YYY"
python app.py --webhook-summary
```

Or pass explicitly:

```bash
python app.py \
	--webhook-url "https://discord.com/api/webhooks/XXX/YYY" \
	--webhook-username "Proxy Hunter" \
	--webhook-summary
```

Notes:
- Discord posts are best-effort and rate-limit aware. If the queue backs up, some hit notifications may be dropped to keep scanning fast.
- GeoIP lookups are subject to ip-api.com throttling; use `--no-geo` to skip or reduce `--workers`.
 - Previous scan results are always re-used: the checker merges proxies harvested from remote lists with what was found in prior runs (`proxies/alive_proxies.*`). Hits are appended without duplicates across runs.

## Configuration

Proxy source links are defined in `config.json` at the repository root. Edit the `http` and `https` arrays to add or remove list endpoints. You can also point to a different file with:

```bash
python app.py --config /path/to/your/config.json
```

If the config file is missing or invalid, the app uses embedded defaults.

