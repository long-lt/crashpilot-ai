import fnmatch
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.analyzer.source_mapper import build_diagnosis_report
from app.constrained_runner.isolation import run_agent
from app.constrained_runner.agents import (
    AgentSettings,
    build_collect_triage_prompt,
    build_prepare_context_prompt,
    load_agent_settings,
    resolve_agents_for_need,
    resolve_step_agents,
)
from app.constrained_runner.models import (
    AgentTrack,
    ConstraintConfig,
    PipelineRunReport,
    StepResult,
    StepStatus,
)
from app.constrained_runner.runtime_verifier import run_runtime_verifier
from app.constrained_runner.verifier import (
    PrGateOutcome,
    commit_push_and_create_pr,
    observed_pr_state,
    detect_independent_verify_checks,
    run_independent_verification,
    run_verifier_agent,
)
from app.context_collector.base import EventCategory, EventCluster
from app.context_collector.sources.inbox_source import mark_checklist_done
from app.hardening import load_environment_rules


def forbidden_path(name: str, pattern: str) -> bool:
    path = Path(name.casefold())
    rule = pattern.casefold().rstrip("/")
    if not rule:
        return False
    if "/" in rule:
        return path.match(rule) or path.as_posix().startswith(rule + "/")
    if rule == ".env":
        return any(part == rule or part.startswith(".env.") for part in path.parts)
    if rule.startswith("."):
        return any(part == rule or part.endswith(rule) for part in path.parts)
    return any(fnmatch.fnmatchcase(part, rule) or part.startswith(rule + ".") for part in path.parts)


def classify_track(category: EventCategory) -> AgentTrack:
    if category in (EventCategory.CRASH, EventCategory.BUG):
        return AgentTrack.BUG_FIX
    if category == EventCategory.PERFORMANCE:
        return AgentTrack.PERFORMANCE
    if category == EventCategory.TECH_DEBT:
        return AgentTrack.CODE_GARDENING
    return AgentTrack.EXPERIMENTAL


def detect_default_checks(repo_root: Path) -> list[list[str]]:
    """Tự nhận diện lệnh kiểm tra cơ học thật theo loại dự án trong repo."""
    checks: list[list[str]] = []
    if (repo_root / "pubspec.yaml").exists() and shutil.which("dart"):
        checks.append(["dart", "analyze"])
    elif (repo_root / "pyproject.toml").exists():
        venv_pytest = repo_root / ".venv" / "bin" / "pytest"
        if venv_pytest.exists():
            checks.append([str(venv_pytest), "-q"])
        elif shutil.which("pytest"):
            checks.append(["pytest", "-q"])
    elif (repo_root / "package.json").exists() and shutil.which("npm"):
        checks.append(["npm", "test", "--", "--watchAll=false"])
    return checks


def flutter_analysis_checks(worktree: Path, checks: list[list[str]]) -> list[list[str]]:
    """Resolve the post-edit manifest before each Dart analysis in a Flutter worktree."""
    manifest = worktree / "pubspec.yaml"
    if not manifest.is_file():
        return checks
    try:
        spec = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        spec = {}  # A malformed Flutter manifest should still produce a pub diagnosis.
    is_flutter = isinstance(spec, dict) and any(
        isinstance(dep, dict) and dep.get("sdk") == "flutter"
        for section in ("dependencies", "dev_dependencies")
        if isinstance(spec.get(section), dict)
        for dep in spec[section].values()
    )
    if not is_flutter and not (worktree / ".metadata").is_file():
        return checks
    ordered = []
    for cmd in checks:
        if len(cmd) >= 2 and Path(cmd[0]).name == "dart" and cmd[1] == "analyze":
            if not ordered or ordered[-1][:3] != ["flutter", "pub", "get"]:
                ordered.append(["flutter", "pub", "get", "--offline"])
        ordered.append(cmd)
    return ordered


def bootstrap_flutter_worktree(source_repo: Path, worktree: Path) -> Optional[str]:
    """Khởi tạo .dart_tool/package_config.json cho worktree Flutter/Dart mới để dart analyze không báo lỗi giả."""
    if not (worktree / "pubspec.yaml").is_file():
        return None
    pkg_cfg = worktree / ".dart_tool" / "package_config.json"
    if pkg_cfg.is_file():
        return None

    had_lock = (worktree / "pubspec.lock").is_file()
    lock_tracked = (
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "pubspec.lock"],
            cwd=worktree,
            capture_output=True,
        ).returncode
        == 0
    )

    flutter_bin = shutil.which("flutter")
    dart_bin = shutil.which("dart")
    pub_cmds: list[list[str]] = []
    if flutter_bin:
        pub_cmds.extend([[flutter_bin, "pub", "get", "--offline"], [flutter_bin, "pub", "get"]])
    elif dart_bin:
        pub_cmds.extend([[dart_bin, "pub", "get", "--offline"], [dart_bin, "pub", "get"]])

    last_err = ""
    for cmd in pub_cmds:
        try:
            res = subprocess.run(cmd, cwd=worktree, capture_output=True, text=True, timeout=60)
            if res.returncode == 0 and pkg_cfg.is_file():
                break
            last_err = (res.stderr or res.stdout or "").strip()[-300:]
        except Exception as exc:
            last_err = str(exc)

    if not pkg_cfg.is_file() and (source_repo / ".dart_tool").is_dir():
        shutil.copytree(source_repo / ".dart_tool", worktree / ".dart_tool", dirs_exist_ok=True)

    # Giữ cây Git sạch như ban đầu để tránh làm bẩn diff nếu pub get tự chạm vào pubspec.lock
    if lock_tracked:
        subprocess.run(["git", "checkout", "--", "pubspec.lock"], cwd=worktree, capture_output=True)
    elif not had_lock and (worktree / "pubspec.lock").is_file():
        ig = subprocess.run(["git", "check-ignore", "-q", "pubspec.lock"], cwd=worktree).returncode == 0
        if not ig:
            (worktree / "pubspec.lock").unlink(missing_ok=True)

    return None if pkg_cfg.is_file() else (last_err or "Không tạo được .dart_tool/package_config.json")


class ConstrainedFourStepRunner:
    """Quy trình 4 bước gói gọn để thực thi trong môi trường ràng buộc (Inner Loop).

    Bước 1: Khóa phạm vi & Ràng buộc môi trường (Scope & Constraint Guard)
    Bước 2: Tổng hợp ngữ cảnh & Kế hoạch chuyên trách (Context & Brief Synthesis)
    Bước 3: Thực thi cách ly & Kiểm tra cơ học (Isolated Execution & Mechanical Checks)
    Bước 4: Kiểm định độc lập & Cổng tạo PR (Independent Verification & PR Gate)
    """

    def __init__(self, runs_dir: Path | str = "data/runs", agent_settings: Optional[AgentSettings] = None):
        self.runs_dir = Path(runs_dir)
        self._agent_settings = agent_settings

    def run(
        self, cluster: EventCluster, config: ConstraintConfig, task_id: Optional[str] = None
    ) -> PipelineRunReport:
        if config.dry_run:
            with tempfile.TemporaryDirectory(prefix="crashpilot-preview-") as temp:
                return self._run(cluster, config, Path(temp) / "repo", task_id=task_id)
        return self._run(cluster, config, None, task_id=task_id)

    def _run(
        self,
        cluster: EventCluster,
        config: ConstraintConfig,
        preview: Path | None,
        task_id: Optional[str] = None,
    ) -> PipelineRunReport:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"run_{cluster.cluster_id}_{stamp}_{uuid4().hex[:12]}"
        artifacts_dir = self.runs_dir / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        with (artifacts_dir / "run.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._run_steps(cluster, config, preview, run_id, artifacts_dir, task_id=task_id)

    def _run_steps(
        self,
        cluster: EventCluster,
        config: ConstraintConfig,
        preview: Path | None,
        run_id: str,
        artifacts_dir: Path,
        task_id: Optional[str] = None,
    ) -> PipelineRunReport:
        track = classify_track(cluster.category)
        steps: list[StepResult] = []
        verification_rounds: list[dict] = []
        branch_name: Optional[str] = None

        def mark_running(step_num: int, step_name: str) -> None:
            print(f"    [B{step_num}/4] Đang chạy: {step_name}...", flush=True)
            running_step = StepResult(
                step_number=step_num,
                name=step_name,
                status=StepStatus.RUNNING,
                summary=f"Đang thực thi Bước {step_num}/4: {step_name}...",
            )
            self._write_report(
                run_id=run_id,
                cluster=cluster,
                track=track,
                config=config,
                branch_name=branch_name,
                overall_status="RUNNING",
                steps=[*steps, running_step],
                artifacts_dir=artifacts_dir,
                current_step=step_num,
                current_step_name=step_name,
                task_id=task_id,
                verification_rounds=verification_rounds,
            )

        # --- BƯỚC 1: Khóa phạm vi & Ràng buộc môi trường + AI Triage (nếu gán Agent) ---
        mark_running(1, "Scope & Constraint Guard")
        step1, branch_name = self.step_1_guard_environment(cluster, config, artifacts_dir, preview)
        triage_hypothesis = ""
        if step1.status == StepStatus.PASSED:
            track, triage_hypothesis = self._run_step1_triage_agent(
                cluster, track, config, step1, artifacts_dir
            )
        steps.append(step1)
        print(f"    [B1/4] -> {step1.status.value.upper()}: {step1.summary}", flush=True)
        if step1.status != StepStatus.PASSED:
            return self._finalize(
                run_id, cluster, track, config, branch_name, "BLOCKED", steps, artifacts_dir,
                task_id=task_id, verification_rounds=verification_rounds,
            )

        execution_config = config.model_copy(update={"repository_path": step1.details["repo_root"]})

        defaults = detect_default_checks(Path(execution_config.repository_path))
        if preview and (preview / "pyproject.toml").exists():
            local_pytest = Path(config.repository_path).expanduser().resolve() / ".venv/bin/pytest"
            if local_pytest.is_file():
                defaults = [[str(local_pytest), "-q"]]
        execution_config.auto_merge = config.auto_merge and not any(
            fnmatch.fnmatchcase(config.base_branch, rule) for rule in config.protected_branches)
        env_rules_stage1 = load_environment_rules()
        execution_config.forbidden_path_patterns = list(
            dict.fromkeys([*config.forbidden_path_patterns, *env_rules_stage1.forbidden_path_patterns])
        )
        execution_config.mechanical_checks = [
            *(config.mechanical_checks or defaults),
            *env_rules_stage1.extra_mechanical_checks,
        ]
        execution_config.mechanical_checks = flutter_analysis_checks(
            Path(execution_config.repository_path), execution_config.mechanical_checks
        )
        if preview is not None and sys.platform == "darwin":
            # A configured check may use an absolute path back into the original checkout.
            profile = '(version 1)(allow default)(deny file-write* (subpath (param "SOURCE")))'
            guard = ["/usr/bin/sandbox-exec", "-D", f"SOURCE={Path(config.repository_path).expanduser().resolve()}", "-p", profile]
            guard.extend(["/usr/bin/env", f"TMPDIR={preview.parent}", f"XDG_CACHE_HOME={preview.parent}/cache",
                          "PYTHONDONTWRITEBYTECODE=1"])
            execution_config.mechanical_checks = [guard + cmd for cmd in
                execution_config.mechanical_checks]
            execution_config.verify_commands = [guard + cmd for cmd in
                (config.verify_commands or detect_independent_verify_checks(preview))]
        # --- BƯỚC 2: Tổng hợp ngữ cảnh & Kế hoạch chuyên trách ---
        mark_running(2, "Context & Brief Synthesis")
        step2, brief_path = self.step_2_synthesize_brief(
            cluster, track, execution_config, artifacts_dir, triage_hypothesis=triage_hypothesis
        )
        steps.append(step2)
        print(f"    [B2/4] -> {step2.status.value.upper()}: {step2.summary}", flush=True)
        if step2.status != StepStatus.PASSED:
            return self._finalize(
                run_id, cluster, track, config, branch_name, "FAILED", steps, artifacts_dir,
                task_id=task_id, verification_rounds=verification_rounds,
            )

        # --- BƯỚC 3 & BƯỚC 4 + Vòng Self-healing khi Verifier (Bước 4) báo FAIL ---
        feedback_from_verifier = ""
        max_heal_rounds = 1 if config.dry_run else (config.max_retries + 1)
        cumulative_attempts: list[dict] = []

        for heal_round in range(1, max_heal_rounds + 1):
            mark_running(3, "Isolated Execution & Mechanical Checks")
            step3 = self.step_3_execute_and_check(
                cluster,
                track,
                execution_config,
                brief_path,
                initial_error=feedback_from_verifier,
                start_attempt=len(cumulative_attempts) + 1,
            )
            round_attempts = list(step3.details.get("attempts") or [])
            cumulative_attempts.extend(round_attempts)
            step3.details["attempts"] = list(cumulative_attempts)
            # Giữ bản mới nhất của Bước 3 trong danh sách 4 bước (kèm trọn lịch sử attempts)
            steps = [s for s in steps if s.step_number < 3] + [step3]
            print(f"    [B3/4] -> {step3.status.value.upper()}: {step3.summary}", flush=True)
            if step3.status != StepStatus.PASSED:
                return self._finalize(
                    run_id, cluster, track, config, branch_name,
                    "BLOCKED" if step3.status == StepStatus.BLOCKED else "FAILED", steps, artifacts_dir,
                    task_id=task_id, verification_rounds=verification_rounds,
                )

            mark_running(4, "Independent Verification & PR Gate")
            step4, final_status, pr_url, verifier_feedback = self.step_4_verify_and_pr_gate(
                cluster, track, execution_config, branch_name, brief_path, step3, artifacts_dir,
                step1.details.get("base_commit"), attempt_no=heal_round,
            )
            steps = [s for s in steps if s.step_number < 4] + [step4]
            s4_det = step4.details or {}
            last_att = cumulative_attempts[-1] if cumulative_attempts else {}
            verification_rounds.append(
                {
                    "attempt": heal_round,
                    "agent": (last_att.get("executor") or {}).get("agent") or step3.details.get("selected_agent"),
                    "mechanical_passed": True,
                    "mechanical_checks": last_att.get("checks") or [],
                    "stage_4a": s4_det.get("stage_4a", "PASS" if step4.status == StepStatus.PASSED else "FAIL"),
                    "stage_4b": s4_det.get("stage_4b", "SKIP"),
                    "stage_4c": s4_det.get("stage_4c", "SKIP"),
                    "passed": step4.status == StepStatus.PASSED,
                    "verdict": "PASS" if step4.status == StepStatus.PASSED else "FAIL",
                    "feedback": verifier_feedback,
                    "patch_file": s4_det.get("attempt_patch_file"),
                    "patch_sha256": s4_det.get("patch_sha256"),
                }
            )
            print(f"    [B4/4] -> {step4.status.value.upper()}: {step4.summary}", flush=True)

            if final_status == "VERIFICATION_FAILED" and heal_round < max_heal_rounds and verifier_feedback:
                print(
                    f"    [Self-healing] Verifier báo chưa đạt (vòng {heal_round}/{max_heal_rounds - 1}) "
                    "-> đẩy phản hồi về Bước 3 (verify_fix) để tự sửa lại...",
                    flush=True,
                )
                feedback_from_verifier = verifier_feedback
                continue

            if not config.dry_run and final_status in {"PUSHED_BRANCH", "PR_CREATED", "AUTO_MERGE_REQUESTED", "AUTO_MERGED", "VERIFIED_NO_CHANGES"}:
                # Git refuses removal if a check left additional work in this checkout.
                subprocess.run(
                    ["git", "worktree", "remove", execution_config.repository_path],
                    cwd=Path(config.repository_path).expanduser(), capture_output=True,
                )
            if (not config.dry_run and step4.status == StepStatus.PASSED
                    and step3.details["attempts"][-1]["executor"].get("exit_code") == 0
                    and any(event.checklist_item for event in cluster.events)):
                step4.details["processing_status"] = final_status
                pending = self._finalize(
                    run_id, cluster, track, config, branch_name, "CHECKLIST_UPDATE_PENDING",
                    steps, artifacts_dir, pr_url=pr_url, task_id=task_id, verification_rounds=verification_rounds,
                )
                return self.finish_checklist(cluster, pending)
            return self._finalize(
                run_id, cluster, track, config, branch_name, final_status, steps, artifacts_dir,
                pr_url=pr_url, task_id=task_id, verification_rounds=verification_rounds,
            )

        return self._finalize(
            run_id, cluster, track, config, branch_name, "VERIFICATION_FAILED", steps, artifacts_dir,
            task_id=task_id, verification_rounds=verification_rounds,
        )

    # ------------------------------------------------------------------
    # BƯỚC 1: Scope & Constraint Guard
    # ------------------------------------------------------------------
    def _copy_preview(self, source: Path, target: Path, is_git: bool) -> None:
        """Copy working files (including pending edits) into a disposable independent checkout."""
        if is_git:
            subprocess.run(["git", "clone", "--quiet", "--no-local", "--no-checkout", str(source), str(target)],
                           check=True, capture_output=True)
            subprocess.run(["git", "remote", "remove", "origin"], cwd=target, check=True, capture_output=True, timeout=30)

            names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others",
                                            "--exclude-standard", "--deduplicate"], cwd=source)
            files = [source / os.fsdecode(name) for name in names.split(b"\0") if name]
            # Node/Dart checks resolve installed packages relative to their working directory.
            for name in ("node_modules", ".dart_tool", "Pods"):
                deps = source / name
                if deps.is_dir():
                    files.extend([deps, *deps.rglob("*")])
        else:
            target.mkdir()
            files = source.rglob("*")
        runs = self.runs_dir.resolve()
        for path in dict.fromkeys(files):
            if path.is_relative_to(runs) or path.is_relative_to(target.parent):
                continue
            destination = target / path.relative_to(source)
            if path.is_symlink():
                resolved = path.resolve()
                if not resolved.is_relative_to(source):
                    raise ValueError(f"Liên kết vượt ngoài repository: {path.relative_to(source)}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(os.path.relpath(target / resolved.relative_to(source), destination.parent))
            elif path.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
            elif path.is_dir():
                destination.mkdir(parents=True, exist_ok=True)

        if is_git:
            # Establish a private baseline including the developer's uncommitted work.
            subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.name=CrashPilot Preview", "-c", "user.email=preview@localhost",
                            "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "--no-gpg-sign",
                            "-qm", "Preview baseline"], cwd=target, check=True, capture_output=True)

    def step_1_guard_environment(
        self, cluster: EventCluster, config: ConstraintConfig, artifacts_dir: Path, preview: Path | None = None
    ) -> tuple[StepResult, Optional[str]]:
        repo_root = Path(config.repository_path).expanduser()
        if not config.repository_path or not repo_root.is_dir():
            return (
                StepResult(
                    step_number=1,
                    name="Scope & Constraint Guard",
                    status=StepStatus.BLOCKED,
                    summary=f"Repository không tồn tại: {config.repository_path}",
                    details={"repository_path": config.repository_path, "reason": "missing_repository"},
                ),
                None,
            )

        from app.config import inspect_git_repository

        git_info = inspect_git_repository(repo_root)
        is_git = (repo_root / ".git").exists()
        checked_out_branch = git_info.get("current_branch")
        available_branches = git_info.get("branches") or []
        current_branch: Optional[str] = None
        base_commit = None
        if not config.dry_run:
            worktree = (artifacts_dir / "worktree").resolve()
            current_branch = f"crashpilot/{artifacts_dir.name}"
            if any(fnmatch.fnmatchcase(current_branch, rule) for rule in config.protected_branches):
                return StepResult(step_number=1, name="Scope & Constraint Guard", status=StepStatus.BLOCKED,
                                  summary=f"Nhánh thực thi được bảo vệ: {current_branch}"), None
            base_ref = f"refs/heads/{config.base_branch}"
            if is_git:
                has_local = subprocess.run(
                    ["git", "show-ref", "--verify", "--quiet", base_ref],
                    cwd=repo_root, capture_output=True, text=True,
                ).returncode == 0
                if not has_local:
                    remote_ref = f"refs/remotes/origin/{config.base_branch}"
                    has_remote = subprocess.run(
                        ["git", "show-ref", "--verify", "--quiet", remote_ref],
                        cwd=repo_root, capture_output=True, text=True,
                    ).returncode == 0
                    if has_remote:
                        base_ref = remote_ref
            checkout = subprocess.run(
                ["git", "worktree", "add", "-b", current_branch, str(worktree), base_ref],
                cwd=repo_root, capture_output=True, text=True,
            ) if is_git else None
            if checkout is None or checkout.returncode:
                return StepResult(
                    step_number=1, name="Scope & Constraint Guard", status=StepStatus.BLOCKED,
                    summary=(
                        f"Không thể tạo worktree từ nhánh cơ sở '{config.base_branch}' "
                        f"(đang đứng: {checked_out_branch or 'N/A'}, các nhánh: {', '.join(available_branches) or 'N/A'})."
                    ),
                    details={"reason": "worktree_failed",
                             "base_branch": config.base_branch,
                             "checked_out_branch": checked_out_branch,
                             "available_branches": available_branches,
                             "error": checkout.stderr.strip() if checkout else "Không phải Git repository"},
                ), None
            source_repo = repo_root.resolve()
            repo_root = worktree
            base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()
            bootstrap_err = bootstrap_flutter_worktree(source_repo, worktree)
            if bootstrap_err:
                return StepResult(
                    step_number=1,
                    name="Scope & Constraint Guard",
                    status=StepStatus.BLOCKED,
                    summary=f"Không thể khởi tạo dependencies Flutter/Dart trong worktree: {bootstrap_err}",
                    details={"reason": "pub_get_failed", "repo_root": str(worktree), "base_commit": base_commit},
                ), current_branch
        elif preview is not None:
            try:
                self._copy_preview(repo_root.resolve(), preview, is_git)
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                return StepResult(step_number=1, name="Scope & Constraint Guard", status=StepStatus.BLOCKED,
                                  summary=f"Không tạo được bản sao chạy thử: {exc}"), None
            repo_root = preview

        checks = config.mechanical_checks or detect_default_checks(repo_root)
        return (
            StepResult(
                step_number=1,
                name="Scope & Constraint Guard",
                status=StepStatus.PASSED,
                summary=(
                    f"Môi trường hợp lệ (git={is_git}, đang đứng={checked_out_branch or 'N/A'}, "
                    f"base={config.base_branch}, branch={current_branch or 'N/A'}, checks={len(checks)})."
                ),
                details={
                    "repo_root": str(repo_root.resolve()),
                    "is_git": is_git,
                    "checked_out_branch": checked_out_branch,
                    "base_branch": config.base_branch,
                    "available_branches": available_branches,
                    "base_commit": base_commit,
                    "branch": current_branch,
                    "dry_run": config.dry_run,
                    "mechanical_checks": [" ".join(c) for c in checks],
                },
            ),
            current_branch,
        )

    def _run_step1_triage_agent(
        self,
        cluster: EventCluster,
        default_track: AgentTrack,
        config: ConstraintConfig,
        step1: StepResult,
        artifacts_dir: Path,
    ) -> tuple[AgentTrack, str]:
        agent_settings = self._agent_settings or load_agent_settings()
        if config.dry_run or config.executor_command or not agent_settings.enable_agent_execution:
            step1.details["collector_agent"] = {"executed": False, "skipped_reason": "dry_run hoặc tắt CLI agent"}
            return default_track, ""

        cluster_file = artifacts_dir / "cluster_events.json"
        cluster_payload = {
            "cluster_id": cluster.cluster_id,
            "project_key": cluster.project_key,
            "title": cluster.title,
            "category": cluster.category.value,
            "sources": cluster.sources,
            "total_impact": cluster.total_impact,
            "blame_file": cluster.blame_file,
            "blame_line": cluster.blame_line,
            "events": [ev.model_dump(mode="json") for ev in cluster.events[:8]],
        }
        cluster_file.write_text(json.dumps(cluster_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        prompt = build_collect_triage_prompt(cluster_file)
        candidates = [
            c for c in resolve_step_agents("collect_group", prompt, cluster_file, settings=agent_settings)
            if c.installed
        ]
        if not candidates:
            step1.details["collector_agent"] = {"executed": False, "skipped_reason": "Chưa gán hoặc chưa cài Agent Bước 1"}
            return default_track, ""

        ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        repo_dir = Path(step1.details.get("repo_root") or config.repository_path).expanduser().resolve()
        valid_tracks = {t.value: t for t in AgentTrack}
        chosen_track = default_track
        hypothesis = ""

        for cand in candidates:
            prof = agent_settings.profiles.get(cand.agent_id)
            res = run_agent(
                cand.command,
                repo_dir,
                writable=False,
                inputs=[cluster_file],
                variables={"CI": "1"},
                timeout=min(cand.timeout_sec, 120),
                credential_names=prof.credential_env if prof else (),
                proxy_port=prof.loopback_proxy_port if prof else None,
            )
            clean_out = ansi_re.sub("", (res.stdout or "")).strip()
            if res.returncode == 0 and clean_out:
                m_track = re.search(
                    r"(?im)^\s*TRACK\s*:\s*(bug_fix|performance|code_gardening|experimental)\b",
                    clean_out,
                )
                m_root = re.search(r"(?im)^\s*ROOT_CAUSE\s*:\s*(.+)$", clean_out)
                if m_track:
                    chosen_track = valid_tracks[m_track.group(1).lower()]
                if m_root:
                    hypothesis = m_root.group(1).strip()
                elif not m_track:
                    hypothesis = clean_out.splitlines()[0][:240]
                step1.details["collector_agent"] = {
                    "executed": True,
                    "agent": f"{cand.agent_id} ({cand.label})",
                    "exit_code": 0,
                    "recommended_track": chosen_track.value,
                    "root_cause": hypothesis[:300],
                }
                step1.summary = f"{step1.summary[:-1]}, triage={cand.agent_id}->{chosen_track.value})."
                return chosen_track, hypothesis
            step1.details["collector_agent"] = {
                "executed": True,
                "agent": f"{cand.agent_id} ({cand.label})",
                "exit_code": res.returncode,
                "error_tail": ((res.stderr or res.stdout or "").strip())[-300:],
            }

        return chosen_track, hypothesis

    # ------------------------------------------------------------------
    # BƯỚC 2: Context & Brief Synthesis
    # ------------------------------------------------------------------
    def step_2_synthesize_brief(
        self,
        cluster: EventCluster,
        track: AgentTrack,
        config: ConstraintConfig,
        artifacts_dir: Path,
        triage_hypothesis: str = "",
    ) -> tuple[StepResult, Path]:
        primary_ev = cluster.events[0] if cluster.events else None
        stack_trace = primary_ev.body if primary_ev else ""
        meta = primary_ev.metadata if primary_ev else {}

        diagnosis = build_diagnosis_report(
            issue_id=cluster.cluster_id,
            title=cluster.title,
            subtitle=f"Sources: {', '.join(cluster.sources)} | Total impact: {cluster.total_impact}",
            stack_trace=stack_trace,
            blame_file=cluster.blame_file,
            blame_line=cluster.blame_line,
            repository_path=config.repository_path,
            app_version=meta.get("app_version"),
            os_version=meta.get("os_version"),
        )

        header_lines = [
            f"# Execution Brief — `{cluster.cluster_id}`",
            "",
            f"- **Agent Track**: `{track.value}`",
            f"- **Project**: `{cluster.project_key}`",
            f"- **Category**: `{cluster.category.value}`",
            f"- **Sources**: `{', '.join(cluster.sources)}`",
            f"- **Total Impact**: `{cluster.total_impact}` ({cluster.event_count} events)",
            f"- **Forbidden Paths**: `{', '.join(config.forbidden_path_patterns)}`",
        ]
        if triage_hypothesis:
            header_lines.append(f"- **AI Triage Hypothesis (Step 1)**: {triage_hypothesis}")
        header_lines.extend(["", diagnosis.markdown_report])

        brief_md = "\n".join(header_lines)
        brief_path = artifacts_dir / "brief.md"
        brief_path.write_text(brief_md, encoding="utf-8")

        mapped_files = [loc.resolved_path for loc in diagnosis.locations if loc.resolved_path]
        agent_settings = self._agent_settings or load_agent_settings()
        context_agent_info: dict = {"executed": False}
        used_ctx_agent = ""

        if not config.dry_run and not config.executor_command and agent_settings.enable_agent_execution:
            prompt = build_prepare_context_prompt(brief_path)
            candidates = [
                c for c in resolve_step_agents("prepare_context", prompt, brief_path, settings=agent_settings)
                if c.installed
            ]
            repo_dir = Path(config.repository_path).expanduser().resolve()
            ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
            for cand in candidates:
                prof = agent_settings.profiles.get(cand.agent_id)
                res = run_agent(
                    cand.command,
                    repo_dir,
                    writable=False,
                    inputs=[brief_path],
                    variables={"CI": "1"},
                    timeout=min(cand.timeout_sec, 180),
                    credential_names=prof.credential_env if prof else (),
                    proxy_port=prof.loopback_proxy_port if prof else None,
                )
                clean_out = ansi_re.sub("", (res.stdout or "")).strip()
                if res.returncode == 0 and clean_out:
                    discovered: list[str] = []
                    m_files = re.search(r"(?im)^\s*MAPPED_FILES\s*:\s*(.+)$", clean_out)
                    if m_files:
                        raw_list = m_files.group(1).strip()
                        if raw_list.upper() != "NONE":
                            for part in raw_list.split(","):
                                rel = part.strip().strip("`'\"")
                                if not rel:
                                    continue
                                candidate_file = (repo_dir / rel).resolve()
                                if candidate_file.is_relative_to(repo_dir) and candidate_file.is_file():
                                    rel_norm = str(candidate_file.relative_to(repo_dir))
                                    if rel_norm not in discovered:
                                        discovered.append(rel_norm)
                                    if rel_norm not in mapped_files:
                                        mapped_files.append(rel_norm)
                    notes_lines = [
                        ln for ln in clean_out.splitlines()
                        if not re.match(r"(?i)^\s*MAPPED_FILES\s*:", ln)
                    ]
                    notes_text = "\n".join(notes_lines).strip()
                    if notes_text or discovered:
                        extra_section = "\n\n## Khảo sát Ngữ cảnh Mã nguồn (Step 2 Context Agent)\n\n"
                        if discovered:
                            extra_section += "- **File nguồn liên quan đã xác thực**: " + ", ".join(f"`{p}`" for p in discovered) + "\n\n"
                        if notes_text:
                            extra_section += notes_text[:3000] + "\n"
                        brief_path.write_text(brief_md + extra_section, encoding="utf-8")
                    used_ctx_agent = f"{cand.agent_id} ({cand.label})"
                    context_agent_info = {
                        "executed": True,
                        "agent": used_ctx_agent,
                        "exit_code": 0,
                        "discovered_files": discovered,
                    }
                    break
                context_agent_info = {
                    "executed": True,
                    "agent": f"{cand.agent_id} ({cand.label})",
                    "exit_code": res.returncode,
                    "error_tail": ((res.stderr or res.stdout or "").strip())[-300:],
                }

        total_locs = max(len(diagnosis.locations), len(mapped_files))
        agent_suffix = f", agent={used_ctx_agent}" if used_ctx_agent else ""
        return (
            StepResult(
                step_number=2,
                name="Context & Brief Synthesis",
                status=StepStatus.PASSED,
                summary=(
                    f"Đã sinh Execution Brief ({track.value}), ánh xạ được "
                    f"{len(mapped_files)}/{total_locs} file nguồn{agent_suffix}."
                ),
                details={
                    "track": track.value,
                    "brief_path": str(brief_path),
                    "mapped_files": mapped_files,
                    "context_agent": context_agent_info,
                },
            ),
            brief_path,
        )

    # ------------------------------------------------------------------
    # BƯỚC 3: Isolated Execution & Mechanical Checks (+ Self-healing Retry)
    # ------------------------------------------------------------------
    def step_3_execute_and_check(
        self,
        cluster: EventCluster,
        track: AgentTrack,
        config: ConstraintConfig,
        brief_path: Path,
        initial_error: str = "",
        start_attempt: int = 1,
    ) -> StepResult:
        repo_root = Path(config.repository_path).expanduser()
        checks = config.mechanical_checks or detect_default_checks(repo_root)
        agent_settings = self._agent_settings or load_agent_settings()

        initial_candidates = []
        if not config.executor_command:
            initial_candidates = resolve_agents_for_need(
                track.value, brief_path, settings=agent_settings, attempt=start_attempt, previous_error=initial_error
            )
        resolved_first = next((c for c in initial_candidates if c.installed), initial_candidates[0] if initial_candidates else None)

        will_run_executor = bool(
            not config.dry_run
            and (
                config.executor_command
                or (agent_settings.enable_agent_execution and resolved_first and resolved_first.installed)
            )
        )

        attempts_log: list[dict] = []
        max_attempts = (config.max_retries + 1) if will_run_executor else 1
        previous_error = initial_error

        for offset in range(max_attempts):
            attempt = start_attempt + offset
            attempt_entry: dict = {"attempt": attempt, "executor": None, "checks": []}

            exec_candidates: list[tuple[Optional[str], list[str], str, int]] = []
            if config.executor_command and not config.dry_run:
                exec_candidates = [(None, config.executor_command, "custom_executor", config.check_timeout_sec)]
                agent_label = "custom_executor"
            elif resolved_first and not config.dry_run and agent_settings.enable_agent_execution:
                resolved_list = resolve_agents_for_need(
                    track.value,
                    brief_path,
                    settings=agent_settings,
                    attempt=attempt,
                    previous_error=previous_error,
                )
                installed_list = [r for r in resolved_list if r.installed]
                exec_candidates = [
                    (r.agent_id, r.command, f"{r.agent_id} ({r.label})", r.timeout_sec) for r in installed_list
                ]
                agent_label = (
                    exec_candidates[0][2]
                    if exec_candidates
                    else (resolved_list[0].agent_id if resolved_list else "none")
                )
            else:
                agent_label = f"{resolved_first.agent_id} ({resolved_first.label})" if resolved_first else "none"

            if exec_candidates:
                env = {
                    "CRASHPILOT_BRIEF_PATH": str(brief_path.resolve()),
                    "CRASHPILOT_REPO_PATH": str(repo_root.resolve()),
                    "CRASHPILOT_CLUSTER_ID": cluster.cluster_id,
                    "CRASHPILOT_ATTEMPT": str(attempt),
                    "CRASHPILOT_PREVIOUS_ERROR": previous_error[:2000],
                }
                for cand_id, exec_cmd, cand_label, exec_timeout in exec_candidates:
                    agent_label = cand_label
                    try:
                        profile = agent_settings.profiles.get(cand_id) if cand_id else None
                        exec_res = run_agent(
                            exec_cmd,
                            repo_root,
                            writable=True,
                            inputs=[brief_path],
                            variables=env,
                            timeout=exec_timeout,
                            credential_names=profile.credential_env if profile else (),
                            proxy_port=profile.loopback_proxy_port if profile else None,
                        )
                        attempt_entry["executor"] = {
                            "agent": agent_label,
                            "exit_code": exec_res.returncode,
                            "stdout_tail": exec_res.stdout[-500:],
                            "stderr_tail": exec_res.stderr[-500:],
                        }
                        if exec_res.returncode == 0:
                            break
                    except Exception as exc:
                        attempt_entry["executor"] = {
                            "agent": agent_label,
                            "exit_code": -1,
                            "stdout_tail": "",
                            "stderr_tail": str(exc),
                        }
            else:
                reason = (
                    "dry_run=True"
                    if config.dry_run
                    else ("enable_agent_execution=False" if not agent_settings.enable_agent_execution else "agent chưa cài hoặc tắt")
                )
                attempt_entry["executor"] = {"agent": agent_label, "skipped": True, "reason": reason}

            if not config.dry_run and not exec_candidates:
                return StepResult(step_number=3, name="Isolated Execution & Mechanical Checks",
                                  status=StepStatus.BLOCKED, summary="Live cần executor đã bật và sẵn sàng.",
                                  details={"attempts": [*attempts_log, attempt_entry]})

            # Chạy toàn bộ kiểm tra cơ học thật
            executor = attempt_entry["executor"]
            all_passed = executor.get("exit_code", 0) == 0
            failed_outputs: list[str] = []
            if not all_passed:
                failed_outputs.append(f"Executor exit {executor['exit_code']}: {executor['stderr_tail']}")
            for cmd in checks:
                try:
                    if config.dry_run:
                        res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=config.check_timeout_sec)
                    else:
                        res = run_agent(cmd, repo_root, writable=True, inputs=[], variables={}, timeout=config.check_timeout_sec)
                    passed = res.returncode == 0
                    tail = (res.stdout + "\n" + res.stderr).strip()[-600:]
                    if not passed:
                        all_passed = False
                        failed_outputs.append(f"$ {' '.join(cmd)}\n{tail}")
                    attempt_entry["checks"].append(
                        {
                            "cmd": " ".join(cmd),
                            "exit_code": res.returncode,
                            "passed": passed,
                            "output_tail": tail,
                        }
                    )
                except Exception as exc:
                    all_passed = False
                    failed_outputs.append(f"$ {' '.join(cmd)}\n{exc}")
                    attempt_entry["checks"].append(
                        {"cmd": " ".join(cmd), "exit_code": -1, "passed": False, "output_tail": str(exc)}
                    )

                if cmd[-3:] == ["pub", "get", "--offline"] and not attempt_entry["checks"][-1]["passed"]:
                    break

            previous_error = "\n\n".join(failed_outputs)
            attempts_log.append(attempt_entry)
            if all_passed:
                return StepResult(
                    step_number=3,
                    name="Isolated Execution & Mechanical Checks",
                    status=StepStatus.PASSED,
                    summary=f"Đạt {len(checks)}/{len(checks)} kiểm tra cơ học sau {attempt} lượt (agent={agent_label}, dry_run={config.dry_run}).",
                    details={"selected_agent": agent_label, "attempts": attempts_log},
                )

        return StepResult(
            step_number=3,
            name="Isolated Execution & Mechanical Checks",
            status=StepStatus.FAILED,
            summary=f"Kiểm tra cơ học thất bại sau {max_attempts} lượt chạy (agent={agent_label}).",
            details={"selected_agent": agent_label, "attempts": attempts_log},
        )

    # ------------------------------------------------------------------
    # BƯỚC 4: Independent Verification & PR Gate
    # ------------------------------------------------------------------
    def step_4_verify_and_pr_gate(
        self,
        cluster: EventCluster,
        track: AgentTrack,
        config: ConstraintConfig,
        branch_name: Optional[str],
        brief_path: Path,
        step3: StepResult,
        artifacts_dir: Path,
        base_commit: Optional[str] = None,
        attempt_no: int = 1,
    ) -> tuple[StepResult, str, Optional[str], str]:
        repo_root = Path(config.repository_path).expanduser()
        env_rules = load_environment_rules()
        changed_files: list[str] = []
        diff_stat = ""
        diff_text = ""

        deleted_files: set[str] = set()
        tree = None
        if not config.dry_run or (repo_root / ".git").exists():
            try:
                def git(*args: str, env=None) -> str:
                    return subprocess.check_output(
                        ["git", *args], cwd=repo_root, env=env, stderr=subprocess.PIPE,
                        text=True, errors="replace", timeout=config.check_timeout_sec,
                    )

                head = git("rev-parse", "HEAD").strip()
                if base_commit and (head != base_commit or git("branch", "--show-current").strip() != branch_name):
                    raise ValueError("Executor đã thay đổi HEAD hoặc nhánh của worktree.")
                with tempfile.TemporaryDirectory() as temp:
                    env = None
                    if config.dry_run:
                        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp) / "index")}
                        index_path = Path(git("rev-parse", "--path-format=absolute", "--git-path", "index").strip())
                        if index_path.exists():
                            shutil.copyfile(index_path, env["GIT_INDEX_FILE"])
                        else:
                            git("read-tree", head, env=env)
                    git("add", "-A", env=env)
                    tree = git("write-tree", env=env).strip()
                args = ("--no-ext-diff", "--no-renames", head, tree)
                changed_files = git("diff", "--name-only", "-z", *args).split("\0")[:-1]
                deleted_files = set(git("diff", "--name-only", "--diff-filter=D", "-z", *args).split("\0")[:-1])
                diff_stat = git("diff", "--stat", *args).strip()
                diff_text = subprocess.check_output(["git", "diff", "--binary", "--full-index", "--no-textconv", *args],
                                                    cwd=repo_root, timeout=config.check_timeout_sec).decode("utf-8", "surrogateescape")
            except (subprocess.SubprocessError, ValueError) as exc:
                return StepResult(
                    step_number=4, name="Independent Verification & PR Gate",
                    status=StepStatus.FAILED, summary=f"Không thể xác định bản vá để kiểm định: {exc}",
                ), "VERIFICATION_FAILED", None, ""

        # Kiểm tra vi phạm đường dẫn cấm (hợp nhất từ config + environment_rules.yaml)
        forbidden_pats = set(config.forbidden_path_patterns) | set(env_rules.forbidden_path_patterns)
        task_text = "\n".join([cluster.title or "", *(ev.body or "" for ev in cluster.events)])
        violations = [
            f for f in changed_files
            if any(forbidden_path(f, pat) for pat in forbidden_pats)
            and not (f in deleted_files and (f in task_text or Path(f).name in task_text))
        ]
        if violations:
            return (
                StepResult(
                    step_number=4,
                    name="Independent Verification & PR Gate",
                    status=StepStatus.BLOCKED,
                    summary=f"Vi phạm ràng buộc bảo mật — chạm vào file cấm: {', '.join(violations)}",
                    details={"violations": violations, "changed_files": changed_files},
                ),
                "BLOCKED",
                None,
                "",
            )

        # Lưu patch riêng của từng Attempt để phục vụ đối chiếu và liên kết PR
        diff_bytes = diff_text.encode("utf-8", errors="surrogateescape")
        patch_sha256 = hashlib.sha256(diff_bytes).hexdigest()[:16] if diff_bytes else None
        attempt_patch_name: Optional[str] = None
        if diff_bytes:
            att_patch = artifacts_dir / f"attempt_{attempt_no}.patch"
            att_patch.write_bytes(diff_bytes)
            attempt_patch_name = att_patch.name

        # Kiểm định độc lập Giai đoạn 4A (diff rules + independent verify commands)
        verification = run_independent_verification(
            repo_root=repo_root,
            verify_commands=config.verify_commands,
            diff_text=diff_text,
            isolate=not config.dry_run,
            env_rules=env_rules,
            timeout_sec=config.check_timeout_sec,
            project_key=cluster.project_key,
        )
        stage_4a = "PASS" if verification.passed else "FAIL"

        # Kiểm định độc lập Giai đoạn 4B (Runtime Verifier — tự khởi chạy App & Fuzzing / CDP)
        runtime_outcome = run_runtime_verifier(
            repo_root=repo_root,
            cfg=config.runtime_verifier,
            artifacts_dir=artifacts_dir,
        )
        stage_4b = ("PASS" if runtime_outcome.passed else "FAIL") if runtime_outcome.executed else "SKIP"
        if not runtime_outcome.passed:
            verification.passed = False

        # Kiểm định độc lập Giai đoạn 4C (Verifier AI Agent độc lập review git diff)
        verifier_outcome = run_verifier_agent(
            repo_root=repo_root,
            brief_path=brief_path,
            diff_text=diff_text,
            dry_run=config.dry_run,
            custom_verifier_command=config.verifier_command,
            agent_settings=self._agent_settings,
            timeout_sec=config.check_timeout_sec,
        )
        verification.verifier_agent = verifier_outcome
        stage_4c = ("PASS" if verifier_outcome.passed else "FAIL") if verifier_outcome.executed else "SKIP"
        if not verifier_outcome.passed:
            verification.passed = False

        evidence = bool(verification.checks or runtime_outcome.executed or verifier_outcome.executed
                        or any(a.get("checks") for a in step3.details.get("attempts", [])))
        runtime_required = bool(config.runtime_verifier and config.runtime_verifier.enabled)
        missing_runtime = runtime_required and (not runtime_outcome.executed or
            (not config.dry_run and runtime_outcome.mode == "web_cdp" and not config.runtime_verifier.start_command))
        missing_merge_check = (not config.dry_run and config.auto_merge and
                               not (verification.checks or runtime_outcome.executed))
        if verification.passed and (missing_runtime or missing_merge_check or not evidence):
            return StepResult(
                step_number=4, name="Independent Verification & PR Gate", status=StepStatus.BLOCKED,
                summary="Chưa đủ bằng chứng kiểm định: chưa chạy được kiểm tra bắt buộc.",
                details={"verify_checks": verification.checks, "runtime_verifier": runtime_outcome.model_dump(),
                         "verifier_agent": verifier_outcome.model_dump()},
            ), "VERIFICATION_INSUFFICIENT", None, ""

        if not verification.passed:
            feedback_parts = list(verification.diff_violations)
            for chk in verification.checks:
                if not chk.get("passed"):
                    feedback_parts.append(f"Check failed ({chk.get('cmd')}): {chk.get('output_tail')}")
            if runtime_outcome.executed and not runtime_outcome.passed:
                feedback_parts.append(
                    f"Runtime Verifier ({runtime_outcome.mode}) FAILED: "
                    f"console_errors={runtime_outcome.console_errors}, "
                    f"page_errors={runtime_outcome.page_errors}, "
                    f"server_errors={runtime_outcome.server_errors} {runtime_outcome.mobile_output[:500]}"
                )
            if verifier_outcome.executed and not verifier_outcome.passed:
                feedback_parts.append(f"Verifier Agent ({verifier_outcome.agent}): {verifier_outcome.verdict_text}")
            return (
                StepResult(
                    step_number=4,
                    name="Independent Verification & PR Gate",
                    status=StepStatus.FAILED,
                    summary=f"Kiểm định độc lập thất bại (runtime={runtime_outcome.mode}, verifier={verifier_outcome.agent}).",
                    details={
                        "changed_files": changed_files,
                        "diff_violations": verification.diff_violations,
                        "verify_checks": verification.checks,
                        "runtime_verifier": runtime_outcome.model_dump(),
                        "verifier_agent": verifier_outcome.model_dump(),
                        "stage_4a": stage_4a,
                        "stage_4b": stage_4b,
                        "stage_4c": stage_4c,
                        "attempt_patch_file": attempt_patch_name,
                        "patch_sha256": patch_sha256,
                    },
                ),
                "VERIFICATION_FAILED",
                None,
                "\n".join(feedback_parts),
            )

        # Sinh PR body artifact sẵn sàng tạo PR
        verifier_line = (
            f"PASSED by {verifier_outcome.agent}"
            if verifier_outcome.executed
            else f"SKIPPED ({verifier_outcome.skipped_reason or 'disabled'})"
        )
        pr_body = "\n".join(
            [
                f"## [{track.value}] {cluster.title}",
                "",
                f"- **Cluster ID**: `{cluster.cluster_id}`",
                f"- **Sources**: `{', '.join(cluster.sources)}`",
                f"- **Impact**: {cluster.total_impact} ({cluster.event_count} events)",
                f"- **Branch**: `{branch_name or 'N/A'}`",
                f"- **Mechanical Checks**: {step3.summary}",
                f"- **Independent Verification**: PASSED ({len(verification.checks)} check(s)) | Verifier Agent: {verifier_line}",
                "",
                "### Changed Files",
                *([f"- `{f}`" for f in changed_files] if changed_files else ["- _(Không có thay đổi file — dry-run / verify-only)_"]),
                "",
                "### Diff Summary",
                "```text",
                diff_stat or "(no changes)",
                "```",
            ]
        )
        pr_body_path = artifacts_dir / "pr_body.md"
        pr_body_path.write_text(pr_body, encoding="utf-8")

        pr_url: Optional[str] = None
        pr_details: dict = {}
        if changed_files and not config.dry_run and config.auto_create_pr and branch_name:
            pr_title = f"fix(crashpilot): {cluster.title[:65]} [{cluster.cluster_id}]"
            pr_out = commit_push_and_create_pr(
                repo_root=repo_root,
                branch_name=branch_name,
                base_branch=config.base_branch,
                pr_title=pr_title,
                pr_body_path=pr_body_path,
                auto_create_pr=config.auto_create_pr,
                auto_merge=config.auto_merge,
                verified_tree=tree,
                base_commit=base_commit,
            )
            pr_details = pr_out.model_dump()
            pr_url = pr_out.pr_url
            if pr_out.error:
                return (
                    StepResult(
                        step_number=4,
                        name="Independent Verification & PR Gate",
                        status=StepStatus.FAILED,
                        summary=f"Kiểm định đạt chuẩn nhưng bước tạo PR gặp lỗi: {pr_out.error}",
                        details={
                            "changed_files": changed_files,
                            "pr_body_path": str(pr_body_path),
                            "verifier_agent": verifier_outcome.model_dump(),
                            "pr": pr_details,
                            "delivery": {"base_commit": base_commit, "verified_tree": tree, "base_branch": config.base_branch,
                                         "pr_title": pr_title, "auto_merge": config.auto_merge},
                        },
                    ),
                    "PR_MERGE_FAILED" if pr_out.pr_created else "PR_CREATE_FAILED",
                    pr_url,
                    "",
                )
            if pr_out.auto_merge_requested and not pr_out.merged:
                final_status = "AUTO_MERGE_REQUESTED"
            elif pr_out.merged:
                final_status = "AUTO_MERGED"
            elif pr_out.pr_created:
                final_status = "PR_CREATED"
            elif pr_out.pushed:
                final_status = "PUSHED_BRANCH"
            else:
                final_status = "VERIFIED_READY_FOR_PR"
        elif config.dry_run:
            final_status = "DRY_RUN_VERIFIED"
        else:
            final_status = "VERIFIED_READY_FOR_PR" if changed_files else "VERIFIED_NO_CHANGES"

        if diff_bytes:
            (artifacts_dir / "verified.patch").write_bytes(diff_bytes)

        return (
            StepResult(
                step_number=4,
                name="Independent Verification & PR Gate",
                status=StepStatus.PASSED,
                summary=f"Kiểm định độc lập đạt chuẩn ({final_status}, verifier={verifier_outcome.agent}). PR body: {pr_body_path}",
                details={
                    "changed_files": changed_files,
                    "violations": [],
                    "verify_checks": verification.checks,
                    "runtime_verifier": runtime_outcome.model_dump(),
                    "verifier_agent": verifier_outcome.model_dump(),
                    "stage_4a": stage_4a,
                    "stage_4b": stage_4b,
                    "stage_4c": stage_4c,
                    "attempt_patch_file": attempt_patch_name,
                    "patch_sha256": patch_sha256,
                    "verified_patch_sha256": patch_sha256,
                    "verified_attempt": attempt_no,
                    "pr_body_path": str(pr_body_path),
                    "pr": pr_details,
                    "final_status": final_status,
                },
            ),
            final_status,
            pr_url,
            "",
        )

    def resume_delivery(self, cluster: EventCluster, report: PipelineRunReport, auto_merge: bool) -> PipelineRunReport:
        step = report.steps[-1]
        delivery = dict(step.details["delivery"])
        # Resuming must never expand the merge permission granted during verification.
        delivery["auto_merge"] = auto_merge and delivery["auto_merge"]
        checkout = Path(report.steps[0].details["repo_root"])
        outcome = commit_push_and_create_pr(checkout, report.branch_name, pr_body_path=Path(step.details["pr_body_path"]),
                                            auto_create_pr=True, previous=PrGateOutcome(**step.details["pr"]), **delivery)
        step.details["pr"] = outcome.model_dump()
        report.pr_url = outcome.pr_url
        if outcome.error:
            report.overall_status = "PR_MERGE_FAILED" if outcome.pr_created else "PR_CREATE_FAILED"
            step.summary = outcome.error
        else:
            status = ("AUTO_MERGED" if outcome.merged else "AUTO_MERGE_REQUESTED" if outcome.auto_merge_requested
                      else "PR_CREATED" if outcome.pr_created else "PUSHED_BRANCH")
            report.overall_status = status
            step.details["final_status"] = status
            step.status = StepStatus.PASSED
            step.summary = status
            subprocess.run(["git", "worktree", "remove", str(checkout)], cwd=report.repository_path, capture_output=True)
            if any(event.checklist_item for event in cluster.events):
                step.details["processing_status"] = status
                report.overall_status = "CHECKLIST_UPDATE_PENDING"
                self._save_report(report)
                return self.finish_checklist(cluster, report)
        return self._save_report(report)

    def refresh_requested_merge(self, report: PipelineRunReport) -> None:
        gh = shutil.which("gh")
        if not gh or not report.pr_url or not report.steps:
            return
        state = observed_pr_state(gh, Path(report.repository_path).expanduser(), report.pr_url)
        if state not in {"MERGED", "CLOSED"}:
            return
        merged = state == "MERGED"
        report.overall_status = "AUTO_MERGED" if merged else "PR_CLOSED"
        step = report.steps[-1]
        step.details.setdefault("pr", {})["merged"] = merged
        step.details["final_status"] = report.overall_status
        step.status = StepStatus.PASSED if merged else StepStatus.BLOCKED
        step.summary = "PR đã được merge." if merged else "PR đã đóng mà chưa merge."
        self._save_report(report)

    def finish_checklist(self, cluster: EventCluster, report: PipelineRunReport) -> PipelineRunReport:
        step = report.steps[-1]
        try:
            for event in cluster.events:
                if event.checklist_item is not None:
                    mark_checklist_done(event.checklist_item, event.body)
        except (OSError, ValueError) as exc:
            report.overall_status = "CHECKLIST_UPDATE_FAILED"
            step.status = StepStatus.FAILED
            step.summary = f"Đã xử lý task nhưng chưa cập nhật được checklist: {exc}"
        else:
            report.overall_status = step.details["processing_status"]
            step.status = StepStatus.PASSED
            step.summary = f"{report.overall_status} — đã đánh dấu hoàn thành trong checklist."
        return self._save_report(report)

    def _write_report(
        self,
        run_id: str,
        cluster: EventCluster,
        track: AgentTrack,
        config: ConstraintConfig,
        branch_name: Optional[str],
        overall_status: str,
        steps: list[StepResult],
        artifacts_dir: Path,
        current_step: Optional[int] = None,
        current_step_name: Optional[str] = None,
        pr_url: Optional[str] = None,
        task_id: Optional[str] = None,
        verification_rounds: Optional[list[dict]] = None,
    ) -> PipelineRunReport:
        report = PipelineRunReport(
            run_id=run_id,
            cluster_id=cluster.cluster_id,
            task_id=task_id,
            project_key=cluster.project_key,
            cluster_impact=cluster.total_impact,
            track=track,
            repository_path=config.repository_path,
            branch_name=branch_name,
            pr_url=pr_url,
            dry_run=config.dry_run,
            overall_status=overall_status,
            current_step=current_step,
            current_step_name=current_step_name,
            steps=steps,
            verification_rounds=list(verification_rounds or []),
            artifacts_dir=str(artifacts_dir),
        )
        return self._save_report(report)

    def _save_report(self, report: PipelineRunReport) -> PipelineRunReport:
        artifacts_dir = self.runs_dir / report.run_id
        # Ghi atomic qua file tạm để Dashboard/API không bao giờ đọc phải JSON dở dang
        tmp_path = artifacts_dir / "report.json.tmp"
        tmp_path.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(artifacts_dir / "report.json")
        return report

    def _finalize(
        self,
        run_id: str,
        cluster: EventCluster,
        track: AgentTrack,
        config: ConstraintConfig,
        branch_name: Optional[str],
        overall_status: str,
        steps: list[StepResult],
        artifacts_dir: Path,
        pr_url: Optional[str] = None,
        task_id: Optional[str] = None,
        verification_rounds: Optional[list[dict]] = None,
    ) -> PipelineRunReport:
        # Only dispose of the checkout we created. Keep deliverable work and timed-out PRs for resumption.
        keep = {"VERIFIED_READY_FOR_PR", "CHECKLIST_UPDATE_PENDING"}
        if branch_name and not config.dry_run and overall_status not in keep and steps:
            checkout = (artifacts_dir / "worktree").resolve()
            if (steps[0].details.get("repo_root") == str(checkout)
                    and not steps[-1].details.get("pr", {}).get("timed_out")):
                owner = Path(config.repository_path).expanduser()
                base = steps[0].details.get("base_commit")
                if checkout.exists():
                    subprocess.run(["git", "worktree", "remove", "--force", str(checkout)],
                                   cwd=owner, capture_output=True, timeout=30)
                if base and not checkout.exists():
                    # Compare-and-delete only an unmodified generated branch.
                    subprocess.run(["git", "update-ref", "-d", f"refs/heads/{branch_name}", base],
                                   cwd=owner, capture_output=True, timeout=15)
        return self._write_report(
            run_id=run_id,
            cluster=cluster,
            track=track,
            config=config,
            branch_name=branch_name,
            overall_status=overall_status,
            steps=steps,
            artifacts_dir=artifacts_dir,
            current_step=None,
            current_step_name=None,
            pr_url=pr_url,
            task_id=task_id,
            verification_rounds=verification_rounds,
        )

    def list_runs(self) -> list[PipelineRunReport]:
        if not self.runs_dir.exists():
            return []
        reports: list[PipelineRunReport] = []
        for report_file in sorted(self.runs_dir.glob("*/report.json"), reverse=True):
            try:
                raw = json.loads(report_file.read_text(encoding="utf-8"))
                report = PipelineRunReport.model_validate(raw)
                if report.overall_status in {"RUNNING", "IN_PROGRESS"}:
                    with (report_file.parent / "run.lock").open("a") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            report = PipelineRunReport.model_validate_json(report_file.read_bytes())
                            if report.overall_status in {"RUNNING", "IN_PROGRESS"}:
                                report.overall_status = "INTERRUPTED"
                                report.current_step = report.current_step_name = None
                                for step in report.steps:
                                    if step.status == StepStatus.RUNNING:
                                        step.status = StepStatus.FAILED
                                        step.summary = "Tiến trình đã dừng trước khi hoàn tất bước này."
                                if report.branch_name and not report.dry_run and report.steps:
                                    checkout = (report_file.parent / "worktree").resolve()
                                    owner = Path(report.repository_path).expanduser()
                                    base = report.steps[0].details.get("base_commit")
                                    if checkout.exists() and owner.is_dir():
                                        subprocess.run(
                                            ["git", "worktree", "remove", "--force", str(checkout)],
                                            cwd=owner,
                                            capture_output=True,
                                            timeout=30,
                                        )
                                    if base and not checkout.exists() and owner.is_dir():
                                        subprocess.run(
                                            ["git", "update-ref", "-d", f"refs/heads/{report.branch_name}", base],
                                            cwd=owner,
                                            capture_output=True,
                                            timeout=15,
                                        )
                                self._save_report(report)
                # Older releases counted a skipped Live executor as a completed task.
                if not report.dry_run and report.overall_status == "VERIFIED_NO_CHANGES":
                    execution = next((step for step in report.steps if step.step_number == 3), None)
                    attempts = execution.details.get("attempts") if execution else None
                    if attempts and (attempts[-1].get("executor") or {}).get("skipped"):
                        report.overall_status = "BLOCKED"
                        execution.status = StepStatus.BLOCKED
                        execution.summary = "Lượt Live trước đây đã bỏ qua executor."
                reports.append(report)
            except Exception:
                continue
        return sorted(reports, key=lambda report: report.created_at, reverse=True)
