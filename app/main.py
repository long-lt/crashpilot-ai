import argparse
import asyncio
from app.browser.phase1_check import run as run_browser_check
from app.collector import run_sync
from app.db.database import SessionLocal, init_db
from app.db.repository import get_all_issues


def list_issues(project_key: str | None = None, platform: str | None = None):
    init_db()
    db = SessionLocal()
    try:
        issues = get_all_issues(db, project_key, platform=platform)
        if not issues:
            print("\nNo issues found in local database. Run 'python -m app.main sync' first.\n")
            return

        print(f"\nFound {len(issues)} issues in database:")
        print(f"{'PROJECT':<12} | {'PLATFORM':<9} | {'ISSUE ID':<20} | {'EVENTS':<8} | {'USERS':<8} | {'TYPE':<10} | {'TITLE'}")
        print("-" * 104)
        for iss in issues:
            title = iss.title.replace("\n", " ")[:45]
            plat = (iss.platform or "unknown").lower()
            print(f"{iss.project_key:<12} | {plat:<9} | {iss.issue_id:<20} | {iss.events_count:<8} | {iss.users_count:<8} | {iss.issue_type:<10} | {title}")
        print("-" * 104)
    finally:
        db.close()


def _cli_issue(db, issue_id, project_key):
    from app.db.models import IssueModel
    query = db.query(IssueModel).filter_by(issue_id=issue_id)
    if project_key:
        query = query.filter_by(project_key=project_key)
    matches = query.limit(2).all()
    if len(matches) > 1:
        raise SystemExit("Issue thuộc nhiều dự án. Hãy chọn --project.")
    return next(iter(matches), None)


def show_issue(issue_id: str, project_key: str | None = None):
    init_db()
    db = SessionLocal()
    try:
        from app.db.models import IssueModel
        issue = _cli_issue(db, issue_id, project_key)
        if not issue:
            print(f"\nIssue with ID '{issue_id}' not found in database.\n")
            return

        print(f"\n================ ISSUE DETAILS ================")
        print(f"ID:          {issue.issue_id}")
        print(f"Project:     {issue.project_key} ({issue.firebase_project_id})")
        print(f"Type:        {issue.issue_type.upper()}")
        print(f"Events:      {issue.events_count} | Users: {issue.users_count}")
        print(f"Title:       {issue.title}")
        if issue.subtitle:
            print(f"Subtitle:    {issue.subtitle}")
        print(f"Console URL: {issue.issue_url}")

        if issue.details:
            detail = issue.details[-1]  # Latest detail
            print(f"\n--- Latest Event Crash Metadata ---")
            print(f"App Version:  {detail.app_version or 'N/A'}")
            print(f"OS Version:   {detail.os_version or 'N/A'}")
            print(f"Device:       {detail.device_model or 'N/A'}")
            if detail.blame_file:
                print(f"Blame Code:   {detail.blame_file}:{detail.blame_line or 0}")
            print(f"\n--- Stack Trace ---")
            print(detail.stack_trace_raw or "No raw stack trace captured.")
        else:
            print("\nNo detailed event captured yet.")
        print(f"===============================================\n")
    finally:
        db.close()


async def check_session() -> int:
    from app.browser.extractor import CrashlyticsExtractor
    from app.browser.session import FirebaseBrowser
    from app.config import (
        Settings,
        get_project_console_urls,
        load_projects,
        project_sources,
        resolve_crashlytics_backend,
    )

    settings = Settings()
    crashlytics_projects = [
        p
        for p in load_projects()
        if any(s.get("type", "").lower() == "crashlytics" for s in project_sources(p))
        and (
            p.get("firebase_project_id")
            or p.get("firebase_app_ids")
            or get_project_console_urls(p)
        )
    ]
    if not crashlytics_projects:
        return 0

    backends = {resolve_crashlytics_backend(p, settings) for p in crashlytics_projects}
    if "firebase_cli" in backends:
        from app.firebase_cli import FirebaseCliClient, FirebaseCliError

        try:
            with FirebaseCliClient() as client:
                ok, info = client.check_auth()
            if ok:
                print(f"Firebase CLI session: OK ({info})")
            else:
                print(f"Firebase CLI session: LOGIN REQUIRED ({info})")
                return 2
        except FirebaseCliError as exc:
            print(f"Firebase CLI session: LOGIN REQUIRED ({exc})")
            return 2

    if "browser" not in backends:
        return 0

    browser = FirebaseBrowser(settings.browser_profile, headless=True)
    page = await browser.start()
    try:
        await page.goto("https://console.firebase.google.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        if await CrashlyticsExtractor(page).is_login_required():
            print("Google session: LOGIN REQUIRED")
            return 2
        print("Google session: OK")
        return 0
    finally:
        await browser.close()


def diagnose_issue_cli(issue_id: str, project_key: str | None = None):
    from app.analyzer.source_mapper import build_diagnosis_report
    from app.db.models import IssueModel, ProjectModel

    init_db()
    db = SessionLocal()
    try:
        issue = _cli_issue(db, issue_id, project_key)
        if not issue:
            print(f"\nIssue with ID '{issue_id}' not found in database.\n")
            return
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
        print(report.markdown_report)
    finally:
        db.close()


def collect_context_cli(
    sources: list[str] | None = None,
    input_paths: list[str] | None = None,
    project_key: str = "default",
):
    from app.config import load_projects
    from app.context_collector import create_default_collector
    from app.context_collector.sources import FileInputContextSource
    from app.orchestrator import build_collector_from_projects

    projects = load_projects()
    scoped_key = None if project_key == "default" else project_key
    if scoped_key:
        projects = [p for p in projects if p["key"] == scoped_key]
    if projects and not input_paths:
        manager, _ = build_collector_from_projects(projects)
    else:
        manager = create_default_collector()
    extra_names: list[str] = []
    if input_paths:
        for idx, p in enumerate(input_paths, start=1):
            name = f"cli_input_{idx}"
            manager.register(
                FileInputContextSource(
                    inbox_dir=p,
                    source_name=name,
                    default_project_key=project_key,
                )
            )
            extra_names.append(name)

    selected = list(sources) if sources else None
    if selected is not None and extra_names:
        selected.extend(extra_names)

    result = manager.collect_all(only_sources=selected, project_key=scoped_key)
    print("\n=== Kết quả thu thập ngữ cảnh (Outer Loop) ===")
    for st in result.statuses:
        print(f"  - [{st.status.upper():<12}] {st.source:<14}: {st.message}")
    print(f"\nTổng sự kiện: {result.total_events} | Gom thành: {len(result.clusters)} cụm -> {result.store_path}\n")


def doctor_cli():
    import shutil
    import subprocess
    from pathlib import Path
    from app.config import load_projects
    from app.constrained_runner.agents import detect_installed_agents, load_agent_settings

    print("\n=========== CrashPilot AI — Doctor Check ===========")
    print("[1] Công cụ hệ thống:")
    for tool in ("git", "gh", "firebase", "npx", "flutter", "dart", "adb"):
        p = shutil.which(tool)
        print(f"  - {tool:<8}: {'OK (' + p + ')' if p else 'MISSING'}")

    firebase_bin = shutil.which("firebase")
    if firebase_bin:
        fb_ver = subprocess.run([firebase_bin, "--version"], capture_output=True, text=True, check=False)
        fb_auth = subprocess.run([firebase_bin, "login:list"], capture_output=True, text=True, check=False)
        fb_user = next(
            (line.strip() for line in fb_auth.stdout.splitlines() if "logged in" in line.lower()),
            "CHƯA ĐĂNG NHẬP (chạy: ./run.sh login)",
        )
        print(f"  - firebase: v{fb_ver.stdout.strip() or '?'} | {fb_user}")

    gh = shutil.which("gh")
    if gh:
        auth = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
        print(f"  - gh auth : {'OK (đã đăng nhập GitHub)' if auth.returncode == 0 else 'CHƯA ĐĂNG NHẬP (chạy: gh auth login)'}")

    print("\n[2] CLI Coding Agents (config/agents.yaml):")
    agent_cfg = load_agent_settings()
    print(f"  - enable_agent_execution: {agent_cfg.enable_agent_execution}")
    for a in detect_installed_agents(agent_cfg):
        st = f"OK ({a['resolved_path']})" if a["installed"] else "CHƯA CÀI"
        print(f"  - {a['id']:<10} ({a['label']}): {st}")

    print("\n[3] Projects (config/projects.yaml):")
    for proj in load_projects():
        repo = Path(proj.get("repository_path") or "").expanduser()
        exists = repo.exists() and repo.is_dir()
        is_git = (repo / ".git").exists() if exists else False
        exec_cfg = proj.get("execution") or {}
        print(
            f"  - {proj['key']} ({proj['name']}): repo={'OK' if exists else 'CHƯA GẮN (' + str(repo) + ')'} | "
            f"git={is_git} | dry_run={exec_cfg.get('dry_run', True)} | auto_create_pr={exec_cfg.get('auto_create_pr', False)}"
        )
    print("====================================================\n")


def list_clusters_cli():
    from app.context_collector import EventBufferQueue

    clusters = EventBufferQueue().load()
    if not clusters:
        print("\nChưa có cụm ngữ cảnh nào. Chạy 'python -m app.main collect' trước.\n")
        return
    print(f"\nTìm thấy {len(clusters)} cụm nguyên nhân gốc:")
    print(f"{'CLUSTER ID':<18} | {'CATEGORY':<10} | {'IMPACT':<7} | {'SOURCES':<18} | {'TITLE'}")
    print("-" * 96)
    for c in clusters:
        srcs = ",".join(c.sources)[:18]
        print(f"{c.cluster_id:<18} | {c.category.value:<10} | {c.total_impact:<7} | {srcs:<18} | {c.title[:38]}")
    print("-" * 96)


def execute_cluster_cli(cluster_id: str, repo_path: str | None = None, apply_changes: bool = False):
    import sys
    from app.config import load_projects
    from app.constrained_runner import ConstrainedFourStepRunner, ConstraintConfig
    from app.context_collector import EventBufferQueue

    cluster = EventBufferQueue().get_cluster(cluster_id)
    if not cluster:
        print(f"Không tìm thấy cluster '{cluster_id}'. Chạy 'python -m app.main clusters' để xem danh sách.")
        sys.exit(1)

    resolved_repo = repo_path
    if not resolved_repo:
        for p in load_projects():
            if p["key"] == cluster.project_key:
                resolved_repo = p.get("repository_path", "")
                break

    config = ConstraintConfig(
        repository_path=resolved_repo or "",
        dry_run=not apply_changes,
    )
    runner = ConstrainedFourStepRunner()
    report = runner.run(cluster, config)

    print(f"\n=== Quy trình 4 bước (Inner Loop) — {report.run_id} ===")
    print(f"Cluster: {report.cluster_id} | Track: {report.track.value} | Status: {report.overall_status}")
    for s in report.steps:
        print(f"  [Bước {s.step_number}] {s.name:<38} -> {s.status.value.upper():<8} | {s.summary}")
    print(f"Artifacts: {report.artifacts_dir}\n")
    from app.constrained_runner.models import FAILED_RUN_STATUSES
    if report.overall_status in FAILED_RUN_STATUSES:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="CrashPilot AI — Firebase Crashlytics CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # browser-check
    subparsers.add_parser("browser-check", help="Verify legacy Firebase browser access")
    subparsers.add_parser("login", help="Sign in for Crashlytics collection")

    # sync
    sync_parser = subparsers.add_parser("sync", help="Extract crash issues from Firebase Crashlytics into local DB")
    sync_parser.add_argument("--project", "-p", help="Sync only a specific project key", default=None)
    sync_parser.add_argument("--headed", action="store_true", help="Run browser in visible mode (default: headless)")
    sync_parser.add_argument("--limit", type=int, default=5, help="Limit number of issue details to fetch (default: 5)")

    # cycle (End-to-End Autopilot: Sources -> Cluster -> 4-Step Constrained Runner)
    cycle_parser = subparsers.add_parser("cycle", help="Chạy trọn vẹn 1 chu trình: Sync nguồn -> Gom cụm -> Quy trình 4 bước")
    cycle_parser.add_argument("--limit", type=int, default=5, help="Số issue lấy chi tiết mỗi project (default: 5)")
    cycle_parser.add_argument("--project", default=None)
    cycle_parser.add_argument("--cluster", default=None)
    cycle_parser.add_argument("--headed", action="store_true", help="Mở trình duyệt có giao diện")
    cycle_parser.add_argument("--skip-browser-sync", "--skip-crashlytics-sync", action="store_true", help="Dùng dữ liệu Crashlytics hiện có trong DB")
    cycle_parser.add_argument("--scheduled", action="store_true", help="Chỉ quét các dự án đã đến hạn scan_interval_minutes")

    # list-issues
    list_parser = subparsers.add_parser("list-issues", help="List all tracked crash issues from local DB")
    list_parser.add_argument("--project", "-p", help="Filter by project key", default=None)
    list_parser.add_argument("--platform", help="Filter by platform (e.g. android, ios, web)", default=None)

    # show-issue
    show_parser = subparsers.add_parser("show-issue", help="Show full details and stack trace for an issue")
    show_parser.add_argument("--project")
    show_parser.add_argument("issue_id", help="The issue ID to inspect")

    # diagnose
    diag_parser = subparsers.add_parser("diagnose", help="Map stack trace frames to local source code and generate diagnosis context")
    diag_parser.add_argument("--project")
    diag_parser.add_argument("issue_id", help="The issue ID to diagnose")

    # collect (Outer Loop)
    collect_parser = subparsers.add_parser("collect", help="Thu thập ngữ cảnh tự động từ mọi nguồn đã đăng ký và gom cụm")
    collect_parser.add_argument("--source", "-s", nargs="+", default=None, help="Chỉ định nguồn cụ thể (VD: crashlytics code_audit inbox_json)")
    collect_parser.add_argument("--input", "-i", nargs="+", default=None, help="Đường dẫn tới 1 file đơn lẻ (.json/.jsonl/.csv/.txt/.log/.md) hoặc thư mục")
    collect_parser.add_argument("--project", "-p", default="default", help="Gán project_key mặc định cho --input (default: default)")

    # clusters
    subparsers.add_parser("clusters", help="Xem danh sách cụm nguyên nhân gốc trong Buffer & Event Queue")

    # doctor
    subparsers.add_parser("doctor", help="Kiểm tra sẵn sàng toàn bộ môi trường trước khi chạy thật với repo")

    # morning-audit (Giai đoạn 5)
    ma_parser = subparsers.add_parser(
        "morning-audit",
        help="Giai đoạn 5: Lấy mẫu kiểm tra các lượt chạy gần nhất & đề xuất nâng cấp vĩnh viễn luật môi trường",
    )
    ma_parser.add_argument("--ai", action="store_true", help="Gọi CLI Agent (sandbox chỉ-đọc) phân tích sâu mẫu lỗi")
    ma_parser.add_argument("--limit", type=int, default=25, help="Số lượt chạy gần nhất lấy mẫu (mặc định: 25)")

    # execute (Inner Loop 4 bước)
    exec_parser = subparsers.add_parser("execute", help="Chạy quy trình 4 bước trong môi trường ràng buộc cho một cluster")
    exec_parser.add_argument("cluster_id", help="ID của cluster (lấy từ lệnh 'clusters')")
    exec_parser.add_argument("--repo", default=None, help="Đường dẫn repository local (mặc định lấy từ config/projects.yaml)")
    exec_parser.add_argument("--apply", action="store_true", help="Tắt dry-run (tách nhánh & chạy executor thật nếu có)")

    # retry-task (Chạy tiếp từ bước lỗi, không chạy lại từ đầu)
    retry_parser = subparsers.add_parser("retry-task", help="Chạy tiếp/retry một task từ bước lỗi (Bước 3 hoặc Bước 4) thay vì chạy lại từ đầu")
    retry_parser.add_argument("task_ref", help="Mã task (VD: TASK-1012 hoặc task_id)")
    retry_parser.add_argument("--from-step", type=int, default=3, choices=[3, 4], help="Bước bắt đầu chạy lại (3: Agent thực thi & checks cơ học, 4: Kiểm định độc lập & PR gate)")

    # check-session
    subparsers.add_parser("check-session", help="Headless check: exit 0 if Google session valid, 2 if login required")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start local FastAPI Web Dashboard")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, default=8787, help="Port to bind (default: 8787)")

    args = parser.parse_args()

    if args.command in {"cycle", "sync", "execute"}:
        import signal
        import sys

        signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(143))

    if args.command == "browser-check":
        asyncio.run(run_browser_check())
    elif args.command == "login":
        import shutil
        import subprocess
        from app.config import Settings

        backend = Settings().crashlytics_backend
        if backend == "browser":
            asyncio.run(run_browser_check())
        elif backend == "firebase_cli":
            from app.firebase_cli import FirebaseCliError, resolve_firebase_login_command

            try:
                cmd = resolve_firebase_login_command()
            except FirebaseCliError as exc:
                raise SystemExit(str(exc)) from exc
            raise SystemExit(subprocess.call(cmd))
        else:
            gcloud = shutil.which("gcloud")
            if not gcloud:
                raise SystemExit("Install the Google Cloud CLI (gcloud), then run ./run.sh login.")
            raise SystemExit(subprocess.call([gcloud, "auth", "application-default", "login"]))
    elif args.command == "sync":
        import sys
        results = run_sync(project_key=args.project, headless=not args.headed, fetch_details_limit=args.limit)
        print(
            f"\nSync summary: {results['synced_projects']} project(s), "
            f"{results['total_issues_found']} issue(s), {results['details_extracted']} detail(s), "
            f"{len(results['errors'])} error(s)"
        )
        if results["errors"]:
            login_errors = [e for e in results["errors"] if "login required" in e["error"].lower()]
            sys.exit(2 if login_errors else 1)
    elif args.command == "cycle":
        import sys
        from app.constrained_runner.models import FAILED_RUN_STATUSES
        from app.orchestrator import run_cycle_sync

        summary = run_cycle_sync(
            fetch_details_limit=args.limit,
            headless=not args.headed,
            skip_browser_sync=args.skip_browser_sync,
            project_key=args.project,
            cluster_id=args.cluster,
            respect_schedule=args.scheduled,
        )
        print(
            f"\n=== Hoàn tất chu trình: {summary.collection.total_events} sự kiện -> "
            f"{len(summary.collection.clusters)} cụm -> {len(summary.runs)} lượt chạy 4 bước ==="
        )
        if summary.login_required:
            sys.exit(2)
        if (summary.sync_results or {}).get("errors") or any(st.status == "error" for st in summary.collection.statuses):
            sys.exit(3)
        if any(r.overall_status in FAILED_RUN_STATUSES for r in summary.runs):
            sys.exit(1)
    elif args.command == "list-issues":
        list_issues(project_key=args.project, platform=args.platform)
    elif args.command == "show-issue":
        show_issue(issue_id=args.issue_id, project_key=args.project)
    elif args.command == "diagnose":
        diagnose_issue_cli(issue_id=args.issue_id, project_key=args.project)
    elif args.command == "collect":
        collect_context_cli(sources=args.source, input_paths=args.input, project_key=args.project)
    elif args.command == "clusters":
        list_clusters_cli()
    elif args.command == "doctor":
        doctor_cli()
    elif args.command == "morning-audit":
        from app.hardening import propose_rules_with_ai_agent, sample_recent_runs

        report = propose_rules_with_ai_agent(limit=args.limit) if args.ai else sample_recent_runs(limit=args.limit)
        sm = report["summary"]
        print("\n=== GIAI ĐOẠN 5: BÁO CÁO LẤY MẪU BUỔI SÁNG & NÂNG CẤP MÔI TRƯỜNG ===")
        print(
            f"Đã quét: {sm['total_sampled']} lượt chạy | PR Live đạt: {sm['completed_live']} | "
            f"Verifier chặn: {sm['verifier_rejected']} | Lỗi khác: {sm['other_failed']} | Tự sửa: {sm['self_healed']}"
        )
        if report.get("ai_message"):
            print(f"AI Analysis: {report['ai_message']}")
        props = report.get("proposals") or []
        if not props:
            print("-> Chưa phát hiện đề xuất luật mới nào cần bổ sung.")
        else:
            print(f"\nĐề xuất nâng cấp vĩnh viễn ({len(props)} quy tắc):")
            for p in props:
                print(f"  * [{p['kind']}] {p['id']}: {p['pattern']}")
                print(f"    Lý do: {p.get('rationale') or p.get('message') or ''}")
                if p.get("source_runs"):
                    print(f"    Nguồn phát hiện: {', '.join(p['source_runs'][:3])}")
    elif args.command == "execute":
        execute_cluster_cli(cluster_id=args.cluster_id, repo_path=args.repo, apply_changes=args.apply)
    elif args.command == "retry-task":
        import sys
        from pathlib import Path
        from app.db.database import get_db
        from app.db.models import TaskModel
        from app.context_collector import EventBufferQueue
        from app.context_collector.base import EventCluster
        from app.constrained_runner import ConstrainedFourStepRunner, ConstraintConfig
        from app.config import load_all_projects
        from app.db.database import SessionLocal

        with SessionLocal() as db:
            task = (
                db.query(TaskModel)
                .filter((TaskModel.task_id == args.task_ref) | (TaskModel.cluster_id == args.task_ref))
                .first()
            )
            if not task:
                print(f"Lỗi: Không tìm thấy task '{args.task_ref}' trong cơ sở dữ liệu.")
                sys.exit(1)

            cluster = None
            queue = EventBufferQueue()
            clusters = {c.cluster_id: c for c in queue.load()}
            if task.cluster_id:
                cluster = clusters.get(task.cluster_id)

            if not cluster and task.latest_run_id:
                run_dir = Path("data/runs") / str(task.latest_run_id)
                cev = run_dir / "cluster_events.json"
                if cev.is_file():
                    try:
                        cluster = EventCluster.model_validate_json(cev.read_text(encoding="utf-8"))
                    except Exception:
                        cluster = None

            if not cluster:
                # Nếu cluster không có trong file events, tạo một cluster tối thiểu từ thông tin của task
                cluster = EventCluster(
                    cluster_id=task.cluster_id or f"cls_{task.task_id}",
                    project_key=task.project_key,
                    events=[],
                    title=task.title or task.task_id,
                )

            from app.constrained_runner.models import RuntimeVerifierConfig

            project_cfg = next((p for p in load_all_projects() if p.get("key") == task.project_key), {})
            exec_cfg = project_cfg.get("execution", {})
            repo_path = project_cfg.get("repository_path")
            rv_raw = exec_cfg.get("runtime_verifier")
            runtime_verifier = RuntimeVerifierConfig.model_validate(rv_raw) if isinstance(rv_raw, dict) else None
            cfg = ConstraintConfig(
                repository_path=repo_path or "",
                base_branch=str(exec_cfg.get("base_branch", "main")),
                dry_run=bool(exec_cfg.get("dry_run", True)),
                **{key: exec_cfg[key] for key in ("protected_branches", "forbidden_path_patterns") if key in exec_cfg},
                mechanical_checks=exec_cfg.get("mechanical_checks") or [],
                verify_commands=exec_cfg.get("verify_commands") or [],
                runtime_verifier=runtime_verifier,
                executor_command=exec_cfg.get("executor_command"),
                verifier_command=exec_cfg.get("verifier_command"),
                max_retries=int(exec_cfg.get("max_retries", 1)),
                auto_create_pr=bool(exec_cfg.get("auto_create_pr", False)),
                auto_merge=bool(exec_cfg.get("auto_merge", False)),
            )
            runner = ConstrainedFourStepRunner()
            print(f"[*] Bắt đầu retry cho {task.task_id} từ Bước {args.from_step}/4 (run: {task.latest_run_id})...")
            report = runner.resume_from_step(
                run_id=task.latest_run_id or f"run_{task.cluster_id}",
                cluster=cluster,
                config=cfg,
                from_step=args.from_step,
                task_id=task.task_id,
            )
            print(f"[+] Kết quả retry: overall_status={report.overall_status}")
            sys.exit(0 if report.overall_status in {"VERIFIED_READY_FOR_PR", "PR_CREATED", "AUTO_MERGED"} else 1)
    elif args.command == "check-session":
        import sys
        sys.exit(asyncio.run(check_session()))
    elif args.command == "serve":
        import uvicorn
        from ipaddress import ip_address
        try:
            local = args.host == "localhost" or ip_address(args.host).is_loopback
        except ValueError:
            local = False
        if not local:
            parser.error("Dashboard local chỉ cho phép địa chỉ loopback.")
        uvicorn.run("app.api.server:app", host=args.host, port=args.port, reload=False, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
