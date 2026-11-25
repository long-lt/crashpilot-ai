from pathlib import Path
from sqlalchemy.orm import selectinload
from app.context_collector.base import ContextEvent, ContextSource, EventCategory
from app.db.database import DATABASE_FILE, SessionLocal, init_db
from app.db.models import IssueModel


class CrashlyticsContextSource(ContextSource):
    """Nguồn dữ liệu Crashlytics thật từ SQLite local (data/crashpilot.db)."""

    name = "crashlytics"

    def __init__(self, db_path: Path | None = None, project_keys: set[str] | None = None):
        self.project_keys = project_keys
        self.db_path = db_path or DATABASE_FILE

    def is_configured(self) -> tuple[bool, str]:
        if not self.db_path.exists():
            return False, f"Chưa có database {self.db_path}. Chạy './run.sh sync' trước."
        return True, "ready"

    def collect(self) -> list[ContextEvent]:
        init_db()
        db = SessionLocal()
        events: list[ContextEvent] = []
        try:
            query = db.query(IssueModel).options(selectinload(IssueModel.details))
            if self.project_keys is not None:
                query = query.filter(IssueModel.project_key.in_(self.project_keys))
            issues = query.order_by(IssueModel.events_count.desc()).all()
            for issue in issues:
                detail = issue.details[-1] if issue.details else None
                blame_file = detail.blame_file if detail else None
                blame_line = detail.blame_line if detail else None

                severity = "critical" if issue.events_count >= 10 else ("high" if issue.issue_type == "fatal" else "medium")
                # Fingerprint gom theo blame_file:line nếu có, ngược lại theo title
                fp_core = f"{blame_file}:{blame_line}" if blame_file else issue.title.strip().lower()
                fingerprint = f"{issue.project_key}::crash::{fp_core}"

                body_parts = []
                if issue.subtitle:
                    body_parts.append(issue.subtitle)
                if detail and detail.stack_trace_raw:
                    body_parts.append(detail.stack_trace_raw)

                events.append(
                    ContextEvent(
                        event_id=f"crashlytics:{issue.project_key}:{issue.issue_id}",
                        source=self.name,
                        category=EventCategory.CRASH,
                        severity=severity,
                        project_key=issue.project_key,
                        title=issue.title,
                        body="\n\n".join(body_parts),
                        fingerprint=fingerprint,
                        blame_file=blame_file,
                        blame_line=blame_line,
                        impact_count=max(1, issue.events_count or 1),
                        source_url=issue.issue_url,
                        metadata={
                            "firebase_issue_id": issue.issue_id,
                            "firebase_project_id": issue.firebase_project_id,
                            "platform": issue.platform,
                            "events_count": issue.events_count,
                            "users_count": issue.users_count,
                            "app_version": detail.app_version if detail else None,
                            "os_version": detail.os_version if detail else None,
                        },
                    )
                )
        finally:
            db.close()
        return events
