import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.browser.session import FirebaseBrowser
from app.config import Settings, load_projects


async def run() -> None:
    settings = Settings()
    projects = load_projects()
    if not projects:
        print("No enabled projects. Edit config/projects.yaml and set enabled: true.")
        return

    browser = FirebaseBrowser(settings.browser_profile, settings.headless)
    page = await browser.start()
    try:
        print("\nOpening Firebase Console. Sign in manually if prompted.")
        await page.goto("https://console.firebase.google.com/", wait_until="domcontentloaded")
        print("When the Console is ready, press Enter here to continue.")
        await asyncio.to_thread(input, "> ")

        print("\nConfigured projects:")
        for idx, project in enumerate(projects, start=1):
            print(f"{idx}. {project['name']} [{project['firebase_project_id']}]")
            await page.goto(project["console_url"], wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)

            title = await page.title()
            current_url = page.url
            body_text = ""
            try:
                body_text = (await page.locator("body").inner_text(timeout=8000))[:1200]
            except Exception as exc:
                body_text = f"Could not read visible page text: {exc}"

            snapshot_dir = Path("data/snapshots")
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe_key = project["key"].replace("/", "_")
            screenshot_path = snapshot_dir / f"{safe_key}-{stamp}.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)

            result = {
                "project_key": project["key"],
                "expected_project_id": project["firebase_project_id"],
                "page_title": title,
                "current_url": current_url,
                "url_contains_expected_id": project["firebase_project_id"] in current_url,
                "visible_text_preview": body_text,
                "screenshot": str(screenshot_path),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("\nCheck the browser: confirm the correct project and Crashlytics page are shown.")
            await asyncio.to_thread(input, "Press Enter for next project (or Ctrl-C to stop)... ")
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())
