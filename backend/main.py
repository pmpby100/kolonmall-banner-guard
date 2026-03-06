import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="KolonMall Banner Integrity Guard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

TARGET_URL = "https://www.kolonmall.com/"
POPUP_SELECTOR = "h2#swal2-title"
POPUP_TEXT = "코오롱몰 메인으로 이동합니다."
POPUP_TIMEOUT_MS = 3000
CONCURRENCY = 15

# Global state for SSE streaming
scan_queue: asyncio.Queue = asyncio.Queue()
scan_running = False
scan_cancelled = False


async def extract_banners(page) -> list[dict]:
    """Extract all banners from the KolonMall main page very quickly."""
    banners = []

    try:
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass  # Just need the DOM

    # Force extraction of lazy-loaded images without scrolling/waiting
    # KolonMall's lazy loading usually keeps original images in data attributes (like data-src) 
    # or inside noscript tags before initialization, but mostly just requires finding all slides.
    
    # ─── 1. Carousel (Main) banners ───────────────────────────────────────────
    try:
        # Avoid duplicated slides used by Swiper for infinite loop
        slides = await page.query_selector_all(".swiper-wrapper .swiper-slide:not(.swiper-slide-duplicate)")
        seen_urls = set()
        for idx, slide in enumerate(slides):
            a_tag = await slide.query_selector("a")
            if not a_tag:
                continue
            href = await a_tag.get_attribute("href") or ""
            if not href or href == "#" or href in seen_urls:
                continue
            seen_urls.add(href)

            # Resolve relative URLs
            if href.startswith("/"):
                href = "https://www.kolonmall.com" + href

            # Get image src
            img = await slide.query_selector("img")
            image_url = ""
            alt = ""
            if img:
                image_url = await img.get_attribute("src") or ""
                alt = await img.get_attribute("alt") or ""
                # If src is a placeholder, try grabbing the highly-likely real image lazy-attribute
                if "placeholder" in image_url or not image_url or "data:image" in image_url:
                    lazy_src = await img.get_attribute("data-src")
                    if lazy_src:
                        image_url = lazy_src

            banners.append({
                "index": len(banners),
                "type": "carousel",
                "name": alt or f"캐러셀 배너 {idx + 1}",
                "image_url": image_url,
                "landing_url": href,
                "status": "PENDING",
                "error_message": "",
                "screenshot_path": None,
            })
    except Exception as e:
        print(f"[WARN] Carousel extraction failed: {e}")

    # ─── 2. Sub (하단) banners ─────────────────────────────────────────────────
    try:
        sub_containers = await page.query_selector_all("div.flex-d_column.gap_36px")
        seen_sub_urls = set()
        for container in sub_containers:
            a_tags = await container.query_selector_all("a")
            for a_tag in a_tags:
                href = await a_tag.get_attribute("href") or ""
                if not href or href == "#" or href in seen_sub_urls:
                    continue
                seen_sub_urls.add(href)

                if href.startswith("/"):
                    href = "https://www.kolonmall.com" + href

                img = await a_tag.query_selector("img")
                image_url = ""
                if img:
                    image_url = await img.get_attribute("src") or ""
                    alt = await img.get_attribute("alt") or ""
                else:
                    # Text link
                    alt = (await a_tag.inner_text()).strip()[:40]

                banners.append({
                    "index": len(banners),
                    "type": "sub",
                    "name": alt or f"서브 배너 {len(banners)}",
                    "image_url": image_url,
                    "landing_url": href,
                    "status": "PENDING",
                    "error_message": "",
                    "screenshot_path": None,
                })
    except Exception as e:
        print(f"[WARN] Sub-banner extraction failed: {e}")

    return banners


async def check_banner(semaphore: asyncio.Semaphore, browser, banner: dict, total: int) -> dict:
    """Check a single banner for dead-link (popup detection)."""
    global scan_cancelled
    
    async with semaphore:
        result = banner.copy()
        page = None
        
        # Immediate abort check
        if scan_cancelled:
            result["status"] = "CANCELLED"
            result["error_message"] = "검수가 사용자에 의해 중단되었습니다."
            return result
        try:
            page = await browser.new_page()
            # Aggressively block heavy resources (images, fonts, css, tracking scripts)
            await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "stylesheet", "media"] or "google-analytics" in route.request.url or "youtube" in route.request.url or "facebook" in route.request.url else route.continue_())

            try:
                await page.goto(result["landing_url"], wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass  # Page may redirect; that's OK — we just check for the popup

            # Check for dead-link popup within 3 seconds
            try:
                popup_el = await page.wait_for_selector(
                    POPUP_SELECTOR,
                    timeout=POPUP_TIMEOUT_MS,
                    state="visible",
                )
                popup_text = (await popup_el.inner_text()).strip()
                if POPUP_TEXT in popup_text:
                    result["status"] = "CRITICAL_ERROR"
                    result["error_message"] = f"데드링크 팝업 감지: '{popup_text}'"
                    # Screenshot
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    shot_path = SCREENSHOTS_DIR / f"{result['index']:03d}_{ts}.png"
                    await page.screenshot(path=str(shot_path), full_page=False)
                    result["screenshot_path"] = str(shot_path)
                else:
                    # Popup appeared but with different text — treat as success
                    result["status"] = "SUCCESS"
            except PlaywrightTimeoutError:
                # No popup appeared within 3 seconds → link is valid
                result["status"] = "SUCCESS"

        except Exception as e:
            result["status"] = "ERROR"
            result["error_message"] = str(e)[:200]
            try:
                if page:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    shot_path = SCREENSHOTS_DIR / f"{result['index']:03d}_{ts}_err.png"
                    await page.screenshot(path=str(shot_path), full_page=False)
                    result["screenshot_path"] = str(shot_path)
            except Exception:
                pass
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

        return result


async def run_scan(queue: asyncio.Queue):
    """Main scan coroutine: extract banners then validate each concurrently."""
    global scan_running
    scan_running = True

    async with async_playwright() as p:
        # Instead of full heavy mobile emulation (`p.devices['iPhone 13']`),
        # we just set a mobile viewport, which is much faster to load and tricks responsive CSS/JS.
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={'width': 375, 'height': 812},
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        )
        extract_page = await context.new_page()

        # Phase 1: Extract banners
        await queue.put(json.dumps({"event": "extracting", "message": "배너 추출 중..."}))
        try:
            banners = await extract_banners(extract_page)
        except Exception as e:
            await queue.put(json.dumps({"event": "error", "message": str(e)}))
            await browser.close()
            scan_running = False
            await queue.put(None)  # sentinel
            return
        finally:
            await extract_page.close()

        total = len(banners)
        await queue.put(json.dumps({"event": "extracted", "total": total, "banners": banners}))

        if total == 0:
            await browser.close()
            scan_running = False
            await queue.put(None)
            return

        # Phase 2: Validate banners concurrently
        semaphore = asyncio.Semaphore(CONCURRENCY)
        tasks = [
            asyncio.create_task(check_banner(semaphore, browser, banner, total))
            for banner in banners
        ]

        completed = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            completed += 1
            await queue.put(json.dumps({
                "event": "result",
                "completed": completed,
                "total": total,
                "banner": result,
            }))
            # Stop scheduling queue updates if cancelled mid-way (but let current tasks finish fast)
            if scan_cancelled and completed < total:
                # Optionally send a cancellation event, but setting 'done' is usually cleaner
                pass

        await browser.close()

    # Final event
    final_event = "cancelled" if scan_cancelled else "done"
    await queue.put(json.dumps({"event": final_event, "total": total, "completed": completed}))
    await queue.put(None)  # sentinel
    scan_running = False
    scan_cancelled = False


@app.post("/api/scan")
async def start_scan():
    """Start or restart a scan. Returns immediately."""
    global scan_queue, scan_running, scan_cancelled
    if scan_running:
        return {"status": "already_running"}
    # Reset queue
    scan_queue = asyncio.Queue()
    scan_cancelled = False
    asyncio.create_task(run_scan(scan_queue))
    return {"status": "started"}


@app.post("/api/cancel")
async def cancel_scan():
    """Cancel a running scan."""
    global scan_running, scan_cancelled
    if not scan_running:
        return {"status": "not_running"}
    
    scan_cancelled = True
    return {"status": "cancelling"}


@app.get("/api/stream")
async def stream_results():
    """SSE endpoint — streams scan progress and results."""
    async def event_generator() -> AsyncGenerator:
        while True:
            msg = await scan_queue.get()
            if msg is None:
                break
            yield {"data": msg}

    return EventSourceResponse(event_generator())


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "message": "KolonMall Banner Integrity Guard API is running",
        "docs": "API documentation is available at /docs"
    }
