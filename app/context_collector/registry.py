from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

from app.context_collector.base import ContextEvent, ContextSource, EventCluster, SourceRunStatus
from app.context_collector.queue import EventBufferQueue
from app.context_collector.sources import (
    CodeAuditContextSource,
    CrashlyticsContextSource,
    InboxJsonContextSource,
)


class CollectionResult(BaseModel):
    statuses: list[SourceRunStatus] = Field(default_factory=list)
    total_events: int = 0
    clusters: list[EventCluster] = Field(default_factory=list)
    store_path: str = ""


class ContextCollectorManager:
    """Điều phối thu thập ngữ cảnh tự động từ nhiều nguồn linh hoạt -> Buffer & Event Queue."""

    def __init__(self, queue: Optional[EventBufferQueue] = None):
        self._sources: dict[str, ContextSource] = {}
        self.queue = queue or EventBufferQueue()

    def register(self, source: ContextSource) -> None:
        key = source.name
        if hasattr(source, "input_path"):
            key = f"{source.name} [{source.default_project_key}: {source.input_path}]"
        if key in self._sources:
            raise ValueError(f"Nguồn đã đăng ký: {key}")
        self._sources[key] = source

    def list_sources(self) -> list[str]:
        return list(self._sources.keys())

    def collect_all(self, only_sources: Optional[list[str]] = None, *, project_key: str | None = None) -> CollectionResult:
        selected = list(self._sources)
        if only_sources:
            selected = []
            for selector in only_sources:
                matches = [key for key, source in self._sources.items() if selector in (key, source.name)]
                selected.extend(matches or [selector])
            selected = list(dict.fromkeys(selected))
        all_events: list[ContextEvent] = []
        statuses: list[SourceRunStatus] = []

        for name in selected:
            source = self._sources.get(name)
            if not source:
                statuses.append(SourceRunStatus(source=name, status="error", message=f"Nguồn '{name}' chưa được đăng ký."))
                continue

            try:
                ready, reason = source.is_configured()
                if not ready:
                    statuses.append(SourceRunStatus(source=name, status="unconfigured", message=reason))
                    continue
                events = source.collect()
                for event in events:
                    event.collector_key = str(getattr(source, "input_path", name))
                all_events.extend(events)
                statuses.append(
                    SourceRunStatus(
                        source=name,
                        status="ok",
                        events_collected=len(events),
                        message=f"Thu thập thành công {len(events)} sự kiện.",
                    )
                )
            except Exception as exc:
                statuses.append(SourceRunStatus(source=name, status="error", message=str(exc)))

        if project_key:
            all_events = [event for event in all_events if event.project_key == project_key]
        clusters = self.queue.cluster_events(all_events)
        successful = {str(getattr(self._sources[st.source], "input_path", st.source))
                      for st in statuses if st.status == "ok"}
        unavailable = {str(getattr(self._sources[st.source], "input_path", st.source)): st
                       for st in statuses if st.status == "unconfigured"}
        incomplete = any(st.status != "ok" for st in statuses)
        with self.queue.updating():
            retained = []
            for old in self.queue.load():
                for event in old.events:
                    if project_key and event.project_key != project_key:
                        retained.append(event)
                        continue
                    origin = event.collector_key or event.source
                    if not event.collector_key and "input_file" in event.metadata:
                        path = Path(event.metadata["input_file"])
                        origin = next((key for key in (*successful, *unavailable) if Path(key) in (path, path.parent)), origin)
                    if (incomplete or only_sources) and origin not in successful:
                        retained.append(event)
                        if origin in unavailable:
                            unavailable[origin].status = "error"
            saved_path = self.queue.save(self.queue.cluster_events(retained + all_events))
        # Retained events stay visible, but stale data is never dispatched for execution.
        return CollectionResult(
            statuses=statuses,
            total_events=len(all_events),
            clusters=clusters,
            store_path=str(saved_path),
        )


def create_default_collector(store_path: Path | str = "data/context_queue/clusters.json") -> ContextCollectorManager:
    """Khởi tạo bộ thu thập mặc định gồm: Crashlytics, Code Audit, và Inbox JSON đa năng."""
    manager = ContextCollectorManager(queue=EventBufferQueue(store_path=store_path))
    from app.config import load_projects, project_sources
    crash_projects = {p["key"] for p in load_projects()
                      if any(s.get("type", "").lower() == "crashlytics" for s in project_sources(p))}
    manager.register(CrashlyticsContextSource(project_keys=crash_projects))
    manager.register(CodeAuditContextSource())
    manager.register(InboxJsonContextSource())
    return manager
