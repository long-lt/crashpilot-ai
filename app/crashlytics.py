"""Read issues and sample events through the public Firebase Crashlytics API."""

import re
from collections.abc import Iterator
from urllib.parse import quote, unquote, urlsplit

from app.config import get_project_console_urls
from app.models.issue import CrashDetail, CrashIssue, StackFrame

CRASHLYTICS_API = "https://firebasecrashlytics.googleapis.com/v1alpha"
FIREBASE_API = "https://firebase.googleapis.com/v1beta1"
APP_ID = re.compile(r"1:(\d+):(android|ios|web):[a-zA-Z0-9]+")


class CrashlyticsClient:
    def __init__(self, session):
        self.session = session

    @staticmethod
    def authorize():
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return AuthorizedSession(credentials)

    def get(self, url: str, **params) -> dict:
        with self.session.get(url, params=params, timeout=30) as response:
            if not response.ok:
                try:
                    message = response.json().get("error", {}).get("message")
                except ValueError:
                    message = None
                action = "Google login required. Run ./run.sh login. " if response.status_code == 401 else ""
                raise RuntimeError(f"{action}Firebase API HTTP {response.status_code}: {message or response.reason}")
            return response.json()

    def pages(self, url: str, key: str) -> Iterator[dict]:
        params = {"pageSize": 100}
        while True:
            page = self.get(url, **params)
            yield from page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    def app_ids(self, project: dict) -> list[str]:
        explicit_ids = project.get("firebase_app_ids")
        if explicit_ids:
            if not isinstance(explicit_ids, list) or any(
                not isinstance(app_id, str) or not APP_ID.fullmatch(app_id)
                for app_id in explicit_ids
            ):
                raise ValueError("firebase_app_ids must be a list of Firebase App IDs.")
            return list(dict.fromkeys(explicit_ids))

        targets = []
        for platform, url in get_project_console_urls(project):
            parts = [unquote(segment) for segment in urlsplit(url).path.split("/") if segment]
            namespace = next(
                (segment for segment in parts if segment.startswith(("android:", "ios:", "web:"))
                 or APP_ID.fullmatch(segment)),
                "",
            )
            if APP_ID.fullmatch(namespace):
                targets.append((namespace.split(":")[2], namespace))
            elif namespace:
                targets.append(tuple(namespace.split(":", 1)))
            else:
                targets.append((platform, ""))

        if targets and all(APP_ID.fullmatch(selector) for _, selector in targets):
            return list(dict.fromkeys(selector for _, selector in targets))

        project_id = quote(project["firebase_project_id"], safe="")
        apps = list(self.pages(f"{FIREBASE_API}/projects/{project_id}:searchApps", "apps"))
        selected = {}
        for platform, selector in targets or [(project.get("platform") or "flutter", "")]:
            platform = platform.lower()
            platforms = {platform} if platform in {"android", "ios", "web"} else {"android", "ios"}
            matches = [
                app["appId"] for app in apps
                if app.get("platform", "").lower() in platforms
                and (not selector or selector in {app.get("namespace"), app["appId"]})
            ]
            if not matches:
                raise ValueError("No Firebase apps match the project configuration. Check firebase_app_ids.")
            selected.update(dict.fromkeys(matches))
        return list(selected)

    def issues(self, project: dict, app_id: str) -> Iterator[CrashIssue]:
        for group in self.pages(f"{_app_url(app_id)}/reports/topIssues", "groups"):
            issue = group["issue"]
            metrics = next(iter(group.get("metrics", [])), {})
            yield CrashIssue(
                issue_id=issue["id"],
                project_key=project["key"],
                firebase_project_id=project["firebase_project_id"],
                platform=app_id.split(":")[2],
                title=issue["title"],
                subtitle=issue.get("subtitle"),
                issue_type=issue.get("errorType", "FATAL").lower(),
                status=issue.get("state", "OPEN").lower(),
                events_count=int(metrics.get("eventsCount", 0)),
                users_count=int(metrics.get("impactedUsersCount", 0)),
                first_seen=issue.get("firstSeenTime"),
                last_seen=issue.get("lastSeenTime"),
                issue_url=issue["uri"],
            )

    def detail(self, app_id: str, issue_id: str) -> CrashDetail | None:
        data = self.get(
            f"{_app_url(app_id)}/events",
            pageSize=1,
            readMask="eventId,eventTime,version,operatingSystem,device,blameFrame,exceptions,errors,threads,logs,customKeys",
            **{"filter.issue.id": issue_id},
        )
        event = next(iter(data.get("events", [])), None)
        return _event_detail(issue_id, event) if event else None


def _app_url(app_id: str) -> str:
    match = APP_ID.fullmatch(app_id)
    if not match:
        raise ValueError("Invalid Firebase App ID.")
    return f"{CRASHLYTICS_API}/projects/{match[1]}/apps/{quote(app_id, safe=':')}"


def _event_detail(issue_id: str, event: dict) -> CrashDetail:
    stacks = event.get("exceptions", []) + event.get("errors", [])
    if not any(stack.get("frames") for stack in stacks):
        threads = event.get("threads", [])
        stacks = [thread for thread in threads if thread.get("blamed")] or threads[:1]
    raw_frames = [frame for stack in stacks for frame in stack.get("frames", [])]
    frames = []
    for index, frame in enumerate(raw_frames):
        path = frame.get("file")
        line = int(frame.get("line") or 0) or None
        symbol = frame.get("symbol")
        location = f"{path}:{line}" if path and line else path or frame.get("library") or "unknown"
        frames.append(StackFrame(
            index=index,
            package_or_binary=frame.get("library"),
            file_path=path,
            line_number=line,
            symbol_name=symbol,
            is_app_code=frame.get("owner") == "DEVELOPER",
            raw_text=f"#{index} {symbol or '?'} ({location})",
        ))

    blame = event.get("blameFrame") or next(
        (frame for frame in raw_frames if frame.get("blamed")), {}
    )
    version = event.get("version", {})
    os = event.get("operatingSystem", {})
    device = event.get("device", {})
    return CrashDetail(
        issue_id=issue_id,
        event_id=event.get("eventId"),
        timestamp=event.get("eventTime"),
        app_version=version.get("displayVersion"),
        os_version=os.get("displayName") or os.get("displayVersion"),
        device_model=device.get("model"),
        stack_trace_raw="\n".join(frame.raw_text for frame in frames),
        frames=frames,
        blame_file=blame.get("file"),
        blame_line=int(blame.get("line") or 0) or None,
        logs=[log["message"] for log in event.get("logs", []) if "message" in log],
        keys=event.get("customKeys", {}),
    )
