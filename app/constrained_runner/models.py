from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentTrack(str, Enum):
    BUG_FIX = "bug_fix"
    PERFORMANCE = "performance"
    CODE_GARDENING = "code_gardening"
    EXPERIMENTAL = "experimental"


class StepStatus(str, Enum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class RuntimeVerifierConfig(BaseModel):
    """Cấu hình Giai đoạn 4 — Verifier chạy app tự động (Web CDP/Playwright hoặc Flutter Mobile)."""

    enabled: bool = False
    mode: str = "web_cdp"  # "web_cdp" | "flutter_mobile"
    # Cho mode web_cdp:
    url: Optional[str] = None
    start_command: Optional[list[str]] = None
    startup_timeout_sec: int = 30
    fuzz_clicks: int = 8
    # Cho mode flutter_mobile:
    device_id: Optional[str] = None
    test_target: str = "integration_test"
    android_package: Optional[str] = None
    monkey_events: int = 0
    timeout_sec: int = 180


class ConstraintConfig(BaseModel):
    """Các ràng buộc cơ học của môi trường thực thi (Inner Loop Guardrails)."""

    repository_path: str
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master", "production"])
    forbidden_path_patterns: list[str] = Field(
        default_factory=lambda: [".env", "browser_profiles/", ".pem", ".key", ".keystore", "secrets"]
    )
    mechanical_checks: list[list[str]] = Field(default_factory=list)
    verify_commands: list[list[str]] = Field(default_factory=list)
    runtime_verifier: Optional[RuntimeVerifierConfig] = None
    executor_command: Optional[list[str]] = None
    verifier_command: Optional[list[str]] = None
    max_retries: int = 1
    check_timeout_sec: int = 120
    dry_run: bool = True
    base_branch: str = "main"
    auto_create_pr: bool = False
    auto_merge: bool = False


class StepResult(BaseModel):
    step_number: int
    name: str
    status: StepStatus
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class PipelineRunReport(BaseModel):
    run_id: str
    cluster_id: str
    task_id: Optional[str] = None
    project_key: str
    cluster_impact: Optional[int] = None
    track: AgentTrack
    repository_path: str
    branch_name: Optional[str] = None
    pr_url: Optional[str] = None
    dry_run: bool = True
    overall_status: str  # RUNNING, VERIFIED_READY_FOR_PR, VERIFIED_NO_CHANGES, BLOCKED, FAILED
    current_step: Optional[int] = None
    current_step_name: Optional[str] = None
    steps: list[StepResult] = Field(default_factory=list)
    verification_rounds: list[dict[str, Any]] = Field(default_factory=list)
    artifacts_dir: str
    created_at: datetime = Field(default_factory=utc_now)


COMPLETED_RUN_STATUSES = {"VERIFIED_READY_FOR_PR", "VERIFIED_NO_CHANGES", "PUSHED_BRANCH", "PR_CREATED", "VERIFIED_READY_FOR_REVIEW", "VERIFIED_MERGED", "AUTO_MERGED", "COMPLETED"}
FAILED_RUN_STATUSES = {"INTERRUPTED", "PR_CLOSED", "PR_MERGE_FAILED", "VERIFICATION_INSUFFICIENT", "FAILED", "BLOCKED", "VERIFICATION_FAILED", "PR_CREATE_FAILED", "CHECKLIST_UPDATE_FAILED", "ERROR"}
_RUNNING_RUN_STATUSES = {"RUNNING", "IN_PROGRESS"}


def classify_run_status(status: str) -> str:
    st = (status or "").upper()
    if st == "DRY_RUN_VERIFIED":
        return "preview"
    if st in COMPLETED_RUN_STATUSES:
        return "completed"
    if st in _RUNNING_RUN_STATUSES:
        return "running"
    if st in FAILED_RUN_STATUSES:
        return "failed"
    return "pending"
