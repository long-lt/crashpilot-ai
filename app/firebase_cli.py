"""Read Crashlytics issues and sample events through the official Firebase CLI (MCP)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Iterator
from typing import Any, Self
from urllib.parse import unquote, urlsplit

import yaml

from app.config import get_project_console_urls
from app.crashlytics import APP_ID, _event_detail
from app.models.issue import CrashDetail, CrashIssue, StackFrame

logger = logging.getLogger(__name__)

AUTH_ERROR_MARKERS = (
    "not authenticated",
    "unauthenticated",
    "authentication error",
    "invalid authentication credentials",
    "firebase login",
    "reauth",
    "401",
    "no authenticated user",
    "failed to authenticate",
)

FRAME_LINE_RE = re.compile(
    r"^\s*at\s+(?:(?P<symbol>.+?)\s+)?\((?P<file>[^():]+?)(?::(?P<line>\d+))?\)\s*$"
)
FRAME_SYMBOL_ONLY_RE = re.compile(r"^\s*at\s+(?P<symbol>.+?)\s*$")


class FirebaseCliError(RuntimeError):
    """Base error raised when invoking Firebase CLI."""


class FirebaseCliAuthError(FirebaseCliError):
    """Raised when Firebase CLI authentication is missing or expired."""


def _firebase_major_version(firebase_bin: str) -> int | None:
    try:
        res = subprocess.run(
            [firebase_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode != 0:
            return None
        match = re.search(r"(\d+)\.\d+\.\d+", res.stdout.strip())
        return int(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_firebase_login_command() -> list[str]:
    """Return the Firebase CLI command used for interactive login."""
    firebase_bin = shutil.which("firebase")
    if firebase_bin:
        return [firebase_bin, "login", "--reauth"]
    npx_bin = shutil.which("npx")
    if npx_bin:
        return [npx_bin, "-y", "firebase-tools@latest", "login", "--reauth"]
    raise FirebaseCliError(
        "Firebase CLI not found. Install it with 'npm install -g firebase-tools@latest', "
        "then run ./run.sh login."
    )


def resolve_firebase_mcp_command() -> list[str]:
    """Return the command to launch Firebase CLI MCP server (core + crashlytics)."""
    firebase_bin = shutil.which("firebase")
    if firebase_bin:
        major = _firebase_major_version(firebase_bin)
        if major is not None and major >= 15:
            return [firebase_bin, "mcp", "--only", "core,crashlytics"]
    npx_bin = shutil.which("npx")
    if npx_bin:
        return [npx_bin, "-y", "firebase-tools@latest", "mcp", "--only", "core,crashlytics"]
    if firebase_bin:
        raise FirebaseCliError(
            "Installed Firebase CLI is older than v15 and 'npx' is unavailable. "
            "Upgrade with: npm install -g firebase-tools@latest"
        )
    raise FirebaseCliError(
        "Firebase CLI not found. Install it with 'npm install -g firebase-tools@latest'."
    )


def _is_auth_error_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)


def _maybe_yaml(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        parsed = yaml.safe_load(stripped)
        return parsed if parsed is not None else value
    except yaml.YAMLError:
        return value


def _parse_text_frames(section_text: str) -> list[StackFrame]:
    frames: list[StackFrame] = []
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("at "):
            continue
        match = FRAME_LINE_RE.match(line)
        if match:
            symbol = (match.group("symbol") or "").strip() or None
            file_path = (match.group("file") or "").strip() or None
            line_num = int(match.group("line")) if match.group("line") else None
            idx = len(frames)
            location = f"{file_path}:{line_num}" if file_path and line_num else (file_path or "unknown")
            frames.append(
                StackFrame(
                    index=idx,
                    file_path=file_path,
                    line_number=line_num,
                    symbol_name=symbol,
                    is_app_code=bool(file_path and not file_path.startswith(("dart:", "package:flutter/"))),
                    raw_text=f"#{idx} {symbol or '?'} ({location})",
                )
            )
            continue
        sym_match = FRAME_SYMBOL_ONLY_RE.match(line)
        if sym_match:
            symbol = sym_match.group("symbol").strip()
            idx = len(frames)
            frames.append(
                StackFrame(
                    index=idx,
                    symbol_name=symbol,
                    raw_text=f"#{idx} {symbol}",
                )
            )
    return frames


def parse_mcp_event_detail(issue_id: str, raw_event: dict[str, Any]) -> CrashDetail:
    """Parse a Crashlytics event returned by Firebase CLI MCP into CrashDetail."""
    # If the event still has structured list frames (raw API shape), reuse _event_detail.
    if isinstance(raw_event.get("exceptions"), list) or isinstance(raw_event.get("threads"), list):
        return _event_detail(issue_id, raw_event)

    sections: list[str] = []
    for key in ("exceptions", "errors", "threads"):
        val = raw_event.get(key)
        if isinstance(val, str) and val.strip():
            sections.append(val.strip())

    raw_stack = "\n\n".join(sections)
    frames = _parse_text_frames(raw_stack)

    blame_raw = _maybe_yaml(raw_event.get("blameFrame"))
    blame = blame_raw if isinstance(blame_raw, dict) else {}
    blame_file = blame.get("file")
    blame_line = int(blame.get("line") or 0) or None
    if not blame_file:
        app_frame = next((f for f in frames if f.is_app_code and f.file_path), None)
        if app_frame:
            blame_file = app_frame.file_path
            blame_line = app_frame.line_number

    version_raw = _maybe_yaml(raw_event.get("version"))
    version = version_raw if isinstance(version_raw, dict) else {}
    os_raw = _maybe_yaml(raw_event.get("operatingSystem"))
    os_info = os_raw if isinstance(os_raw, dict) else {}
    device_raw = _maybe_yaml(raw_event.get("device"))
    device = device_raw if isinstance(device_raw, dict) else {}

    logs_raw = raw_event.get("logs")
    logs: list[str] = []
    if isinstance(logs_raw, str):
        logs = [line.strip() for line in logs_raw.splitlines() if line.strip()]
    elif isinstance(logs_raw, list):
        logs = [
            item["message"] if isinstance(item, dict) and "message" in item else str(item)
            for item in logs_raw
        ]

    keys_raw = _maybe_yaml(raw_event.get("customKeys"))
    custom_keys = keys_raw if isinstance(keys_raw, dict) else {}

    return CrashDetail(
        issue_id=issue_id,
        event_id=raw_event.get("eventId"),
        timestamp=str(raw_event.get("eventTime")) if raw_event.get("eventTime") else None,
        app_version=version.get("displayVersion") or version.get("displayName"),
        os_version=os_info.get("displayName") or os_info.get("displayVersion"),
        device_model=device.get("displayName") or device.get("model"),
        stack_trace_raw=raw_stack or "\n".join(f.raw_text for f in frames),
        frames=frames,
        blame_file=blame_file,
        blame_line=blame_line,
        logs=logs,
        keys=custom_keys,
    )


class FirebaseCliClient:
    """Client talking to Firebase CLI's official MCP server over stdio JSON-RPC."""

    def __init__(self, command: list[str] | None = None, *, timeout: float = 60.0):
        self.command = command or resolve_firebase_mcp_command()
        self.timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 1

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "crashpilot-ai", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized")

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            logger.debug("Failed to close Firebase CLI MCP stdin", exc_info=True)
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                logger.debug("Failed to kill Firebase CLI MCP process", exc_info=True)

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self._proc or not self._proc.stdin:
            raise FirebaseCliError("Firebase CLI MCP process is not running.")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise FirebaseCliError("Firebase CLI MCP process is not running.")
        req_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

        while True:
            line = self._proc.stdout.readline()
            if not line:
                stderr_out = ""
                if self._proc.stderr:
                    try:
                        stderr_out = self._proc.stderr.read().strip()
                    except OSError:
                        stderr_out = ""
                raise FirebaseCliError(
                    f"Firebase CLI MCP exited unexpectedly. {stderr_out}".strip()
                )
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Ignoring non-JSON stdout line from Firebase CLI MCP: %s", line[:200])
                continue
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                err_msg = msg["error"].get("message") or str(msg["error"])
                if _is_auth_error_text(err_msg):
                    raise FirebaseCliAuthError(
                        f"Firebase CLI login required. Run ./run.sh login. ({err_msg})"
                    )
                raise FirebaseCliError(f"Firebase CLI MCP error: {err_msg}")
            return msg.get("result") or {}

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        texts = [
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        combined = "\n".join(t for t in texts if t).strip()
        if result.get("isError"):
            if _is_auth_error_text(combined):
                raise FirebaseCliAuthError(
                    f"Firebase CLI login required. Run ./run.sh login. ({combined})"
                )
            raise FirebaseCliError(f"Firebase CLI tool '{name}' failed: {combined}")
        return combined

    def check_auth(self) -> tuple[bool, str]:
        """Return (is_authenticated, detail_message) via firebase_get_environment."""
        text = self.call_tool("firebase_get_environment", {})
        match = re.search(r"Authenticated User:\s*(.+)", text)
        user = match.group(1).strip() if match else ""
        if not user or user.upper() in {"<NONE>", "NONE", "N/A"}:
            return False, "Firebase CLI not logged in. Run ./run.sh login."
        # Verify the stored token is still valid by listing 1 project
        try:
            self.call_tool("firebase_list_projects", {"page_size": 1})
        except FirebaseCliAuthError as exc:
            return False, str(exc)
        except FirebaseCliError as exc:
            if _is_auth_error_text(str(exc)):
                return False, f"Firebase CLI login required. Run ./run.sh login. ({exc})"
            return False, str(exc)
        return True, user

    def app_ids(self, project: dict[str, Any]) -> list[str]:
        explicit_ids = project.get("firebase_app_ids")
        if explicit_ids:
            if not isinstance(explicit_ids, list) or any(
                not isinstance(app_id, str) or not APP_ID.fullmatch(app_id)
                for app_id in explicit_ids
            ):
                raise ValueError("firebase_app_ids must be a list of Firebase App IDs.")
            return list(dict.fromkeys(explicit_ids))

        targets: list[tuple[str, str]] = []
        for platform, url in get_project_console_urls(project):
            parts = [unquote(segment) for segment in urlsplit(url).path.split("/") if segment]
            namespace = next(
                (
                    segment
                    for segment in parts
                    if segment.startswith(("android:", "ios:", "web:")) or APP_ID.fullmatch(segment)
                ),
                "",
            )
            if APP_ID.fullmatch(namespace):
                targets.append((namespace.split(":")[2], namespace))
            elif namespace:
                plat, sel = namespace.split(":", 1)
                targets.append((plat, sel))
            else:
                targets.append((platform, ""))

        if targets and all(APP_ID.fullmatch(selector) for _, selector in targets):
            return list(dict.fromkeys(selector for _, selector in targets))

        project_id = project.get("firebase_project_id")
        if not project_id:
            raise ValueError("Missing firebase_project_id in the project configuration.")

        self.call_tool("firebase_update_environment", {"active_project": project_id})
        apps_text = self.call_tool("firebase_list_apps", {"platform": "all"})
        apps_data = _maybe_yaml(apps_text)
        apps = apps_data if isinstance(apps_data, list) else []

        selected: dict[str, None] = {}
        for platform, selector in targets or [(project.get("platform") or "flutter", "")]:
            plat_lower = platform.lower()
            platforms = {plat_lower} if plat_lower in {"android", "ios", "web"} else {"android", "ios"}
            matches = [
                app["appId"]
                for app in apps
                if isinstance(app, dict)
                and app.get("appId")
                and str(app.get("platform", "")).lower() in platforms
                and (
                    not selector
                    or selector
                    in {
                        app.get("namespace"),
                        app.get("packageName"),
                        app.get("bundleId"),
                        app.get("appId"),
                    }
                )
            ]
            if not matches:
                raise ValueError(
                    "No Firebase apps match the project configuration. Check firebase_app_ids."
                )
            selected.update(dict.fromkeys(matches))
        return list(selected)

    def issues(self, project: dict[str, Any], app_id: str, *, page_size: int = 100) -> Iterator[CrashIssue]:
        report_text = self.call_tool(
            "crashlytics_get_report",
            {"appId": app_id, "report": "topIssues", "pageSize": page_size},
        )
        report_doc = _maybe_yaml(report_text)
        if not isinstance(report_doc, dict):
            return
        groups_raw = _maybe_yaml(report_doc.get("groups"))
        if not isinstance(groups_raw, list):
            return

        platform = app_id.split(":")[2] if APP_ID.fullmatch(app_id) else str(project.get("platform") or "unknown")
        for group in groups_raw:
            if not isinstance(group, dict):
                continue
            issue = group.get("issue")
            if not isinstance(issue, dict) or not issue.get("id"):
                continue
            metrics_list = group.get("metrics") or []
            metrics = metrics_list[0] if isinstance(metrics_list, list) and metrics_list else {}
            yield CrashIssue(
                issue_id=str(issue["id"]),
                project_key=project["key"],
                firebase_project_id=project["firebase_project_id"],
                platform=platform,
                title=str(issue.get("title") or "Unknown Crash"),
                subtitle=issue.get("subtitle"),
                issue_type=str(issue.get("errorType") or "FATAL").lower(),
                status=str(issue.get("state") or "OPEN").lower(),
                events_count=int(metrics.get("eventsCount", 0) or 0),
                users_count=int(metrics.get("impactedUsersCount", 0) or 0),
                first_seen=str(issue["firstSeenTime"]) if issue.get("firstSeenTime") else None,
                last_seen=str(issue["lastSeenTime"]) if issue.get("lastSeenTime") else None,
                issue_url=str(issue.get("uri") or ""),
            )

    def detail(self, app_id: str, issue_id: str) -> CrashDetail | None:
        events_text = self.call_tool(
            "crashlytics_list_events",
            {
                "appId": app_id,
                "filter": {"issueId": issue_id},
                "pageSize": 1,
            },
        )
        events_data = _maybe_yaml(events_text)
        if not isinstance(events_data, list) or not events_data:
            return None
        first_event = events_data[0]
        if not isinstance(first_event, dict):
            return None
        return parse_mcp_event_detail(issue_id, first_event)
