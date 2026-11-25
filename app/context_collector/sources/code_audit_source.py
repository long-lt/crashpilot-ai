import hashlib
import re
from pathlib import Path

from app.config import load_projects
from app.context_collector.base import ContextEvent, ContextSource, EventCategory
from app.hardening import load_environment_rules

IGNORED_DIRS = {".git", "build", ".dart_tool", "node_modules", ".venv", "__pycache__", "Pods", ".gradle"}

# Các mẫu footgun / tech debt cơ học cho Flutter/Dart và Python/JS
AUDIT_PATTERNS: list[tuple[str, str, re.Pattern[str], set[str]]] = [
    ("todo_fixme", "low", re.compile(r"\b(FIXME|HACK|XXX)\b"), {".dart", ".py", ".ts", ".tsx", ".js", ".kt", ".java", ".swift"}),
    ("empty_catch", "medium", re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}"), {".dart", ".ts", ".tsx", ".js", ".kt", ".java"}),
    ("bare_except", "medium", re.compile(r"^\s*except\s*:\s*$"), {".py"}),
]


class CodeAuditContextSource(ContextSource):
    """Cron quét code local tìm footguns / tech debt thật trên các repository đã cấu hình."""

    name = "code_audit"

    def __init__(
        self,
        repo_overrides: dict[str, str] | None = None,
        max_findings_per_repo: int = 50,
        env_rules_path: Path | str | None = None,
    ):
        self.repo_overrides = repo_overrides
        self.max_findings_per_repo = max_findings_per_repo
        self.env_rules_path = env_rules_path

    def _effective_patterns(self, project_key: str | None = None) -> list[tuple[str, str, re.Pattern[str], set[str]]]:
        patterns = list(AUDIT_PATTERNS)
        env_rules = (
            load_environment_rules(self.env_rules_path)
            if self.env_rules_path
            else load_environment_rules()
        )
        for r in env_rules.custom_audit_rules:
            if getattr(r, "project_key", None) and project_key and r.project_key != project_key:
                continue
            try:
                patterns.append((r.id, r.severity, re.compile(r.pattern), {e.lower() for e in r.extensions}))
            except re.error:
                continue
        return patterns

    def _resolve_repos(self) -> list[tuple[str, Path]]:
        repos: list[tuple[str, Path]] = []
        if self.repo_overrides is not None:
            items = [(k, v) for k, v in self.repo_overrides.items()]
        else:
            items = [(p["key"], p.get("repository_path", "")) for p in load_projects()]

        for key, raw_path in items:
            if not raw_path:
                continue
            p = Path(raw_path).expanduser()
            if p.exists() and p.is_dir():
                repos.append((key, p))
        return repos

    def is_configured(self) -> tuple[bool, str]:
        repos = self._resolve_repos()
        if not repos:
            return False, "Chưa có repository_path nào tồn tại trên máy trong config/projects.yaml."
        return True, f"ready ({len(repos)} repo)"

    def collect(self) -> list[ContextEvent]:
        events: list[ContextEvent] = []
        for project_key, repo_root in self._resolve_repos():
            all_patterns = self._effective_patterns(project_key=project_key)
            found = 0
            for file_path in repo_root.rglob("*"):
                if found >= self.max_findings_per_repo:
                    break
                if any(part in IGNORED_DIRS for part in file_path.parts) or not file_path.is_file():
                    continue
                suffix = file_path.suffix.lower()
                active_rules = [r for r in all_patterns if suffix in r[3]]
                if not active_rules:
                    continue

                rel_path = str(file_path.relative_to(repo_root))
                try:
                    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue

                for line_no, line in enumerate(lines, start=1):
                    for rule_id, severity, pattern, _ in active_rules:
                        if pattern.search(line):
                            eid_hash = hashlib.sha1(f"{project_key}:{rel_path}:{line_no}:{rule_id}".encode()).hexdigest()[:12]
                            events.append(
                                ContextEvent(
                                    event_id=f"code_audit:{eid_hash}",
                                    source=self.name,
                                    category=EventCategory.TECH_DEBT,
                                    severity=severity,
                                    project_key=project_key,
                                    title=f"[{rule_id}] {rel_path}:{line_no}",
                                    body=line.strip(),
                                    fingerprint=f"{project_key}::tech_debt::{rule_id}::{rel_path}",
                                    blame_file=rel_path,
                                    blame_line=line_no,
                                    impact_count=1,
                                    metadata={"rule": rule_id, "repo_root": str(repo_root)},
                                )
                            )
                            found += 1
                            if found >= self.max_findings_per_repo:
                                break
                    if found >= self.max_findings_per_repo:
                        break
        return events
