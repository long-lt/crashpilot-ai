from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StackFrame(BaseModel):
    index: int = 0
    package_or_binary: Optional[str] = None
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    symbol_name: Optional[str] = None
    is_app_code: bool = False
    raw_text: str = ""


class CrashIssue(BaseModel):
    issue_id: str
    project_key: str
    firebase_project_id: str
    platform: Optional[str] = None
    title: str
    subtitle: Optional[str] = None
    issue_type: str = "fatal"  # fatal, non_fatal, anr
    status: str = "open"       # open, closed, muted
    events_count: int = 0
    users_count: int = 0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    issue_url: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CrashDetail(BaseModel):
    issue_id: str
    event_id: Optional[str] = None
    timestamp: Optional[str] = None
    app_version: Optional[str] = None
    os_version: Optional[str] = None
    device_model: Optional[str] = None
    stack_trace_raw: str = ""
    frames: list[StackFrame] = Field(default_factory=list)
    blame_file: Optional[str] = None
    blame_line: Optional[int] = None
    logs: list[str] = Field(default_factory=list)
    keys: dict[str, str] = Field(default_factory=dict)
