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

        # 2. Deploy the settle-eod descriptor (v0.1.5 feature spotlight:
        # it uses secrets in two of its SQL steps, so the dashboard
        # will show 🔐 indicators next to those events).
        page.select_option("#descriptor", "descriptors/sample_settle_eod.yaml")
        page.click("#submit")
        page.wait_for_selector(".runs-list li", timeout=5000)
        # Wait for the detail panel to show a terminal state.
        page.wait_for_function(
            "() => document.querySelector('#detail-body .badge')"
            " && (document.querySelector('#detail-body .badge').classList"
            ".contains('ok') || document.querySelector('#detail-body .badge')"
            ".classList.contains('failed') || document.querySelector('#detail-body .badge')"
            ".classList.contains('aborted'))",
            timeout=10000,
        )
        page.wait_for_function(
            "() => document.getElementById('detail-body')"
            " && document.getElementById('detail-body').innerText"
            ".includes('run finished')",
            timeout=10000,
        )
        page.wait_for_function(
            "() => document.getElementById('detail-body')"
            " && !document.getElementById('detail-body').innerText"
            ".includes('Cancel run')",
            timeout=10000,
        )
        time.sleep(0.5)
        page.screenshot(path=str(SHOTS / "02_dashboard_after_run.png"), full_page=True)
        print("02_dashboard_after_run.png ok")

        # 3. The detail panel
        detail = page.locator("#detail").first
        detail.screenshot(path=str(SHOTS / "03_detail_timeline.png"))
        print("03_detail_timeline.png ok")

        browser.close()


if __name__ == "__main__":
    main()
