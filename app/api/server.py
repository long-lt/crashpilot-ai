from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform as py_platform
import subprocess
from typing import Optional
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from ipaddress import ip_address
from urllib.parse import urlsplit
from sqlalchemy.orm import Session, selectinload
from app.config_files import editing_config

from app.analyzer.source_mapper import build_diagnosis_report
from app.constrained_runner.models import COMPLETED_RUN_STATUSES, FAILED_RUN_STATUSES, classify_run_status
from app.db.database import get_db, init_db
from app.db.models import IssueModel, ProjectModel


DASHBOARD_FILE = Path(__file__).with_name("dashboard.html")
scheduler = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global scheduler
    import os
    from app.scheduler import scheduler_from_env

    init_db()
    scheduler = scheduler_from_env()
    if scheduler:
        startup = os.environ.get("CRASHPILOT_STARTUP_CYCLE") == "1"
        delay = scheduler.interval if startup else int(os.environ.get("CRASHPILOT_SYNC_INITIAL_DELAY_MIN", scheduler.interval))
        scheduler.start(initial_delay_minutes=delay, startup=startup)
    yield
    if scheduler:
        await scheduler.stop()


app = FastAPI(
    title="CrashPilot AI",
    description="Local-first Firebase Crashlytics Monitoring & Analysis Dashboard",
    version="0.4.0",
    lifespan=lifespan,
)


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host or "").is_loopback
    except ValueError:
        return False


@app.middleware("http")
async def local_access_only(request, call_next):
    # The supported deployment is a single-user desktop, without a reverse proxy.
    try:
        allowed = (request.client and _loopback(request.client.host) and _loopback(request.url.hostname)
                   and not any(h in request.headers for h in ("forwarded", "x-forwarded-for", "x-forwarded-host",
                                                              "x-forwarded-proto", "x-real-ip")))
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = allowed and request.headers.get("sec-fetch-site") not in {"cross-site", "same-site"}
            if origin:
                allowed = allowed and origin == str(request.base_url).rstrip("/")
    except ValueError:
        allowed = False
    if not allowed:
        return JSONResponse({"detail": "Dashboard chỉ cho phép truy cập cục bộ."}, status_code=403)
    return await call_next(request)


@app.get("/health")
@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "crashpilot-ai"}


@app.get("/api/sync/status")
def sync_status():
    if not scheduler:
        return {"enabled": False}
    return scheduler.status()


@app.post("/api/sync/run")
async def sync_run_now(project_key: Optional[str] = None):
    return _queue_cycle(project_key)


def _queue_cycle(project_key=None, cluster_id=None):
    from app.config import load_all_projects

    if project_key is not None:
        project = next((p for p in load_all_projects() if p["key"] == project_key), None)
        if project is None:
            raise HTTPException(404, "Không tìm thấy dự án.")
        if not project.get("enabled", False):
            raise HTTPException(409, "Dự án đang tắt.")
    if not scheduler or not scheduler.interval:
        raise HTTPException(status_code=400, detail="Scheduler disabled (CRASHPILOT_SYNC_INTERVAL_MIN=0)")
    queued = scheduler.trigger_now(project_key, cluster_id)
    return {"queued": queued, "reason": None if queued else "already_running",
            "project_key": project_key, "cluster_id": cluster_id}


@app.get("/api/projects")
def list_projects(db: Session = Depends(get_db)):
    projects = db.query(ProjectModel).options(selectinload(ProjectModel.issues)).all()
    return [
        {
            "key": p.key,
            "name": p.name,
            "firebase_project_id": p.firebase_project_id,
            "console_url": p.console_url,
            "repository_path": p.repository_path,
            "platform": p.platform,
            "enabled": p.enabled,
            "last_synced_at": p.last_synced_at.isoformat() if p.last_synced_at else None,
            "issue_count": len(p.issues),
        }
        for p in projects
    ]


@app.get("/api/issues")
def list_issues(
    project_key: Optional[str] = Query(default=None),
    issue_type: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    query = db.query(IssueModel).options(selectinload(IssueModel.details))
    if project_key:
        query = query.filter(IssueModel.project_key == project_key)
    if issue_type:
        query = query.filter(IssueModel.issue_type == issue_type)

    issues = query.order_by(IssueModel.events_count.desc()).all()
    return [
        {
            "issue_id": i.issue_id,
            "project_key": i.project_key,
            "firebase_project_id": i.firebase_project_id,
            "platform": i.platform,
            "title": i.title,
            "subtitle": i.subtitle,
            "issue_type": i.issue_type,
            "status": i.status,
            "events_count": i.events_count,
            "users_count": i.users_count,
            "issue_url": i.issue_url,
            "has_details": len(i.details) > 0,
            "updated_at": i.updated_at.isoformat() if i.updated_at else None,
        }
        for i in issues
    ]


def _find_issue(db: Session, issue_id: str, project_key: str | None):
    query = db.query(IssueModel).filter(IssueModel.issue_id == issue_id)
    if project_key is not None:
        query = query.filter(IssueModel.project_key == project_key)
    matches = query.limit(2).all()
    if not matches:
        raise HTTPException(404, "Không tìm thấy issue.")
    if len(matches) > 1:
        raise HTTPException(409, "Issue ID trùng giữa các dự án; cần project_key.")
    return matches[0]


@app.get("/api/issues/{issue_id}")
def get_issue_detail(issue_id: str, db: Session = Depends(get_db), project_key: Optional[str] = None):
    issue = _find_issue(db, issue_id, project_key)

    latest_detail = issue.details[-1] if issue.details else None
    detail_payload = None
    if latest_detail:
        detail_payload = {
            "event_id": latest_detail.event_id,
            "timestamp": latest_detail.timestamp,
            "app_version": latest_detail.app_version,
            "os_version": latest_detail.os_version,
            "device_model": latest_detail.device_model,
            "blame_file": latest_detail.blame_file,
            "blame_line": latest_detail.blame_line,
            "stack_trace_raw": latest_detail.stack_trace_raw,
            "frames": json.loads(latest_detail.frames_json) if latest_detail.frames_json else [],
            "created_at": latest_detail.created_at.isoformat() if latest_detail.created_at else None,
        }

    return {
        "issue_id": issue.issue_id,
        "project_key": issue.project_key,
        "firebase_project_id": issue.firebase_project_id,
        "platform": issue.platform,
        "title": issue.title,
        "subtitle": issue.subtitle,
        "issue_type": issue.issue_type,
        "status": issue.status,
        "events_count": issue.events_count,
        "users_count": issue.users_count,
        "issue_url": issue.issue_url,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "latest_detail": detail_payload,
    }


@app.get("/api/issues/{issue_id}/diagnose")
def diagnose_issue(issue_id: str, db: Session = Depends(get_db), project_key: Optional[str] = None):
    issue = _find_issue(db, issue_id, project_key)

    project = db.query(ProjectModel).filter(ProjectModel.key == issue.project_key).first()
    latest_detail = issue.details[-1] if issue.details else None

    report = build_diagnosis_report(
        issue_id=issue.issue_id,
        title=issue.title,
        subtitle=issue.subtitle,
        stack_trace=latest_detail.stack_trace_raw if latest_detail else "",
        blame_file=latest_detail.blame_file if latest_detail else None,
        blame_line=latest_detail.blame_line if latest_detail else None,
        repository_path=project.repository_path if project else None,
        app_version=latest_detail.app_version if latest_detail else None,
        os_version=latest_detail.os_version if latest_detail else None,
    )
    return report.model_dump()


@app.get("/api/context/clusters")
def get_context_clusters():
    from app.context_collector import EventBufferQueue

    return [c.model_dump(mode="json") for c in EventBufferQueue().load()]


@app.post("/api/context/collect")
def run_context_collection():
    from app.config import load_projects
    from app.orchestrator import build_collector_from_projects

    manager, _ = build_collector_from_projects(load_projects())
    return manager.collect_all().model_dump(mode="json")


@app.get("/api/runs")
def list_constrained_runs():
    from app.constrained_runner import ConstrainedFourStepRunner

    return [r.model_dump(mode="json") for r in ConstrainedFourStepRunner().list_runs()]


@app.get("/api/settings/agents")
def get_agent_settings_api():
    from app.constrained_runner.agents import NEED_LABELS, detect_installed_agents, load_agent_settings

    settings = load_agent_settings()
    return {
        "enable_agent_execution": settings.enable_agent_execution,
        "default_agent": settings.default_agent,
        "routing": settings.routing,
        "pipeline_steps": {
            k: [item.model_dump() for item in v]
            for k, v in settings.pipeline_steps.items()
        },
        "need_labels": NEED_LABELS,
        "agents": detect_installed_agents(settings),
    }


@app.put("/api/settings/agents")
def update_agent_settings_api(payload: dict = Body(...)):
    from app.constrained_runner.agents import DEFAULT_AGENTS_CONFIG_PATH
    with editing_config(DEFAULT_AGENTS_CONFIG_PATH):
        return _update_agent_settings(payload)


def _update_agent_settings(payload):
    from app.constrained_runner.agents import (
        PipelineAgentAssignment,
        detect_installed_agents,
        load_agent_settings,
        save_agent_settings,
    )

    settings = load_agent_settings()
    valid_ids = set(settings.profiles.keys()) | {"none"}

    if "enable_agent_execution" in payload:
        settings.enable_agent_execution = bool(payload["enable_agent_execution"])
    if "default_agent" in payload:
        da = str(payload["default_agent"])
        if da not in valid_ids:
            raise HTTPException(status_code=400, detail=f"Agent không hợp lệ: {da}")
        settings.default_agent = da
    if "routing" in payload and isinstance(payload["routing"], dict):
        for need, agent_id in payload["routing"].items():
            aid = str(agent_id)
            if aid not in valid_ids:
                raise HTTPException(status_code=400, detail=f"Agent không hợp lệ cho '{need}': {aid}")
            settings.routing[need] = aid
        # Đồng bộ ngược sang pipeline_steps để không bị lệch giữa 2 cấu trúc
        for step_key in ("collect_group", "prepare_context", "agent_execution", "validate_deliver"):
            for item in settings.pipeline_steps.get(step_key) or []:
                if item.track and item.track in settings.routing:
                    item.agent_id = settings.routing[item.track]
    if "models" in payload and isinstance(payload["models"], dict):
        for agent_id, model_val in payload["models"].items():
            if agent_id in settings.profiles:
                settings.profiles[agent_id].model = str(model_val or "").strip()
    if "profiles" in payload and isinstance(payload["profiles"], dict):
        for agent_id, pdata in payload["profiles"].items():
            if agent_id in settings.profiles and isinstance(pdata, dict):
                if "model" in pdata:
                    settings.profiles[agent_id].model = str(pdata.get("model") or "").strip()
                if "timeout_sec" in pdata and pdata["timeout_sec"]:
                    settings.profiles[agent_id].timeout_sec = max(30, int(pdata["timeout_sec"]))
    if "pipeline_steps" in payload and isinstance(payload["pipeline_steps"], dict):
        allowed_steps = {"collect_group", "prepare_context", "agent_execution", "validate_deliver"}
        new_steps: dict[str, list[PipelineAgentAssignment]] = {}
        for step_key, items in payload["pipeline_steps"].items():
            if step_key not in allowed_steps or not isinstance(items, list):
                continue
            parsed: list[PipelineAgentAssignment] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                aid = str(item.get("agent_id") or "").strip()
                if aid not in valid_ids:
                    raise HTTPException(status_code=400, detail=f"Agent không hợp lệ cho bước '{step_key}': {aid}")
                parsed.append(
                    PipelineAgentAssignment(
                        name=str(item.get("name") or f"{aid}-agent").strip() or f"{aid}-agent",
                        agent_id=aid,
                        role=str(item.get("role") or "Primary").strip() or "Primary",
                        description=str(item.get("description") or "").strip(),
                        model=str(item.get("model") or "").strip(),
                        track=str(item.get("track")).strip() if item.get("track") else None,
                    )
                )
            new_steps[step_key] = parsed
        if new_steps:
            settings.pipeline_steps.update(new_steps)
            # Đồng bộ routing cho cả 4 bước pipeline (collect_group, prepare_context, agent_execution, validate_deliver)
            for step_key in ("collect_group", "prepare_context", "agent_execution", "validate_deliver"):
                items_for_step = settings.pipeline_steps.get(step_key)
                if items_for_step is not None and step_key in ("collect_group", "prepare_context") and len(items_for_step) == 0:
                    settings.routing[step_key] = "none"
                for item in items_for_step or []:
                    if item.track:
                        settings.routing[item.track] = item.agent_id
                    elif step_key in ("collect_group", "prepare_context"):
                        settings.routing[step_key] = item.agent_id

    saved_path = save_agent_settings(settings)
    return {
        "saved": True,
        "path": str(saved_path),
        "enable_agent_execution": settings.enable_agent_execution,
        "default_agent": settings.default_agent,
        "routing": settings.routing,
        "pipeline_steps": {
            k: [item.model_dump() for item in v]
            for k, v in settings.pipeline_steps.items()
        },
        "need_labels": {
            k: v
            for k, v in __import__("app.constrained_runner.agents", fromlist=["NEED_LABELS"]).NEED_LABELS.items()
        },
        "agents": detect_installed_agents(settings),
    }


@app.post("/api/settings/agents/scan")
def scan_and_probe_agents_api(payload: dict = Body(default_factory=dict)):
    """Quét CLI Coding Agents trên máy, kiểm tra hoạt động thực tế (health probe) và danh sách model."""
    from concurrent.futures import ThreadPoolExecutor
    from app.constrained_runner.agents import (
        NEED_LABELS,
        detect_installed_agents,
        load_agent_settings,
        probe_cli_agent,
    )

    settings = load_agent_settings()
    target_id = str(payload.get("agent_id") or "").strip()
    model_overrides: dict = payload.get("models") if isinstance(payload.get("models"), dict) else {}

    for aid, mval in model_overrides.items():
        if aid in settings.profiles and mval is not None:
            settings.profiles[aid].model = str(mval).strip()

    to_probe = (
        [(target_id, settings.profiles[target_id])]
        if (target_id and target_id in settings.profiles)
        else list(settings.profiles.items())
    )

    def _run_one(item):
        aid, prof = item
        override = model_overrides.get(aid) if aid in model_overrides else None
        return aid, probe_cli_agent(aid, prof, model_override=override)

    with ThreadPoolExecutor(max_workers=max(1, len(to_probe))) as pool:
        list(pool.map(_run_one, to_probe))

    return {
        "scanned": True,
        "enable_agent_execution": settings.enable_agent_execution,
        "default_agent": settings.default_agent,
        "routing": settings.routing,
        "need_labels": NEED_LABELS,
        "agents": detect_installed_agents(settings, probe=False, force_refresh=True),
    }


@app.get("/api/settings/projects")
def get_projects_settings_api(db: Session = Depends(get_db)):
    from app.config import inspect_git_repository, load_all_projects

    projects = load_all_projects()
    db_map = {p.key: p for p in db.query(ProjectModel).options(selectinload(ProjectModel.issues)).all()}
    out = []
    for p in projects:
        repo = str(p.get("repository_path") or "")
        git_info = inspect_git_repository(repo)
        repo_exists = git_info["repo_exists"]
        exec_cfg = p.get("execution") or {}
        file_src = next((s for s in (p.get("sources") or []) if s.get("type") == "file"), None)
        db_proj = db_map.get(p["key"])
        out.append(
            {
                "key": p["key"],
                "name": p.get("name") or p["key"],
                "firebase_project_id": p.get("firebase_project_id") or "",
                "console_url": p.get("console_url") or "",
                "console_url_ios": p.get("console_url_ios") or "",
                "console_url_android": p.get("console_url_android") or "",
                "repository_path": repo,
                "repo_exists": repo_exists,
                "is_git": git_info["is_git"],
                "current_branch": git_info["current_branch"],
                "git_branches": git_info["branches"],
                "default_base_branch": git_info["default_base_branch"],
                "platform": p.get("platform") or "flutter",
                "enabled": bool(p.get("enabled", True)),
                "scan_interval_minutes": int(p.get("scan_interval_minutes") or 60),
                "dry_run": bool(exec_cfg.get("dry_run", True)),
                "auto_create_pr": bool(exec_cfg.get("auto_create_pr", False)),
                "auto_merge": bool(exec_cfg.get("auto_merge", False)),
                "base_branch": str(exec_cfg.get("base_branch") or git_info["default_base_branch"] or "main"),
                "input_file_path": str(file_src.get("path") or "") if file_src else "",
                "issue_count": len(db_proj.issues) if db_proj else 0,
            }
        )
    return out


@app.post("/api/settings/projects")
def upsert_project_settings_api(payload: dict = Body(...), db: Session = Depends(get_db)):
    from app.config import load_all_projects, upsert_project_config
    from app.db.repository import sync_projects

    try:
        saved = upsert_project_config(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sync_projects(db, load_all_projects())
    return {"saved": True, "project": saved}


@app.patch("/api/settings/projects/{project_key}")
def patch_project_settings_api(project_key: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    from app.config import load_all_projects, toggle_project_enabled, upsert_project_config
    from app.db.repository import sync_projects

    try:
        if set(payload.keys()) == {"enabled"}:
            saved = toggle_project_enabled(project_key, bool(payload["enabled"]))
        else:
            saved = upsert_project_config({**payload, "key": project_key})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sync_projects(db, load_all_projects())
    return {"saved": True, "project": saved}


@app.delete("/api/settings/projects/{project_key}")
def delete_project_settings_api(project_key: str, db: Session = Depends(get_db)):
    from app.config import delete_project_config

    deleted = delete_project_config(project_key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy dự án '{project_key}'")

    db_proj = db.query(ProjectModel).filter(ProjectModel.key == project_key).first()
    if db_proj:
        db_proj.enabled = False
        db.commit()
    return {"deleted": True, "key": project_key}


@app.get("/api/settings/wakeup")
def get_wakeup_schedule_api():
    from app.wakeup_schedule import get_wakeup_status

    return get_wakeup_status()


@app.put("/api/settings/wakeup")
def update_wakeup_schedule_api(payload: dict = Body(default_factory=dict)):
    from app.wakeup_schedule import apply_wakeup_schedule

    try:
        return apply_wakeup_schedule(payload)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/hardening/rules")
def get_environment_rules_api():
    from app.hardening import load_environment_rules

    return load_environment_rules().model_dump(mode="json", exclude_none=True)


@app.post("/api/hardening/rules")
def add_environment_rule_api(payload: dict = Body(...), db: Session = Depends(get_db)):
    from app.hardening import (
        CustomAuditRule,
        DiffRule,
        add_custom_audit_rule,
        add_diff_rule,
        add_extra_mechanical_check,
        add_forbidden_path_pattern,
    )

    sess = db if isinstance(db, Session) else None
    proj_key = str(payload.get("project_key") or "").strip() or None
    kind = str(payload.get("kind") or "audit_rule").strip().lower()
    try:
        if kind in ("audit_rule", "custom_audit_rules"):
            rule = CustomAuditRule.model_validate({**payload, "project_key": proj_key})
            return add_custom_audit_rule(rule, db=sess)
        if kind in ("diff_rule", "forbidden_diff_patterns"):
            rule = DiffRule(
                id=str(payload.get("id") or "").strip(),
                pattern=str(payload.get("pattern") or "").strip(),
                message=str(payload.get("message") or payload.get("description") or payload.get("id") or "").strip(),
                project_key=proj_key,
            )
            return add_diff_rule(rule, db=sess)
        if kind in ("forbidden_path", "forbidden_path_patterns"):
            return add_forbidden_path_pattern(str(payload.get("pattern") or ""), db=sess, project_key=proj_key)
        if kind in ("extra_check", "extra_mechanical_checks"):
            return add_extra_mechanical_check(
                payload.get("command") or payload.get("pattern") or "", db=sess, project_key=proj_key
            )
        raise ValueError(f"Loại luật không được hỗ trợ: {kind}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Rule không hợp lệ: {exc}") from exc


@app.post("/api/hardening/rules/delete")
def delete_environment_rule_api(payload: dict = Body(...), db: Session = Depends(get_db)):
    from app.hardening import delete_environment_rule

    sess = db if isinstance(db, Session) else None
    proj_key = str(payload.get("project_key") or "").strip() or None
    kind = str(payload.get("kind") or "").strip()
    target = payload.get("target")
    if target is None:
        target = payload.get("id") or payload.get("pattern") or payload.get("index")
    if not kind or target is None:
        raise HTTPException(status_code=400, detail="Thiếu thông tin kind hoặc target để xóa luật.")
    try:
        return delete_environment_rule(kind, target, db=sess, project_key=proj_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/hardening/sampling")
def get_morning_sampling_api(
    limit: int = Query(default=25, ge=1, le=100),
    project_key: Optional[str] = Query(default=None),
):
    from app.hardening import sample_recent_runs

    return sample_recent_runs(limit=limit, project_key=project_key)


@app.post("/api/hardening/sampling/ai")
def run_ai_morning_sampling_api(payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    from app.db.models import TaskModel
    from app.hardening import propose_rules_with_ai_agent, sync_rule_proposals_to_db

    limit = max(1, min(50, int(payload.get("limit") or 15)))
    project_key = str(payload.get("project_key") or "").strip() or None
    res = propose_rules_with_ai_agent(limit=limit, project_key=project_key)
    if isinstance(db, Session):
        tasks = db.query(TaskModel).all()
        t_by_cls = {t.cluster_id: t.task_id for t in tasks}
        t_by_run = {t.latest_run_id: t.task_id for t in tasks if t.latest_run_id}
        res["saved_proposals"] = sync_rule_proposals_to_db(
            db,
            res.get("proposals") or [],
            project_key=project_key,
            task_by_cluster=t_by_cls,
            task_by_run=t_by_run,
        )
    return res


@app.post("/api/hardening/proposals/{proposal_id}/decide")
def decide_hardening_proposal_api(
    proposal_id: str,
    payload: dict = Body(default_factory=dict),
    db: Session = Depends(get_db),
):
    from app.hardening import decide_rule_proposal

    decision = str(payload.get("decision") or "approve").strip().lower()
    scope = str(payload.get("scope") or "global").strip().lower()
    try:
        return decide_rule_proposal(db, proposal_id, decision, scope=scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/hardening/history/{change_id}/rollback")
def rollback_hardening_change_api(change_id: int, db: Session = Depends(get_db)):
    from app.hardening import rollback_rule_change

    try:
        return rollback_rule_change(db, change_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _run_status(r: dict) -> str:
    status = str(r.get("overall_status") or r.get("status") or "").upper()
    if r.get("dry_run") and status == "VERIFIED_NO_CHANGES":
        return "DRY_RUN_VERIFIED"
    return status


def _run_timestamp(r: dict) -> Optional[str]:
    return r.get("finished_at") or r.get("created_at") or r.get("started_at")


def _run_summary(r: dict) -> str:
    if r.get("summary"):
        return str(r["summary"])
    steps = r.get("steps") or []
    for s in reversed(steps):
        msg = s.get("summary") or s.get("detail")
        if msg:
            return str(msg)
    return _run_status(r)


def _compute_pipeline_steps_state(latest_run: Optional[dict], has_clusters: bool) -> list[dict]:
    """Tính trạng thái 4 bước Processing Flow từ dữ liệu chạy thật."""
    if not latest_run:
        return [
            {"step": 1, "key": "collect_group", "status": "completed" if has_clusters else "pending"},
            {"step": 2, "key": "prepare_context", "status": "pending"},
            {"step": 3, "key": "agent_execution", "status": "pending"},
            {"step": 4, "key": "validate_deliver", "status": "pending"},
        ]

    run_status = _run_status(latest_run)
    raw_steps = latest_run.get("steps") or []
    step_map = {int(s.get("step") or s.get("step_number") or 0): str(s.get("status") or "").upper() for s in raw_steps}

    def norm_step(num: int, fallback: str = "pending") -> str:
        st = step_map.get(num, "")
        if st in {"DONE", "PASSED", "COMPLETED", "OK", "SKIPPED"}:
            return "completed"
        if st in {"RUNNING", "IN_PROGRESS"}:
            return "running"
        if st in {"FAILED", "BLOCKED", "ERROR"}:
            return "failed"
        return fallback

    # Mapping: B1 (collect_group) luôn completed khi đã có run; B2=step1/2, B3=step3, B4=step4
    s1 = "completed"
    s2 = norm_step(1, "completed" if run_status in COMPLETED_RUN_STATUSES else "pending")
    if s2 == "completed" and step_map.get(2):
        s2 = norm_step(2, s2)
    s3 = norm_step(3, "completed" if run_status in COMPLETED_RUN_STATUSES else "pending")
    s4 = norm_step(4, "completed" if run_status in COMPLETED_RUN_STATUSES else "pending")
    if run_status in FAILED_RUN_STATUSES and "failed" not in {s2, s3, s4}:
        if s3 == "pending":
            s2 = "failed"
        elif s4 == "pending":
            s3 = "failed"
        else:
            s4 = "failed"
    return [
        {"step": 1, "key": "collect_group", "status": s1},
        {"step": 2, "key": "prepare_context", "status": s2},
        {"step": 3, "key": "agent_execution", "status": s3},
        {"step": 4, "key": "validate_deliver", "status": s4},
    ]


def _build_trend_7d(issues: list[IssueModel], runs: list[dict], project_key: Optional[str] = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
    created_by_day = {d.isoformat(): 0 for d in days}
    completed_by_day = {d.isoformat(): 0 for d in days}

    for iss in issues:
        if project_key and iss.project_key != project_key:
            continue
        dt = iss.created_at or iss.updated_at
        if dt:
            key = dt.date().isoformat()
            if key in created_by_day:
                created_by_day[key] += 1

    seen_cluster_created: set[tuple[str, str]] = set()
    for r in reversed(runs):
        if project_key and r.get("project_key") != project_key:
            continue
        dt = _parse_iso_dt(_run_timestamp(r))
        if dt:
            key = dt.date().isoformat()
            cid_pair = (str(r.get("project_key") or ""), str(r.get("cluster_id") or r.get("run_id") or ""))
            if cid_pair not in seen_cluster_created:
                seen_cluster_created.add(cid_pair)
                if key in created_by_day:
                    created_by_day[key] += 1
            if classify_run_status(_run_status(r)) == "completed" and key in completed_by_day:
                completed_by_day[key] += 1

    return [
        {
            "date": d.isoformat(),
            "label": d.strftime("%b %d"),
            "created": created_by_day[d.isoformat()],
            "completed": completed_by_day[d.isoformat()],
        }
        for d in days
    ]


def _project_sources_labels(proj_cfg: dict) -> list[str]:
    labels: list[str] = []
    if proj_cfg.get("console_url") or proj_cfg.get("console_url_ios") or proj_cfg.get("console_url_android"):
        labels.append("Crashlytics")
    if proj_cfg.get("repository_path"):
        labels.append("Code Scan")
    for src in proj_cfg.get("sources") or []:
        if not src.get("enabled", True):
            continue
        t = str(src.get("type") or "").lower()
        if t in {"file", "inbox_json"} and "Task File" not in labels:
            labels.append("Task File")
    return labels or ["Crashlytics"]


@app.get("/api/dashboard/overview")
def get_dashboard_overview_api(
    project_key: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    from app.config import inspect_git_repository, load_all_projects
    from app.constrained_runner import ConstrainedFourStepRunner
    from app.context_collector import EventBufferQueue

    cfg_projects = load_all_projects()
    db_projects = {p.key: p for p in db.query(ProjectModel).options(selectinload(ProjectModel.issues)).all()}
    issues = db.query(IssueModel).options(selectinload(IssueModel.details)).order_by(IssueModel.events_count.desc()).all()
    queued = EventBufferQueue().load()
    clusters = [c.model_dump(mode="json") for c in queued]
    runs = [r.model_dump(mode="json") for r in ConstrainedFourStepRunner().list_runs()]
    aliases = EventBufferQueue.history_aliases(queued)
    for run in runs:
        run["cluster_id"] = aliases.get((run["project_key"], run["cluster_id"]), run["cluster_id"])
    sync_info = scheduler.status() if scheduler else {"enabled": False, "running": False}

    # Map latest run by (project_key, cluster_id)
    latest_run_by_cluster: dict[tuple[str, str], dict] = {}
    for r in runs:
        cid = r.get("cluster_id")
        pkey = str(r.get("project_key") or "")
        if cid and (pkey, cid) not in latest_run_by_cluster:
            latest_run_by_cluster[(pkey, cid)] = r

    # Filter for Task Status Donut (optional project_key)
    filtered_clusters = [c for c in clusters if not project_key or c.get("project_key") == project_key]
    filtered_runs = [r for r in runs if not project_key or r.get("project_key") == project_key]

    status_counts = {"completed": 0, "preview": 0, "running": 0, "pending": 0, "failed": 0}
    seen_cids: set[tuple[str, str]] = set()
    for c in filtered_clusters:
        cid = c.get("cluster_id")
        pkey = str(c.get("project_key") or "")
        seen_cids.add((pkey, cid))
        r = latest_run_by_cluster.get((pkey, cid))
        if r:
            status_counts[classify_run_status(_run_status(r))] += 1
        else:
            status_counts["pending"] += 1
    # Runs whose cluster is no longer in buffer
    for r in filtered_runs:
        cid = r.get("cluster_id")
        pkey = str(r.get("project_key") or "")
        if cid and (pkey, cid) not in seen_cids:
            seen_cids.add((pkey, cid))
            status_counts[classify_run_status(_run_status(r))] += 1

    # Global totals (across all projects)
    all_status = {"completed": 0, "preview": 0, "running": 0, "pending": 0, "failed": 0}
    all_seen: set[tuple[str, str]] = set()
    for c in clusters:
        cid = c.get("cluster_id")
        pkey = str(c.get("project_key") or "")
        all_seen.add((pkey, cid))
        r = latest_run_by_cluster.get((pkey, cid))
        all_status[classify_run_status(_run_status(r)) if r else "pending"] += 1
    for r in runs:
        cid = r.get("cluster_id")
        pkey = str(r.get("project_key") or "")
        if cid and (pkey, cid) not in all_seen:
            all_seen.add((pkey, cid))
            all_status[classify_run_status(_run_status(r))] += 1

    prs_created = sum(1 for r in runs if r.get("pr_url"))
    total_tasks_all = sum(all_status.values())
    total_tasks_filtered = sum(status_counts.values())

    # Per-project overview
    name_map = {p["key"]: (p.get("name") or p["key"]) for p in cfg_projects}
    projects_overview = []
    for p in cfg_projects:
        pkey = p["key"]
        db_p = db_projects.get(pkey)
        p_clusters = [c for c in clusters if c.get("project_key") == pkey]
        p_runs = [r for r in runs if r.get("project_key") == pkey]
        p_issues = [i for i in issues if i.project_key == pkey]
        p_seen = set()
        p_completed = 0
        p_running = 0
        p_failed = 0
        for c in p_clusters:
            cid = c.get("cluster_id")
            p_seen.add(cid)
            r = latest_run_by_cluster.get((pkey, cid))
            if r:
                cls = classify_run_status(_run_status(r))
                if cls == "completed":
                    p_completed += 1
                elif cls == "running":
                    p_running += 1
                elif cls == "failed":
                    p_failed += 1
        for r in p_runs:
            cid = r.get("cluster_id")
            if cid and cid not in p_seen:
                p_seen.add(cid)
                cls = classify_run_status(_run_status(r))
                if cls == "completed":
                    p_completed += 1
                elif cls == "running":
                    p_running += 1
                elif cls == "failed":
                    p_failed += 1

        p_total = len(p_seen)
        progress_pct = round((p_completed / p_total) * 100) if p_total > 0 else 0
        repo = str(p.get("repository_path") or "")
        git_info = inspect_git_repository(repo)
        repo_exists = git_info["repo_exists"]
        enabled = bool(p.get("enabled", True))

        if p_running > 0:
            p_status = "running"
        elif not enabled:
            p_status = "disabled"
        elif p_failed > 0 or not repo_exists:
            p_status = "attention"
        else:
            p_status = "active"

        last_cycle = None
        if p_runs:
            last_cycle = _run_timestamp(p_runs[0])
        elif db_p and db_p.last_synced_at:
            last_cycle = db_p.last_synced_at.isoformat()

        exec_cfg = p.get("execution") or {}
        projects_overview.append(
            {
                "key": pkey,
                "name": p.get("name") or pkey,
                "platform": p.get("platform") or "flutter",
                "enabled": enabled,
                "scan_interval_minutes": int(p.get("scan_interval_minutes") or 60),
                "dry_run": bool(exec_cfg.get("dry_run", True)),
                "auto_create_pr": bool(exec_cfg.get("auto_create_pr", False)),
                "auto_merge": bool(exec_cfg.get("auto_merge", False)),
                "base_branch": str(exec_cfg.get("base_branch") or git_info["default_base_branch"] or "main"),
                "current_branch": git_info["current_branch"],
                "git_branches": git_info["branches"],
                "is_git": git_info["is_git"],
                "repository_path": repo,
                "repo_exists": repo_exists,
                "tasks_count": p_total,
                "issues_count": len(p_issues),
                "completed_count": p_completed,
                "failed_count": p_failed,
                "prs_count": sum(1 for r in p_runs if r.get("pr_url")),
                "progress_pct": progress_pct,
                "status": p_status,
                "last_cycle_at": last_cycle,
                "sources": _project_sources_labels(p),
            }
        )

    # Recent activity from real runs + latest sync + issues
    activity = []
    for r in runs[:8]:
        r_st = _run_status(r) or "PENDING"
        st = classify_run_status(r_st)
        pkey = r.get("project_key") or ""
        pname = name_map.get(pkey, pkey)
        ts = _run_timestamp(r)
        if r.get("pr_url"):
            activity.append(
                {
                    "kind": "pr",
                    "title": f"PR created for {pname}",
                    "title_vi": f"Đã tạo PR cho {pname}",
                    "detail": r.get("cluster_title") or r.get("run_id"),
                    "timestamp": ts,
                }
            )
        else:
            activity.append(
                {
                    "kind": st,
                    "title": f"{r_st}: {r.get('cluster_title') or r.get('cluster_id')}",
                    "title_vi": f"{r_st}: {r.get('cluster_title') or r.get('cluster_id')}",
                    "detail": f"{pname} · {_run_summary(r)[:80]}",
                    "timestamp": ts,
                }
            )
    for iss in issues[:4]:
        pname = name_map.get(iss.project_key, iss.project_key)
        activity.append(
            {
                "kind": "collect",
                "title": f"Crashlytics: {iss.title} ({iss.events_count} events)",
                "title_vi": f"Crashlytics: {iss.title} ({iss.events_count} lượt)",
                "detail": pname,
                "timestamp": iss.updated_at.isoformat() if iss.updated_at else None,
            }
        )
    activity.sort(key=lambda x: x.get("timestamp") or "", reverse=True)

    # Failed / Blocked tasks (real)
    failed_tasks = []
    for r in latest_run_by_cluster.values():
        r_st = _run_status(r)
        if classify_run_status(r_st) == "failed":
            pkey = r.get("project_key") or ""
            failed_tasks.append(
                {
                    "run_id": r.get("run_id"),
                    "cluster_id": r.get("cluster_id"),
                    "project_key": pkey,
                    "project_name": name_map.get(pkey, pkey),
                    "title": r.get("cluster_title") or r.get("cluster_id"),
                    "status": r_st,
                    "reason": _run_summary(r),
                    "timestamp": _run_timestamp(r),
                }
            )

    active_runs = [r for r in runs if classify_run_status(_run_status(r)) == "running"]
    return {
        "kpis": {
            "total_projects": len(cfg_projects),
            "enabled_projects": sum(1 for p in cfg_projects if p.get("enabled", True)),
            "total_tasks": total_tasks_all,
            "total_issues": len(issues),
            "in_progress": all_status["running"],
            "prs_created": prs_created,
            "completed_tasks": all_status["completed"],
            "failed_tasks": all_status["failed"],
        },
        "task_status": {
            **status_counts,
            "total": total_tasks_filtered,
            "project_key": project_key,
        },
        "processing_flow": _compute_pipeline_steps_state(runs[0] if runs else None, len(clusters) > 0),
        "projects_overview": projects_overview,
        "recent_activity": activity[:6],
        "trend_7d": _build_trend_7d(issues, runs, project_key=project_key),
        "failed_tasks": failed_tasks[:8],
        "workers": {
            "running": len(active_runs) + (1 if sync_info.get("running") else 0),
            "active_run_id": active_runs[0].get("run_id") if active_runs else None,
            "scheduler": sync_info,
        },
    }


def _build_project_lifecycle_bundle(project_key: str, db: Session) -> dict:
    from app.config import inspect_git_repository, load_all_projects
    from app.constrained_runner import ConstrainedFourStepRunner
    from app.constrained_runner.agents import load_agent_settings
    from app.context_collector import EventBufferQueue
    from app.db.models import DiscoveryCycleModel, ProjectModel
    from app.db.task_lifecycle import (
        extract_verification_matrix,
        serialize_task,
        sync_tasks_for_project,
    )
    from app.hardening import (
        list_rule_change_history,
        load_environment_rules,
        read_rules_yaml_text,
        sample_recent_runs,
        sync_rule_proposals_to_db,
    )

    cfg_projects = load_all_projects()
    proj_cfg = next((p for p in cfg_projects if p["key"] == project_key), None)
    if not proj_cfg:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy dự án '{project_key}'")

    db_proj = db.query(ProjectModel).filter(ProjectModel.key == project_key).first()
    issues = (
        db.query(IssueModel).options(selectinload(IssueModel.details))
        .filter(IssueModel.project_key == project_key)
        .order_by(IssueModel.events_count.desc())
        .all()
    )
    queued = [c for c in EventBufferQueue().load() if c.project_key == project_key]
    clusters = [c.model_dump(mode="json") for c in queued]
    runs = [r.model_dump(mode="json") for r in ConstrainedFourStepRunner().list_runs() if r.project_key == project_key]
    aliases = EventBufferQueue.history_aliases(queued)
    for run in runs:
        run["cluster_id"] = aliases.get((run["project_key"], run["cluster_id"]), run["cluster_id"])
    agent_cfg = load_agent_settings()

    # Đồng bộ bền vững bảng `tasks` (TASK-1001...)
    task_models = sync_tasks_for_project(db, project_key, clusters, runs, record_discovery=False)
    task_by_cluster = {t.cluster_id: t for t in task_models}

    exec_cfg = proj_cfg.get("execution") or {}
    repo = str(proj_cfg.get("repository_path") or "")
    git_info = inspect_git_repository(repo)
    default_proj_base = str(exec_cfg.get("base_branch") or git_info["default_base_branch"] or "main")
    cluster_titles = {c.get("cluster_id"): c.get("title") for c in clusters if c.get("cluster_id")}

    runs_by_cluster: dict[str, list[dict]] = {}
    task_id_by_run: dict[str, str] = {}
    for run in runs:
        cid = run.get("cluster_id")
        t_obj = task_by_cluster.get(str(cid or ""))
        if t_obj and not run.get("task_id"):
            run["task_id"] = t_obj.task_id
        if run.get("task_id") and run.get("run_id"):
            task_id_by_run[run["run_id"]] = run["task_id"]
        run["status_group"] = classify_run_status(_run_status(run))
        run["summary"] = _run_summary(run)
        if not run.get("cluster_title"):
            run["cluster_title"] = (t_obj.title if t_obj else None) or cluster_titles.get(cid) or cid
        s1 = next((s for s in (run.get("steps") or []) if (s.get("step_number") or s.get("step")) == 1), None)
        s4 = next((s for s in (run.get("steps") or []) if (s.get("step_number") or s.get("step")) == 4), None)
        run["base_branch"] = str(((s1 or {}).get("details") or {}).get("base_branch") or default_proj_base)
        if s4:
            run["step4"] = s4
        run["verification"] = extract_verification_matrix(run)
        if cid:
            runs_by_cluster.setdefault(cid, []).append(run)

    recent_tasks: list[dict] = []
    completed = 0
    for t_obj in task_models:
        c_runs = runs_by_cluster.get(t_obj.cluster_id, [])
        latest_r = c_runs[0] if c_runs else None
        serialized = serialize_task(t_obj, latest_run=latest_r, runs_count=len(c_runs))
        if serialized["status"] == "completed":
            completed += 1
        recent_tasks.append(serialized)

    # Sắp xếp Task Queue theo mức ưu tiên (High -> Medium -> Low) và điểm tác động
    prio_rank = {"high": 0, "medium": 1, "low": 2}
    status_rank = {"running": 0, "failed": 1, "pending": 2, "preview": 3, "completed": 4, "skipped": 5}
    task_queue = sorted(
        recent_tasks,
        key=lambda item: (
            status_rank.get(item["status"], 3),
            prio_rank.get(item["priority"], 1),
            -(item.get("impact_score") or 0),
        ),
    )

    total_tasks = len(recent_tasks)
    progress_pct = round((completed / total_tasks) * 100) if total_tasks > 0 else 0

    # --- Discovery Status (Giai đoạn 1 + 2) ---
    scan_state_file = Path("data/context_queue/scan_state.json")
    raw_last_scan: Optional[str] = None
    if scan_state_file.is_file():
        try:
            st_map = json.loads(scan_state_file.read_text(encoding="utf-8"))
            if isinstance(st_map, dict):
                raw_last_scan = st_map.get(project_key)
        except Exception:
            raw_last_scan = None
    last_disc = (
        db.query(DiscoveryCycleModel)
        .filter(DiscoveryCycleModel.project_key == project_key)
        .order_by(DiscoveryCycleModel.id.desc())
        .first()
    )
    last_scan_dt = (
        _parse_iso_dt(raw_last_scan)
        or (last_disc.created_at if last_disc else None)
        or (db_proj.last_synced_at if db_proj else None)
    )
    if last_scan_dt and last_scan_dt.tzinfo is None:
        last_scan_dt = last_scan_dt.replace(tzinfo=timezone.utc)
    interval_min = int(proj_cfg.get("scan_interval_minutes") or 60)
    next_scan_iso: Optional[str] = None
    if bool(proj_cfg.get("enabled", True)):
        if last_scan_dt:
            next_scan_iso = (last_scan_dt + timedelta(minutes=interval_min)).isoformat()
        elif scheduler and scheduler.next_run:
            next_scan_iso = scheduler.next_run.isoformat(timespec="seconds")

    last_orch_candidates = [
        *(
            [last_disc.created_at.replace(tzinfo=timezone.utc) if last_disc.created_at.tzinfo is None else last_disc.created_at]
            if last_disc and last_disc.created_at
            else []
        ),
        *[
            t.last_orchestrated_at.replace(tzinfo=timezone.utc) if t.last_orchestrated_at.tzinfo is None else t.last_orchestrated_at
            for t in task_models
            if t.last_orchestrated_at
        ],
    ]
    last_orch_dt = max(last_orch_candidates) if last_orch_candidates else last_scan_dt
    events_collected_total = sum(int(c.get("event_count") or len(c.get("events") or [])) for c in clusters)
    sources_labels = _project_sources_labels(proj_cfg)

    discovery_status = {
        "last_scan": last_scan_dt.isoformat() if last_scan_dt else None,
        "next_scan": next_scan_iso,
        "scan_interval_minutes": interval_min,
        "sources": sources_labels,
        "events_collected": events_collected_total or sum(i.events_count for i in issues),
        "clusters_detected": len(clusters),
        "tasks_generated": total_tasks,
        "ready_tasks": sum(1 for t in recent_tasks if t["lifecycle_status"] == "ready"),
        "last_orchestration": last_orch_dt.isoformat() if last_orch_dt else None,
        "auto_run": bool(exec_cfg.get("auto_run", True)),
    }

    # --- Verifications Center (Giai đoạn 4) ---
    prs = []
    seen_pr_cids: set[str] = set()
    verif_rows: list[dict] = []
    for r in runs:
        cid = str(r.get("cluster_id") or r.get("run_id") or "")
        if (r.get("pr_url") or r.get("status_group") in {"completed", "preview"}) and cid not in seen_pr_cids:
            seen_pr_cids.add(cid)
            prs.append(r)
        vm = r["verification"]
        t_obj = task_by_cluster.get(str(r.get("cluster_id") or ""))
        verif_rows.append(
            {
                "run_id": r.get("run_id"),
                "task_id": r.get("task_id") or (t_obj.task_id if t_obj else cid),
                "task_title": r.get("cluster_title") or (t_obj.title if t_obj else cid),
                "cluster_id": r.get("cluster_id"),
                "stage_4a": vm["stage_4a"],
                "stage_4b": vm["stage_4b"],
                "stage_4c": vm["stage_4c"],
                "result": vm["result"],
                "overall_status": r.get("overall_status"),
                "status_group": r.get("status_group"),
                "dry_run": bool(r.get("dry_run", False)),
                "attempts_count": vm["attempts_count"],
                "self_healed": vm["self_healed"],
                "changed_files": vm["changed_files"],
                "diff_violations": vm["diff_violations"],
                "verifier_agent": vm["verifier_agent"],
                "delivery": vm["delivery"],
                "pr_url": r.get("pr_url"),
                "created_at": r.get("created_at"),
            }
        )

    verifications_center = {
        "kpis": {
            "total": len(verif_rows),
            "passed": sum(1 for v in verif_rows if v["result"] == "PASS"),
            "failed": sum(1 for v in verif_rows if v["result"] == "FAIL"),
            "skipped": sum(1 for v in verif_rows if v["result"] == "SKIP"),
            "running": sum(1 for v in verif_rows if v["result"] == "Running"),
            "self_healed": sum(1 for v in verif_rows if v["self_healed"]),
            "prs": len(prs),
        },
        "recent": verif_rows[:30],
        "queue": [v for v in verif_rows if v["result"] == "Running"],
        "failed": [v for v in verif_rows if v["result"] == "FAIL"][:20],
        "self_healing": [v for v in verif_rows if v["self_healed"]][:20],
        "deliveries": prs,
    }

    # --- Feedback & Hardening (Giai đoạn 5) ---
    sampled = sample_recent_runs(project_key=project_key, limit=30, preloaded_reports=runs[:30])
    persisted_proposals = sync_rule_proposals_to_db(
        db,
        sampled.get("proposals") or [],
        project_key=project_key,
        task_by_cluster={cid: t.task_id for cid, t in task_by_cluster.items()},
        task_by_run=task_id_by_run,
    )
    active_rules = load_environment_rules().model_dump(mode="json", exclude_none=True)
    rule_history = list_rule_change_history(db, project_key=project_key, limit=30)
    review_items: list[dict] = []
    for r in prs:
        if not r.get("verification", {}).get("delivery", {}).get("merged"):
            review_items.append(
                {
                    "kind": "pr_review",
                    "task_id": r.get("task_id") or r.get("cluster_id"),
                    "run_id": r.get("run_id"),
                    "title": r.get("cluster_title") or r.get("cluster_id"),
                    "status": r.get("overall_status"),
                    "pr_url": r.get("pr_url"),
                    "detail": r.get("summary") or "",
                    "timestamp": r.get("created_at"),
                }
            )
    for v in verif_rows:
        if v["result"] == "FAIL":
            va_txt = (v.get("verifier_agent") or {}).get("verdict_text") or ""
            viol = "; ".join(v.get("diff_violations") or [])
            review_items.append(
                {
                    "kind": "verification_failure",
                    "task_id": v["task_id"],
                    "run_id": v["run_id"],
                    "title": v["task_title"],
                    "status": v["overall_status"],
                    "pr_url": None,
                    "detail": (viol or va_txt or "Kiểm định độc lập báo lỗi")[:240],
                    "timestamp": v["created_at"],
                }
            )

    hardening_center = {
        "summary": {
            **sampled.get("summary", {}),
            "pending_proposals": sum(1 for p in persisted_proposals if p["status"] == "pending_review"),
            "approved_proposals": sum(1 for p in persisted_proposals if p["status"] == "approved"),
            "review_needed": len(review_items),
        },
        "review_center": review_items[:20],
        "proposals": persisted_proposals,
        "environment_rules": active_rules,
        "yaml_text": read_rules_yaml_text(),
        "change_history": rule_history,
    }

    # --- Unified Project Activity Timeline (Tab 05. Activity) ---
    activity_items: list[dict] = []
    for dc in (
        db.query(DiscoveryCycleModel)
        .filter(DiscoveryCycleModel.project_key == project_key)
        .order_by(DiscoveryCycleModel.id.desc())
        .limit(10)
        .all()
    ):
        activity_items.append(
            {
                "type": "discovery",
                "tone": "info",
                "title": f"Discovery & Task Planning · {dc.clusters_detected} cụm ({dc.events_collected} sự kiện)",
                "detail": f"Tạo mới {dc.tasks_created} task · Cập nhật {dc.tasks_updated} task",
                "timestamp": dc.created_at.isoformat() if dc.created_at else None,
            }
        )
    for r in runs[:25]:
        t_id = r.get("task_id") or r.get("cluster_id")
        vm = r["verification"]
        tone = "ok" if r["status_group"] == "completed" else ("danger" if r["status_group"] == "failed" else "info")
        activity_items.append(
            {
                "type": "run",
                "tone": tone,
                "task_id": t_id,
                "run_id": r.get("run_id"),
                "title": f"{t_id} · {r.get('cluster_title') or r.get('cluster_id')}",
                "detail": f"{r.get('overall_status')} · 4A:{vm['stage_4a']} 4B:{vm['stage_4b']} 4C:{vm['stage_4c']}"
                + (f" · Self-healed ({vm['attempts_count']} attempts)" if vm["self_healed"] else ""),
                "pr_url": r.get("pr_url"),
                "timestamp": r.get("created_at"),
            }
        )
    for ch in rule_history[:15]:
        activity_items.append(
            {
                "type": "hardening",
                "tone": "warn" if ch["action"] == "rollback" else "ok",
                "task_id": ch.get("source_task_id"),
                "run_id": ch.get("source_run_id"),
                "title": f"Hardening ({ch['action']}) · {ch['summary']}",
                "detail": f"Rule: {ch.get('rule_id') or 'YAML'}",
                "timestamp": ch.get("created_at"),
            }
        )
    activity_items.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)

    logs = []
    for r in runs[:8]:
        for s in r.get("steps") or []:
            logs.append(
                {
                    "run_id": r.get("run_id"),
                    "task_id": r.get("task_id"),
                    "step_number": s.get("step") or s.get("step_number") or 0,
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "detail": s.get("summary") or s.get("detail") or "",
                    "timestamp": s.get("finished_at") or s.get("started_at") or _run_timestamp(r),
                }
            )
    file_src = next((s for s in (proj_cfg.get("sources") or []) if s.get("type") == "file"), None)
    primary_agent_id = agent_cfg.routing.get("bug_fix") or agent_cfg.default_agent
    primary_agent_label = (
        agent_cfg.profiles[primary_agent_id].label
        if primary_agent_id in agent_cfg.profiles
        else primary_agent_id
    )
    rv_cfg = exec_cfg.get("runtime_verifier") if isinstance(exec_cfg.get("runtime_verifier"), dict) else {}

    return {
        "project": {
            "key": project_key,
            "name": proj_cfg.get("name") or project_key,
            "platform": proj_cfg.get("platform") or "flutter",
            "firebase_project_id": proj_cfg.get("firebase_project_id") or "",
            "console_url": proj_cfg.get("console_url") or "",
            "console_url_ios": proj_cfg.get("console_url_ios") or "",
            "console_url_android": proj_cfg.get("console_url_android") or "",
            "repository_path": repo,
            "repo_exists": git_info["repo_exists"],
            "is_git": git_info["is_git"],
            "current_branch": git_info["current_branch"],
            "git_branches": git_info["branches"],
            "default_base_branch": git_info["default_base_branch"],
            "enabled": bool(proj_cfg.get("enabled", True)),
            "scan_interval_minutes": interval_min,
            "auto_run": bool(exec_cfg.get("auto_run", True)),
            "dry_run": bool(exec_cfg.get("dry_run", True)),
            "auto_create_pr": bool(exec_cfg.get("auto_create_pr", False)),
            "auto_merge": bool(exec_cfg.get("auto_merge", False)),
            "max_clusters_per_cycle": int(exec_cfg.get("max_clusters_per_cycle") or 3),
            "max_retries": int(exec_cfg.get("max_retries") if exec_cfg.get("max_retries") is not None else 1),
            "protected_branches": list(exec_cfg.get("protected_branches") or ["main", "master", "production"]),
            "runtime_verifier": {
                "enabled": bool(rv_cfg.get("enabled", False)),
                "mode": str(rv_cfg.get("mode") or ("flutter_mobile" if proj_cfg.get("platform") == "flutter" else "web_cdp")),
                "url": rv_cfg.get("url") or "",
                "device_id": rv_cfg.get("device_id") or "",
                "test_target": rv_cfg.get("test_target") or "integration_test",
            },
            "base_branch": default_proj_base,
            "input_file_path": str(file_src.get("path") or "") if file_src else "",
            "sources": sources_labels,
            "agent_label": primary_agent_label,
        },
        "kpis": {
            "tasks": total_tasks,
            "issues": len(issues),
            "progress_pct": progress_pct,
            "prs": len(prs),
            "completed": completed,
        },
        "discovery_status": discovery_status,
        "task_queue": task_queue,
        "execution_pipeline": _compute_pipeline_steps_state(runs[0] if runs else None, len(clusters) > 0),
        "progress_7d": _build_trend_7d(issues, runs, project_key=project_key),
        "recent_tasks": recent_tasks,
        "verifications": verifications_center,
        "hardening": hardening_center,
        "activity": activity_items[:50],
        "issues": [
            {
                "issue_id": i.issue_id,
                "title": i.title,
                "subtitle": i.subtitle,
                "issue_type": i.issue_type,
                "events_count": i.events_count,
                "users_count": i.users_count,
                "has_details": len(i.details) > 0,
                "updated_at": i.updated_at.isoformat() if i.updated_at else None,
            }
            for i in issues
        ],
        "prs": prs,
        "cycles": runs,
        "logs": logs[:50],
    }


@app.get("/api/projects/{project_key}/overview")
def get_project_detail_overview_api(project_key: str, db: Session = Depends(get_db)):
    return _build_project_lifecycle_bundle(project_key, db)


@app.post("/api/projects/{project_key}/discover")
def run_project_discovery_api(project_key: str, db: Session = Depends(get_db)):
    """Chạy chu trình Discovery & Task Planning (Giai đoạn 1 + 2): thu thập ngữ cảnh & cập nhật Task Queue."""
    from app.config import load_all_projects, project_sources
    from app.constrained_runner import ConstrainedFourStepRunner
    from app.orchestrator import _load_scan_state, _save_scan_state, build_collector_from_projects
    from app.db.task_lifecycle import sync_tasks_for_project

    proj = next((p for p in load_all_projects() if p["key"] == project_key), None)
    if not proj:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy dự án '{project_key}'")
    if not proj.get("enabled", True):
        raise HTTPException(status_code=409, detail="Dự án đang tắt.")

    manager, _ = build_collector_from_projects([proj])
    collection = manager.collect_all(project_key=project_key)
    runs = [r.model_dump(mode="json") for r in ConstrainedFourStepRunner().list_runs() if r.project_key == project_key]
    cls_dicts = [c.model_dump(mode="json") for c in collection.clusters]
    src_names = [str(s.get("type") or "") for s in project_sources(proj)]
    tasks = sync_tasks_for_project(
        db,
        project_key,
        cls_dicts,
        runs,
        record_discovery=True,
        events_collected=collection.total_events,
        sources_labels=src_names,
    )
    scan_state_path = Path("data/context_queue/scan_state.json")
    st = _load_scan_state(scan_state_path)
    st[project_key] = datetime.now(timezone.utc).isoformat()
    _save_scan_state(scan_state_path, st)

    return {
        "discovered": True,
        "project_key": project_key,
        "events_collected": collection.total_events,
        "clusters_detected": len(collection.clusters),
        "tasks_count": len(tasks),
    }


@app.get("/api/projects/{project_key}/tasks")
def list_project_tasks_api(project_key: str, db: Session = Depends(get_db)):
    bundle = _build_project_lifecycle_bundle(project_key, db)
    return {"project_key": project_key, "tasks": bundle["task_queue"]}


@app.get("/api/tasks/{task_ref}")
def get_task_detail_api(
    task_ref: str,
    project_key: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    from app.constrained_runner import ConstrainedFourStepRunner
    from app.context_collector import EventBufferQueue
    from app.db.models import TaskModel
    from app.db.task_lifecycle import extract_verification_matrix, read_run_diff_text, serialize_task

    q = db.query(TaskModel).filter((TaskModel.task_id == task_ref) | (TaskModel.cluster_id == task_ref))
    if project_key:
        q = q.filter(TaskModel.project_key == project_key)
    task = q.first()
    if not task:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy Task '{task_ref}'")

    cluster_obj = EventBufferQueue().get_cluster(task.cluster_id)
    all_runs = [
        r.model_dump(mode="json")
        for r in ConstrainedFourStepRunner().list_runs()
        if r.project_key == task.project_key and (r.cluster_id == task.cluster_id or r.task_id == task.task_id)
    ]
    for r in all_runs:
        r["task_id"] = task.task_id
        r["status_group"] = classify_run_status(_run_status(r))
        r["summary"] = _run_summary(r)
        r["verification"] = extract_verification_matrix(r)

    latest_r = all_runs[0] if all_runs else None
    diff_info = read_run_diff_text(latest_r["run_id"]) if latest_r else {"found": False, "patch_text": "", "patch_files": []}
    brief_md = ""
    if latest_r:
        brief_file = Path("data/runs") / latest_r["run_id"] / "brief.md"
        if brief_file.is_file():
            brief_md = brief_file.read_text(encoding="utf-8", errors="replace")[:60_000]

    return {
        "task": serialize_task(task, latest_run=latest_r, runs_count=len(all_runs)),
        "cluster": cluster_obj.model_dump(mode="json") if cluster_obj else None,
        "runs": all_runs,
        "brief_markdown": brief_md,
        "diff": diff_info,
    }


@app.patch("/api/tasks/{task_ref}")
def update_task_api(task_ref: str, payload: dict = Body(default_factory=dict), db: Session = Depends(get_db)):
    from app.db.models import TaskModel
    from app.db.task_lifecycle import serialize_task

    task = db.query(TaskModel).filter((TaskModel.task_id == task_ref) | (TaskModel.cluster_id == task_ref)).first()
    if not task:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy Task '{task_ref}'")
    if "priority" in payload and payload["priority"] in ("high", "medium", "low"):
        task.priority = payload["priority"]
        task.priority_manual = True
    if "status" in payload and payload["status"] in ("ready", "skipped"):
        task.status = payload["status"]
        task.raw_status = payload["status"].upper()
    db.commit()
    db.refresh(task)
    return {"saved": True, "task": serialize_task(task)}


@app.post("/api/tasks/{task_ref}/run")
async def execute_task_api(
    task_ref: str,
    project_key: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    from app.context_collector import EventBufferQueue
    from app.db.models import TaskModel

    q = db.query(TaskModel).filter((TaskModel.task_id == task_ref) | (TaskModel.cluster_id == task_ref))
    if project_key:
        q = q.filter(TaskModel.project_key == project_key)
    task = q.first()
    cid = task.cluster_id if task else task_ref
    pkey = task.project_key if task else project_key
    cluster = EventBufferQueue().get_cluster(cid)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Tác vụ không còn trong hàng đợi ngữ cảnh.")
    if task and task.status == "skipped":
        task.status = "ready"
        task.raw_status = "READY"
        db.commit()
    return _queue_cycle(pkey or cluster.project_key, cluster.cluster_id)


@app.get("/api/runs/{run_id}/diff")
def get_run_diff_and_brief_api(run_id: str):
    from app.db.task_lifecycle import read_run_diff_text

    try:
        diff_info = read_run_diff_text(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runs_root = Path("data/runs").resolve()
    art_dir = (runs_root / run_id).resolve()
    if not art_dir.is_relative_to(runs_root) or not art_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy Run '{run_id}'")
    brief_path = art_dir / "brief.md"
    brief_md = brief_path.read_text(encoding="utf-8", errors="replace")[:60_000] if brief_path.is_file() else ""
    return {**diff_info, "brief_markdown": brief_md}


@app.get("/api/projects/{project_key}/verifications")
def get_project_verifications_api(project_key: str, db: Session = Depends(get_db)):
    bundle = _build_project_lifecycle_bundle(project_key, db)
    return bundle["verifications"]


@app.get("/api/projects/{project_key}/hardening")
def get_project_hardening_api(project_key: str, db: Session = Depends(get_db)):
    bundle = _build_project_lifecycle_bundle(project_key, db)
    return bundle["hardening"]


@app.get("/api/projects/{project_key}/activity")
def get_project_activity_api(project_key: str, db: Session = Depends(get_db)):
    bundle = _build_project_lifecycle_bundle(project_key, db)
    return {"project_key": project_key, "activity": bundle["activity"]}


@app.get("/api/system/git-branches")
def inspect_git_branches_api(repository_path: str = Query(default="")):
    """Kiểm tra nhánh Git đang đứng (current_branch) và danh sách các nhánh trong repository local."""
    from app.config import inspect_git_repository

    info = inspect_git_repository(repository_path)
    return {"repository_path": repository_path, **info}


@app.post("/api/system/pick-path")
def pick_local_path_api(payload: dict = Body(default={})):
    """Mở hộp thoại Finder/OS gốc để chọn thư mục Source Code hoặc File đầu vào."""
    mode = str(payload.get("mode") or "folder").lower()
    if py_platform.system() != "Darwin":
        raise HTTPException(status_code=501, detail="Native folder picker hiện hỗ trợ trên macOS. Vui lòng nhập đường dẫn trực tiếp.")

    if mode == "file":
        script = 'POSIX path of (choose file with prompt "Chọn file dữ liệu đầu vào")'
    else:
        script = 'POSIX path of (choose folder with prompt "Chọn thư mục Source Code Local")'

    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"cancelled": True, "path": None}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "-128" in err or "User canceled" in err:
            return {"cancelled": True, "path": None}
        raise HTTPException(status_code=500, detail=f"Không mở được hộp thoại chọn đường dẫn: {err}")

    selected = (proc.stdout or "").strip().rstrip("/")
    return {"cancelled": False, "path": selected}


@app.post("/api/runs/{cluster_id}/retry")
async def retry_cluster_run_api(cluster_id: str):
    from app.context_collector import EventBufferQueue

    cluster = EventBufferQueue().get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(404, "Tác vụ không còn trong hàng đợi.")
    return _queue_cycle(cluster.project_key, cluster.cluster_id)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=DASHBOARD_FILE.read_text(encoding="utf-8"))
