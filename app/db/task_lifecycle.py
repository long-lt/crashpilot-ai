"""Quản lý vòng đời Task (Task Lifecycle): Cluster -> Task -> Run -> Attempt & Verification."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.constrained_runner.models import classify_run_status
from app.db.models import DiscoveryCycleModel, TaskModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def infer_specialist(category_or_track: Optional[str]) -> str:
    val = str(category_or_track or "").strip().lower()
    if val in ("bug_fix", "crash", "bug"):
        return "bug_fix"
    if val == "performance":
        return "performance"
    if val in ("code_gardening", "tech_debt", "gardening"):
        return "code_gardening"
    if val == "experimental":
        return "experimental"
    return "bug_fix"


def specialist_label(specialist: Optional[str], lang: str = "vi") -> str:
    sp = infer_specialist(specialist)
    labels = {
        "bug_fix": ("Bug Fix", "Bug Fix"),
        "performance": ("Performance", "Performance"),
        "code_gardening": ("Gardening", "Gardening"),
        "experimental": ("Experimental", "Experimental"),
    }
    vi_lbl, en_lbl = labels.get(sp, ("Bug Fix", "Bug Fix"))
    return vi_lbl if lang == "vi" else en_lbl


def infer_priority(cluster: dict[str, Any]) -> str:
    events = cluster.get("events") or []
    severities = {str(ev.get("severity") or "").lower() for ev in events if isinstance(ev, dict)}
    cat = str(cluster.get("category") or "").lower()
    impact = int(cluster.get("total_impact") or cluster.get("events_count") or 0)
    if "critical" in severities or "high" in severities or cat == "crash" or impact >= 5:
        return "high"
    if cat == "tech_debt" and impact <= 1 and "medium" not in severities:
        return "low"
    return "medium"


def format_source_labels(sources: list[str]) -> list[str]:
    out: list[str] = []
    for s in sources or []:
        low = str(s).lower()
        lbl = "Crashlytics"
        if "code_audit" in low:
            lbl = "Code Audit"
        elif any(k in low for k in ("file", "inbox")):
            lbl = "File Input"
        elif "crashlytics" in low:
            lbl = "Crashlytics"
        else:
            lbl = str(s)
        if lbl not in out:
            out.append(lbl)
    return out or ["Crashlytics"]


def _next_task_number(db: Session) -> int:
    rows = db.query(TaskModel.task_id).all()
    max_num = 1000
    for (tid,) in rows:
        m = re.match(r"^TASK-(\d+)$", str(tid or ""))
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max_num + 1


def _run_effective_status(run: dict[str, Any]) -> str:
    st = str(run.get("overall_status") or "")
    if run.get("dry_run") and st in {"VERIFIED_READY_FOR_PR", "VERIFIED_NO_CHANGES", "DRY_RUN_VERIFIED"}:
        return "DRY_RUN_VERIFIED"
    return st or "PENDING"


WORKFLOW_STATUSES = (
    "TODO",
    "IN_PROGRESS",
    "IN_REVIEW",
    "DONE",
    "CANCELLED",
    "BLOCKED",
    "ON_HOLD",
    "REOPENED",
)


def _is_missing_repo_run(run: Optional[dict[str, Any]]) -> bool:
    if not run:
        return False
    for s in run.get("steps") or []:
        if (s.get("step_number") or s.get("step")) == 1:
            summary = str(s.get("summary") or "")
            det = s.get("details") or {}
            if "repository không tồn tại" in summary.lower() or det.get("reason") == "missing_repo":
                return True
    return False


def extract_failure_info(
    latest_run: Optional[dict[str, Any]],
    runs_dir: Path = Path("data/runs"),
) -> Optional[dict[str, Any]]:
    """Trích xuất bước bị lỗi/chặn và lý do chi tiết thực tế để hiển thị & cho phép Retry từ bước đó."""
    if not latest_run or _is_missing_repo_run(latest_run):
        return None
    failed_step = next(
        (s for s in (latest_run.get("steps") or []) if s.get("status") in {"failed", "blocked"}),
        None,
    )
    if not failed_step:
        return None
    step_num = int(failed_step.get("step_number") or failed_step.get("step") or 1)
    step_name = str(failed_step.get("name") or f"Step {step_num}")
    summary = str(failed_step.get("summary") or "")
    det = failed_step.get("details") or {}

    detail_parts: list[str] = []
    rv = det.get("runtime_verifier") or {}
    if rv.get("passed") is False:
        rv_msg = (
            rv.get("skipped_reason")
            or rv.get("mobile_output")
            or "; ".join(str(e) for e in (rv.get("page_errors") or rv.get("console_errors") or rv.get("server_errors") or [])[:2])
        )
        if rv_msg:
            detail_parts.append(f"4B Runtime ({rv.get('mode') or 'runtime'}): {str(rv_msg).strip()[:320]}")
    va = det.get("verifier_agent") or {}
    if va.get("error"):
        detail_parts.append(str(va["error"]).strip())
    elif va.get("verdict_text") and va.get("passed") is False:
        detail_parts.append(str(va["verdict_text"]).strip()[:400])
    if det.get("diff_violations"):
        detail_parts.append("Diff violations: " + "; ".join(str(v) for v in det["diff_violations"][:3]))
    for chk in det.get("verify_checks") or []:
        if chk.get("passed") is False:
            tail = " ".join(str(chk.get("output_tail") or "").splitlines())[-220:]
            detail_parts.append(f"{chk.get('cmd')} (exit {chk.get('exit_code')}): {tail}")
            break
    for att in (det.get("attempts") or [])[-1:]:
        exec_info = att.get("executor") or {}
        if exec_info.get("error"):
            detail_parts.append(str(exec_info["error"]).strip()[:260])
        for chk in att.get("checks") or []:
            if chk.get("passed") is False:
                tail = " ".join(str(chk.get("output_tail") or "").splitlines())[-220:]
                detail_parts.append(f"{chk.get('cmd')} (exit {chk.get('exit_code')}): {tail}")
                break

    run_id = str(latest_run.get("run_id") or "")
    art_dir = runs_dir / run_id if run_id else None
    worktree_ok = bool(art_dir and (art_dir / "worktree").is_dir())
    can_resume = bool(run_id and step_num >= 3 and worktree_ok)

    return {
        "step_number": step_num,
        "step_code": f"Bước {step_num}",
        "step_name": step_name,
        "summary": summary,
        "detail": "\n".join(p for p in detail_parts if p)[:600],
        "run_id": run_id,
        "can_resume": can_resume,
        "resume_step": step_num if can_resume else 1,
    }


def map_workflow_status(
    task: TaskModel,
    latest_run: Optional[dict[str, Any]] = None,
) -> str:
    """Ánh xạ về 8 trạng thái chuẩn: TODO, IN_PROGRESS, IN_REVIEW, DONE, CANCELLED, BLOCKED, ON_HOLD, REOPENED."""
    raw = str(task.raw_status or "").strip().upper()
    if raw in WORKFLOW_STATUSES:
        if raw == "BLOCKED" and _is_missing_repo_run(latest_run):
            return "TODO"
        return raw
    st = str(task.status or "").strip().lower()
    if st in {"skipped", "cancelled"}:
        return "CANCELLED"
    if st == "on_hold":
        return "ON_HOLD"
    if st == "reopened":
        return "REOPENED"
    if st == "running":
        return "IN_PROGRESS"
    if st in {"verifying", "preview"} or raw in {
        "PR_CREATED",
        "AUTO_MERGE_REQUESTED",
        "VERIFIED_READY_FOR_PR",
        "PUSHED_BRANCH",
        "DRY_RUN_VERIFIED",
    }:
        return "IN_REVIEW"
    if st == "completed" or raw in {"AUTO_MERGED", "VERIFIED_NO_CHANGES", "PR_CLOSED"}:
        return "DONE"
    if st == "failed":
        if _is_missing_repo_run(latest_run):
            return "TODO"
        return "BLOCKED"
    return "TODO"


def derive_task_status_from_run(run: Optional[dict[str, Any]]) -> tuple[str, str]:
    """Trả về (task_status, raw_status)."""
    if not run:
        return "ready", "TODO"
    if _is_missing_repo_run(run):
        return "ready", "TODO"
    raw = _run_effective_status(run)
    group = classify_run_status(raw)
    if group == "running":
        cur_step = run.get("current_step")
        if cur_step == 4:
            return "verifying", "IN_REVIEW"
        return "running", "IN_PROGRESS"
    if group == "completed":
        return "completed", raw
    if group == "preview":
        return "preview", raw
    if group == "failed":
        return "failed", raw
    return "ready", "TODO"


def sync_tasks_for_project(
    db: Session,
    project_key: Optional[str],
    clusters: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    *,
    record_discovery: bool = False,
    events_collected: Optional[int] = None,
    sources_labels: Optional[list[str]] = None,
) -> list[TaskModel]:
    """Đồng bộ idempotent EventCluster + lịch sử Run vào bảng `tasks` (chống trùng lặp)."""
    next_num = _next_task_number(db)
    now = utc_now()

    query = db.query(TaskModel)
    if project_key:
        query = query.filter(TaskModel.project_key == project_key)
    existing_tasks = query.all()
    by_dedup: dict[str, TaskModel] = {t.dedup_key: t for t in existing_tasks}
    by_cluster: dict[tuple[str, str], TaskModel] = {(t.project_key, t.cluster_id): t for t in existing_tasks}

    # Sắp xếp runs mới nhất trước
    sorted_runs = sorted(
        runs,
        key=lambda r: str(r.get("created_at") or r.get("run_id") or ""),
        reverse=True,
    )
    latest_run_by_cluster: dict[tuple[str, str], dict[str, Any]] = {}
    verified_pr_by_cluster: dict[tuple[str, str], str] = {}
    for r in sorted_runs:
        pk = str(r.get("project_key") or "")
        cid = str(r.get("cluster_id") or "")
        if not pk or not cid:
            continue
        if project_key and pk != project_key:
            continue
        if (pk, cid) not in latest_run_by_cluster:
            latest_run_by_cluster[(pk, cid)] = r
        if r.get("pr_url") and (pk, cid) not in verified_pr_by_cluster:
            verified_pr_by_cluster[(pk, cid)] = str(r["pr_url"])

    created_cnt = 0
    updated_cnt = 0
    dirty = False
    seen_pairs: set[tuple[str, str]] = set()

    def _set_if_changed(obj: TaskModel, field: str, val: Any) -> None:
        nonlocal dirty
        if getattr(obj, field) != val:
            setattr(obj, field, val)
            dirty = True

    for c in clusters:
        pk = str(c.get("project_key") or "")
        cid = str(c.get("cluster_id") or "")
        if not pk or not cid:
            continue
        if project_key and pk != project_key:
            continue
        seen_pairs.add((pk, cid))
        root_key = str(c.get("root_cause_key") or cid)
        dedup_key = f"{pk}::{root_key}"

        latest_run = latest_run_by_cluster.get((pk, cid))
        task_st, raw_st = derive_task_status_from_run(latest_run)
        run_track = latest_run.get("track") if latest_run else None
        spec = infer_specialist(run_track or c.get("category"))
        prio = infer_priority(c)
        src_labels = format_source_labels(c.get("sources") or [])
        ev0 = (c.get("events") or [{}])[0] if c.get("events") else {}
        summary_txt = str(ev0.get("body") or c.get("sample_stack_trace") or "").strip()[:2000]
        ev_cnt = int(c.get("event_count") or c.get("total_events") or len(c.get("events") or []))
        impact = int(c.get("total_impact") or ev_cnt)
        users_cnt = int(c.get("total_users") or 0)
        pr_url = verified_pr_by_cluster.get((pk, cid)) or (latest_run.get("pr_url") if latest_run else None)
        latest_rid = str(latest_run.get("run_id") or "") if latest_run else ""

        task = by_dedup.get(dedup_key) or by_cluster.get((pk, cid))
        if task:
            has_newer_run = bool(latest_rid and latest_rid != str(task.latest_run_id or ""))
            _set_if_changed(task, "cluster_id", cid)
            _set_if_changed(task, "dedup_key", dedup_key)
            _set_if_changed(task, "title", str(c.get("title") or task.title or cid)[:500])
            if summary_txt:
                _set_if_changed(task, "summary", summary_txt)
            _set_if_changed(task, "category", str(c.get("category") or task.category or "other"))
            if not task.priority_manual:
                _set_if_changed(task, "priority", prio)
            _set_if_changed(task, "specialist", spec)
            if task.status != "skipped" or has_newer_run:
                _set_if_changed(task, "status", task_st)
                _set_if_changed(task, "raw_status", raw_st)
            _set_if_changed(task, "sources_json", json.dumps(src_labels, ensure_ascii=False))
            if c.get("blame_file"):
                _set_if_changed(task, "blame_file", c.get("blame_file"))
            if c.get("blame_line"):
                _set_if_changed(task, "blame_line", c.get("blame_line"))
            _set_if_changed(task, "events_count", ev_cnt)
            _set_if_changed(task, "users_count", users_cnt)
            _set_if_changed(task, "impact_score", impact)
            if latest_rid:
                _set_if_changed(task, "latest_run_id", latest_rid)
            if pr_url:
                _set_if_changed(task, "verified_pr_url", pr_url)
            if record_discovery:
                task.last_orchestrated_at = now
                dirty = True
            updated_cnt += 1
        else:
            tid = f"TASK-{next_num}"
            next_num += 1
            created_dt = _parse_iso(latest_run.get("created_at")) if latest_run else (_parse_iso(c.get("updated_at")) or now)
            task = TaskModel(
                task_id=tid,
                project_key=pk,
                cluster_id=cid,
                dedup_key=dedup_key,
                title=str(c.get("title") or cid)[:500],
                summary=summary_txt,
                category=str(c.get("category") or "other"),
                priority=prio,
                priority_manual=False,
                specialist=spec,
                status=task_st,
                raw_status=raw_st,
                sources_json=json.dumps(src_labels, ensure_ascii=False),
                blame_file=c.get("blame_file"),
                blame_line=c.get("blame_line"),
                events_count=ev_cnt,
                users_count=users_cnt,
                impact_score=impact,
                latest_run_id=latest_rid or None,
                verified_pr_url=pr_url,
                last_orchestrated_at=now,
                created_at=created_dt or now,
                updated_at=now,
            )
            db.add(task)
            by_dedup[dedup_key] = task
            by_cluster[(pk, cid)] = task
            created_cnt += 1
            dirty = True

    # Backfill các lượt chạy lịch sử mà cluster không còn nằm trong clusters.json
    for (pk, cid), latest_run in latest_run_by_cluster.items():
        if (pk, cid) in seen_pairs:
            continue
        dedup_key = f"{pk}::legacy::{cid}"
        task_st, raw_st = derive_task_status_from_run(latest_run)
        spec = infer_specialist(latest_run.get("track"))
        pr_url = verified_pr_by_cluster.get((pk, cid)) or latest_run.get("pr_url")
        s1 = next((s for s in (latest_run.get("steps") or []) if (s.get("step_number") or s.get("step")) == 1), None)
        root_hyp = ((s1 or {}).get("details") or {}).get("collector_agent", {}).get("root_cause") or ""
        title = str(latest_run.get("cluster_title") or (root_hyp[:110] if root_hyp else cid))
        latest_rid = str(latest_run.get("run_id") or "")

        task = by_cluster.get((pk, cid)) or by_dedup.get(dedup_key)
        if task:
            has_newer_run = bool(latest_rid and latest_rid != str(task.latest_run_id or ""))
            if task.status != "skipped" or has_newer_run:
                _set_if_changed(task, "status", task_st)
                _set_if_changed(task, "raw_status", raw_st)
            _set_if_changed(task, "specialist", spec)
            if latest_rid:
                _set_if_changed(task, "latest_run_id", latest_rid)
            if pr_url:
                _set_if_changed(task, "verified_pr_url", pr_url)
        else:
            tid = f"TASK-{next_num}"
            next_num += 1
            created_dt = _parse_iso(latest_run.get("created_at")) or now
            task = TaskModel(
                task_id=tid,
                project_key=pk,
                cluster_id=cid,
                dedup_key=dedup_key,
                title=title[:500],
                summary=root_hyp[:2000],
                category=spec,
                priority="medium",
                priority_manual=False,
                specialist=spec,
                status=task_st,
                raw_status=raw_st,
                sources_json=json.dumps(["Task File"], ensure_ascii=False),
                events_count=int(latest_run.get("cluster_impact") or 1),
                users_count=0,
                impact_score=int(latest_run.get("cluster_impact") or 1),
                latest_run_id=latest_rid,
                verified_pr_url=pr_url,
                last_orchestrated_at=created_dt,
                created_at=created_dt,
                updated_at=created_dt,
            )
            db.add(task)
            by_cluster[(pk, cid)] = task
            by_dedup[dedup_key] = task
            created_cnt += 1
            dirty = True

    if record_discovery and project_key:
        total_evs = events_collected if events_collected is not None else sum(
            int(c.get("event_count") or len(c.get("events") or []))
            for c in clusters
            if c.get("project_key") == project_key
        )
        proj_clusters = [c for c in clusters if c.get("project_key") == project_key]
        db.add(
            DiscoveryCycleModel(
                project_key=project_key,
                sources_json=json.dumps(sources_labels or [], ensure_ascii=False),
                events_collected=total_evs,
                clusters_detected=len(proj_clusters),
                tasks_created=created_cnt,
                tasks_updated=updated_cnt,
                status="ok",
                created_at=now,
            )
        )
        dirty = True

    if dirty:
        db.commit()
    out_q = db.query(TaskModel)
    if project_key:
        out_q = out_q.filter(TaskModel.project_key == project_key)
    return out_q.order_by(TaskModel.impact_score.desc(), TaskModel.updated_at.desc()).all()


def serialize_task(
    task: TaskModel,
    latest_run: Optional[dict[str, Any]] = None,
    runs_count: int = 0,
) -> dict[str, Any]:
    try:
        sources = json.loads(task.sources_json or "[]")
    except Exception:
        sources = ["Crashlytics"]
    primary_source = sources[0] if sources else "Crashlytics"
    # Tương thích ngược với UI filter cũ ('ready' vẫn có thể hiển thị status_group chuẩn)
    ui_status = "pending" if task.status == "ready" else ("running" if task.status == "verifying" else task.status)
    workflow_status = map_workflow_status(task, latest_run)
    failure_info = extract_failure_info(latest_run)
    if workflow_status == "TODO" and ui_status == "failed":
        ui_status = "pending"
    return {
        "task_id": task.task_id,
        "id": task.cluster_id,  # giữ `id` tương thích ngược cho các liên kết theo cluster_id
        "cluster_id": task.cluster_id,
        "project_key": task.project_key,
        "title": task.title,
        "body": task.summary or "",
        "summary": task.summary or "",
        "category": task.category,
        "priority": task.priority,
        "specialist": task.specialist,
        "specialist_label": specialist_label(task.specialist),
        "lifecycle_status": task.status,  # ready | running | verifying | completed | preview | failed | skipped
        "workflow_status": workflow_status,  # TODO | IN_PROGRESS | IN_REVIEW | DONE | CANCELLED | BLOCKED | ON_HOLD | REOPENED
        "status": ui_status,  # pending | running | completed | preview | failed | skipped
        "raw_status": task.raw_status,
        "failure_info": failure_info,
        "source": primary_source,
        "sources": sources,
        "events_count": task.events_count,
        "users_count": task.users_count,
        "impact_score": task.impact_score,
        "blame_file": task.blame_file,
        "blame_line": task.blame_line,
        "latest_run_id": task.latest_run_id,
        "runs_count": runs_count,
        "pr_url": task.verified_pr_url or (latest_run.get("pr_url") if latest_run else None),
        "last_orchestrated_at": task.last_orchestrated_at.isoformat() if task.last_orchestrated_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": (
            latest_run.get("created_at")
            if latest_run and latest_run.get("created_at")
            else (task.updated_at.isoformat() if task.updated_at else None)
        ),
        "latest_run": latest_run,
    }


def extract_verification_matrix(run: dict[str, Any], runs_dir: Path = Path("data/runs")) -> dict[str, Any]:
    """Chuẩn hóa kết quả kiểm định Giai đoạn 4 (4A / 4B / 4C), Self-healing Attempts & Patch/Delivery."""
    steps = run.get("steps") or []
    s1 = next((s for s in steps if (s.get("step_number") or s.get("step")) == 1), None) or {}
    s2 = next((s for s in steps if (s.get("step_number") or s.get("step")) == 2), None) or {}
    s3 = next((s for s in steps if (s.get("step_number") or s.get("step")) == 3), None) or {}
    s4 = next((s for s in steps if (s.get("step_number") or s.get("step")) == 4), None)
    s3_det = s3.get("details") or {}
    s4_det = (s4 or {}).get("details") or {}

    overall = str(run.get("overall_status") or "")
    is_running = overall in {"RUNNING", "IN_PROGRESS"}
    cur_step = run.get("current_step")

    # --- 4A: Diff Violations + Independent Regression Checks ---
    if not s4:
        st_4a = "WAIT" if is_running else "SKIP"
    elif s4.get("status") == "running":
        st_4a = "RUN"
    else:
        violations = s4_det.get("violations") or s4_det.get("diff_violations") or []
        v_checks = s4_det.get("verify_checks") or []
        if violations or any(not c.get("passed", True) for c in v_checks):
            st_4a = "FAIL"
        elif v_checks or "changed_files" in s4_det:
            st_4a = "PASS"
        else:
            st_4a = "SKIP"

    # --- 4B: Runtime Verifier (Web CDP / Flutter Mobile) ---
    rv = s4_det.get("runtime_verifier") or {}
    explicit_4b = s4_det.get("stage_4b")
    if not s4:
        st_4b = "WAIT" if is_running else "BLOCKED"
    elif s4.get("status") == "running":
        st_4b = "RUN" if st_4a == "PASS" else "WAIT"
    elif st_4a == "FAIL":
        st_4b = "BLOCKED"
    elif explicit_4b in {"PASS", "FAIL", "BLOCKED", "WAIT", "RUN"}:
        st_4b = explicit_4b
    elif rv.get("passed") is False:
        st_4b = "FAIL"
    elif rv.get("executed"):
        st_4b = "PASS" if rv.get("passed", True) else "FAIL"
    else:
        st_4b = "BLOCKED" if s4.get("status") in {"failed", "blocked"} else "SKIP"

    # --- 4C: Independent Verifier AI Agent ---
    va = s4_det.get("verifier_agent") or {}
    explicit_4c = s4_det.get("stage_4c")
    if not s4:
        st_4c = "WAIT" if is_running else "BLOCKED"
    elif s4.get("status") == "running":
        st_4c = "WAIT"
    elif st_4a == "FAIL" or st_4b == "FAIL":
        st_4c = "BLOCKED"
    elif explicit_4c in {"PASS", "FAIL", "BLOCKED", "WAIT", "RUN"}:
        st_4c = explicit_4c
    elif va.get("executed"):
        st_4c = "PASS" if va.get("passed", True) else "FAIL"
    else:
        st_4c = "BLOCKED" if s4.get("status") in {"failed", "blocked"} else "SKIP"

    # --- Overall Result ---
    if is_running:
        result = "Running"
    elif s4 and s4.get("status") == "passed":
        result = "PASS"
    elif overall in {"DRY_RUN_VERIFIED", "VERIFIED_READY_FOR_PR", "VERIFIED_NO_CHANGES", "PR_CREATED", "PUSHED_BRANCH", "AUTO_MERGE_REQUESTED", "AUTO_MERGED"}:
        result = "PASS"
    elif not s4:
        if overall in {"VERIFICATION_FAILED", "VERIFICATION_INSUFFICIENT"}:
            result = "FAIL"
        else:
            result = "SKIP"
    else:
        result = "FAIL"

    # --- Attempts & Self-Healing ---
    raw_attempts = list(s3_det.get("attempts") or [])
    v_rounds = list(run.get("verification_rounds") or [])
    run_id = str(run.get("run_id") or "")
    art_dir = runs_dir / run_id

    verified_patch_file = art_dir / "verified.patch"
    verified_patch_sha: Optional[str] = s4_det.get("verified_patch_sha256")
    if not verified_patch_sha and verified_patch_file.is_file():
        try:
            verified_patch_sha = hashlib.sha256(verified_patch_file.read_bytes()).hexdigest()[:16]
        except OSError:
            verified_patch_sha = None

    attempts_structured: list[dict[str, Any]] = []
    if v_rounds:
        for vr in v_rounds:
            att_no = int(vr.get("attempt") or len(attempts_structured) + 1)
            attempts_structured.append(
                {
                    "attempt": att_no,
                    "agent": vr.get("agent") or s3_det.get("selected_agent") or "Agent",
                    "mechanical_passed": vr.get("mechanical_passed", True),
                    "checks": vr.get("mechanical_checks") or [],
                    "stage_4a": vr.get("stage_4a", st_4a),
                    "stage_4b": vr.get("stage_4b", st_4b),
                    "stage_4c": vr.get("stage_4c", st_4c),
                    "verdict": vr.get("verdict") or ("PASS" if vr.get("passed") else "FAIL"),
                    "feedback": vr.get("feedback") or "",
                    "patch_file": vr.get("patch_file"),
                    "patch_sha256": vr.get("patch_sha256"),
                }
            )
    else:
        for idx, att in enumerate(raw_attempts):
            att_no = int(att.get("attempt") or idx + 1)
            is_last = idx == len(raw_attempts) - 1
            mech_ok = all(chk.get("passed", True) for chk in (att.get("checks") or [])) and (
                (att.get("executor") or {}).get("exit_code", 0) in (0, None)
            )
            att_patch = art_dir / f"attempt_{att_no}.patch"
            patch_rel = str(att_patch) if att_patch.is_file() else (str(verified_patch_file) if (is_last and verified_patch_file.is_file()) else None)
            attempts_structured.append(
                {
                    "attempt": att_no,
                    "agent": (att.get("executor") or {}).get("agent") or s3_det.get("selected_agent") or "Agent",
                    "mechanical_passed": mech_ok,
                    "checks": att.get("checks") or [],
                    "executor": att.get("executor") or {},
                    "stage_4a": st_4a if is_last else ("SKIP" if not mech_ok else "PASS"),
                    "stage_4b": st_4b if is_last else "SKIP",
                    "stage_4c": st_4c if is_last else "SKIP",
                    "verdict": result if is_last else ("FAIL" if not mech_ok else "RETRY"),
                    "feedback": "",
                    "patch_file": patch_rel,
                    "patch_sha256": verified_patch_sha if (is_last and result == "PASS") else None,
                }
            )

    pr_info = s4_det.get("pr") or {}
    self_healed = len(attempts_structured) > 1 or len(raw_attempts) > 1
    return {
        "run_id": run_id,
        "cluster_id": run.get("cluster_id"),
        "task_id": run.get("task_id"),
        "project_key": run.get("project_key"),
        "created_at": run.get("created_at"),
        "overall_status": overall,
        "current_step": cur_step,
        "dry_run": bool(run.get("dry_run", False)),
        "stage_4a": st_4a,
        "stage_4b": st_4b,
        "stage_4c": st_4c,
        "result": result,
        "attempts_count": max(len(attempts_structured), len(raw_attempts), 1 if steps else 0),
        "self_healed": self_healed,
        "attempts": attempts_structured,
        "changed_files": s4_det.get("changed_files") or [],
        "diff_violations": s4_det.get("diff_violations") or s4_det.get("violations") or [],
        "verify_checks": s4_det.get("verify_checks") or [],
        "runtime_verifier": rv,
        "verifier_agent": va,
        "mapped_files": (s2.get("details") or {}).get("mapped_files") or [],
        "brief_path": (s2.get("details") or {}).get("brief_path"),
        "delivery": {
            "branch_name": run.get("branch_name") or (s1.get("details") or {}).get("branch"),
            "base_branch": (s1.get("details") or {}).get("base_branch") or "main",
            "base_commit": (s1.get("details") or {}).get("base_commit"),
            "commit_sha": pr_info.get("commit_sha"),
            "pushed": bool(pr_info.get("pushed") or overall in {"PUSHED_BRANCH", "PR_CREATED", "AUTO_MERGE_REQUESTED", "AUTO_MERGED"}),
            "pr_created": bool(pr_info.get("pr_created") or run.get("pr_url")),
            "pr_url": run.get("pr_url") or pr_info.get("pr_url"),
            "merged": bool(pr_info.get("merged") or overall == "AUTO_MERGED"),
            "auto_merge_requested": bool(pr_info.get("auto_merge_requested") or overall == "AUTO_MERGE_REQUESTED"),
            "verified_attempt": len(attempts_structured) if result == "PASS" and attempts_structured else None,
            "verified_patch_sha256": verified_patch_sha if result == "PASS" else None,
            "has_verified_patch": verified_patch_file.is_file(),
        },
    }


def read_run_diff_text(run_id: str, runs_dir: Path = Path("data/runs"), max_bytes: int = 120_000) -> dict[str, Any]:
    """Đọc nội dung bản vá (patch) thật từ thư mục artifacts của Run."""
    clean_id = str(run_id or "").strip()
    if not clean_id or not re.fullmatch(r"[\w.-]+", clean_id) or clean_id in {".", ".."}:
        raise ValueError("run_id không hợp lệ.")
    runs_root = Path(runs_dir).resolve()
    art_dir = (runs_root / clean_id).resolve()
    if not art_dir.is_relative_to(runs_root):
        raise ValueError("run_id vượt ngoài thư mục data/runs.")
    if not art_dir.is_dir():
        return {"run_id": clean_id, "found": False, "patch_text": "", "patch_files": []}
    patch_files = sorted(
        [p.name for p in art_dir.glob("*.patch") if p.is_file()],
        key=lambda n: (0 if n == "verified.patch" else 1, n),
    )
    chosen = art_dir / patch_files[0] if patch_files else None
    text = ""
    sha = None
    if chosen and chosen.is_file():
        raw = chosen.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()[:16]
        text = raw[:max_bytes].decode("utf-8", errors="replace")
    return {
        "run_id": run_id,
        "found": bool(chosen),
        "active_patch": chosen.name if chosen else None,
        "patch_sha256": sha,
        "patch_files": patch_files,
        "patch_text": text,
    }
