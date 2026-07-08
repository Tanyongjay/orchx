"""Render the live dashboard with one in-flight run + a finished run."""
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

WEB = "http://127.0.0.1:8765"
SHOTS = Path("D:/123/orchx/docs/screenshots")
SHOTS.mkdir(parents=True, exist_ok=True)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        # 1. Empty state
        page.goto(WEB + "/", wait_until="networkidle")
        page.screenshot(path=str(SHOTS / "01_empty_state.png"), full_page=True)
        print("01_empty_state.png ok")

        # 2. Pick OAuth descriptor and Deploy
        page.select_option("#descriptor", "descriptors/sample_oauth_service.yaml")
        page.click("#submit")
        # Wait for run to be selected
        page.wait_for_selector(".runs-list li", timeout=5000)
        # Give the WebSocket time to replay the full event history
        # and for renderDetail() to paint the final state. The mock
        # finishes in ~30ms, but the dashboard fetches, then opens
        # the WS, which replays history, then closes. 4s is plenty
        # of slack on a real box and avoids flake on slow CI.
        time.sleep(4)
        page.screenshot(path=str(SHOTS / "02_dashboard_after_run.png"), full_page=True)
        print("02_dashboard_after_run.png ok")

        # 3. Force a failure run via chaos JSON (set env then restart — easier to
        #    simulate by clicking a second run; we already have one good. The
        #    screenshot of the happy path is what the README will show.)
        # ...we'll just take a focused screenshot of the detail panel.
        detail = page.locator("#detail").first
        detail.screenshot(path=str(SHOTS / "03_detail_timeline.png"))
        print("03_detail_timeline.png ok")

        browser.close()


if __name__ == "__main__":
    main()
