import json
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session

from app.db.models import CrashDetailModel, IssueModel, ProjectModel
from app.models.issue import CrashDetail, CrashIssue


def utc_now():
    return datetime.now(timezone.utc)


def sync_projects(db: Session, projects_config: list[dict]) -> None:
    """Sync projects from YAML into database."""
    for p in projects_config:
        existing = db.query(ProjectModel).filter(ProjectModel.key == p["key"]).first()
        if existing:
            existing.name = p.get("name", existing.name)
            existing.firebase_project_id = p.get("firebase_project_id", existing.firebase_project_id)
            existing.console_url = p.get("console_url", existing.console_url)
            existing.repository_path = p.get("repository_path", existing.repository_path)
            existing.platform = p.get("platform", existing.platform)
            existing.enabled = p.get("enabled", True)
        else:
            db_proj = ProjectModel(
                key=p["key"],
                name=p.get("name", p["key"]),
                firebase_project_id=p.get("firebase_project_id", ""),
                console_url=p.get("console_url", ""),
                repository_path=p.get("repository_path"),
                platform=p.get("platform"),
                enabled=p.get("enabled", True),
            )
            db.add(db_proj)
    db.commit()


def _is_temp_row_id(issue_id: str) -> bool:
    return issue_id.startswith("row_")


def _norm(text: Optional[str]) -> str:
    return (text or "").strip().lower()


def _subtitle_matches(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return True
    return na == nb or na[:30] == nb[:30]


def save_or_update_issue(db: Session, issue: CrashIssue) -> IssueModel:
    """Upsert an issue from Crashlytics, preventing duplicates between temp row_ IDs and real Firebase IDs."""
    existing = (
        db.query(IssueModel)
        .filter(IssueModel.issue_id == issue.issue_id, IssueModel.project_key == issue.project_key)
        .first()
    )

    same_proj = (
        db.query(IssueModel)
        .filter(IssueModel.project_key == issue.project_key, IssueModel.title == issue.title)
        .all()
    )

    if not _is_temp_row_id(issue.issue_id):
        # Đang lưu Firebase ID thật -> luôn xóa mọi bản ghi ID tạm `row_...` cũ cùng project + title
        for stale in same_proj:
            if _is_temp_row_id(stale.issue_id) and _subtitle_matches(stale.subtitle, issue.subtitle):
                db.delete(stale)
        db.flush()
    elif not existing:
        # Đang lưu ID tạm `row_...` -> nếu đã có bản ghi Firebase ID thật tương ứng thì cập nhật vào nó
        real_match = next(
            (r for r in same_proj if not _is_temp_row_id(r.issue_id) and _subtitle_matches(r.subtitle, issue.subtitle)),
            None,
        )
        if real_match:
            real_match.events_count = issue.events_count
            real_match.users_count = issue.users_count
            real_match.updated_at = utc_now()
            db.commit()
            db.refresh(real_match)
            return real_match

    if existing:
        existing.title = issue.title
        existing.subtitle = issue.subtitle
        existing.issue_type = issue.issue_type
        existing.status = issue.status
        existing.events_count = issue.events_count
        existing.users_count = issue.users_count
        existing.first_seen = issue.first_seen or existing.first_seen
        existing.last_seen = issue.last_seen or existing.last_seen
        existing.issue_url = issue.issue_url
        existing.updated_at = utc_now()
        db.commit()
        db.refresh(existing)
        return existing
    else:
        new_issue = IssueModel(
            issue_id=issue.issue_id,
            project_key=issue.project_key,
            firebase_project_id=issue.firebase_project_id,
            platform=issue.platform,
            title=issue.title,
            subtitle=issue.subtitle,
            issue_type=issue.issue_type,
            status=issue.status,
            events_count=issue.events_count,
            users_count=issue.users_count,
            first_seen=issue.first_seen,
            last_seen=issue.last_seen,
            issue_url=issue.issue_url,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.add(new_issue)
        db.commit()
        db.refresh(new_issue)
        return new_issue


def save_crash_detail(db: Session, project_key: str, detail: CrashDetail) -> CrashDetailModel:
    """Upsert crash detail without inserting duplicate rows for the same event_id or stack trace."""
    frames_str = json.dumps([f.model_dump() for f in detail.frames], ensure_ascii=False)
    logs_str = json.dumps(detail.logs, ensure_ascii=False)
    keys_str = json.dumps(detail.keys, ensure_ascii=False)

    existing = None
    if detail.event_id:
        existing = (
            db.query(CrashDetailModel)
            .filter(
                CrashDetailModel.issue_id == detail.issue_id,
                CrashDetailModel.project_key == project_key,
                CrashDetailModel.event_id == detail.event_id,
            )
            .first()
        )
    if not existing and detail.stack_trace_raw:
        existing = (
            db.query(CrashDetailModel)
            .filter(
                CrashDetailModel.issue_id == detail.issue_id,
                CrashDetailModel.project_key == project_key,
                CrashDetailModel.stack_trace_raw == detail.stack_trace_raw,
            )
            .first()
        )

    if existing:
        existing.timestamp = detail.timestamp or existing.timestamp
        existing.app_version = detail.app_version or existing.app_version
        existing.os_version = detail.os_version or existing.os_version
        existing.device_model = detail.device_model or existing.device_model
        existing.stack_trace_raw = detail.stack_trace_raw or existing.stack_trace_raw
        existing.frames_json = frames_str
        existing.blame_file = detail.blame_file or existing.blame_file
        existing.blame_line = detail.blame_line or existing.blame_line
        existing.logs_json = logs_str
        existing.keys_json = keys_str
        db.commit()
        db.refresh(existing)
        return existing

    model = CrashDetailModel(
        issue_id=detail.issue_id,
        project_key=project_key,
        event_id=detail.event_id,
        timestamp=detail.timestamp,
        app_version=detail.app_version,
        os_version=detail.os_version,
        device_model=detail.device_model,
        stack_trace_raw=detail.stack_trace_raw,
        frames_json=frames_str,
        blame_file=detail.blame_file,
        blame_line=detail.blame_line,
        logs_json=logs_str,
        keys_json=keys_str,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def get_all_issues(
    db: Session,
    project_key: Optional[str] = None,
    platform: Optional[str] = None,
) -> list[IssueModel]:
    query = db.query(IssueModel)
    if project_key:
        query = query.filter(IssueModel.project_key == project_key)
    if platform and platform.strip().lower() not in {"all", ""}:
        query = query.filter(IssueModel.platform == platform.strip().lower())
    return query.order_by(IssueModel.events_count.desc()).all()


def update_project_sync_time(db: Session, project_key: str) -> None:
    proj = db.query(ProjectModel).filter(ProjectModel.key == project_key).first()
    if proj:
        proj.last_synced_at = utc_now()
        db.commit()
