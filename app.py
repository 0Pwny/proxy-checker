# proxy_checker.py
import argparse
import concurrent.futures as cf
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from random import shuffle

import requests
import resource
import queue

# --------------------- Defaults ---------------------
DEFAULT_TIMEOUT = 2.0      # hard per-proxy wall-clock budget (seconds)
DEFAULT_WORKERS = 1000
FETCH_WORKERS = 32
GEO_WORKERS = 16

# Must download at least this many bytes within timeout to pass
MIN_BYTES = 200 * 1024  # 200 KiB

# Big, fast test URLs (will try several until one succeeds)
# We use Range to request only MIN_BYTES and stop reading once that’s reached.
TEST_URLS_HTTP = [
    "http://speedtest.tele2.net/1MB.zip",
    "http://ipv4.download.thinkbroadband.com/1MB.zip",
    "http://cachefly.cachefly.net/200mb.test",
]
TEST_URLS_HTTPS = [
    "https://speed.cloudflare.com/__down?bytes=300000",  # returns N bytes directly
    "https://proof.ovh.net/files/1Mb.dat",
    "https://speed.hetzner.de/1MB.bin",
]

PROXIES_DIR = os.path.join("proxies")
OUT_TXT   = os.path.join(PROXIES_DIR, "alive_proxies.txt")    # ip:port
OUT_JSONL = os.path.join(PROXIES_DIR, "alive_proxies.jsonl")  # streamed {"proxy","country","country_code","supports"}
OUT_JSON  = os.path.join(PROXIES_DIR, "alive_proxies.json")   # final consolidated array

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Encoding": "identity",  # avoid gzip to keep byte counting predictable
    "Connection": "close",
}

# --------------------- Sources ----------------------
# Default embedded sources (used if config.json missing or invalid)
DEFAULT_SOURCES = {
    "http": [
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http",
        "https://api.proxyscrape.com/?request=displayproxies&proxytype=http",
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://naawy.com/api/public/proxylist/getList/?proxyType=http&format=txt",
        "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
        "https://proxyspace.pro/http.txt",
        "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
        "https://raw.githubusercontent.com/casals-ar/proxy-list/main/http",
        "https://internet.limited/http.txt",
        "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
        "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt",
        "http://pubproxy.com/api/proxy?format=txt&type=http&limit=5",
        "https://raw.githubusercontent.com/ObcbO/getproxy/master/http.txt",
        "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt",
        "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt",
        "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Http.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
        "https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt",
        "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/http.txt",
        "https://raw.githubusercontent.com/opsxcq/proxy-list/master/list.txt"  # mixed
    ],
    "https": [
        "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-https.txt",
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=https",
        "https://api.proxyscrape.com/?request=displayproxies&proxytype=https",
        "https://www.proxy-list.download/api/v1/get?type=https",
        "https://naawy.com/api/public/proxylist/getList/?proxyType=https&format=txt",
        "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
        "https://proxyspace.pro/https.txt",
        "https://raw.githubusercontent.com/zloi-user/hideip.me/main/https.txt",
        "https://raw.githubusercontent.com/casals-ar/proxy-list/main/https",
        "https://internet.limited/https.txt",
        "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/https.txt",
        "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/https.txt",
        "http://pubproxy.com/api/proxy?format=txt&type=https&limit=5&https=true",
        "https://raw.githubusercontent.com/ObcbO/getproxy/master/https.txt",
        "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/https_proxies.txt",
        "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/https.txt",
        "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Https.txt",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/https.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/https.txt",
        "https://raw.githubusercontent.com/prxchk/proxy-list/main/https.txt",
        "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/https.txt",
        "https://raw.githubusercontent.com/zevtyardt/proxy-list/main/https.txt",
        "https://raw.githubusercontent.com/HyperBeats/proxy-list/main/https.txt",
        "https://raw.githubusercontent.com/opsxcq/proxy-list/master/list.txt"  # mixed
    ]
}

def _load_sources_from_config(config_path: str) -> dict:
    """Load proxy source URLs from a JSON config file.

    Expected format:
    {
      "http": ["url1", ...],
      "https": ["url2", ...]
    }
    Returns DEFAULT_SOURCES on any validation error.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # basic validation
        if not isinstance(data, dict):
            raise ValueError("config root must be an object")
        http_list = data.get("http")
        https_list = data.get("https")
        if not isinstance(http_list, list) or not isinstance(https_list, list):
            raise ValueError("config must contain 'http' and 'https' arrays")
        # ensure strings
        http_list = [str(u).strip() for u in http_list if str(u).strip()]
        https_list = [str(u).strip() for u in https_list if str(u).strip()]
        if not http_list and not https_list:
            raise ValueError("both http and https lists are empty")
        return {"http": http_list, "https": https_list}
    except Exception as e:
        safe_print(f"⚠️  Failed to load config.json ({config_path}): {e}. Using embedded defaults.")
        return DEFAULT_SOURCES

# --------------------- Globals/Locks -----------------
print_lock = threading.Lock()
file_lock  = threading.Lock()
results_lock = threading.Lock()
geo_lock   = threading.Lock()

good_proxies = {}   # proxy -> {"supports": set(["http","https"]), "country": str|None, "country_code": str|None, "isp": str|None, "is_mobile": bool|None, "is_proxy": bool|None, "is_hosting": bool|None}
all_results  = []   # for final JSON array
emitted_jsonl = set()
existing_txt = set()
discord_notifier = None  # set in main if webhook configured
ip_api_key = None  # set in main if provided

# Progress counters
processed_count = 0
good_count = 0

ip_port_re = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3}):(\d{2,5})\b")

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs, flush=True)

def update_progress():
    """Update single-line progress: GOOD: N | BAD: N."""
    # Snapshot counts under results_lock to avoid inconsistent reads
    with results_lock:
        good = good_count
        bad = processed_count - good_count
    with print_lock:
        print(f"\rGOOD: {good} | BAD: {bad}", end="", flush=True)

# --------------------- Parsing -----------------------
def valid_ip(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip); return True
    except Exception:
        return False

def parse_proxies_from_text(text: str):
    out = set()
    for m in ip_port_re.finditer(text):
        ip = m.group(1).split(":")[0]
        port = m.group(2)
        if valid_ip(ip) and 1 <= int(port) <= 65535:
            out.add(f"{ip}:{port}")
    return out

def fetch_source(url: str, timeout: float = 10.0) -> set:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200 and r.text:
            return parse_proxies_from_text(r.text)
    except Exception:
        pass
    return set()

def harvest_all_sources(selected: list[str]) -> set:
    proxies = set()
    with cf.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = [pool.submit(fetch_source, u) for u in selected]
        for f in cf.as_completed(futures):
            try:
                proxies |= f.result()
            except Exception:
                pass
    return proxies

# --------------------- De-dup ------------------------
def dedupe_proxies(proxies) -> list[str]:
    """Normalize ip:port and remove duplicates, preserving order."""
    seen = set()
    out = []
    for p in proxies:
        try:
            ip, port = p.strip().split(":")
            ip_norm = str(ipaddress.IPv4Address(ip))
            port_norm = str(int(port))
            key = f"{ip_norm}:{port_norm}"
        except Exception:
            continue
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out

# --------------------- Transfer Test -----------------
def _try_download(url: str, proxies_dict: dict, deadline: float, min_bytes: int) -> bool:
    """Attempt to transfer at least min_bytes from url before deadline."""
    remaining = max(0.05, deadline - time.time())
    if remaining <= 0:
        return False

    headers = dict(HEADERS)
    # If endpoint isn't the Cloudflare bytes endpoint, use Range
    if "__down?bytes=" not in url:
        headers["Range"] = f"bytes=0-{min_bytes-1}"

    # Use a small per-op timeout so we don't overrun the deadline on stalled reads
    connect_to = min(0.8, remaining)
    read_to = min(0.8, remaining)

    try:
        with requests.get(
            url,
            headers=headers,
            proxies=proxies_dict,
            timeout=(connect_to, read_to),
            stream=True,
            allow_redirects=True,
            verify=True,  # https certs through CONNECT
        ) as r:
            if r.status_code not in (200, 206):  # range=206 OK
                return False

            downloaded = 0
            for chunk in r.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded >= min_bytes:
                    return True
                if time.time() > deadline:
                    break
    except Exception:
        return False
    return False

def can_transfer_min_bytes(urls: list[str], proxies_dict: dict, timeout: float, min_bytes: int) -> bool:
    """Try multiple URLs; pass if any yields >=min_bytes within the global timeout."""
    deadline = time.time() + timeout
    # randomize to avoid hammering first host
    u = urls[:]
    shuffle(u)
    for url in u:
        if time.time() >= deadline:
            return False
        if _try_download(url, proxies_dict, deadline, min_bytes):
            return True
    return False

# --------------------- Geo ---------------------------
def geo_ip_via_proxy(proxy: str, timeout: float = 2.0) -> tuple[str | None, str | None, str | None, bool | None, bool | None, bool | None]:
    """
    Resolve the proxy's egress data via ip-api.com THROUGH the proxy.
    Returns: (country, country_code, isp, mobile, proxy, hosting)
    """
    proxies = {
        "http":  f"http://{proxy}",
        "https": f"http://{proxy}",
    }
    fields = "status,country,countryCode,isp,mobile,proxy,hosting,query"
    # Prefer Pro endpoint if API key available
    key = ip_api_key
    endpoints = []
    if key:
        endpoints.extend([
            f"https://pro.ip-api.com/json/?fields={fields}&key={key}",
            f"http://pro.ip-api.com/json/?fields={fields}&key={key}",
        ])
    # Fallback to free
    endpoints.extend([
        f"https://ip-api.com/json/?fields={fields}",
        f"http://ip-api.com/json/?fields={fields}",
    ])

    for url in endpoints:
        try:
            r = requests.get(url, proxies=proxies, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "success":
                    return (
                        data.get("country"),
                        data.get("countryCode"),
                        data.get("isp"),
                        data.get("mobile"),
                        data.get("proxy"),
                        data.get("hosting"),
                    )
        except Exception:
            pass
    return None, None, None, None, None, None

# --------------------- IO helpers --------------------
def append_txt(proxy: str):
    with file_lock:
        if proxy in existing_txt:
            return
        os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
        with open(OUT_TXT, "a", encoding="utf-8") as f:
            f.write(proxy + "\n")
        existing_txt.add(proxy)

def append_jsonl_once(obj: dict):
    key = obj["proxy"]
    with file_lock:
        if key in emitted_jsonl:
            return
        emitted_jsonl.add(key)
        os.makedirs(os.path.dirname(OUT_JSONL), exist_ok=True)
        with open(OUT_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        
def _load_existing_outputs_into_sets():
    """Seed in-memory de-dupe sets from existing output files to avoid duplicates across runs."""
    # Seed TXT
    try:
        if os.path.exists(OUT_TXT):
            with open(OUT_TXT, "r", encoding="utf-8") as f:
                for line in f:
                    p = line.strip()
                    if p:
                        existing_txt.add(p)
    except Exception:
        pass
    # Seed JSONL
    try:
        if os.path.exists(OUT_JSONL):
            with open(OUT_JSONL, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        key = obj.get("proxy")
                        if key:
                            emitted_jsonl.add(key)
                    except Exception:
                        continue
    except Exception:
        pass

def _read_previous_proxies() -> set[str]:
    """Read previously found proxies from proxies/ outputs and legacy root files."""
    prev = set()
    candidates = [
        OUT_TXT,
        OUT_JSONL,
        OUT_JSON,
        "alive_proxies.txt",   # legacy root path
        "alive_proxies.jsonl",
        "alive_proxies.json",
    ]
    for path in candidates:
        try:
            if not os.path.exists(path):
                continue
            if path.endswith(".txt"):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        p = line.strip()
                        if p:
                            prev.add(p)
            elif path.endswith(".jsonl"):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                            p = obj.get("proxy")
                            if p:
                                prev.add(p)
                        except Exception:
                            continue
            elif path.endswith(".json"):
                with open(path, "r", encoding="utf-8") as f:
                    try:
                        arr = json.load(f)
                        if isinstance(arr, list):
                            for obj in arr:
                                if isinstance(obj, dict):
                                    p = obj.get("proxy")
                                    if p:
                                        prev.add(p)
                    except Exception:
                        continue
        except Exception:
            continue
    return prev

def _write_consolidated_json_from_jsonl():
    """Build a consolidated JSON array from the JSONL file (unique by proxy)."""
    items = {}
    try:
        if os.path.exists(OUT_JSONL):
            with open(OUT_JSONL, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                        key = obj.get("proxy")
                        if key:
                            items[key] = obj
                    except Exception:
                        continue
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(list(items.values()), f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _git_commit_and_push(files: list[str], message: str, allow_push: bool = True):
    """Best-effort git add/commit/push. Requires repo configured with write auth."""
    try:
        # Filter existing files
        files = [f for f in files if os.path.exists(f)]
        if not files:
            return
        # Add files
        subprocess.run(["git", "add", "--" ] + files, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Commit (may noop if no changes)
        commit = subprocess.run(["git", "commit", "-m", message], check=False, capture_output=True, text=True)
        if commit.returncode != 0 and "nothing to commit" in (commit.stderr or "") + (commit.stdout or ""):
            return
        # Pull --rebase then push
        if allow_push:
            subprocess.run(["git", "pull", "--rebase"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "push"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        # best-effort; ignore errors
        pass

# --------------------- Discord Notifier --------------------
class DiscordNotifier:
    def __init__(self, webhook_url: str, username: str | None = None):
        self.webhook_url = webhook_url.strip()
        self.username = username or "Proxy Checker"
        self.q: queue.Queue = queue.Queue(maxsize=2048)
        self.alive = True
        self.worker = threading.Thread(target=self._run, name="discord-notifier", daemon=True)
        self.worker.start()

    def _post(self, payload: dict):
        try:
            r = requests.post(self.webhook_url, json=payload, timeout=10)
            if r.status_code == 429:
                # respect rate limit
                try:
                    retry = float(r.json().get("retry_after", 1.5))
                except Exception:
                    retry = 1.5
                time.sleep(min(10.0, max(0.5, retry)))
            elif r.status_code >= 400:
                # backoff a bit on errors to avoid hot loop
                time.sleep(0.5)
        except Exception:
            # swallow and continue; best-effort notifier
            time.sleep(0.5)

    def _run(self):
        while self.alive or not self.q.empty():
            try:
                item = self.q.get(timeout=0.25)
            except Exception:
                continue
            if item is None:
                break
            self._post(item)
            self.q.task_done()

    @staticmethod
    def _flag_emoji(country_code: str | None) -> str:
        if not country_code or len(country_code) != 2:
            return "🏳️"
        cc = country_code.upper()
        base = 127397
        try:
            return chr(ord(cc[0]) + base) + chr(ord(cc[1]) + base)
        except Exception:
            return "🏳️"

    @staticmethod
    def _yn_bool(v: bool | None) -> str:
        if v is True:
            return "Yes"
        if v is False:
            return "No"
        return "Unknown"

    def send_hit(self, obj: dict):
        proxy = obj.get("proxy")
        country = obj.get("country")
        cc = obj.get("country_code")
        isp = obj.get("isp")
        mobile = obj.get("is_mobile")
        proxy_flag = obj.get("is_proxy")
        hosting = obj.get("is_hosting")
        supports = obj.get("supports") or []

        flag = self._flag_emoji(cc)
        title = f"✅ Alive Proxy {flag}"
        desc = f"`{proxy}`"
        color = 0x57F287  # Discord green
        fields = []
        fields.append({"name": "Country", "value": f"{flag} {country or 'Unknown'} ({cc or '??'})", "inline": True})
        fields.append({"name": "ISP", "value": isp or "Unknown", "inline": True})
        fields.append({"name": "Supports", "value": ", ".join(supports) or "—", "inline": True})
        fields.append({"name": "Mobile", "value": f"📱 {self._yn_bool(mobile)}", "inline": True})
        fields.append({"name": "Proxy", "value": f"🛡️ {self._yn_bool(proxy_flag)}", "inline": True})
        fields.append({"name": "Hosting", "value": f"🏢 {self._yn_bool(hosting)}", "inline": True})

        embed = {
            "title": title,
            "description": desc,
            "color": color,
            "fields": fields,
            "footer": {"text": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())},
        }
        payload = {
            "username": self.username,
            "allowed_mentions": {"parse": []},
            "embeds": [embed],
        }
        try:
            self.q.put_nowait(payload)
        except queue.Full:
            # drop if overloaded
            pass

    def send_summary(self, total: int, good: int, duration_sec: float, top_countries: list[tuple[str, int]]):
        color = 0x5865F2  # blurple
        title = "🎉 Proxy Scan Completed"
        fields = [
            {"name": "Alive", "value": f"✅ {good}", "inline": True},
            {"name": "Total Processed", "value": f"{total}", "inline": True},
            {"name": "Duration", "value": f"{duration_sec:.1f}s", "inline": True},
        ]
        if top_countries:
            lines = []
            for cc, cnt in top_countries[:10]:
                lines.append(f"{self._flag_emoji(cc)} {cc or '??'} — {cnt}")
            fields.append({"name": "Top Countries", "value": "\n".join(lines), "inline": False})
        embed = {
            "title": title,
            "color": color,
            "fields": fields,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        }
        payload = {
            "username": self.username,
            "allowed_mentions": {"parse": []},
            "embeds": [embed],
        }
        try:
            self.q.put_nowait(payload)
        except queue.Full:
            pass

    def close(self):
        self.alive = False
        try:
            self.q.put_nowait(None)
        except Exception:
            pass
        try:
            self.worker.join(timeout=5)
        except Exception:
            pass

# --------------------- Checker -----------------------
def check_single(proxy: str, timeout: float, do_geo: bool, geo_pool: cf.ThreadPoolExecutor | None):
    global processed_count, good_count
    proxies_dict = {
        "http":  f"http://{proxy}",
        "https": f"http://{proxy}",  # HTTP CONNECT for both
    }

    http_ok = can_transfer_min_bytes(TEST_URLS_HTTP, proxies_dict, timeout, MIN_BYTES)
    https_ok = can_transfer_min_bytes(TEST_URLS_HTTPS, proxies_dict, timeout, MIN_BYTES)

    # Only consider GOOD if both protocols pass
    if not (http_ok and https_ok):
        with results_lock:
            processed_count += 1
        update_progress()
        return

    with results_lock:
        entry = good_proxies.setdefault(proxy, {
            "supports": set(),
            "country": None,
            "country_code": None,
            "isp": None,
            "is_mobile": None,
            "is_proxy": None,
            "is_hosting": None,
        })
        entry["supports"].add("http")
        entry["supports"].add("https")
        processed_count += 1
        good_count += 1

    # No per-proxy OK print; only update the single progress line
    update_progress()
    append_txt(proxy)

    def do_geo_task():
        country, cc, isp, mobile, proxy_flag, hosting = geo_ip_via_proxy(proxy)
        with results_lock:
            entry = good_proxies[proxy]
            entry["country"] = country
            entry["country_code"] = cc
            entry["isp"] = isp
            entry["is_mobile"] = mobile
            entry["is_proxy"] = proxy_flag
            entry["is_hosting"] = hosting
            obj = {
                "proxy": proxy,  # keep ip:port here
                "country": country,
                "country_code": cc,
                "isp": isp,
                "is_mobile": mobile,
                "is_proxy": proxy_flag,
                "is_hosting": hosting,
                "supports": sorted(list(entry["supports"])),
            }
            all_results.append(obj)
        append_jsonl_once(obj)
        # Send Discord notification for this hit
        if discord_notifier:
            try:
                discord_notifier.send_hit(obj)
            except Exception:
                pass
        # No geo print

    if do_geo:
        geo_pool.submit(do_geo_task)
    else:
        with results_lock:
            obj = {
                "proxy": proxy,
                "country": None,
                "country_code": None,
                "isp": None,
                "is_mobile": None,
                "is_proxy": None,
                "is_hosting": None,
                "supports": sorted(list(good_proxies[proxy]["supports"])),
            }
            all_results.append(obj)
        append_jsonl_once(obj)
        if discord_notifier:
            try:
                discord_notifier.send_hit(obj)
            except Exception:
                pass

# --------------------- Auto worker sizing ---------------------
def _mem_available_bytes() -> int | None:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return int(parts[1]) * 1024  # kB -> bytes
    except Exception:
        return None
    return None

def _compute_auto_worker_cap(requested: int) -> tuple[int, dict]:
    # Caps from system limits
    try:
        nofile_soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        nofile_soft = 1024
    fd_margin = 512
    fds_per_worker = 3
    if nofile_soft in (None, resource.RLIM_INFINITY):
        allowed_by_fd = 4096
    else:
        allowed_by_fd = max(16, (max(0, nofile_soft - fd_margin)) // fds_per_worker)

    try:
        nproc_soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    except Exception:
        nproc_soft = resource.RLIM_INFINITY
    if nproc_soft in (None, resource.RLIM_INFINITY):
        allowed_by_nproc = 10_000
    else:
        allowed_by_nproc = max(16, int(nproc_soft - 50))  # leave headroom

    # Don't limit by CPU for IO-bound proxy checks; set a high ceiling
    allowed_by_cpu = 10000

    mem_avail = _mem_available_bytes()
    per_thread_stack = 512 * 1024  # 512 KiB (we set this below)
    if mem_avail and mem_avail > 0:
        allowed_by_mem = max(16, int(mem_avail // int(per_thread_stack * 1.2)))
    else:
        allowed_by_mem = 4096

    hard_cap = 3000
    cap = int(max(16, min(allowed_by_fd, allowed_by_nproc, allowed_by_cpu, allowed_by_mem, hard_cap)))

    # Always clamp to system-derived cap to avoid OS limits
    chosen = max(16, min(requested, cap))

    caps = {
        "fd": int(allowed_by_fd),
        "nproc": int(allowed_by_nproc),
        "cpu": int(allowed_by_cpu),
        "mem": int(allowed_by_mem),
        "cap": int(cap),
        "chosen": int(chosen),
    }
    return chosen, caps

# --------------------- Main --------------------------
def main():
    parser = argparse.ArgumentParser(description="Threaded HTTP/HTTPS proxy checker with ≥200KB real-transfer test & 2s budget.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Hard per-proxy wall-clock budget (seconds). Default: 2.0")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Concurrent proxy checks. Default: 200")
    parser.add_argument("--no-geo", action="store_true", help="Disable GeoIP lookups (faster, avoids rate limits).")
    parser.add_argument("--only", choices=["http", "https"], help="Harvest only HTTP or only HTTPS lists.")
    parser.add_argument("--min-bytes", type=int, default=MIN_BYTES, help="Minimum bytes that must transfer (default: 204800).")
    parser.add_argument("--config", type=str, default=os.environ.get("PROXY_SOURCES_CONFIG", "config.json"), help="Path to config JSON with 'http' and 'https' arrays. Defaults to ./config.json; falls back to embedded defaults if missing/invalid.")
    parser.add_argument("--ip-api-key", type=str, default=os.environ.get("IP_API_KEY"), help="ip-api.com Pro API key (uses pro.ip-api.com when provided). Or set env IP_API_KEY.")
    parser.add_argument("--webhook-url", type=str, default=os.environ.get("DISCORD_WEBHOOK_URL"), help="Discord webhook URL to post hits. Can also be set via DISCORD_WEBHOOK_URL env var.")
    parser.add_argument("--webhook-username", type=str, default=os.environ.get("DISCORD_WEBHOOK_USERNAME", "Proxy Checker"), help="Webhook username override (optional).")
    parser.add_argument("--webhook-summary", action="store_true", help="Also send a final summary embed when done.")
    parser.add_argument("--no-git-push", action="store_true", help="Do not auto-commit and push results to the git repo.")
    args = parser.parse_args()

    # Use smaller thread stacks so we can run more threads safely
    try:
        threading.stack_size(512 * 1024)  # 512 KiB
    except Exception:
        pass

    # Ensure output directory exists and seed de-dupe from existing outputs (don't delete; we append across runs)
    try:
        os.makedirs(PROXIES_DIR, exist_ok=True)
    except Exception:
        pass
    _load_existing_outputs_into_sets()

    # Load sources from config (or use embedded defaults)
    cfg_path = args.config or "config.json"
    if cfg_path and os.path.exists(cfg_path):
        sources = _load_sources_from_config(cfg_path)
    else:
        sources = DEFAULT_SOURCES

    # Select sources
    selected_sources = sources[args.only] if args.only else sorted(set(sources["http"] + sources["https"]))

    safe_print(f"🌐 Harvesting proxy lists from {len(selected_sources)} sources…")
    t0 = time.time()
    harvested = harvest_all_sources(selected_sources)
    safe_print(f"✅ Harvested {len(harvested):,} unique proxies in {time.time()-t0:.2f}s")

    # Include previously found proxies from prior scans
    prev = _read_previous_proxies()
    if prev:
        safe_print(f"♻️  Including {len(prev):,} proxies from previous scans")
    combined_list = list(harvested) + [p for p in prev if p not in harvested]

    deduped = dedupe_proxies(combined_list)
    if len(deduped) != len(combined_list):
        safe_print(f"🧹 De-duplicated to {len(deduped):,} unique proxies")
    else:
        safe_print("🧹 De-duplicated: no change")

    # Auto-size worker pools
    workers, caps = _compute_auto_worker_cap(args.workers)
    if not args.no_geo:
        geo_workers = max(4, min(GEO_WORKERS, workers // 10))
    else:
        geo_workers = 0
    safe_print(f"⚙ Using {workers} threads (caps: fd={caps['fd']}, nproc={caps['nproc']}, cpu={caps['cpu']}, mem={caps['mem']})")

    geo_pool = cf.ThreadPoolExecutor(max_workers=geo_workers) if not args.no_geo else None

    safe_print(f"🧪 Checking (≥{args.min_bytes//1024}KB within {args.timeout:.1f}s) using {workers} threads… (live results below)")

    # Init Discord notifier if configured
    global discord_notifier
    global ip_api_key
    ip_api_key = args.ip_api_key
    if args.webhook_url:
        discord_notifier = DiscordNotifier(args.webhook_url, username=args.webhook_username)
        safe_print("🔔 Discord webhook configured: hits will be posted")

    start_scan = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(check_single, proxy, args.timeout, not args.no_geo, geo_pool) for proxy in deduped]
        for _ in cf.as_completed(futures):
            pass

    if geo_pool:
        geo_pool.shutdown(wait=True)

    # Finish the progress line before summary
    with print_lock:
        print()

    # Append-only JSONL/TXT already handled during scan; now create consolidated JSON from JSONL
    _write_consolidated_json_from_jsonl()

    ok_count = len({o["proxy"] for o in all_results})
    duration = time.time() - start_scan
    safe_print("—" * 60)
    safe_print(f"🎉 Done. Alive proxies: {ok_count:,}")
    safe_print(f"📝 {OUT_TXT}  (ip:port)")
    safe_print(f"🧾 {OUT_JSONL} (streamed JSONL)")
    safe_print(f"📦 {OUT_JSON}  (consolidated JSON array)")

    # Optional summary to Discord
    if discord_notifier and args.webhook_summary:
        try:
            # Count total processed safely
            with results_lock:
                total = processed_count
            # Top countries by count
            counts = defaultdict(int)
            for o in all_results:
                cc = o.get("country_code") or "??"
                counts[cc] += 1
            top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
            discord_notifier.send_summary(total=total, good=ok_count, duration_sec=duration, top_countries=top)
        except Exception:
            pass

    if discord_notifier:
        discord_notifier.close()

    # Auto-commit and push changes by default unless disabled
    if not args.no_git_push and os.path.isdir(os.path.join(".git")):
        ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        msg = f"Update alive proxies ({ok_count}) — {ts}"
        _git_commit_and_push([OUT_TXT, OUT_JSONL, OUT_JSON], msg, allow_push=True)

if __name__ == "__main__":
    main()
