import csv
import fcntl
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Optional

from app.analyzer.source_mapper import extract_file_line_refs
from app.context_collector.base import ChecklistItem, ContextEvent, ContextSource, EventCategory

SUPPORTED_EXTENSIONS = {".json", ".jsonl", ".ndjson", ".csv", ".txt", ".log", ".md"}


_LIST_PREFIX = r"(?:[-*+]|\d+[.)])\s+"
_TASK_MARKER = re.compile(
    rf"^(?:(?:{_LIST_PREFIX})?\[(?P<checkbox>[ xX])\](?:\s+|$)|{_LIST_PREFIX})"
)
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_HR = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_STACK_FRAME = re.compile(r"^(?:#\d+\s|at\s+\S+\(|File \"[^\"]+\", line \d+)")


def _split_task_text(text: str) -> list[tuple[str, int | None]]:
    """Read explicit list items, or one unindented task per line in a plain list."""
    items: list[tuple[list[str], int | None]] = []
    current = None
    fence = ""
    explicit_list = False
    seen_checkbox = False
    group_indent: int | None = None
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if fence:
            if current is not None:
                current.append(line)
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = ""
            continue
        opening = re.match(r"^(`{3,}|~{3,})", stripped)
        if opening:
            fence = opening[1]
            if current is not None:
                current.append(line)
            continue
        if not stripped:
            continue
        if _HEADING.match(stripped) or _HR.match(stripped):
            current = None
            group_indent = None
            continue
        indent = len(line) - len(line.lstrip())
        marker = _TASK_MARKER.match(stripped)
        if marker:
            if group_indent is not None and indent > group_indent:
                if current is not None:
                    current.append(line)
                continue
            group_indent = None
            if marker["checkbox"] is not None and not seen_checkbox:
                items.clear()  # Bullets/prose before the first checkbox are introductory inventory.
                seen_checkbox = True
            if not explicit_list:
                items.clear()  # Text before the first list item is an introduction.
                explicit_list = True
            stripped = stripped[marker.end():].strip()
            if stripped.rstrip("*_` ").endswith(":"):
                group_indent = indent
            if marker["checkbox"] in ("x", "X") or not stripped:
                current = None
                continue
        elif current is not None and (explicit_list or line[:1].isspace() or _STACK_FRAME.match(stripped)):
            current.append(line)
            continue
        elif explicit_list:
            continue
        current = [stripped]
        checklist_line = line_number if marker and marker["checkbox"] == " " else None
        items.append((current, checklist_line))
    return [("\n".join(lines), number) for lines, number in items]


def mark_checklist_done(item: ChecklistItem, body: str) -> None:
    """Recheck the source task under lock and replace only its checkbox byte."""
    with Path(item.path).open("r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        raw = stream.read()
        text = raw.decode("utf-8")
        lines = text.splitlines(keepends=True)
        if item.line > len(lines):
            raise ValueError("Dòng checklist đã bị di chuyển hoặc xóa.")
        line = lines[item.line - 1]
        marker = _TASK_MARKER.match(line.lstrip())
        if not marker or marker["checkbox"] is None:
            raise ValueError("Dòng nguồn không còn là một mục checklist.")
        column = len(line) - len(line.lstrip()) + marker.start("checkbox")
        # Reparse as open to verify both the task body and its Markdown context,
        # including on retries where a previous run already marked the item.
        lines[item.line - 1] = line[:column] + " " + line[column + 1:]
        if (body, item.line) not in _split_task_text("".join(lines)):
            raise ValueError("Nội dung checklist đã thay đổi trong lúc xử lý.")
        if marker["checkbox"] == " ":
            prefix = "".join(lines[:item.line - 1]) + line[:column]
            stream.seek(len(prefix.encode("utf-8")))
            stream.write(b"x")



class FileInputContextSource(ContextSource):
    """Nguồn đầu vào linh hoạt: nhận ĐƯỜNG DẪN TỚI 1 FILE ĐƠN LẺ hoặc 1 THƯ MỤC.

    Hỗ trợ tự động parse:
      - .json (object hoặc list)
      - .jsonl / .ndjson (mỗi dòng 1 JSON object)
      - .csv (mỗi dòng 1 bản ghi)
      - .txt / .md (mỗi dòng hoặc mục danh sách là một task, bỏ qua mục [x] và [X])
      - .log (giữ toàn bộ stack trace trong cùng một sự kiện)
    """

    name = "inbox_json"

    def __init__(
        self,
        inbox_dir: Path | str = "data/inbox",
        *,
        source_name: Optional[str] = None,
        default_project_key: str = "default",
        default_category: str = "other",
    ):
        self.input_path = Path(inbox_dir).expanduser()
        self.inbox_dir = self.input_path  # giữ tương thích ngược
        if source_name:
            self.name = source_name
        self.default_project_key = default_project_key
        self.default_category = default_category

    def _resolve_files(self) -> list[Path]:
        if not self.input_path.exists():
            return []
        if self.input_path.is_file():
            return [self.input_path]
        if self.input_path.is_dir():
            return sorted(
                p for p in self.input_path.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
            )
        return []

    def is_configured(self) -> tuple[bool, str]:
        if not self.input_path.exists():
            return False, f"Đường dẫn '{self.input_path}' chưa tồn tại."
        files = self._resolve_files()
        if not files:
            return True, f"Không tìm thấy file hỗ trợ ({', '.join(sorted(SUPPORTED_EXTENSIONS))}) tại '{self.input_path}'."
        kind = "file" if self.input_path.is_file() else f"{len(files)} file"
        return True, f"ready ({kind})"

    def _parse_category(self, raw: Optional[str]) -> EventCategory:
        val = (raw or self.default_category or "other").strip().lower()
        try:
            return EventCategory(val)
        except ValueError:
            return EventCategory.OTHER

    def _build_event_from_record(self, rec: dict, file_path: Path, idx: int) -> ContextEvent:
        title = str(rec.get("title") or rec.get("summary") or rec.get("error") or f"{file_path.stem} #{idx + 1}").strip()
        source = str(rec.get("source") or self.name)
        project_key = str(rec.get("project_key") or self.default_project_key)
        category = self._parse_category(rec.get("category"))
        body = str(rec.get("body") or rec.get("description") or rec.get("stack_trace") or "")

        blame_file = rec.get("blame_file")
        blame_line = int(rec["blame_line"]) if rec.get("blame_line") not in (None, "") else None
        if not blame_file and body:
            refs = extract_file_line_refs(body)
            if refs:
                blame_file = refs[0].file_name
                blame_line = refs[0].line_number

        event_id = str(
            rec.get("event_id")
            or f"{source}:{hashlib.sha1(f'{file_path.name}:{idx}:{title}'.encode()).hexdigest()[:12]}"
        )
        fp_core = f"{blame_file}:{blame_line}" if blame_file else title.lower()
        fingerprint = str(rec.get("fingerprint") or f"{project_key}::{category.value}::{fp_core}")

        raw_impact = rec.get("impact_count") or rec.get("events_count") or 1
        try:
            impact_count = max(1, int(raw_impact))
        except (TypeError, ValueError):
            impact_count = 1

        return ContextEvent(
            event_id=event_id,
            source=source,
            category=category,
            severity=str(rec.get("severity") or "medium"),
            project_key=project_key,
            title=title,
            body=body,
            fingerprint=fingerprint,
            blame_file=blame_file,
            blame_line=blame_line,
            impact_count=impact_count,
            source_url=rec.get("source_url"),
            metadata={**(rec.get("metadata") or {}), "input_file": str(file_path)},
        )

    def _collect_from_file(self, file_path: Path) -> list[ContextEvent]:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return []

        suffix = file_path.suffix.lower()

        if suffix in (".jsonl", ".ndjson"):
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
            return [self._build_event_from_record(rec, file_path, idx) for idx, rec in enumerate(records)]

        if suffix == ".json":
            parsed = json.loads(text)
            records = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
            return [self._build_event_from_record(rec, file_path, idx) for idx, rec in enumerate(records)]

        if suffix == ".csv":
            reader = csv.DictReader(io.StringIO(text))
            return [self._build_event_from_record(dict(row), file_path, idx) for idx, row in enumerate(reader)]

        if suffix not in {".txt", ".md"}:
            return [self._text_event(text.strip(), file_path, 0)] if text.strip() else []
        events = []
        for idx, (body, number) in enumerate(_split_task_text(text)):
            event = self._text_event(body, file_path, idx)
            if number is not None:
                item = ChecklistItem(path=str(file_path.resolve()), line=number)
                event.checklist_item = item
                # Each checkbox is independently actionable, even for identical titles.
                identity = json.dumps([event.project_key, item.path, number, body])
                digest = hashlib.sha256(identity.encode()).hexdigest()
                event.fingerprint = event.event_id = f"checklist:{digest}"
            events.append(event)
        return events

    def _text_event(self, text: str, file_path: Path, idx: int) -> ContextEvent:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        first_line = lines[0].lstrip("#").strip() if lines else file_path.name
        if first_line.startswith("**") and first_line.endswith("**") and len(first_line) > 4:
            first_line = first_line[2:-2].strip()
        if first_line.endswith(":"):
            first_line = first_line[:-1].strip()
        title = first_line[:140] or file_path.name
        refs = extract_file_line_refs(text)
        blame_file = refs[0].file_name if refs else None
        blame_line = refs[0].line_number if refs else None
        inferred_cat = (
            EventCategory.CRASH.value
            if (refs or any(k in text.lower() for k in ("exception", "fatal", "traceback", "stack trace")))
            else self.default_category
        )
        rec = {
            "title": title,
            "body": text,
            "category": inferred_cat,
            "blame_file": blame_file,
            "blame_line": blame_line,
            "impact_count": 1,
        }
        return self._build_event_from_record(rec, file_path, idx)

    def collect(self) -> list[ContextEvent]:
        events: list[ContextEvent] = []
        for file_path in self._resolve_files():
            events.extend(self._collect_from_file(file_path))
        return events


# Giữ alias tương thích ngược với các chỗ đang import InboxJsonContextSource
InboxJsonContextSource = FileInputContextSource
