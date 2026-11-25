import hashlib
import logging
import re
from typing import Optional
from playwright.async_api import Locator, Page

from app.models.issue import CrashDetail, CrashIssue, StackFrame

logger = logging.getLogger(__name__)


class CrashlyticsExtractor:
    def __init__(self, page: Page):
        self.page = page

    async def is_login_required(self) -> bool:
        """Check if redirected to Google Accounts login."""
        url = self.page.url
        title = await self.page.title()
        return "accounts.google.com" in url or "Sign in" in title

    async def wait_for_crashlytics_ready(self, timeout_ms: int = 25000) -> bool:
        """Wait until Crashlytics issues table rows or empty state is rendered."""
        try:
            await self.page.wait_for_selector(
                "tr.fire-table-row.data-row, div:has-text('No issues found'), div:has-text(\"don't have any crashes\")",
                timeout=timeout_ms,
            )
            await self.page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    async def get_issue_row_locators(self) -> list[Locator]:
        """Return locators for all real data rows in the Crashlytics issues table."""
        return await self.page.locator("tr.fire-table-row.data-row").all()

    async def parse_issue_row(
        self,
        row: Locator,
        project_key: str,
        firebase_project_id: str,
        platform: Optional[str] = None,
        base_url: str = "",
    ) -> CrashIssue:
        """Parse a single tr.fire-table-row.data-row into a CrashIssue."""
        # Event type (Crash / Non-fatal / ANR)
        issue_type = "fatal"
        ev_type_loc = row.locator("[data-test-id='eventTypeWrapper'] .event-type-text")
        if await ev_type_loc.count() > 0:
            ev_text = (await ev_type_loc.first.inner_text()).strip().lower()
            if "non-fatal" in ev_text:
                issue_type = "non_fatal"
            elif "anr" in ev_text:
                issue_type = "anr"

        # Blamed file (if present in metadata row)
        blamed_file = ""
        bf_loc = row.locator("[data-test-id='blamedFileWrapper']")
        if await bf_loc.count() > 0:
            bf_text = (await bf_loc.first.inner_text()).strip()
            # Remove ligature icon text like 'insert_drive_file'
            bf_lines = [l.strip() for l in bf_text.splitlines() if l.strip() and l.strip() != "insert_drive_file"]
            blamed_file = bf_lines[-1] if bf_lines else ""

        # Issue caption cell: title and subtitle
        title = ""
        subtitle = ""
        caption_cell = row.locator("td.mat-column-issue, issue-caption-table-cell").first
        if await caption_cell.count() > 0:
            # Extract lines excluding the metadata row
            full_caption_lines = [l.strip() for l in (await caption_cell.inner_text()).splitlines() if l.strip()]
            meta_loc = caption_cell.locator("c9s-issue-caption-metadata-row")
            meta_lines: set[str] = set()
            if await meta_loc.count() > 0:
                meta_lines = {l.strip() for l in (await meta_loc.first.inner_text()).splitlines() if l.strip()}

            content_lines = [l for l in full_caption_lines if l not in meta_lines]
            if content_lines:
                title = content_lines[0]
            if len(content_lines) > 1:
                subtitle = " ".join(content_lines[1:])

        if not title:
            title = blamed_file or "Unknown Crash Issue"

        # Events & Users counts from table cells
        events_count = 0
        users_count = 0
        cells = await row.locator("td").all()
        if len(cells) >= 2:
            # In Firebase Crashlytics table: [Issues, Versions, Trends, Events, Users]
            users_text = (await cells[-1].inner_text()).strip()
            events_text = (await cells[-2].inner_text()).strip()
            events_count = self._parse_number(events_text)
            users_count = self._parse_number(users_text)

        # Check if there is an explicit link with /issues/<id>
        issue_id = ""
        issue_url = base_url or self.page.url
        link_loc = row.locator("a[href*='/issues/']")
        if await link_loc.count() > 0:
            href = await link_loc.first.get_attribute("href") or ""
            m = re.search(r"/issues/([a-zA-Z0-9_-]+)", href)
            if m:
                issue_id = m.group(1)
                issue_url = href if href.startswith("http") else f"https://console.firebase.google.com{href}"

        if not issue_id:
            # Deterministic fallback ID until detail page click resolves the real Firebase hash ID
            raw_key = f"{project_key}:{title}:{subtitle}"
            issue_id = "row_" + hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]

        return CrashIssue(
            issue_id=issue_id,
            project_key=project_key,
            firebase_project_id=firebase_project_id,
            platform=platform,
            title=title,
            subtitle=subtitle or None,
            issue_type=issue_type,
            events_count=events_count,
            users_count=users_count,
            issue_url=issue_url,
        )

    async def extract_issues_list(
        self,
        project_key: str,
        firebase_project_id: str,
        platform: Optional[str] = None,
    ) -> list[CrashIssue]:
        """Extract all crash issues visible in the Crashlytics issues table."""
        if await self.is_login_required():
            raise PermissionError(
                "Session expired or not logged in. Please run 'python -m app.main browser-check' to log in."
            )

        rows = await self.get_issue_row_locators()
        issues: list[CrashIssue] = []
        seen_ids: set[str] = set()

        for row in rows:
            issue = await self.parse_issue_row(
                row=row,
                project_key=project_key,
                firebase_project_id=firebase_project_id,
                platform=platform,
                base_url=self.page.url,
            )
            if issue.issue_id not in seen_ids:
                seen_ids.add(issue.issue_id)
                issues.append(issue)

        return issues

    async def extract_issue_detail(self, issue_id: str) -> CrashDetail:
        """Extract detailed stack trace, OS, device, and version info from the issue detail page."""
        # Resolve real issue ID from URL if available
        m = re.search(r"/issues/([a-zA-Z0-9_-]+)", self.page.url)
        resolved_id = m.group(1) if m else issue_id

        event_m = re.search(r"sessionEventKey=([^&]+)", self.page.url)
        event_id = event_m.group(1) if event_m else None

        detail = CrashDetail(issue_id=resolved_id, event_id=event_id)

        # Wait a bit and scroll down so lazy-loaded stack trace renders
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self.page.wait_for_timeout(2000)

        # Extract blamed file from header if present
        bf_loc = self.page.locator("[data-test-id='blamedFileWrapper']")
        if await bf_loc.count() > 0:
            bf_text = (await bf_loc.first.inner_text()).strip()
            bf_lines = [l.strip() for l in bf_text.splitlines() if l.strip() and l.strip() != "insert_drive_file"]
            if bf_lines:
                parts = bf_lines[-1].rsplit(":", 1)
                detail.blame_file = parts[0]
                if len(parts) == 2 and parts[1].isdigit():
                    detail.blame_line = int(parts[1])

        # Extract visible stack trace from Crashlytics stack trace component or fallback
        stack_locators = [
            "c9s-stack-trace",
            "[data-test-id*='stack-trace']",
            "[class*='stack-trace']",
            "[class*='stacktrace']",
            "pre",
            "code",
        ]

        raw_stack = ""
        for selector in stack_locators:
            loc = self.page.locator(selector)
            if await loc.count() > 0:
                candidate = (await loc.first.inner_text()).strip()
                if len(candidate) > 30:
                    raw_stack = candidate
                    break

        body_text = await self.page.locator("body").inner_text()

        # Fallback: slice from "Stack trace" or "Fatal Exception" / "Crashed" section in body_text
        if not raw_stack:
            for marker in ["Stack trace", "Fatal Exception:", "Non-fatal Exception:", "Crashed:"]:
                idx = body_text.find(marker)
                if idx != -1:
                    raw_stack = body_text[idx : idx + 6000].strip()
                    break

        detail.stack_trace_raw = raw_stack

        # Parse stack frames
        frames = self._parse_stack_frames(raw_stack)
        detail.frames = frames

        # Update blame frame from parsed frames if header didn't have it
        if not detail.blame_file:
            for frame in frames:
                if frame.is_app_code and frame.file_path:
                    detail.blame_file = frame.file_path
                    detail.blame_line = frame.line_number
                    break

        # Extract metadata from Event summary
        detail.app_version = self._extract_regex(body_text, r"App version:\s*\n?\s*([^\n]+)")
        detail.os_version = self._extract_regex(body_text, r"OS version:\s*\n?\s*([^\n]+)")
        detail.device_model = self._extract_regex(body_text, r"Device:\s*\n?\s*([^\n]+)")

        return detail

    def _parse_stack_frames(self, raw_trace: str) -> list[StackFrame]:
        """Parse raw stack trace text into structured StackFrame objects."""
        frames: list[StackFrame] = []
        if not raw_trace:
            return frames

        lines = raw_trace.splitlines()
        for idx, line in enumerate(lines):
            line_str = line.strip()
            if not line_str:
                continue

            frame = StackFrame(index=idx, raw_text=line_str)

            # Match Dart/Flutter: #0      _MyHomePageState._incrementCounter (package:myapp/main.dart:45:7)
            dart_match = re.search(r"#(\d+)\s+([^\(]+)\s+\((.+?):(\d+)(?::\d+)?\)", line_str)
            if dart_match:
                frame.index = int(dart_match.group(1))
                frame.symbol_name = dart_match.group(2).strip()
                frame.file_path = dart_match.group(3).strip()
                frame.line_number = int(dart_match.group(4))
                frame.is_app_code = not frame.file_path.startswith("dart:") and not frame.file_path.startswith("package:flutter/")
                frames.append(frame)
                continue

            # Match Java/Kotlin: at com.example.MyClass.method(MyClass.java:45)
            java_match = re.search(r"at\s+([a-zA-Z0-9_$.]+)\(([a-zA-Z0-9_$]+(?:\.java|\.kt)):(\d+)\)", line_str)
            if java_match:
                frame.symbol_name = java_match.group(1).strip()
                frame.file_path = java_match.group(2).strip()
                frame.line_number = int(java_match.group(3))
                frame.is_app_code = not frame.symbol_name.startswith("android.") and not frame.symbol_name.startswith("java.")
                frames.append(frame)
                continue

            frames.append(frame)

        return frames

    def _parse_number(self, val_str: str) -> int:
        """Parse number strings like 1.5k, 2M, 42, 1,234 into int."""
        val_str = val_str.lower().strip().replace(",", "")
        try:
            if val_str.endswith("k"):
                return int(float(val_str[:-1]) * 1000)
            elif val_str.endswith("m"):
                return int(float(val_str[:-1]) * 1000000)
            return int(val_str)
        except Exception:
            return 0

    def _extract_regex(self, text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None
