"""Bộ điều phối chu trình khép kín (End-to-End Autopilot Orchestrator).

Luồng tự động trong 1 chu trình:
  1. Đọc cấu hình nguồn từ config/projects.yaml (crashlytics, code_audit, inbox_json, ...)
  2. Nếu nguồn 'crashlytics' được bật -> kéo dữ liệu thật mới nhất từ Firebase Console về SQLite
  3. Thu thập ngữ cảnh từ tất cả nguồn đã bật & gom cụm nguyên nhân gốc (EventBufferQueue)
  4. Tự động điều phối top cụm ưu tiên của từng project qua Quy trình 4 bước (ConstrainedFourStepRunner)
"""
import asyncio
import fnmatch
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

from app.collector import sync_crashlytics
from app.config import load_projects, project_sources
from app.constrained_runner import (
    ConstrainedFourStepRunner,
    ConstraintConfig,
    PipelineRunReport,
)
from app.constrained_runner.models import RuntimeVerifierConfig
from app.context_collector import (
    CollectionResult,
    ContextCollectorManager,
    EventBufferQueue,
)
from app.context_collector.sources import (
    CodeAuditContextSource,
    CrashlyticsContextSource,
    FileInputContextSource,
)


class CycleSummary(BaseModel):
    sync_results: Optional[dict] = None
    collection: CollectionResult
    runs: list[PipelineRunReport] = Field(default_factory=list)
    login_required: bool = False


def build_collector_from_projects(
    projects: list[dict],
    store_path: Path | str = "data/context_queue/clusters.json",
) -> tuple[ContextCollectorManager, bool]:
    """Khởi tạo ContextCollectorManager động theo cấu hình sources trong projects.yaml."""
    manager = ContextCollectorManager(queue=EventBufferQueue(store_path=store_path))
    crash_projects = set()
    code_audit_repos: dict[str, str] = {}
    file_inputs: list[tuple[str, str, str, Optional[str]]] = []  # (path, project_key, category, custom_name)
    seen_file_keys: set[tuple[str, str]] = set()

    for proj in projects:
        for src in project_sources(proj):
            stype = str(src.get("type", "")).lower()
            if stype == "crashlytics":
                crash_projects.add(proj["key"])
            elif stype == "code_audit":
                if proj.get("repository_path"):
                    code_audit_repos[proj["key"]] = proj["repository_path"]
            elif stype in ("inbox_json", "file", "file_input"):
                p = str(src.get("path") or "data/inbox")
                # Nếu là thư mục data/inbox dùng chung thì đăng ký 1 lần; nếu là file/path riêng theo project thì giữ theo project
                dedup_key = (p, "shared" if p == "data/inbox" else proj["key"])
                if dedup_key not in seen_file_keys:
                    seen_file_keys.add(dedup_key)
                    file_inputs.append(
                        (
                            p,
                            "default" if p == "data/inbox" else proj["key"],
                            str(src.get("category") or "other"),
                            src.get("name"),
                        )
                    )

    if crash_projects:
        manager.register(CrashlyticsContextSource(project_keys=crash_projects))
    if code_audit_repos:
        manager.register(CodeAuditContextSource(repo_overrides=code_audit_repos))
    for idx, (input_path, proj_key, category, custom_name) in enumerate(file_inputs):
        src_name = custom_name or ("inbox_json" if (idx == 0 and input_path == "data/inbox") else f"file_input_{idx + 1}")
        manager.register(
            FileInputContextSource(
                inbox_dir=input_path,
                source_name=src_name,
                default_project_key=proj_key,
                default_category=category,
            )
        )

    return manager, bool(crash_projects)


def _load_scan_state(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_scan_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as tmp:
        json.dump(state, tmp, ensure_ascii=False, indent=2)
    Path(tmp.name).replace(path)


def _is_project_due(proj: dict, scan_state: dict[str, str], now: datetime) -> bool:
    interval_min = max(1, int(proj.get("scan_interval_minutes") or 60))
    raw_last = scan_state.get(proj["key"])
    if not raw_last:
        return True
    try:
        last_dt = datetime.fromisoformat(raw_last.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (now - last_dt).total_seconds() >= interval_min * 60
    except Exception:
        return True


async def run_autopilot_cycle(
    projects_file: Optional[str] = None,
    fetch_details_limit: int = 5,
    headless: bool = True,
    skip_browser_sync: bool = False,
    store_path: Path | str = "data/context_queue/clusters.json",
    runs_dir: Path | str = "data/runs",
    project_key: str | None = None,
    cluster_id: str | None = None,
    respect_schedule: bool = False,
    scan_state_path: Path | str | None = None,
    discovery_only: bool = False,
    db_session=None,
) -> CycleSummary:
    """Chạy toàn bộ chu trình khép kín: Nguồn (projects.yaml) -> Gom cụm -> Quy trình 4 bước."""
    projects = load_projects(projects_file)
    target_cluster_ids = [
        cid.strip()
        for cid in str(cluster_id or "").split(",")
        if cid.strip()
    ]
    target_cluster_set = set(target_cluster_ids)
    has_target_clusters = bool(target_cluster_set)
    if has_target_clusters and not project_key:
        raise ValueError("Cần chọn dự án của tác vụ.")
    if project_key:
        projects = [p for p in projects if p["key"] == project_key]
        if not projects:
            raise ValueError("Dự án không tồn tại hoặc đang tắt.")

    resolved_scan_state = Path(scan_state_path) if scan_state_path else Path(store_path).with_name("scan_state.json")
    now_utc = datetime.now(timezone.utc)
    scan_state = _load_scan_state(resolved_scan_state)
    if respect_schedule and not project_key and not has_target_clusters:
        due_projects = [p for p in projects if _is_project_due(p, scan_state, now_utc)]
        if not due_projects:
            print("\n>>> [Skip] Chưa đến chu kỳ quét tiếp theo cho các dự án đang bật.")
            return CycleSummary(
                sync_results=None,
                collection=CollectionResult(store_path=str(store_path)),
                runs=[],
                login_required=False,
            )
        projects = due_projects

    due_keys = {p["key"] for p in projects}
    manager, has_crashlytics = build_collector_from_projects(projects, store_path=store_path)

    # --- GIAI ĐOẠN 1A: Kéo dữ liệu nguồn trực tiếp (Firebase Crashlytics) ---
    sync_results: Optional[dict] = None
    login_required = False
    if has_crashlytics and not skip_browser_sync and not has_target_clusters:
        print("\n>>> [Giai đoạn 1/3] Đồng bộ nguồn Firebase Crashlytics từ projects.yaml...")
        if len(due_keys) == 1:
            sync_target = next(iter(due_keys))
        else:
            sync_target = project_key
        sync_results = await sync_crashlytics(
            projects_file=projects_file,
            project_key=sync_target,
            fetch_details_limit=fetch_details_limit,
            headless=headless,
        )
        errors = sync_results.get("errors", [])
        if any("login required" in e.get("error", "").lower() for e in errors):
            login_required = True

    # --- GIAI ĐOẠN 1B: Thu thập ngữ cảnh mọi nguồn & Gom cụm Pattern ---
    print("\n>>> [Giai đoạn 2/3] Thu thập ngữ cảnh đa nguồn & Gom cụm nguyên nhân gốc...")
    collection = manager.collect_all(project_key=project_key)
    if has_target_clusters and not any(c.cluster_id in target_cluster_set for c in collection.clusters):
        raise ValueError("Tác vụ đã rời hàng đợi.")
    for st in collection.statuses:
        print(f"  - [{st.status.upper():<12}] {st.source:<14}: {st.message}")
    print(f"  Tổng: {collection.total_events} sự kiện -> {len(collection.clusters)} cụm.")

    # --- GIAI ĐOẠN 2 & 3: Coordinator điều phối tự động sang Quy trình 4 bước ---
    runner = ConstrainedFourStepRunner(runs_dir=runs_dir)
    existing_runs = runner.list_runs()

    # Đồng bộ Discovery & Task Planning (Giai đoạn 1 + 2) vào bảng `tasks` khi chạy trên store mặc định hoặc có db_session
    task_id_by_cluster: dict[tuple[str, str], str] = {}
    default_store = Path(store_path) == Path("data/context_queue/clusters.json")
    if db_session is not None or default_store:
        from app.db.database import SessionLocal, init_db
        from app.db.task_lifecycle import sync_tasks_for_project

        init_db()
        own_db = db_session is None
        sess = SessionLocal() if own_db else db_session
        try:
            cls_dicts = [c.model_dump(mode="json") for c in collection.clusters]
            run_dicts = [r.model_dump(mode="json") for r in existing_runs]
            for pk in due_keys:
                proj_cfg = next((p for p in projects if p["key"] == pk), {})
                src_names = [str(s.get("type") or "") for s in project_sources(proj_cfg)]
                synced = sync_tasks_for_project(
                    sess,
                    pk,
                    cls_dicts,
                    run_dicts,
                    record_discovery=not has_target_clusters,
                    events_collected=sum(
                        c.event_count for c in collection.clusters if c.project_key == pk
                    ),
                    sources_labels=src_names,
                )
                for t_obj in synced:
                    task_id_by_cluster[(t_obj.project_key, t_obj.cluster_id)] = t_obj.task_id
        finally:
            if own_db:
                sess.close()

    if discovery_only:
        finished_iso = datetime.now(timezone.utc).isoformat()
        for k in due_keys:
            scan_state[k] = finished_iso
        _save_scan_state(resolved_scan_state, scan_state)
        return CycleSummary(
            sync_results=sync_results,
            collection=collection,
            runs=[],
            login_required=login_required,
        )

    print("\n>>> [Giai đoạn 3/3] Điều phối tự động qua Quy trình 4 bước (Môi trường ràng buộc)...")
    latest_by_cluster: dict[tuple[str, str], PipelineRunReport] = {}
    consecutive_failures: dict[tuple[str, str], tuple[int, Optional[str]]] = {}
    counting_streak: set[tuple[str, str]] = set()
    blocked_project_repos: set[tuple[str, str]] = set()
    aliases = EventBufferQueue.history_aliases(collection.clusters)
    active_repos = {(p["key"], p.get("repository_path")) for p in projects}
    for prev in existing_runs:
        if (prev.overall_status == "AUTO_MERGE_REQUESTED" and (prev.project_key, prev.repository_path) in active_repos
                and (not has_target_clusters or prev.cluster_id in target_cluster_set)):
            runner.refresh_requested_merge(prev)
        key = (prev.project_key, aliases.get((prev.project_key, prev.cluster_id), prev.cluster_id))
        if key not in latest_by_cluster:
            latest_by_cluster[key] = prev
            counting_streak.add(key)
        if key in counting_streak:
            if not prev.dry_run and prev.overall_status in {"FAILED", "VERIFICATION_FAILED"}:
                prev_base = prev.steps[0].details.get("base_commit") if prev.steps else None
                cur_cnt, ref_base = consecutive_failures.get(key, (0, prev_base))
                if cur_cnt == 0 or prev_base == ref_base:
                    consecutive_failures[key] = (cur_cnt + 1, ref_base)
                else:
                    counting_streak.discard(key)
            else:
                counting_streak.discard(key)
        if (prev.overall_status == "BLOCKED" and prev.steps and prev.steps[-1].step_number == 1
                and (prev.steps[-1].details.get("reason") == "missing_repository"
                     or "repository_path" in prev.steps[-1].details)):
            blocked_project_repos.add((prev.project_key, prev.repository_path))

    runs: list[PipelineRunReport] = []

    project_map = {p["key"]: p for p in projects}
    per_project_count: dict[str, int] = {}
    skipped_missing_repo_projects: set[str] = set()

    for cluster in collection.clusters:
        if has_target_clusters and cluster.cluster_id not in target_cluster_set:
            continue
        proj = project_map.get(cluster.project_key)
        if not proj:
            continue
        exec_cfg = proj.get("execution") or {}
        if not project_key and not exec_cfg.get("auto_run", True):
            continue

        max_per_cycle = int(exec_cfg.get("max_clusters_per_cycle", 3))
        used = per_project_count.get(cluster.project_key, 0)
        if not has_target_clusters and used >= max_per_cycle:
            continue

        repo_path = str(proj.get("repository_path") or "").strip()
        repo_exists = bool(repo_path and Path(repo_path).expanduser().is_dir())
        if not repo_path and not has_target_clusters:
            if cluster.project_key not in skipped_missing_repo_projects:
                skipped_missing_repo_projects.add(cluster.project_key)
                print(
                    f"  * [todo] [{cluster.project_key}] chưa gắn repository local "
                    "— giữ trạng thái TODO (chưa chạy), vào Settings để gắn Source Code Local."
                )
            continue
        if not has_target_clusters and not repo_exists and (cluster.project_key, repo_path) in blocked_project_repos:
            if cluster.project_key not in skipped_missing_repo_projects:
                skipped_missing_repo_projects.add(cluster.project_key)
                print(
                    f"  * [skip] [{cluster.project_key}] đã có bản ghi chờ gắn repo "
                    f"'{repo_path}' — không tạo thêm bản ghi trùng."
                )
            continue

        prev_run = latest_by_cluster.get((cluster.project_key, cluster.cluster_id))
        if prev_run and prev_run.repository_path == repo_path:
            same_mode = prev_run.dry_run == bool(exec_cfg.get("dry_run", True))
            if same_mode and not prev_run.dry_run and prev_run.overall_status in {
                "CHECKLIST_UPDATE_PENDING", "CHECKLIST_UPDATE_FAILED",
            }:
                runs.append(runner.finish_checklist(cluster, prev_run))
                per_project_count[cluster.project_key] = used + 1
                continue
            if (same_mode and not prev_run.dry_run and exec_cfg.get("auto_create_pr")
                    and prev_run.overall_status in {"PR_CREATE_FAILED", "PR_MERGE_FAILED"}
                    and prev_run.steps[-1].details.get("pr", {}).get("timed_out")):
                base = prev_run.steps[-1].details["delivery"]["base_branch"]
                protected = exec_cfg.get("protected_branches", ConstraintConfig(repository_path=repo_path).protected_branches)
                merge = bool(exec_cfg.get("auto_merge")) and not any(fnmatch.fnmatchcase(base, pat) for pat in protected)
                runs.append(runner.resume_delivery(cluster, prev_run, merge))
                per_project_count[cluster.project_key] = used + 1
                continue
            verified = prev_run.overall_status.startswith("VERIFIED") or prev_run.overall_status in {
                "DRY_RUN_VERIFIED", "PR_CREATED", "PUSHED_BRANCH", "AUTO_MERGE_REQUESTED", "AUTO_MERGED", "PR_CLOSED",
            }
            pending_checklist = not prev_run.dry_run and any(e.checklist_item for e in cluster.events)
            if not has_target_clusters and same_mode and verified and not pending_checklist and (
                prev_run.cluster_impact is None or prev_run.cluster_impact >= cluster.total_impact
            ):
                print(
                    f"  * [skip] [{cluster.project_key}] {cluster.cluster_id} đã xử lý "
                    f"({prev_run.overall_status}, impact={cluster.total_impact}) — tránh ghi trùng."
                )
                continue

            fail_count, failed_base = consecutive_failures.get((cluster.project_key, cluster.cluster_id), (0, None))
            max_fail_streak = int(exec_cfg.get("max_consecutive_failures", 2))
            if not has_target_clusters and same_mode and not prev_run.dry_run and fail_count >= max_fail_streak and repo_exists:
                cur_head = None
                try:
                    import subprocess as _sp
                    cur_head = _sp.check_output(
                        ["git", "rev-parse", f"refs/heads/{exec_cfg.get('base_branch', 'main')}"],
                        cwd=Path(repo_path).expanduser(),
                        stderr=_sp.DEVNULL,
                        text=True,
                        timeout=5,
                    ).strip()
                except Exception:
                    cur_head = None
                if failed_base and cur_head == failed_base:
                    print(
                        f"  * [cooldown] [{cluster.project_key}] {cluster.cluster_id} đã lỗi {fail_count} chu kỳ liên tiếp "
                        f"trên commit {cur_head[:8]} — nhường hàng đợi cho tác vụ kế tiếp (bấm Retry để chạy lại)."
                    )
                    continue

        rv_raw = exec_cfg.get("runtime_verifier")
        runtime_verifier = RuntimeVerifierConfig.model_validate(rv_raw) if isinstance(rv_raw, dict) else None

        constraint = ConstraintConfig(
            repository_path=repo_path,
            dry_run=bool(exec_cfg.get("dry_run", True)),
            **{key: exec_cfg[key] for key in ("protected_branches", "forbidden_path_patterns") if key in exec_cfg},
            base_branch=str(exec_cfg.get("base_branch", "main")),
            mechanical_checks=exec_cfg.get("mechanical_checks") or [],
            verify_commands=exec_cfg.get("verify_commands") or [],
            runtime_verifier=runtime_verifier,
            executor_command=exec_cfg.get("executor_command"),
            verifier_command=exec_cfg.get("verifier_command"),
            max_retries=int(exec_cfg.get("max_retries", 1)),
            auto_create_pr=bool(exec_cfg.get("auto_create_pr", False)),
            auto_merge=bool(exec_cfg.get("auto_merge", False)),
        )
        report = runner.run(
            cluster,
            constraint,
            task_id=task_id_by_cluster.get((cluster.project_key, cluster.cluster_id)),
        )
        runs.append(report)
        latest_by_cluster[(cluster.project_key, cluster.cluster_id)] = report
        if report.overall_status == "BLOCKED" and not repo_exists:
            blocked_project_repos.add((cluster.project_key, repo_path))
        per_project_count[cluster.project_key] = used + 1

        step_summary = " -> ".join(f"B{s.step_number}:{s.status.value}" for s in report.steps)
        print(
            f"  * [{report.project_key}] {report.cluster_id} ({report.track.value}) "
            f"=> {report.overall_status} ({step_summary})"
        )

    if runs and (db_session is not None or default_store):
        from app.db.database import SessionLocal
        from app.db.task_lifecycle import sync_tasks_for_project

        own_db = db_session is None
        sess = SessionLocal() if own_db else db_session
        try:
            cls_dicts = [c.model_dump(mode="json") for c in collection.clusters]
            all_run_dicts = [r.model_dump(mode="json") for r in [*runs, *existing_runs]]
            for pk in due_keys:
                sync_tasks_for_project(sess, pk, cls_dicts, all_run_dicts, record_discovery=False)
        finally:
            if own_db:
                sess.close()

    if not has_target_clusters:
        finished_iso = datetime.now(timezone.utc).isoformat()
        for k in due_keys:
            scan_state[k] = finished_iso
        _save_scan_state(resolved_scan_state, scan_state)

    return CycleSummary(
        sync_results=sync_results,
        collection=collection,
        runs=runs,
        login_required=login_required,
    )


def run_cycle_sync(
    fetch_details_limit: int = 5,
    headless: bool = True,
    skip_browser_sync: bool = False,
    project_key: str | None = None,
    cluster_id: str | None = None,
    respect_schedule: bool = False,
    discovery_only: bool = False,
) -> CycleSummary:
    return asyncio.run(
        run_autopilot_cycle(
            fetch_details_limit=fetch_details_limit,
            headless=headless,
            skip_browser_sync=skip_browser_sync,
            project_key=project_key,
            cluster_id=cluster_id,
            respect_schedule=respect_schedule,
            discovery_only=discovery_only,
        )
    )
