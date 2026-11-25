import re
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


IGNORED_DIRS = {".git", "build", ".dart_tool", "node_modules", ".venv", "__pycache__", "Pods", ".gradle"}


class SourceLocation(BaseModel):
    file_name: str
    line_number: int
    symbol: Optional[str] = None
    resolved_path: Optional[str] = None
    snippet: Optional[str] = None


class DiagnosisReport(BaseModel):
    issue_id: str
    title: str
    subtitle: Optional[str] = None
    repository_path: Optional[str] = None
    repository_available: bool = False
    locations: list[SourceLocation] = Field(default_factory=list)
    markdown_report: str = ""


def extract_file_line_refs(stack_trace: str, blame_file: Optional[str] = None, blame_line: Optional[int] = None) -> list[SourceLocation]:
    """Extract (file_name, line_number) pairs from Firebase Crashlytics stack trace formats."""
    locations: list[SourceLocation] = []
    seen: set[tuple[str, int]] = set()

    if blame_file and blame_line:
        fname = blame_file.split("/")[-1]
        key = (fname, blame_line)
        seen.add(key)
        locations.append(SourceLocation(file_name=fname, line_number=blame_line, symbol="blame_frame"))

    # Matches patterns like:
    # UploadFilesService.uploadFiles (upload_files_services.dart:45)
    # #0 Foo.bar (package:app/foo.dart:123:7)
    # at com.example.Foo.bar(Foo.java:37)
    pattern = re.compile(
        r"(?:([a-zA-Z0-9_$.<>]+)\s*)?\((?:[^()\s]*?/)?([a-zA-Z0-9_.-]+\.(?:dart|java|kt|swift|m|mm|h|c|cpp|ts|js|py)):(\d+)(?::\d+)?\)"
    )

    for line in (stack_trace or "").splitlines():
        for m in pattern.finditer(line):
            symbol = m.group(1)
            fname = m.group(2)
            line_no = int(m.group(3))
            key = (fname, line_no)
            if key not in seen:
                seen.add(key)
                locations.append(SourceLocation(file_name=fname, line_number=line_no, symbol=symbol))

    return locations


def find_file_in_repo(repo_root: Path, file_name: str) -> Optional[Path]:
    """Find a source file inside a local repository, skipping build/vendor directories."""
    if not repo_root.exists() or not repo_root.is_dir():
        return None

    if not re.fullmatch(r"[\w.-]+", file_name) or file_name in {".", ".."}:
        return None
    match = None
    root = repo_root.resolve()
    for path in root.rglob(file_name):
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.resolve().is_relative_to(root):
            if match is not None:
                return None
            match = path
    return match


def read_code_snippet(file_path: Path, target_line: int, context_lines: int = 12) -> str:
    """Read lines around target_line with line numbers and a >>> marker on the crashing line."""
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, target_line - context_lines)
    end = min(len(lines), target_line + context_lines)

    out = []
    for num in range(start, end + 1):
        marker = ">>>" if num == target_line else "   "
        out.append(f"{marker} {num:5d} | {lines[num - 1]}")
    return "\n".join(out)


def build_diagnosis_report(
    issue_id: str,
    title: str,
    subtitle: Optional[str],
    stack_trace: str,
    blame_file: Optional[str] = None,
    blame_line: Optional[int] = None,
    repository_path: Optional[str] = None,
    app_version: Optional[str] = None,
    os_version: Optional[str] = None,
) -> DiagnosisReport:
    """Build a structured crash diagnosis report, mapping stack frames to local source code if available."""
    repo_root = Path(repository_path).expanduser() if repository_path else None
    repo_available = bool(repo_root and repo_root.exists() and repo_root.is_dir())

    locations = extract_file_line_refs(stack_trace, blame_file=blame_file, blame_line=blame_line)

    if repo_available and repo_root:
        for loc in locations:
            matched = find_file_in_repo(repo_root, loc.file_name)
            if matched:
                loc.resolved_path = str(matched)
                loc.snippet = read_code_snippet(matched, loc.line_number)

    # Render Markdown diagnosis context
    md_lines = [
        f"# Crash Diagnosis Context — `{issue_id}`",
        "",
        f"- **Title**: `{title}`",
    ]
    if subtitle:
        md_lines.append(f"- **Subtitle / Exception**: {subtitle}")
    if app_version:
        md_lines.append(f"- **App Version**: `{app_version}`")
    if os_version:
        md_lines.append(f"- **OS Version**: `{os_version}`")
    md_lines.append(f"- **Local Repository**: `{repository_path or 'Not configured'}` ({'Available' if repo_available else 'Not found / Not configured yet'})")
    md_lines.append("")

    md_lines.append("## Stack Trace")
    md_lines.append("```text")
    md_lines.append(stack_trace.strip() if stack_trace else "(No stack trace captured)")
    md_lines.append("```")
    md_lines.append("")

    md_lines.append("## Mapped Source Locations")
    if not locations:
        md_lines.append("_No source file/line references parsed from stack trace._")
    else:
        for loc in locations:
            md_lines.append(f"### `{loc.file_name}:{loc.line_number}`" + (f" (`{loc.symbol}`)" if loc.symbol else ""))
            if loc.resolved_path and loc.snippet:
                md_lines.append(f"File: `{loc.resolved_path}`")
                md_lines.append("```")
                md_lines.append(loc.snippet)
                md_lines.append("```")
            else:
                reason = "Local repository path not configured or not found." if not repo_available else "File not found in local repository (may belong to a 3rd-party SDK/package)."
                md_lines.append(f"_{reason}_")
            md_lines.append("")

    return DiagnosisReport(
        issue_id=issue_id,
        title=title,
        subtitle=subtitle,
        repository_path=repository_path,
        repository_available=repo_available,
        locations=locations,
        markdown_report="\n".join(md_lines),
    )
