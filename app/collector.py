import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.browser.extractor import CrashlyticsExtractor
from app.browser.session import FirebaseBrowser
from app.config import Settings, get_project_console_urls, load_projects, project_sources
from app.db.database import SessionLocal, init_db
from app.db.repository import (
    save_crash_detail,
    save_or_update_issue,
    sync_projects,
    update_project_sync_time,
)

logger = logging.getLogger(__name__)


async def sync_crashlytics(
    project_key: Optional[str] = None,
    fetch_details_limit: int = 5,
    headless: bool = True,
    projects_file: str | None = None,
) -> dict:
    """Sync crash issues and details from Firebase Console into local database."""
    init_db()
    settings = Settings()
    all_projects = load_projects(projects_file)

    if project_key:
        projects = [p for p in all_projects if p["key"] == project_key]
    else:
        projects = all_projects

    projects = [p for p in projects if any(
        s.get("type", "").lower() == "crashlytics" for s in project_sources(p)
    )]
    if not projects:
        print("No enabled projects found to sync.")
        return {"synced_projects": 0, "total_issues_found": 0, "details_extracted": 0, "errors": []}

    db = SessionLocal()
    try:
        sync_projects(db, all_projects)
    finally:
        db.close()

    browser = FirebaseBrowser(settings.browser_profile, headless=headless)
    page = await browser.start()
    extractor = CrashlyticsExtractor(page)

    results = {
        "synced_projects": 0,
        "total_issues_found": 0,
        "details_extracted": 0,
        "errors": [],
    }

    seen_urls: set[str] = set()
    try:
        for project in projects:
            p_key = project["key"]
            try:
                p_id = project["firebase_project_id"]
                target_urls = get_project_console_urls(project)
                if not target_urls:
                    continue
                for platform, url in target_urls:
                    if url in seen_urls:
                        print(f"\nSkipping '{p_key}' ({platform}): same console_url as a previous target.")
                        continue
                    seen_urls.add(url)

                    print(f"\n==========================================")
                    print(f"Syncing Project: {project['name']} [{p_id}] ({p_key} / {platform})")
                    print(f"URL: {url}")
                    print(f"==========================================")

                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(4000)

                    if await extractor.is_login_required():
                        msg = (
                            f"Google account login required for project '{p_key}'. "
                            "Please run: 'python -m app.main browser-check' to complete login manually."
                        )
                        print(f"ERROR: {msg}")
                        results["errors"].append({"project": p_key, "error": msg})
                        continue

                    ready = await extractor.wait_for_crashlytics_ready(timeout_ms=25000)
                    if not ready:
                        print("Warning: Crashlytics dashboard took too long or table not detected.")

                    list_page_url = page.url
                    snap_dir = Path("data/snapshots")
                    snap_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    shot_file = snap_dir / f"sync-{p_key}-{platform}-{stamp}.png"
                    await page.screenshot(path=str(shot_file), full_page=True)
                    print(f"Snapshot saved: {shot_file}")

                    # Extract list of issues from visible table rows
                    issues = await extractor.extract_issues_list(
                        project_key=p_key,
                        firebase_project_id=p_id,
                        platform=platform,
                    )
                    print(f"Found {len(issues)} issues on page.")
                    results["total_issues_found"] += len(issues)

                    # Click into top N rows to resolve real Firebase issue IDs, URLs, and full stack traces
                    detail_count = min(len(issues), fetch_details_limit)
                    for idx in range(detail_count):
                        issue = issues[idx]
                        print(f"  [{idx + 1}/{detail_count}] Inspecting issue: {issue.title[:60]} ({issue.events_count} events)...")

                        # Ensure we are on the issues table page and rows are ready
                        if idx > 0:
                            await page.goto(list_page_url, wait_until="domcontentloaded")
                            await extractor.wait_for_crashlytics_ready(timeout_ms=20000)

                        rows = await extractor.get_issue_row_locators()
                        if idx >= len(rows):
                            print(f"  Warning: Row index {idx} no longer available.")
                            break

                        await rows[idx].click()
                        # Wait for URL to navigate to /issues/<id>
                        try:
                            await page.wait_for_url("**/issues/**", timeout=15000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(4000)

                        # Resolve real Firebase issue_id and URL
                        m = re.search(r"/issues/([a-zA-Z0-9_-]+)", page.url)
                        if m:
                            issue.issue_id = m.group(1)
                            issue.issue_url = page.url

                        detail = await extractor.extract_issue_detail(issue.issue_id)

                        db = SessionLocal()
                        try:
                            save_or_update_issue(db, issue)
                            save_crash_detail(db, p_key, detail)
                            results["details_extracted"] += 1
                        finally:
                            db.close()

                    # Save remaining issues beyond detail limit
                    if len(issues) > detail_count:
                        db = SessionLocal()
                        try:
                            for issue in issues[detail_count:]:
                                save_or_update_issue(db, issue)
                        finally:
                            db.close()

                db = SessionLocal()
                try:
                    update_project_sync_time(db, p_key)
                finally:
                    db.close()

                results["synced_projects"] += 1
            except Exception as exc:
                results["errors"].append({"project": p_key, "error": str(exc)})
                logger.exception("Firebase sync failed for %s", p_key)

    finally:
        await browser.close()

    return results


def run_sync(project_key: Optional[str] = None, headless: bool = True, fetch_details_limit: int = 5):
    return asyncio.run(
        sync_crashlytics(
            project_key=project_key,
            headless=headless,
            fetch_details_limit=fetch_details_limit,
        )
    )
