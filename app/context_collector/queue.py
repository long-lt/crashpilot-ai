import fcntl
import hashlib
import tempfile
from contextlib import contextmanager
import json
from pathlib import Path
from typing import Optional

from app.context_collector.base import ContextEvent, EventCluster


class EventBufferQueue:
    """Buffer & Event Queue: gom cụm ContextEvent từ nhiều nguồn theo nguyên nhân gốc."""

    def __init__(self, store_path: Path | str = "data/context_queue/clusters.json"):
        self.store_path = Path(store_path)

    @staticmethod
    def scoped_id(project: str, fingerprint: str) -> str:
        identity = json.dumps([project, fingerprint])
        return "cls_" + hashlib.sha1(identity.encode()).hexdigest()[:12]

    @staticmethod
    def history_aliases(clusters: list[EventCluster]) -> dict[tuple[str, str], str]:
        """Translate pre-scope report IDs only within their recorded project."""
        return {(c.project_key, "cls_" + hashlib.sha1(c.root_cause_key.encode()).hexdigest()[:12]): c.cluster_id
                for c in clusters}

    def cluster_events(self, events: list[ContextEvent]) -> list[EventCluster]:
        grouped: dict[tuple[str, str], list[ContextEvent]] = {}
        for ev in events:
            key = ev.fingerprint or f"{ev.project_key}::{ev.category.value}::{ev.title.strip().lower()}"
            grouped.setdefault((ev.project_key, key), []).append(ev)

        clusters: list[EventCluster] = []
        for (project, root_key), group in grouped.items():
            # Loại trùng event_id nếu chạy thu thập nhiều lần
            dedup: dict[str, ContextEvent] = {e.event_id: e for e in group}
            unique_events = list(dedup.values())

            primary = max(unique_events, key=lambda e: e.impact_count)
            cluster_id = self.scoped_id(project, root_key)
            sources = sorted({e.source for e in unique_events})
            total_impact = sum(e.impact_count for e in unique_events)

            clusters.append(
                EventCluster(
                    cluster_id=cluster_id,
                    root_cause_key=root_key,
                    category=primary.category,
                    project_key=primary.project_key,
                    title=primary.title,
                    total_impact=total_impact,
                    event_count=len(unique_events),
                    sources=sources,
                    blame_file=primary.blame_file,
                    blame_line=primary.blame_line,
                    events=unique_events,
                )
            )

        clusters.sort(key=lambda c: (c.total_impact, c.event_count), reverse=True)
        return clusters

    @contextmanager
    def updating(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        with self.store_path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def save(self, clusters: list[EventCluster]) -> Path:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [c.model_dump(mode="json") for c in clusters]
        with tempfile.NamedTemporaryFile(mode="w", dir=self.store_path.parent, encoding="utf-8", delete=False) as tmp:
            json.dump(payload, tmp, ensure_ascii=False)
        Path(tmp.name).replace(self.store_path)
        return self.store_path

    def load(self) -> list[EventCluster]:
        if not self.store_path.exists():
            return []
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        clusters = [EventCluster.model_validate(item) for item in raw]
        result = []
        for cluster in clusters:
            if any(e.project_key != cluster.project_key for e in cluster.events):
                result.extend(self.cluster_events(cluster.events))
            else:
                cluster.cluster_id = self.scoped_id(cluster.project_key, cluster.root_cause_key)
                result.append(cluster)
        return result

    def get_cluster(self, cluster_id: str) -> Optional[EventCluster]:
        clusters = self.load()
        for cluster in clusters:
            if cluster.cluster_id == cluster_id:
                return cluster
        aliases = self.history_aliases(clusters)
        legacy = [c for c in clusters if aliases.get((c.project_key, cluster_id)) == c.cluster_id]
        return legacy[0] if len(legacy) == 1 else None
