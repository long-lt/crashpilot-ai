import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, Field

from app.constrained_runner.isolation import run_agent
from app.constrained_runner.agents import (
    AgentSettings,
    load_agent_settings,
    resolve_verifier_agents,
)
from app.hardening import EnvironmentRules, load_environment_rules


class VerifierAgentOutcome(BaseModel):
    executed: bool = False
    agent: str = "none"
    passed: bool = True
    exit_code: Optional[int] = None
    verdict_text: str = ""
    skipped_reason: Optional[str] = None


class VerificationResult(BaseModel):
    passed: bool
    diff_violations: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    verifier_agent: Optional[VerifierAgentOutcome] = None


class PrGateOutcome(BaseModel):
    committed: bool = False
    pushed: bool = False
    pr_created: bool = False
    auto_merge_requested: bool = False
    merged: bool = False
    commit_sha: Optional[str] = None
    pr_url: Optional[str] = None
    timed_out: bool = False
    error: Optional[str] = None


def detect_independent_verify_checks(repo_root: Path) -> list[list[str]]:
    """Tự nhận diện bộ kiểm định độc lập cho Giai đoạn 4 (chạy test thật offline trên repo)."""
    checks: list[list[str]] = []
    if (repo_root / "pubspec.yaml").exists() and (repo_root / "test").is_dir():
        flutter_bin = shutil.which("flutter")
        if flutter_bin:
            checks.append([flutter_bin, "test", "--no-pub"])
    return checks


def run_independent_verification(
    repo_root: Path,
    verify_commands: list[list[str]],
    diff_text: str,
    env_rules: Optional[EnvironmentRules] = None,
    timeout_sec: int = 180,
    isolate: bool = False,
    project_key: Optional[str] = None,
) -> VerificationResult:
    """Giai đoạn 4: Kiểm định độc lập diff (chống lộ secret/conflict) & chạy test hồi quy."""
    rules = env_rules or load_environment_rules()
    diff_violations: list[str] = []

    for rule in rules.forbidden_diff_patterns:
        if getattr(rule, "project_key", None) and project_key and rule.project_key != project_key:
            continue
        try:
            if re.search(rule.pattern, diff_text, flags=re.MULTILINE):
                diff_violations.append(f"[{rule.id}] {rule.message}")
        except re.error:
            continue

    cmds = verify_commands or detect_independent_verify_checks(repo_root)
    checks_log: list[dict[str, Any]] = []
    all_ok = len(diff_violations) == 0

    for cmd in cmds:
        try:
            if isolate:
                res = run_agent(cmd, repo_root, writable=True, inputs=[], variables={}, timeout=timeout_sec)
            else:
                res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=timeout_sec)
            passed = res.returncode == 0
            if not passed:
                all_ok = False
            checks_log.append(
                {
                    "cmd": " ".join(cmd),
                    "exit_code": res.returncode,
                    "passed": passed,
                    "output_tail": (res.stdout + "\n" + res.stderr).strip()[-600:],
                }
            )
        except Exception as exc:
            all_ok = False
            checks_log.append({"cmd": " ".join(cmd), "exit_code": -1, "passed": False, "output_tail": str(exc)})

    return VerificationResult(passed=all_ok, diff_violations=diff_violations, checks=checks_log)


def run_verifier_agent(
    repo_root: Path,
    brief_path: Path,
    diff_text: str,
    dry_run: bool = True,
    custom_verifier_command: Optional[list[str]] = None,
    agent_settings: Optional[AgentSettings] = None,
    timeout_sec: int = 300,
) -> VerifierAgentOutcome:
    """Chạy Verifier AI Agent độc lập ở Bước 4 để đánh giá git diff (hỗ trợ chuỗi fallback agent)."""
    cfg = agent_settings or load_agent_settings()
    candidates = [] if custom_verifier_command else resolve_verifier_agents(brief_path, diff_text, settings=cfg)
    resolved = candidates[0] if candidates else None

    if dry_run:
        label = f"{resolved.agent_id} ({resolved.label})" if resolved else ("custom_verifier" if custom_verifier_command else "none")
        return VerifierAgentOutcome(executed=False, agent=label, passed=True, skipped_reason="dry_run=True")

    installed_candidates = [c for c in candidates if c.installed]
    if custom_verifier_command:
        exec_list = [(None, custom_verifier_command, "custom_verifier", timeout_sec)]
    elif cfg.enable_agent_execution and installed_candidates:
        exec_list = [(c, c.command, f"{c.agent_id} ({c.label})", c.timeout_sec) for c in installed_candidates]
    else:
        label = f"{resolved.agent_id} ({resolved.label})" if resolved else "none"
        reason = "enable_agent_execution=False" if not cfg.enable_agent_execution else "verifier agent chưa cài hoặc tắt"
        return VerifierAgentOutcome(
            executed=False,
            agent=label,
            passed=not (cfg.enable_agent_execution and cfg.routing.get("verifier") not in {None, "none"}),
            skipped_reason=reason,
        )

    patch = brief_path.with_name("verified.patch")
    patch.write_bytes(diff_text.encode("utf-8", errors="surrogateescape"))

    last_outcome: Optional[VerifierAgentOutcome] = None
    for cand, cmd, agent_label, run_timeout in exec_list:
        profile = cfg.profiles.get(cand.agent_id) if cand else None
        try:
            res = run_agent(
                cmd,
                repo_root,
                writable=False,
                inputs=[brief_path, patch],
                variables={
                    "CRASHPILOT_BRIEF_PATH": str(brief_path.resolve()),
                    "CRASHPILOT_DIFF_PATH": str(patch.resolve()),
                    "CRASHPILOT_REPO_PATH": str(repo_root.resolve()),
                },
                timeout=run_timeout,
                credential_names=profile.credential_env if profile else (),
                proxy_port=profile.loopback_proxy_port if profile else None,
            )
            combined = (res.stdout + "\n" + res.stderr).strip()
            passed = parse_verifier_verdict(res.stdout, res.returncode)
            last_outcome = VerifierAgentOutcome(
                executed=True,
                agent=agent_label,
                passed=passed,
                exit_code=res.returncode,
                verdict_text=combined[:1200],
            )
            # Nếu Verifier chạy thành công (exit 0 và có phản hồi hợp lệ PASS hoặc FAIL rõ ràng), không cần fallback tiếp
            if res.returncode == 0 and (passed or "VERDICT:" in combined.upper()):
                return last_outcome
        except Exception as exc:
            last_outcome = VerifierAgentOutcome(
                executed=True,
                agent=agent_label,
                passed=False,
                exit_code=-1,
                verdict_text=str(exc),
            )
    return last_outcome or VerifierAgentOutcome(executed=False, passed=False, skipped_reason="Không có verifier khả dụng")


PUBLISH_TIMEOUT_SEC = 120


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_VERDICT_LINE_RE = re.compile(r"^(?:\*\*|`)*VERDICT:\s*(PASS|FAIL)(?:\*\*|`)*\s*$", re.IGNORECASE)


def parse_verifier_verdict(stdout: str, returncode: int) -> bool:
    """Trích xuất kết quả VERDICT: PASS/FAIL an toàn trước mã ANSI hoặc log khởi động của CLI Agent."""
    if returncode != 0 or not stdout:
        return False
    clean = _ANSI_ESCAPE_RE.sub("", stdout)
    verdicts: list[str] = []
    for raw_line in clean.splitlines():
        line = raw_line.split("\r")[-1].strip()
        m = _VERDICT_LINE_RE.match(line)
        if m:
            verdicts.append(m.group(1).upper())
    return bool(verdicts) and verdicts[0] == "PASS" and "FAIL" not in verdicts


def _publish_command(repo: Path, *command: str, timeout: int | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1",
           "GCM_INTERACTIVE": "Never", "SSH_ASKPASS_REQUIRE": "never"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    with subprocess.Popen(command, cwd=repo, env=env, stdin=subprocess.DEVNULL, start_new_session=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout or PUBLISH_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise
        if proc.returncode:
            raise ValueError((stderr or stdout).strip() or f"Exit {proc.returncode}")
        return stdout.strip()


def observed_pr_state(gh: str, repo: Path, pr_url: str) -> str | None:
    try:
        return _publish_command(repo, gh, "pr", "view", pr_url, "--json", "state", "--jq", ".state", timeout=15)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def commit_push_and_create_pr(
    repo_root: Path, branch_name: str, base_branch: str, pr_title: str, pr_body_path: Path,
    auto_create_pr: bool = False, auto_merge: bool = False, *, verified_tree: str,
    base_commit: str, previous: PrGateOutcome | None = None,
) -> PrGateOutcome:
    outcome = previous.model_copy() if previous else PrGateOutcome()
    outcome.error = None
    outcome.timed_out = False
    if not auto_create_pr:
        return outcome

    def run(*cmd: str) -> str:
        return _publish_command(repo_root, *cmd)

    stage = "git commit"
    try:
        head = run("git", "rev-parse", "HEAD")
        if run("git", "branch", "--show-current") != branch_name or run("git", "write-tree") != verified_tree:
            raise ValueError("Nhánh hoặc nội dung đã stage thay đổi sau kiểm định.")
        if head == base_commit and not outcome.committed:
            run("git", "commit", "-m", pr_title)
            head = run("git", "rev-parse", "HEAD")
        elif not previous or (outcome.commit_sha and head != outcome.commit_sha):
            raise ValueError("HEAD đã thay đổi sau kiểm định.")
        if run("git", "rev-parse", f"{head}^{{tree}}") != verified_tree or run("git", "rev-parse", f"{head}^") != base_commit:
            raise ValueError("Commit khác bản vá đã kiểm định.")
        outcome.committed = True
        outcome.commit_sha = head
        stage = "git push"
        remote = run("git", "remote", "get-url", "origin")
        if not outcome.pushed:
            run("git", "push", "origin", f"{head}:refs/heads/{branch_name}")
            outcome.pushed = True

        gh = shutil.which("gh")
        if previous and not gh and "github.com" in remote:
            raise ValueError("Cần GitHub CLI để tiếp tục bước tạo PR.")
        if not gh or "github.com" not in remote:
            return outcome
        stage = "gh pr create"
        if previous and not outcome.pr_url:
            # A timed-out create may already have been accepted by GitHub.
            found = run(gh, "pr", "list", "--state", "all", "--head", branch_name, "--base", base_branch,
                        "--json", "headRefOid,url", "--jq", f'.[] | select(.headRefOid == "{head}") | .url')
            if found:
                outcome.pr_url = found.splitlines()[0]
        if not outcome.pr_url:
            outcome.pr_url = run(gh, "pr", "create", "--base", base_branch, "--head", branch_name,
                                 "--title", pr_title, "--body-file", str(pr_body_path.resolve()))
        if not outcome.pr_url:
            raise ValueError("GitHub chưa trả về URL của PR.")
        outcome.pr_created = True
        if auto_merge:
            stage = "gh pr merge"
            if previous and observed_pr_state(gh, repo_root, outcome.pr_url) == "MERGED":
                outcome.merged = True
            else:
                run(gh, "pr", "merge", outcome.pr_url, "--match-head-commit", head, "--squash", "--auto")
                outcome.auto_merge_requested = True
                outcome.merged = observed_pr_state(gh, repo_root, outcome.pr_url) == "MERGED"
    except subprocess.TimeoutExpired:
        outcome.timed_out = True
        outcome.error = f"{stage}: quá thời hạn chờ."
    except (OSError, ValueError) as exc:
        outcome.error = f"{stage}: {exc}"
    return outcome
