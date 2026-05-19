import asyncio
import os
import re
import subprocess
import time
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

import requests as req_lib
import yaml
from playwright.async_api import async_playwright

# ==================== CONFIG ====================
def _load_config(path: str = "config.yml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

_CONFIG = _load_config()
MODELS = _CONFIG["models"]

_MOUFLON_MSN_RE = re.compile(r'/\d+_(\d+)_[A-Za-z0-9]+/')
_MOUFLON_PART_RE = re.compile(r'_part(\d+)\.mp4')

def _mouflon_msn_part(url: str) -> tuple[int | None, int | None]:
    msn_m = _MOUFLON_MSN_RE.search(url)
    part_m = _MOUFLON_PART_RE.search(url)
    if msn_m and part_m:
        return int(msn_m.group(1)), int(part_m.group(1))
    return None, None
TRANSCODE_CFG: dict = {
    "enabled":       os.environ.get("TRANSCODE_ENABLED", "").lower() in ("1", "true", "yes"),
    "codec":         os.environ.get("TRANSCODE_CODEC", "h264"),
    "crf":           int(os.environ.get("TRANSCODE_CRF", "23")),
    "preset":        os.environ.get("TRANSCODE_PRESET", "fast"),
    "threads":       int(os.environ.get("TRANSCODE_THREADS", "0")),
    "audio_bitrate": os.environ.get("TRANSCODE_AUDIO_BITRATE", "128k"),
}
RECORDINGS_DIR = Path("/recordings")
RECORDINGS_DIR.mkdir(exist_ok=True)

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
# ===============================================

active_recordings = {}
resume_after: dict[str, datetime] = {}
shutdown = threading.Event()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _next_poll_start(model: dict) -> datetime:
    start_t = datetime.strptime(model["poll_start_time"], "%H:%M:%S").time()
    today_start = datetime.combine(date.today(), start_t)
    if datetime.now() < today_start:
        return today_start
    return datetime.combine(date.today() + timedelta(days=1), start_t)


def _parse_duration(val) -> int:
    if isinstance(val, int):
        return val
    total = 0
    for amount, unit in re.findall(r'(\d+)\s*(h|m|s)', str(val).lower()):
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def _parse_size(val) -> int:
    if isinstance(val, int):
        return val
    units = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb)', str(val).strip().lower())
    if m:
        return int(float(m.group(1)) * units[m.group(2)])
    return int(val)


def _normalize_stream_url(url: str) -> str:
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=True)
    for key in ("_HLS_msn", "_HLS_part"):
        qs.pop(key, None)
    return urlunparse(p._replace(query=urlencode(qs, doseq=True)))


def _in_poll_window(model: dict) -> bool:
    now = datetime.now().time()
    start = datetime.strptime(model["poll_start_time"], "%H:%M:%S").time()
    stop  = datetime.strptime(model["poll_stop_time"],  "%H:%M:%S").time()
    return start <= now <= stop


def log(username: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}][{username}] {msg}", flush=True)


def _transcode_file(username: str, path: Path, cfg: dict):
    codec = cfg.get("codec", "h264").lower()
    crf = int(cfg.get("crf", 23))
    audio_br = cfg.get("audio_bitrate", "128k")
    vcodec = "libx265" if codec == "h265" else "libx264"
    preset = cfg.get("preset", "fast")
    threads = str(cfg.get("threads", 0))

    tmp = path.with_suffix(".transcoding.mp4")
    tc_path = path.with_name(path.stem + "_tc" + path.suffix)
    extra_v = ["-tag:v", "hvc1"] if codec == "h265" else []
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-c:v", vcodec, "-crf", str(crf), "-preset", preset, "-threads", threads,
        "-pix_fmt", "yuv420p",
        *extra_v,
        "-c:a", "aac", "-b:a", audio_br,
        "-movflags", "+faststart",
        "-y", str(tmp),
    ]

    size_mb = path.stat().st_size / 1024 / 1024
    timeout_s = max(300, int(size_mb * 10))  # ~10s per MB, min 5min
    log(username, f"transcoding {path.name} ({vcodec} preset={preset} crf={crf} {size_mb:.0f}MB, timeout={timeout_s}s)")
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        elapsed = time.time() - t0
        if result.returncode != 0:
            log(username, f"ffmpeg error after {elapsed:.0f}s: {result.stderr[-300:]}")
            tmp.unlink(missing_ok=True)
            return
        orig_size = path.stat().st_size
        new_size = tmp.stat().st_size
        pct = 100 * (1 - new_size / orig_size) if orig_size else 0
        tmp.rename(tc_path)
        path.unlink()
        log(username, f"transcode done in {elapsed:.0f}s: {path.name} -> {tc_path.name} ({orig_size//1024//1024}MB -> {new_size//1024//1024}MB, {pct:.0f}% smaller)")
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        log(username, f"transcode timed out after {elapsed:.0f}s — leaving original")
        tmp.unlink(missing_ok=True)
    except Exception as e:
        log(username, f"transcode failed: {e}")
        tmp.unlink(missing_ok=True)


def is_live(username: str) -> bool:
    try:
        r = req_lib.get(
            f"https://stripchat.com/api/front/v2/models/username/{username}/cam",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        cam = r.json().get("cam", {})
        active = cam.get("isCamActive", False)
        log(username, f"isCamActive={active}")
        return active
    except Exception as e:
        log(username, f"API error: {e}")
    return False


def _record_with_requests(
    username: str, stream_url: str, cookies_list: list, output_path: Path,
    time_limit: int | None = None, size_limit: int | None = None,
) -> str:
    sess = req_lib.Session()
    sess.headers.update({
        "User-Agent": _UA,
        "Referer": f"https://stripchat.com/{username}",
    })
    if cookies_list:
        sess.headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies_list)

    # LLHLS blocking mode: _HLS_msn/_HLS_part captured from browser at live edge.
    # MOUFLON part URLs expire in ~1-2s, so non-blocking polling always gets 404s.
    # Blocking requests make the server hold until the requested part is fresh.
    msn_m = re.search(r'[?&]_HLS_msn=(\d+)', stream_url)
    part_m = re.search(r'[?&]_HLS_part=(\d+)', stream_url)
    llhls = bool(msn_m and part_m)

    if llhls:
        next_msn = int(msn_m.group(1))
        next_part = int(part_m.group(1))
        base_url = _normalize_stream_url(stream_url)
        log(username, f"LLHLS blocking mode, starting msn={next_msn} part={next_part}")
    else:
        base_url = stream_url

    base = base_url.split("?")[0].rsplit("/", 1)[0] + "/"
    seen: set[str] = set()
    init_seen: set[str] = set()
    errors = 0

    def resolve(uri: str) -> str:
        return uri if uri.startswith("http") else base + uri

    def fetch_and_write(url: str, label: str, f) -> bool:
        try:
            r = sess.get(url, timeout=30)
            if r.status_code != 200:
                log(username, f"{label} HTTP {r.status_code}")
                return False
            f.write(r.content)
            f.flush()
            return True
        except Exception as e:
            log(username, f"{label} error: {e}")
            return False

    start_time = time.time()
    stop_reason = "shutdown"
    seg_count = 0
    empty_polls = 0

    log(username, f"recording -> {output_path.name}")
    with open(output_path, "wb") as f:
        while not shutdown.is_set():
            if llhls:
                poll_url = f"{base_url}&_HLS_msn={next_msn}&_HLS_part={next_part}"
                poll_timeout = 35
            else:
                poll_url = base_url
                poll_timeout = 15

            try:
                resp = sess.get(poll_url, timeout=poll_timeout)
                if llhls and resp.status_code in (400, 410):
                    log(username, f"LLHLS msn={next_msn} part={next_part} out of range ({resp.status_code}), advancing segment")
                    next_msn += 1
                    next_part = 0
                    continue
                resp.raise_for_status()
                errors = 0
            except Exception as e:
                errors += 1
                if errors >= 5:
                    log(username, f"playlist error x5, stopping: {e}")
                    stop_reason = "error"
                    break
                time.sleep(2)
                continue

            if "#EXTM3U" not in resp.text:
                errors += 1
                log(username, f"playlist not M3U8 (HTTP {resp.status_code}, {len(resp.content)}B): {resp.text[:120]!r}")
                if errors >= 5:
                    log(username, "playlist invalid x5, stopping")
                    stop_reason = "error"
                    break
                time.sleep(2)
                continue

            lines = resp.text.splitlines()
            new_segs = 0
            pending_mouflon_uri: str | None = None
            max_seen_msn: int | None = None
            max_seen_part: int | None = None

            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-MAP"):
                    m = re.search(r'URI="([^"]+)"', line)
                    if m:
                        init_url = resolve(m.group(1))
                        if init_url not in init_seen:
                            if fetch_and_write(init_url, "init", f):
                                init_seen.add(init_url)
                                log(username, "wrote init segment")
                    pending_mouflon_uri = None
                    continue

                if line.startswith("#EXT-X-MOUFLON:URI:"):
                    pending_mouflon_uri = line[len("#EXT-X-MOUFLON:URI:"):].strip()
                    continue

                if line.startswith("#EXT-X-PART:"):
                    seg_url = pending_mouflon_uri
                    pending_mouflon_uri = None
                    if not seg_url:
                        m = re.search(r'URI="([^"]+)"', line)
                        seg_url = resolve(m.group(1)) if m else None
                    if seg_url:
                        url_msn, url_part = _mouflon_msn_part(seg_url) if llhls else (None, None)
                        if llhls and url_msn is not None:
                            if max_seen_msn is None or (url_msn, url_part) > (max_seen_msn, max_seen_part):
                                max_seen_msn, max_seen_part = url_msn, url_part
                            if (url_msn, url_part) < (next_msn, next_part):
                                seen.add(seg_url)
                                continue
                        if seg_url not in seen:
                            seen.add(seg_url)
                            ok = fetch_and_write(seg_url, "part", f)
                            if ok:
                                new_segs += 1
                                seg_count += 1
                    continue

                if not line.startswith("#EXTINF") or i + 1 >= len(lines):
                    pending_mouflon_uri = None
                    continue
                uri = lines[i + 1].strip()
                if not uri or uri.startswith("#"):
                    continue
                seg_url = resolve(uri)
                if seg_url not in seen:
                    seen.add(seg_url)
                    ok = fetch_and_write(seg_url, "seg", f)
                    if ok:
                        new_segs += 1
                        seg_count += 1

            if llhls and max_seen_msn is not None:
                next_msn = max_seen_msn
                next_part = max_seen_part + 1

            if new_segs == 0:
                empty_polls += 1
                if empty_polls % 10 == 0:
                    log(username, f"no new segments for {empty_polls} polls (total segs: {seg_count}, file: {output_path.stat().st_size}B)")
            else:
                empty_polls = 0
                if seg_count % 20 == 0:
                    log(username, f"segs: {seg_count}, file: {output_path.stat().st_size}B")

            if "#EXT-X-ENDLIST" in resp.text:
                log(username, "stream ended")
                stop_reason = "stream_ended"
                break
            if time_limit and (time.time() - start_time) >= time_limit:
                log(username, f"time limit reached ({time_limit}s)")
                stop_reason = "time_limit"
                break
            if size_limit and output_path.stat().st_size >= size_limit:
                log(username, f"size limit reached ({size_limit} bytes)")
                stop_reason = "size_limit"
                break
            if not llhls:
                time.sleep(1)

    log(username, f"done: {output_path.name}")
    return stop_reason


async def _record_async(model: dict):
    username = model["name"]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--no-zygote",
                "--disable-gpu",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=_UA,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
            Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
        """)
        page = await context.new_page()

        # Reduce memory: block images and fonts
        await page.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
            lambda route: route.abort(),
        )

        master_url: list[str] = []

        async def on_response(response):
            url = response.url
            if "m3u8" not in url or master_url:
                return
            try:
                body = await response.text()
            except Exception:
                return
            if "#EXT-X-STREAM-INF" in body:
                log(username, f"master: ...{url[-80:]}")
                master_url.append(url)

        page.on("response", on_response)

        try:
            await page.goto(
                f"https://stripchat.com/{username}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as e:
            log(username, f"page.goto: {e}")

        await page.bring_to_front()
        try:
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        except Exception:
            pass

        # Poll performance API every 5s for up to 40s.
        # performance.getEntriesByType('resource') captures all XHR/fetch URLs
        # including MOUFLON's internal requests invisible to page.on("response").
        stream_url = None
        seen_perf: set[str] = set()
        perf_master_urls: list[str] = []

        for attempt in range(8):
            await asyncio.sleep(5)

            try:
                await page.evaluate(
                    "document.querySelectorAll('video').forEach("
                    "  v => { v.muted = true; v.play().catch(() => {}); }"
                    ")"
                )
            except Exception:
                pass

            try:
                perf_urls: list[str] = await page.evaluate(
                    "performance.getEntriesByType('resource').map(e => e.name)"
                )
                for url in perf_urls:
                    if url not in seen_perf:
                        seen_perf.add(url)
                        if "m3u8" in url:
                            log(username, f"perf[{attempt}]: ...{url[-90:]}")

                # Prefer pkey-authenticated URLs (real stream, not ping/health checks)
                pkey_urls = [u for u in perf_urls
                             if "m3u8" in u and "pkey=" in u
                             and "ping" not in u and "master" not in u.lower()]
                other_variants = [u for u in perf_urls
                                  if "m3u8" in u and "ping" not in u
                                  and "master" not in u.lower()]
                candidates = pkey_urls or other_variants
                if candidates:
                    stream_url = candidates[-1]
                    log(username, f"stream URL via perf: ...{stream_url[-90:]}")
                    break

                for u in perf_urls:
                    if "m3u8" in u and "master" in u.lower() and "ping" not in u and u not in perf_master_urls:
                        perf_master_urls.append(u)
            except Exception as e:
                log(username, f"perf query error: {e}")

        # If on_response never fired but perf saw master URLs, use them for the fallback
        if not stream_url and not master_url and perf_master_urls:
            log(username, f"on_response missed master — using perf master: ...{perf_master_urls[-1][-80:]}")
            master_url.extend(perf_master_urls)

        # Fallback: derive variant from master using context.request
        # (uses Chrome's network stack + cookies, bypasses MOUFLON JS interception)
        if not stream_url and master_url:
            log(username, "perf found no variant — trying context.request fallback")
            try:
                mr = await context.request.get(master_url[0], timeout=10_000)
                mbody = await mr.text()
                lines = mbody.splitlines()
                best_bw, best_var = -1, None
                mbase = master_url[0].split("?")[0].rsplit("/", 1)[0] + "/"
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                        bw = 0
                        for part in line.split(","):
                            if part.startswith("BANDWIDTH="):
                                try:
                                    bw = int(part.split("=")[1])
                                except ValueError:
                                    pass
                        nxt = lines[i + 1].strip()
                        if nxt and not nxt.startswith("#") and bw > best_bw:
                            best_bw = bw
                            best_var = nxt if nxt.startswith("http") else urljoin(mbase, nxt)
                if best_var:
                    log(username, f"fetching variant: ...{best_var[-80:]}")
                    vr = await context.request.get(best_var, timeout=10_000)
                    vbody = await vr.text()
                    if "#EXTINF" in vbody and "#EXT-X-ENDLIST" not in vbody:
                        stream_url = best_var
                        log(username, "variant is live")
                    else:
                        log(username, f"variant not live (endlist={'#EXT-X-ENDLIST' in vbody})")
            except Exception as e:
                log(username, f"context.request fallback error: {e}")

        if not stream_url:
            log(username, "no stream URL found — will retry on next poll")
            await browser.close()
            active_recordings.pop(username, None)
            return

        cookies = []
        try:
            cookies = await context.cookies()
        except Exception:
            pass

        await browser.close()

    time_limit = _parse_duration(model["recording_time_limit"]) if model.get("recording_time_limit") else None
    size_limit = _parse_size(model["recording_file_size_limit"]) if model.get("recording_file_size_limit") else None

    on_limit = model.get("on_limit_reached", "stop_for_day")
    rollover_max = model.get("rollover_max_files")
    cooldown_mins = model.get("cooldown_minutes") or 0

    effective_time_limit = None if on_limit == "ignore" else time_limit
    effective_size_limit = None if on_limit == "ignore" else size_limit

    loop = asyncio.get_event_loop()
    file_count = 0

    while True:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        output = RECORDINGS_DIR / f"{username}_{timestamp}.mp4"

        stop_reason = await loop.run_in_executor(
            None, _record_with_requests, username, stream_url, cookies, output,
            effective_time_limit, effective_size_limit,
        )

        if TRANSCODE_CFG.get("enabled"):
            await loop.run_in_executor(None, _transcode_file, username, output, TRANSCODE_CFG)

        file_count += 1

        if stop_reason not in ("time_limit", "size_limit"):
            break

        if on_limit == "rollover":
            if rollover_max and file_count >= rollover_max:
                log(username, f"rollover max ({rollover_max} files) reached, stopped for day")
                resume_after[username] = _next_poll_start(model)
                break
            log(username, f"limit hit ({stop_reason}) — rolling over (file {file_count + 1})")
            continue
        elif on_limit == "pause":
            resume_dt = datetime.now() + timedelta(minutes=cooldown_mins)
            resume_after[username] = resume_dt
            log(username, f"limit hit ({stop_reason}), pausing {cooldown_mins}m — resume after {resume_dt.strftime('%H:%M:%S')}")
            break
        elif on_limit == "stop_for_day":
            nps = _next_poll_start(model)
            resume_after[username] = nps
            log(username, f"limit hit ({stop_reason}), stopped for day — resume at {nps.strftime('%H:%M:%S')}")
            break
        else:
            break

    active_recordings.pop(username, None)


def record(model: dict):
    username = model["name"]
    log(username, "browser launching to find stream URL...")
    asyncio.run(_record_async(model))


def monitor():
    names = [m["name"] for m in MODELS if m.get("enabled", True)]
    print(f"[{time.strftime('%H:%M:%S')}] Monitoring: {names}", flush=True)
    while True:
        for model in MODELS:
            if not model.get("enabled", True):
                continue
            username = model["name"]
            if not _in_poll_window(model):
                log(username, "outside poll window, skipping")
                continue
            if username in resume_after:
                if datetime.now() < resume_after[username]:
                    continue
                del resume_after[username]
            t = active_recordings.get(username)
            if t and t.is_alive():
                continue
            if is_live(username):
                t = threading.Thread(target=record, args=(model,), daemon=True)
                t.start()
                active_recordings[username] = t
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        monitor()
    except KeyboardInterrupt:
        print("Shutting down...")
        shutdown.set()
