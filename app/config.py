from pathlib import Path
from typing import Any, Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
import subprocess
import yaml
import re
from urllib.parse import urlsplit
from app.config_files import editing_config, write_yaml


DEFAULT_PROJECTS_FILE = "config/projects.yaml"
VALID_CRASHLYTICS_BACKENDS = ("firebase_cli", "api", "browser")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRASHPILOT_",
        env_file=".env",
        extra="ignore",
    )
    env: str = "development"
    log_level: str = "INFO"
    projects_file: str = DEFAULT_PROJECTS_FILE
    crashlytics_backend: Literal["firebase_cli", "api", "browser"] = "firebase_cli"
    browser_profile: str = "browser_profiles/firebase"
    headless: bool = False


def resolve_crashlytics_backend(
    project: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> Literal["firebase_cli", "api", "browser"]:
    """Xác định backend Crashlytics cho 1 dự án: ưu tiên cấu hình riêng của dự án, fallback về Settings."""
    raw = str((project or {}).get("crashlytics_backend") or "").strip().lower()
    if raw in VALID_CRASHLYTICS_BACKENDS:
        return raw  # type: ignore[return-value]
    return (settings or Settings()).crashlytics_backend


def _resolve_projects_path(path: str | None = None) -> Path:
    settings = Settings()
    return Path(path or settings.projects_file)


def load_all_projects(path: str | None = None) -> list[dict[str, Any]]:
    """Đọc toàn bộ dự án (cả đang bật lẫn đang tắt) từ config/projects.yaml."""
    config_path = _resolve_projects_path(path)
    try:
        contents = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path is None and config_path == Path(DEFAULT_PROJECTS_FILE):
            return []
        raise
    data = yaml.safe_load(contents) or {}
    projects = data.get("projects", [])
    if not isinstance(projects, list):
        raise ValueError("'projects' must be a list")
    return projects


def load_projects(path: str | None = None) -> list[dict[str, Any]]:
    """Đọc danh sách dự án đang bật (enabled: true)."""
    projects = load_all_projects(path)
    enabled = [p for p in projects if p.get("enabled", False)]
    keys = [p.get("key") for p in enabled]
    if len(keys) != len(set(keys)):
        raise ValueError("Enabled project keys must be unique")
    return enabled


def save_projects_config(projects: list[dict[str, Any]], path: str | None = None) -> Path:
    """Lưu danh sách dự án xuống config/projects.yaml (kiểm tra key duy nhất)."""
    keys = [str(p.get("key") or "").strip() for p in projects]
    if any(not k for k in keys):
        raise ValueError("Mỗi dự án phải có 'key' không rỗng.")
    if len(keys) != len(set(keys)):
        raise ValueError("Project keys phải là duy nhất.")

    config_path = _resolve_projects_path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(config_path, {"projects": projects})
    return config_path


def project_sources(project: dict) -> list[dict]:
    """Lấy danh sách nguồn cấu hình trong project; mặc định bật 3 nguồn chuẩn nếu bỏ trống."""
    raw = project.get("sources")
    if not raw:
        return [
            {"type": "crashlytics", "enabled": True},
            {"type": "code_audit", "enabled": True},
            {"type": "inbox_json", "enabled": True, "path": "data/inbox"},
        ]
    return [s for s in raw if s.get("enabled", True)]


def _default_sources(
    console_url: str, input_file_path: str = "", *, firebase_project_id: str = ""
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = [
        {"type": "crashlytics", "enabled": bool(firebase_project_id or console_url)},
        {"type": "code_audit", "enabled": True},
        {"type": "inbox_json", "enabled": True, "path": "data/inbox"},
    ]
    if input_file_path.strip():
        sources.append({"type": "file", "enabled": True, "path": input_file_path.strip()})
    return sources


def inspect_git_repository(repository_path: str | Path | None) -> dict[str, Any]:
    """Kiểm tra thực tế repository Git: nhánh đang đứng (current_branch) và danh sách các nhánh hiện có."""
    raw = str(repository_path or "").strip()
    if not raw:
        return {
            "repo_exists": False,
            "is_git": False,
            "current_branch": None,
            "branches": [],
            "default_base_branch": None,
        }
    repo = Path(raw).expanduser()
    if not repo.exists() or not repo.is_dir():
        return {
            "repo_exists": False,
            "is_git": False,
            "current_branch": None,
            "branches": [],
            "default_base_branch": None,
        }
    if not (repo / ".git").exists():
        return {
            "repo_exists": True,
            "is_git": False,
            "current_branch": None,
            "branches": [],
            "default_base_branch": None,
        }

    def _git(*args: str) -> str:
        try:
            res = subprocess.run(
                ["git", *args],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return res.stdout.strip() if res.returncode == 0 else ""
        except Exception:
            return ""

    current = _git("branch", "--show-current") or _git("symbolic-ref", "--quiet", "--short", "HEAD") or None
    local_refs = [
        b.strip()
        for b in _git("for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
        if b.strip()
    ]
    remote_refs: list[str] = []
    for r in _git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/").splitlines():
        r = r.strip()
        if not r or r in {"origin/HEAD", "origin"} or not r.startswith("origin/"):
            continue
        bname = r.removeprefix("origin/").strip()
        if bname and bname != "HEAD":
            remote_refs.append(bname)

    branches: list[str] = []
    seen: set[str] = set()
    candidates = (
        ([current] if current else [])
        + [b for b in ("develop", "main", "master") if b in local_refs or b in remote_refs]
        + local_refs
        + remote_refs
    )
    for b in candidates:
        if not b or b in seen:
            continue
        if b.startswith("crashpilot/") and b != current:
            continue
        seen.add(b)
        branches.append(b)

    default_base = current or (branches[0] if branches else "main")
    return {
        "repo_exists": True,
        "is_git": True,
        "current_branch": current,
        "branches": branches,
        "default_base_branch": default_base,
    }


def get_project_console_urls(project: dict[str, Any]) -> list[tuple[str, str]]:
    """Trả về danh sách (platform_tag, console_url) cần quét cho 1 dự án (hỗ trợ Flutter 2 URL iOS + Android)."""
    urls: list[tuple[str, str]] = []
    seen: set[str] = set()
    platform = str(project.get("platform") or "flutter").lower()

    for tag, field in (("ios", "console_url_ios"), ("android", "console_url_android")):
        u = str(project.get(field) or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append((tag, u))

    fallback = str(project.get("console_url") or "").strip()
    if fallback and fallback not in seen:
        seen.add(fallback)
        urls.append((platform, fallback))

    return urls


def upsert_project_config(payload: dict[str, Any], path: str | None = None) -> dict[str, Any]:
    with editing_config(_resolve_projects_path(path)):
        return _upsert_project_config(payload, path)


def _upsert_project_config(payload: dict[str, Any], path: str | None = None) -> dict[str, Any]:
    """Thêm mới hoặc cập nhật 1 dự án trong config/projects.yaml."""
    key = str(payload.get("key") or "").strip()
    if not key:
        raise ValueError("Thiếu 'key' của dự án.")

    if not re.fullmatch(r"[\w.-]+", key) or key in {".", ".."}:
        raise ValueError("Key dự án không hợp lệ.")
    for field in ("console_url", "console_url_android", "console_url_ios"):
        url = str(payload.get(field) or "").strip()
        if url and (urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname):
            raise ValueError("Console URL phải dùng HTTP hoặc HTTPS.")
    raw_backend = payload.get("crashlytics_backend")
    if raw_backend is not None:
        backend_val = str(raw_backend).strip().lower()
        if backend_val and backend_val not in VALID_CRASHLYTICS_BACKENDS:
            raise ValueError(
                f"Crashlytics backend không hợp lệ: '{backend_val}'. "
                f"Chỉ hỗ trợ: {', '.join(VALID_CRASHLYTICS_BACKENDS)}."
            )
    config_path = _resolve_projects_path(path)
    projects = load_all_projects(str(config_path)) if config_path.exists() else []
    existing_idx = next((i for i, p in enumerate(projects) if p.get("key") == key), None)

    console_url = str(payload.get("console_url") or "").strip()
    console_url_ios = str(payload.get("console_url_ios") or "").strip()
    console_url_android = str(payload.get("console_url_android") or "").strip()
    if not console_url and (console_url_android or console_url_ios):
        console_url = console_url_android or console_url_ios
    input_file_path = str(payload.get("input_file_path") or "").strip()

    if existing_idx is not None:
        item = dict(projects[existing_idx])
        for field in (
            "name",
            "firebase_project_id",
            "crashlytics_backend",
            "console_url",
            "console_url_ios",
            "console_url_android",
            "repository_path",
            "platform",
        ):
            if field in payload and payload[field] is not None:
                val = str(payload[field]).strip()
                item[field] = val.lower() if field == "crashlytics_backend" else val
        if not item.get("console_url") and (item.get("console_url_android") or item.get("console_url_ios")):
            item["console_url"] = item.get("console_url_android") or item.get("console_url_ios")
        if "enabled" in payload:
            item["enabled"] = bool(payload["enabled"])
        if "scan_interval_minutes" in payload and payload["scan_interval_minutes"] is not None:
            interval = int(payload["scan_interval_minutes"])
            if interval < 1:
                raise ValueError("Chu kỳ quét phải lớn hơn hoặc bằng 1 phút.")
            item["scan_interval_minutes"] = interval

        exec_cfg = dict(item.get("execution") or {})
        for efield in ("auto_run", "dry_run", "auto_create_pr", "auto_merge"):
            if efield in payload:
                exec_cfg[efield] = bool(payload[efield])
        for nfield in ("max_clusters_per_cycle", "max_retries"):
            if nfield in payload and payload[nfield] is not None:
                exec_cfg[nfield] = max(0 if nfield == "max_retries" else 1, int(payload[nfield]))
        if "protected_branches" in payload and isinstance(payload["protected_branches"], list):
            exec_cfg["protected_branches"] = [str(b).strip() for b in payload["protected_branches"] if str(b).strip()]
        if "runtime_verifier" in payload and isinstance(payload["runtime_verifier"], dict):
            rv_in = payload["runtime_verifier"]
            cur_rv = dict(exec_cfg.get("runtime_verifier") or {})
            cur_rv.update(rv_in)
            exec_cfg["runtime_verifier"] = cur_rv
        git_info = inspect_git_repository(item.get("repository_path"))
        if "base_branch" in payload and payload["base_branch"]:
            req_branch = str(payload["base_branch"]).strip()
            if git_info["is_git"] and git_info["branches"] and req_branch not in git_info["branches"]:
                raise ValueError(
                    f"Nhánh '{req_branch}' không tồn tại trong repository "
                    f"(đang đứng: {git_info['current_branch'] or 'N/A'}, các nhánh hiện có: {', '.join(git_info['branches'])})."
                )
            exec_cfg["base_branch"] = req_branch
        elif git_info["is_git"] and git_info["branches"]:
            cur_base = str(exec_cfg.get("base_branch") or "").strip()
            if not cur_base or cur_base not in git_info["branches"]:
                exec_cfg["base_branch"] = git_info["default_base_branch"] or "main"
        if exec_cfg:
            item["execution"] = exec_cfg

        # Cập nhật nguồn file đơn lẻ nếu có truyền input_file_path
        if "input_file_path" in payload:
            srcs = list(item.get("sources") or _default_sources(
                item.get("console_url", ""), firebase_project_id=item.get("firebase_project_id", "")
            ))
            original = payload.get("original_input_file_path")
            index = next((i for i, src in enumerate(srcs) if src.get("type") == "file"
                          and (original is None or src.get("path") == original)), None)
            if original and index is None:
                raise ValueError("Nguồn đã thay đổi. Hãy tải lại cấu hình trước khi lưu.")
            if index is None and input_file_path:
                srcs.append({"type": "file", "enabled": True, "path": input_file_path})
            elif index is not None:
                if input_file_path:
                    srcs[index] = {**srcs[index], "path": input_file_path}
                else:
                    srcs.pop(index)
            item["sources"] = srcs

        projects[existing_idx] = item
    else:
        interval = int(payload.get("scan_interval_minutes", 60))
        if interval < 1:
            raise ValueError("Chu kỳ quét phải lớn hơn hoặc bằng 1 phút.")
        repo_path_str = str(payload.get("repository_path") or "").strip()
        git_info = inspect_git_repository(repo_path_str)
        raw_base = str(payload.get("base_branch") or "").strip()
        if raw_base and git_info["is_git"] and git_info["branches"] and raw_base not in git_info["branches"]:
            raise ValueError(
                f"Nhánh '{raw_base}' không tồn tại trong repository "
                f"(đang đứng: {git_info['current_branch'] or 'N/A'}, các nhánh hiện có: {', '.join(git_info['branches'])})."
            )
        resolved_base = raw_base or git_info["default_base_branch"] or "main"
        firebase_project_id = str(payload.get("firebase_project_id") or "").strip()
        crashlytics_backend = str(payload.get("crashlytics_backend") or "").strip().lower()
        item = {
            "key": key,
            "name": str(payload.get("name") or key).strip(),
            "firebase_project_id": firebase_project_id,
            **({"crashlytics_backend": crashlytics_backend} if crashlytics_backend else {}),
            "enabled": bool(payload.get("enabled", True)),
            "scan_interval_minutes": interval,
            "console_url": console_url,
            "console_url_ios": console_url_ios,
            "console_url_android": console_url_android,
            "repository_path": repo_path_str,
            "platform": str(payload.get("platform") or "flutter").strip(),
            "sources": _default_sources(
                console_url, input_file_path, firebase_project_id=firebase_project_id
            ),
            "execution": {
                "auto_run": bool(payload.get("auto_run", True)),
                "dry_run": bool(payload.get("dry_run", True)),
                "base_branch": resolved_base,
                "auto_create_pr": bool(payload.get("auto_create_pr", False)),
                "auto_merge": bool(payload.get("auto_merge", False)),
                "max_clusters_per_cycle": int(payload.get("max_clusters_per_cycle", 3)),
            },
        }
        projects.append(item)

    save_projects_config(projects, str(config_path))
    return item


def toggle_project_enabled(project_key: str, enabled: bool, path: str | None = None) -> dict[str, Any]:
    with editing_config(_resolve_projects_path(path)):
        return _toggle_project_enabled(project_key, enabled, path)


def _toggle_project_enabled(project_key: str, enabled: bool, path: str | None = None) -> dict[str, Any]:
    """Bật hoặc tắt 1 dự án ngay trong config/projects.yaml."""
    projects = load_all_projects(path)
    for p in projects:
        if p.get("key") == project_key:
            p["enabled"] = bool(enabled)
            save_projects_config(projects, path)
            return p
    raise KeyError(f"Không tìm thấy dự án '{project_key}'.")


def delete_project_config(project_key: str, path: str | None = None) -> bool:
    with editing_config(_resolve_projects_path(path)):
        return _delete_project_config(project_key, path)


def _delete_project_config(project_key: str, path: str | None = None) -> bool:
    """Xóa 1 dự án khỏi config/projects.yaml."""
    projects = load_all_projects(path)
    remaining = [p for p in projects if p.get("key") != project_key]
    if len(remaining) == len(projects):
        return False
    save_projects_config(remaining, path)
    return True
