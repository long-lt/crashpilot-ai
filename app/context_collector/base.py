from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventCategory(str, Enum):
    CRASH = "crash"
    BUG = "bug"
    PERFORMANCE = "performance"
    TECH_DEBT = "tech_debt"
    FEEDBACK = "feedback"
    OTHER = "other"


class ChecklistItem(BaseModel):
    path: str
    line: int = Field(ge=1)


class ContextEvent(BaseModel):
    """Định dạng sự kiện ngữ cảnh thống nhất từ mọi nguồn đầu vào."""

    event_id: str
    source: str  # vd: crashlytics, code_audit, inbox_json, slack, linear, x_social
    category: EventCategory = EventCategory.OTHER
    severity: str = "medium"  # critical, high, medium, low
    project_key: str = "default"
    title: str
    body: str = ""
    fingerprint: str = ""  # Dùng để gom cụm nguyên nhân gốc
    blame_file: Optional[str] = None
    blame_line: Optional[int] = None
    impact_count: int = 1  # Số events / số user báo cáo
    source_url: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    checklist_item: Optional[ChecklistItem] = None
    collector_key: Optional[str] = None
    collected_at: datetime = Field(default_factory=utc_now)


class SourceRunStatus(BaseModel):
    source: str
    status: str  # ok, unconfigured, error
    events_collected: int = 0
    message: str = ""


class EventCluster(BaseModel):
    """Nhóm các ContextEvent có cùng nguyên nhân gốc (Buffer & Event Queue output)."""

    cluster_id: str
    root_cause_key: str
    category: EventCategory
    project_key: str
    title: str
    total_impact: int = 0
    event_count: int = 0
    sources: list[str] = Field(default_factory=list)
    blame_file: Optional[str] = None
    blame_line: Optional[int] = None
    events: list[ContextEvent] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


class ContextSource(ABC):
    """Interface mở rộng cho bất kỳ nguồn thu thập ngữ cảnh nào."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> tuple[bool, str]:
        """Kiểm tra nguồn có sẵn sàng không. Trả về (ready, reason)."""

    @abstractmethod
    def collect(self) -> list[ContextEvent]:
        """Thu thập dữ liệu thật từ nguồn và chuẩn hóa thành list[ContextEvent]."""
