import asyncio
import json
import re
from playwright.async_api import async_playwright

USERNAME = "eva_miller7"

async def main():
    print(f"Fetching https://stripchat.com/{USERNAME} via Playwright...")
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
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await context.new_page()
        await page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot,css}", lambda r: r.abort())
        res = await page.goto(f"https://stripchat.com/{USERNAME}", wait_until="domcontentloaded", timeout=20000)
        status_code = res.status if res else 0
        text = await page.content()
        await browser.close()

    print(f"HTTP {status_code}  ({len(text)} bytes)")

    # Extract __PRELOADED_STATE__
    m = re.search(r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})(?:\s*;?\s*</script>)', text, re.DOTALL)
    if not m:
        print("\n[FAIL] __PRELOADED_STATE__ not found in page — likely Cloudflare challenge page")
        print("First 500 chars of body:")
        print(text[:500])
        exit(1)

    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"\n[FAIL] JSON parse error: {e}")
        exit(1)

    print(f"\n[OK] __PRELOADED_STATE__ parsed. Top-level keys: {list(state.keys())}\n")

    # viewCam
    view_cam = state.get("viewCam", {})
    print("=== viewCam.model ===")
    model = view_cam.get("model", {})
    print(f"  id       = {model.get('id')}")
    print(f"  isLive   = {model.get('isLive')}")
    print(f"  username = {model.get('username')}")

    show = view_cam.get("show")
    print(f"\n=== viewCam.show ===")
    print(f"  type = {show.get('type') if isinstance(show, dict) else show}")
    print(f"  show = {show}")

    # configV3 path
    print("\n=== configV3 hunt ===")
    config_v3 = state.get("configV3", {})
    static = config_v3.get("static", {})
    features = static.get("features", {})
    hls_fb = features.get("hlsFallback", {})
    domains = hls_fb.get("fallbackDomains", [])
    print(f"  configV3.static.features.hlsFallback.fallbackDomains = {domains}")
    if domains:
        for d in domains:
            host = d.get("hlsStreamHost") if isinstance(d, dict) else d
            print(f"    host: {host}")
            if host and model.get("id"):
                mid = model["id"]
                print(f"    => https://edge-hls.{host}/hls/{mid}/master/{mid}_auto.m3u8")

    # backwards-compat config path
    config = state.get("config", {})
    data = config.get("data", {})
    features2 = data.get("features", data.get("featuresV2", {}))
    hls_fb2 = features2.get("hlsFallback", {})
    domains2 = hls_fb2.get("fallbackDomains", [])
    if domains2:
        print(f"\n  config.data.features.hlsFallback.fallbackDomains = {domains2}")

    # dump any key containing "hls" or "stream" or "host" in top-level
    print("\n=== Keys containing 'hls'/'stream'/'host' (recursive top-2 levels) ===")
    def find_keys(d, prefix="", depth=0):
        if depth > 2 or not isinstance(d, dict):
            return
        for k, v in d.items():
            path = f"{prefix}.{k}" if prefix else k
            if any(x in k.lower() for x in ("hls", "stream", "host", "cdn")):
                print(f"  {path} = {str(v)[:120]}")
            find_keys(v, path, depth + 1)

    find_keys(state)

if __name__ == "__main__":
    asyncio.run(main())
