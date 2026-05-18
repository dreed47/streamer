import asyncio
import subprocess
import time
import threading
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

import requests as req_lib
from playwright.async_api import async_playwright

# ==================== CONFIG ====================
MODELS = ["GabrielaLove_"]
RECORDINGS_DIR = Path("/recordings")
RECORDINGS_DIR.mkdir(exist_ok=True)

POLL_INTERVAL = 30
# ===============================================

active_recordings = {}
shutdown = threading.Event()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _strip_ll_hls_params(url: str) -> str:
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=True)
    qs.pop("_HLS_msn", None)
    qs.pop("_HLS_part", None)
    return urlunparse(p._replace(query=urlencode(qs, doseq=True)))


def log(username: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}][{username}] {msg}", flush=True)


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


def _record_with_ffmpeg(username: str, stream_url: str, cookies_list: list, output_path: Path):
    """
    ffmpeg handles: master→variant selection, AES-128 segment decryption, live polling.
    pkey-authenticated doppiocdn.net URLs don't need cookies — auth is in the URL.
    """
    headers = f"User-Agent: {_UA}\r\nReferer: https://stripchat.com/{username}\r\n"
    if cookies_list and "pkey=" not in stream_url:
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies_list)
        if cookie_str:
            headers = f"Cookie: {cookie_str}\r\n" + headers

    cmd = [
        "ffmpeg", "-loglevel", "warning",
        "-headers", headers,
        "-i", stream_url,
        "-c", "copy",
        str(output_path),
    ]

    log(username, f"ffmpeg -> {output_path.name}")
    proc = subprocess.Popen(cmd)
    while proc.poll() is None:
        if shutdown.is_set():
            proc.terminate()
            proc.wait()
            break
        time.sleep(5)
    log(username, f"ffmpeg done (exit={proc.returncode}): {output_path.name}")


async def _record_async(username: str):
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
                             if "m3u8" in u and "pkey=" in u and "ping" not in u]
                other_variants = [u for u in perf_urls
                                  if "m3u8" in u and "ping" not in u
                                  and "master" not in u.lower()]
                candidates = pkey_urls or other_variants
                if candidates:
                    stream_url = candidates[-1]
                    log(username, f"stream URL via perf: ...{stream_url[-90:]}")
                    break
            except Exception as e:
                log(username, f"perf query error: {e}")

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

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    output = RECORDINGS_DIR / f"{username}_{timestamp}.ts"

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _record_with_ffmpeg, username, _strip_ll_hls_params(stream_url), cookies, output
    )
    active_recordings.pop(username, None)


def record(username: str):
    log(username, "browser launching to find stream URL...")
    asyncio.run(_record_async(username))


def monitor():
    print(f"[{time.strftime('%H:%M:%S')}] Monitoring: {MODELS}", flush=True)
    while True:
        for user in MODELS:
            t = active_recordings.get(user)
            if t and t.is_alive():
                continue
            if is_live(user):
                t = threading.Thread(target=record, args=(user,), daemon=True)
                t.start()
                active_recordings[user] = t
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        monitor()
    except KeyboardInterrupt:
        print("Shutting down...")
        shutdown.set()
