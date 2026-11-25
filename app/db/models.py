from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class ProjectModel(Base):
    __tablename__ = "projects"

    key = Column(String(100), primary_key=True)
    name = Column(String(200), nullable=False)
    firebase_project_id = Column(String(150), nullable=False)
    console_url = Column(String(500), nullable=False)
    repository_path = Column(String(500), nullable=True)
    platform = Column(String(50), nullable=True)
    enabled = Column(Boolean, default=True)
    last_synced_at = Column(DateTime, nullable=True)

    issues = relationship("IssueModel", back_populates="project", cascade="all, delete-orphan")


class IssueModel(Base):
    __tablename__ = "issues"

    issue_id = Column(String(150), primary_key=True)
    project_key = Column(String(100), ForeignKey("projects.key"), nullable=False, primary_key=True)
    firebase_project_id = Column(String(150), nullable=False)
    platform = Column(String(50), nullable=True)
    title = Column(String(500), nullable=False)
    subtitle = Column(Text, nullable=True)
    issue_type = Column(String(50), default="fatal")
    status = Column(String(50), default="open")
    events_count = Column(Integer, default=0)
    users_count = Column(Integer, default=0)
    first_seen = Column(String(100), nullable=True)
    last_seen = Column(String(100), nullable=True)
    issue_url = Column(String(1000), nullable=False)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    project = relationship("ProjectModel", back_populates="issues")
    details = relationship("CrashDetailModel", back_populates="issue", cascade="all, delete-orphan")


class CrashDetailModel(Base):
    __tablename__ = "crash_details"
    __table_args__ = (
        ForeignKeyConstraint(
            ["issue_id", "project_key"],
            ["issues.issue_id", "issues.project_key"],
            ondelete="CASCADE",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_id = Column(String(150), nullable=False)
    project_key = Column(String(100), nullable=False)
    event_id = Column(String(150), nullable=True)
    timestamp = Column(String(100), nullable=True)
    app_version = Column(String(100), nullable=True)
    os_version = Column(String(100), nullable=True)
    device_model = Column(String(200), nullable=True)
    stack_trace_raw = Column(Text, nullable=True)
    frames_json = Column(Text, nullable=True)
    blame_file = Column(String(500), nullable=True)
    blame_line = Column(Integer, nullable=True)
    logs_json = Column(Text, nullable=True)
    keys_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utc_now)

    issue = relationship("IssueModel", back_populates="details")


class TaskModel(Base):
    """Thực thể Công việc (Task) bền vững do Giai đoạn 2 (Chief of Staff) tạo & cập nhật."""

    __tablename__ = "tasks"

    task_id = Column(String(64), primary_key=True)  # VD: TASK-1001
    project_key = Column(String(100), nullable=False, index=True)
    cluster_id = Column(String(150), nullable=False, index=True)
    dedup_key = Column(String(300), nullable=False, unique=True)
    title = Column(String(500), nullable=False)
    summary = Column(Text, nullable=True)
    category = Column(String(50), default="other")
    priority = Column(String(20), default="medium")  # high | medium | low
    priority_manual = Column(Boolean, default=False)
    specialist = Column(String(50), default="bug_fix")  # bug_fix | performance | code_gardening | experimental
    status = Column(String(40), default="ready")  # ready | running | verifying | completed | preview | failed | skipped
    raw_status = Column(String(80), default="READY")
    sources_json = Column(Text, default="[]")
    blame_file = Column(String(500), nullable=True)
    blame_line = Column(Integer, nullable=True)
    events_count = Column(Integer, default=0)
    users_count = Column(Integer, default=0)
    impact_score = Column(Integer, default=0)
    latest_run_id = Column(String(200), nullable=True)
    verified_pr_url = Column(String(1000), nullable=True)
    last_orchestrated_at = Column(DateTime, default=utc_now)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class DiscoveryCycleModel(Base):
    """Nhật ký chu kỳ Discovery & Task Planning (Giai đoạn 1 + 2)."""

    __tablename__ = "discovery_cycles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_key = Column(String(100), nullable=False, index=True)
    sources_json = Column(Text, default="[]")
    events_collected = Column(Integer, default=0)
    clusters_detected = Column(Integer, default=0)
    tasks_created = Column(Integer, default=0)
    tasks_updated = Column(Integer, default=0)
    status = Column(String(40), default="ok")
    created_at = Column(DateTime, default=utc_now)


class RuleProposalModel(Base):
    """Đề xuất luật mới ở Giai đoạn 5 (liên kết Source Task & Source Run)."""

    __tablename__ = "rule_proposals"

    proposal_id = Column(String(120), primary_key=True)  # VD: PROP-1
    rule_key = Column(String(150), nullable=False, index=True)
    project_key = Column(String(100), nullable=True, index=True)
    source_task_id = Column(String(64), nullable=True)
    source_run_id = Column(String(200), nullable=True)
    source_runs_json = Column(Text, default="[]")
    rule_type = Column(String(60), nullable=False)
    target_stage = Column(String(60), nullable=False)
    scope = Column(String(40), default="global")  # global | project
    payload_json = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)
    status = Column(String(40), default="pending_review")  # pending_review | approved | rejected
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class RuleChangeHistoryModel(Base):
    """Lịch sử thay đổi Environment Rules kèm snapshot YAML để Rollback 1 chạm."""

    __tablename__ = "rule_change_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_key = Column(String(100), nullable=True, index=True)
    action = Column(String(60), nullable=False)  # approve_proposal | manual_add | delete_rule | rollback
    rule_type = Column(String(60), nullable=True)
    rule_id = Column(String(200), nullable=True)
    source_proposal_id = Column(String(120), nullable=True)
    source_task_id = Column(String(64), nullable=True)
    source_run_id = Column(String(200), nullable=True)
    summary = Column(String(500), nullable=False)
    before_yaml = Column(Text, nullable=False)
    after_yaml = Column(Text, nullable=False)
    rolled_back = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utc_now)


