import asyncio
import os
import re
import shutil
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
LLHLS_QUEUE_TIMEOUT = int(os.environ.get("LLHLS_QUEUE_TIMEOUT", "30"))
LLHLS_STALL_RETRIES = int(os.environ.get("LLHLS_STALL_RETRIES", "12"))
# ===============================================

from state import active_recordings, resume_after, resume_reason, idle_reason, shutdown, config_lock, daily_file_counts
_browser_sem = threading.Semaphore(int(os.environ.get("MAX_CONCURRENT", "3")))

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _parse_time(val):
    """Parse HH:MM or HH:MM:SS, return time or None if blank/null."""
    if not val:
        return None
    s = str(val).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    return None


def _next_poll_start(model: dict) -> datetime:
    start_t = _parse_time(model.get("poll_start_time"))
    if start_t is None:
        return datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
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
    start = _parse_time(model.get("poll_start_time"))
    stop  = _parse_time(model.get("poll_stop_time"))
    if start is None or stop is None:
        return True  # no restriction
    now = datetime.now().time()
    if stop < start:  # overnight window e.g. 22:00 -> 04:00
        return now >= start or now <= stop
    return start <= now <= stop


def log(username: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}][{username}] {msg}", flush=True)


def _transcode_file(username: str, path: Path, cfg: dict):
    if not path.exists():
        log(username, f"transcode: source missing (file may have been moved or deleted): {path}")
        return
    codec = cfg.get("codec", "h264").lower()
    crf = int(cfg.get("crf", 23))
    audio_br = cfg.get("audio_bitrate", "128k")
    no_audio = cfg.get("no_audio", False)
    vcodec = "libx265" if codec == "h265" else "libx264"
    preset = cfg.get("preset", "fast")
    threads = str(cfg.get("threads", 0))

    tmp = Path("/tmp") / (path.stem + ".transcoding.mp4")
    tc_path = path.with_name(path.stem + "_tc" + path.suffix)
    audio_flags = ["-an"] if no_audio else ["-c:a", "aac", "-b:a", audio_br]
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-c:v", vcodec, "-crf", str(crf), "-preset", preset, "-threads", threads,
        "-pix_fmt", "yuv420p",
        *audio_flags,
        "-movflags", "+faststart",
        "-y", str(tmp),
    ]
    retry_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", str(path),
        "-c:v", vcodec, "-crf", str(crf), "-preset", preset, "-threads", threads,
        "-pix_fmt", "yuv420p",
        *audio_flags,
        "-movflags", "+faststart",
        "-y", str(tmp),
    ]

    size_mb = path.stat().st_size / 1024 / 1024
    timeout_s = max(300, int(size_mb * 10))  # ~10s per MB, min 5min
    audio_desc = "no audio" if no_audio else f"audio={audio_br}"
    log(username, f"transcoding {path.name} ({vcodec} preset={preset} crf={crf} {audio_desc} {size_mb:.0f}MB, timeout={timeout_s}s)")
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        elapsed = time.time() - t0
        if result.returncode != 0:
            log(username, f"ffmpeg error after {elapsed:.0f}s, retrying with tolerant decode: {result.stderr[-300:]}")
            tmp.unlink(missing_ok=True)
            t1 = time.time()
            retry = subprocess.run(retry_cmd, capture_output=True, text=True, timeout=timeout_s)
            retry_elapsed = time.time() - t1
            if retry.returncode != 0:
                log(username, f"ffmpeg retry failed after {retry_elapsed:.0f}s: {retry.stderr[-300:]}")
                tmp.unlink(missing_ok=True)
                return
            elapsed += retry_elapsed
        orig_size = path.stat().st_size
        new_size = tmp.stat().st_size
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", str(tmp)],
            capture_output=True, timeout=30
        )
        if probe.returncode != 0:
            log(username, f"transcode output invalid (moov missing?), keeping original")
            tmp.unlink(missing_ok=True)
            return
        pct = 100 * (1 - new_size / orig_size) if orig_size else 0
        shutil.move(str(tmp), str(tc_path))
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
        available = cam.get("isCamAvailable", False)
        log(username, f"isCamAvailable={available}")
        return available
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

    time_limit = _parse_duration(model["recording_time_limit"]) if model.get("recording_time_limit") else None
    size_limit = _parse_size(model["recording_file_size_limit"]) if model.get("recording_file_size_limit") else None
    on_limit = model.get("on_limit_reached", "stop_for_day")
    rollover_max = model.get("rollover_max_files")
    cooldown_mins = model.get("cooldown_minutes") or 0
    effective_time_limit = None if on_limit == "ignore" else time_limit
    effective_size_limit = None if on_limit == "ignore" else size_limit
    loop = asyncio.get_event_loop()

    daily_key = (username, date.today())
    if on_limit == "rollover" and rollover_max:
        if daily_file_counts.get(daily_key, 0) >= rollover_max:
            log(username, f"rollover max ({rollover_max} files) already reached today, skipping")
            resume_after[username] = _next_poll_start(model)
            resume_reason[username] = "rollover_limit"
            active_recordings.pop(username, None)
            return

    # Collects (url, bytes) for MOUFLON init+part MP4s as browser downloads them.
    # Populated by on_response before we even know if stream is LLHLS.
    media_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--no-zygote",
                "--disable-gpu",
                "--disable-gpu-sandbox",
                "--single-process",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--disable-background-networking",
                "--disable-extensions",
                "--disable-translate",
                "--disable-sync",
                "--metrics-recording-only",
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

        await page.route(
            "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}",
            lambda route: route.abort(),
        )

        master_url: list[str] = []

        async def on_response(response):
            url = response.url
            # Master playlist detection
            if "m3u8" in url and not master_url:
                try:
                    body = await response.text()
                    if "#EXT-X-STREAM-INF" in body:
                        log(username, f"master: ...{url[-80:]}")
                        master_url.append(url)
                except Exception:
                    pass
                return
            # MOUFLON segment capture: browser fetches real part/init URLs;
            # capture bytes here so we never need to re-download from CDN
            if ".mp4" in url and ("_init_" in url or "_part" in url):
                try:
                    body = await response.body()
                    media_queue.put_nowait((url, body))
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto(
                f"https://stripchat.com/{username}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as e:
            log(username, f"page.goto: {e}")

        try:
            await page.bring_to_front()
        except Exception:
            pass
        try:
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        except Exception:
            pass

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

        if not stream_url and not master_url and perf_master_urls:
            log(username, f"on_response missed master — using perf master: ...{perf_master_urls[-1][-80:]}")
            master_url.extend(perf_master_urls)

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
            idle_reason[username] = "no_stream"
            await browser.close()
            active_recordings.pop(username, None)
            return

        is_llhls = "_HLS_msn=" in stream_url

        if not is_llhls:
            # Non-LLHLS: close browser, use requests-based downloader
            cookies = []
            try:
                cookies = await context.cookies()
            except Exception:
                pass
            await browser.close()
        else:
            # LLHLS: browser already has content via on_response capture.
            # Keep browser alive so MOUFLON keeps streaming; record from media_queue.
            log(username, "LLHLS: recording via browser capture")

            # Drain queue for init segment and any early parts already buffered
            init_data: bytes | None = None
            early_parts: list[tuple[str, bytes]] = []
            while True:
                try:
                    url, body = media_queue.get_nowait()
                    if "_init_" in url and init_data is None:
                        init_data = body
                        log(username, f"init segment captured ({len(body)}B)")
                    elif "_part" in url:
                        early_parts.append((url, body))
                except asyncio.QueueEmpty:
                    break

            # If init not yet captured, wait for it
            if init_data is None:
                log(username, "waiting for init segment...")
                deadline = loop.time() + 30
                while loop.time() < deadline and not shutdown.is_set():
                    try:
                        url, body = await asyncio.wait_for(media_queue.get(), timeout=2.0)
                        if "_init_" in url:
                            init_data = body
                            log(username, f"init segment captured ({len(body)}B)")
                            break
                        elif "_part" in url:
                            early_parts.append((url, body))
                    except asyncio.TimeoutError:
                        pass

            if init_data is None:
                log(username, "could not capture init segment, aborting")
                idle_reason[username] = "no_stream"
                await browser.close()
                active_recordings.pop(username, None)
                return

            # Keep video playing for long recordings
            async def keep_playing():
                while not shutdown.is_set():
                    try:
                        await page.evaluate(
                            "document.querySelectorAll('video').forEach(v => v.play().catch(() => {}))"
                        )
                    except Exception as e:
                        log(username, f"keep_playing evaluate error (retrying): {e}")
                        await asyncio.sleep(5)
                        continue
                    await asyncio.sleep(15)

            keep_task = asyncio.create_task(keep_playing())

            try:
                while True:  # rollover loop
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    output = RECORDINGS_DIR / f"{username}_{timestamp}.mp4"
                    log(username, f"recording -> {output.name}")
                    seg_count = 0
                    file_start = time.time()
                    stop_reason = "shutdown"
                    seen_parts: set[str] = set()
                    stall_count = 0

                    with open(output, "wb") as f:
                        f.write(init_data)
                        f.flush()

                        for url, body in early_parts:
                            if url not in seen_parts:
                                seen_parts.add(url)
                                f.write(body)
                                seg_count += 1
                        if early_parts:
                            f.flush()
                            early_parts = []

                        while not shutdown.is_set():
                            try:
                                url, body = await asyncio.wait_for(media_queue.get(), timeout=LLHLS_QUEUE_TIMEOUT)
                            except asyncio.TimeoutError:
                                stall_count += 1
                                if stall_count <= LLHLS_STALL_RETRIES and is_live(username):
                                    log(username, f"no new parts for {LLHLS_QUEUE_TIMEOUT}s (stall {stall_count}/{LLHLS_STALL_RETRIES}) but model still live; waiting")
                                    continue
                                log(username, f"no new parts for {LLHLS_QUEUE_TIMEOUT}s (stall {stall_count}/{LLHLS_STALL_RETRIES}), stopping")
                                stop_reason = "stream_ended"
                                break
                            stall_count = 0
                            if url not in seen_parts:
                                seen_parts.add(url)
                                f.write(body)
                                f.flush()
                                seg_count += 1
                                if seg_count % 20 == 0:
                                    log(username, f"segs: {seg_count}, file: {f.tell()}B")
                            if effective_time_limit and (time.time() - file_start) >= effective_time_limit:
                                log(username, f"time limit reached ({effective_time_limit}s)")
                                stop_reason = "time_limit"
                                break
                            if effective_size_limit and f.tell() >= effective_size_limit:
                                log(username, f"size limit reached ({effective_size_limit}B)")
                                stop_reason = "size_limit"
                                break

                    log(username, f"done: {output.name}")

                    if TRANSCODE_CFG.get("enabled"):
                        model_tc_cfg = {**TRANSCODE_CFG, "no_audio": model.get("transcode_no_audio", False)}
                        await loop.run_in_executor(None, _transcode_file, username, output, model_tc_cfg)

                    daily_file_counts[daily_key] = daily_file_counts.get(daily_key, 0) + 1
                    file_count = daily_file_counts[daily_key]
                    if stop_reason not in ("time_limit", "size_limit"):
                        if on_limit == "rollover" and rollover_max and file_count >= rollover_max:
                            log(username, f"rollover max ({rollover_max} files) reached, stopped for day")
                            resume_after[username] = _next_poll_start(model)
                            resume_reason[username] = "rollover_limit"
                        break
                    if on_limit == "rollover":
                        if rollover_max and file_count >= rollover_max:
                            log(username, f"rollover max ({rollover_max} files) reached, stopped for day")
                            resume_after[username] = _next_poll_start(model)
                            resume_reason[username] = "rollover_limit"
                            break
                        log(username, f"limit hit ({stop_reason}) — rolling over (file {file_count + 1})")
                        continue
                    elif on_limit == "pause":
                        resume_dt = datetime.now() + timedelta(minutes=cooldown_mins)
                        resume_after[username] = resume_dt
                        resume_reason[username] = "cooldown"
                        log(username, f"limit hit ({stop_reason}), pausing {cooldown_mins}m — resume after {resume_dt.strftime('%H:%M:%S')}")
                        break
                    elif on_limit == "stop_for_day":
                        nps = _next_poll_start(model)
                        resume_after[username] = nps
                        resume_reason[username] = "stop_for_day"
                        log(username, f"limit hit ({stop_reason}), stopped for day — resume at {nps.strftime('%H:%M:%S')}")
                        break
                    else:
                        break
            finally:
                keep_task.cancel()
                await browser.close()
                active_recordings.pop(username, None)
            return

    # Non-LLHLS path: requests-based downloader
    try:
        while True:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            output = RECORDINGS_DIR / f"{username}_{timestamp}.mp4"

            stop_reason = await loop.run_in_executor(
                None, _record_with_requests, username, stream_url, cookies, output,
                effective_time_limit, effective_size_limit,
            )

            if TRANSCODE_CFG.get("enabled"):
                model_tc_cfg = {**TRANSCODE_CFG, "no_audio": model.get("transcode_no_audio", False)}
                await loop.run_in_executor(None, _transcode_file, username, output, model_tc_cfg)

            daily_file_counts[daily_key] = daily_file_counts.get(daily_key, 0) + 1
            file_count = daily_file_counts[daily_key]
            if stop_reason not in ("time_limit", "size_limit"):
                if on_limit == "rollover" and rollover_max and file_count >= rollover_max:
                    log(username, f"rollover max ({rollover_max} files) reached, stopped for day")
                    resume_after[username] = _next_poll_start(model)
                    resume_reason[username] = "rollover_limit"
                break
            if on_limit == "rollover":
                if rollover_max and file_count >= rollover_max:
                    log(username, f"rollover max ({rollover_max} files) reached, stopped for day")
                    resume_after[username] = _next_poll_start(model)
                    resume_reason[username] = "rollover_limit"
                    break
                log(username, f"limit hit ({stop_reason}) — rolling over (file {file_count + 1})")
                continue
            elif on_limit == "pause":
                resume_dt = datetime.now() + timedelta(minutes=cooldown_mins)
                resume_after[username] = resume_dt
                resume_reason[username] = "cooldown"
                log(username, f"limit hit ({stop_reason}), pausing {cooldown_mins}m — resume after {resume_dt.strftime('%H:%M:%S')}")
                break
            elif on_limit == "stop_for_day":
                nps = _next_poll_start(model)
                resume_after[username] = nps
                resume_reason[username] = "stop_for_day"
                log(username, f"limit hit ({stop_reason}), stopped for day — resume at {nps.strftime('%H:%M:%S')}")
                break
            else:
                break
    finally:
        active_recordings.pop(username, None)


def record(model: dict):
    username = model["name"]
    if not _browser_sem.acquire(blocking=False):
        log(username, "max concurrent browsers reached, will retry next poll")
        idle_reason[username] = "max_concurrent"
        active_recordings.pop(username, None)
        return
    try:
        log(username, "browser launching to find stream URL...")
        asyncio.run(_record_async(model))
    finally:
        _browser_sem.release()


def monitor():
    while True:
        with config_lock:
            models = _load_config().get("models", [])
        for model in models:
            username = model["name"]
            if not model.get("enabled", True):
                idle_reason.pop(username, None)
                continue
            if not _in_poll_window(model):
                log(username, "outside poll window, skipping")
                idle_reason[username] = "outside_window"
                continue
            if username in resume_after:
                if datetime.now() < resume_after[username]:
                    idle_reason[username] = resume_reason.get(username, "cooldown")
                    continue
                del resume_after[username]
                resume_reason.pop(username, None)
            t = active_recordings.get(username)
            if t and t.is_alive():
                idle_reason.pop(username, None)  # confirmed recording
                continue
            if is_live(username):
                idle_reason[username] = "starting"
                t = threading.Thread(target=record, args=(model,), daemon=True)
                t.start()
                active_recordings[username] = t
            else:
                idle_reason[username] = "offline"
        time.sleep(POLL_INTERVAL)


def _start_web():
    try:
        import uvicorn
        from web import app as _web_app
        port = int(os.environ.get("APP_PORT", "5705"))
        print(f"[{time.strftime('%H:%M:%S')}] Web UI starting on port {port}", flush=True)
        uvicorn.run(_web_app, host="0.0.0.0", port=port, log_level="warning")
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Web UI failed to start: {e}", flush=True)


def _init_daily_file_counts():
    """Scan existing recordings to pre-populate daily_file_counts on startup."""
    today_str = date.today().strftime("%Y%m%d")
    pattern = re.compile(r'^(.+)_(\d{8})_\d{6}(?:_tc)?\.mp4$')
    try:
        for f in RECORDINGS_DIR.iterdir():
            if not f.is_file():
                continue
            m = pattern.match(f.name)
            if m and m.group(2) == today_str:
                username = m.group(1)
                key = (username, date.today())
                daily_file_counts[key] = daily_file_counts.get(key, 0) + 1
        if daily_file_counts:
            for (u, _), cnt in daily_file_counts.items():
                print(f"[{time.strftime('%H:%M:%S')}][{u}] startup: {cnt} recording(s) found for today", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] init_daily_file_counts error: {e}", flush=True)


if __name__ == "__main__":
    _init_daily_file_counts()
    web_thread = threading.Thread(target=_start_web, daemon=True)
    web_thread.start()
    try:
        monitor()
    except KeyboardInterrupt:
        print("Shutting down...")
        shutdown.set()
