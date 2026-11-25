"""Giai đoạn 5 — Lấy mẫu Buổi sáng & Phát hiện Antipattern để Nâng cấp Vĩnh viễn Môi trường.

Thực hiện đúng vòng phản hồi khép kín trên sơ đồ `kien truc.svg`:
  WakeUp -> Sampling (Lấy mẫu lịch sử chạy & commit thật trong data/runs/)
         -> DetectFlaw (Phát hiện lỗi lặp / lỗi bị Verifier chặn / tự sửa nhiều vòng)
         -> HardenEnv (Đề xuất Luật quét Code, Luật chặn Diff, Đường dẫn cấm, hoặc Kiểm tra cơ học mới).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from app.constrained_runner.agents import (
    AgentSettings,
    _build_resolved_command,
    load_agent_settings,
)
from app.constrained_runner.isolation import run_agent
from app.constrained_runner.models import COMPLETED_RUN_STATUSES, FAILED_RUN_STATUSES
from app.hardening.rules_manager import (
    CustomAuditRule,
    DiffRule,
    EnvironmentRules,
    add_custom_audit_rule,
    add_diff_rule,
    add_extra_mechanical_check,
    add_forbidden_path_pattern,
    canonical_rule_type,
    load_environment_rules,
    target_stage_for_kind,
)


def _run_sort_key_from_path(report_file: Path) -> str:
    # Tên thư mục: run_<cluster_id>_<YYYYMMDDTHHMMSSZ>_<hex>
    parts = report_file.parent.name.split("_")
    if len(parts) >= 2 and re.match(r"^\d{8}T\d{6}Z$", parts[-2]):
        return parts[-2]
    try:
        return f"{report_file.stat().st_mtime:020.6f}"
    except OSError:
        return report_file.parent.name


def _load_raw_run_reports(runs_dir: Path, limit: int = 30) -> list[dict[str, Any]]:
    if not runs_dir.is_dir():
        return []
    reports: list[dict[str, Any]] = []
    candidate_files = sorted(runs_dir.glob("*/report.json"), key=_run_sort_key_from_path, reverse=True)
    for report_file in candidate_files:
        try:
            data = json.loads(report_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                reports.append(data)
        except Exception:
            continue
        if len(reports) >= limit:
            break
    reports.sort(key=lambda r: str(r.get("created_at") or r.get("run_id") or ""), reverse=True)
    return reports[:limit]


def _extract_run_digest(raw: dict[str, Any]) -> dict[str, Any]:
    steps = raw.get("steps") or []
    step3 = next((s for s in steps if (s.get("step_number") or s.get("step")) == 3), {}) or {}
    step4 = next((s for s in steps if (s.get("step_number") or s.get("step")) == 4), {}) or {}
    s3_details = step3.get("details") or {}
    s4_details = step4.get("details") or {}
    attempts = s3_details.get("attempts") or []
    ver_agent = s4_details.get("verifier_agent") or {}
    pr_info = s4_details.get("pr") or {}

    failed_checks: list[str] = []
    for att in attempts:
        for chk in att.get("checks") or []:
            if not chk.get("passed", True):
                failed_checks.append(f"{chk.get('cmd')}: {(chk.get('output_tail') or '')[-240:]}")
    for chk in s4_details.get("verify_checks") or []:
        if not chk.get("passed", True):
            failed_checks.append(f"{chk.get('cmd')}: {(chk.get('output_tail') or '')[-240:]}")

    verdict_text = str(ver_agent.get("verdict_text") or "").strip()
    return {
        "run_id": str(raw.get("run_id") or ""),
        "project_key": str(raw.get("project_key") or ""),
        "cluster_id": str(raw.get("cluster_id") or ""),
        "task_id": raw.get("task_id"),
        "track": str(raw.get("track") or "experimental"),
        "dry_run": bool(raw.get("dry_run", False)),
        "overall_status": str(raw.get("overall_status") or ""),
        "created_at": raw.get("created_at"),
        "pr_url": raw.get("pr_url") or pr_info.get("pr_url"),
        "commit_sha": pr_info.get("commit_sha"),
        "attempts_count": len(attempts),
        "self_healed": len(attempts) > 1,
        "changed_files": list(s4_details.get("changed_files") or []),
        "diff_violations": list(s4_details.get("diff_violations") or s4_details.get("violations") or []),
        "verifier_passed": ver_agent.get("passed"),
        "verifier_agent": ver_agent.get("agent"),
        "verdict_excerpt": verdict_text[:500],
        "failed_checks": failed_checks[:4],
    }


def _heuristic_proposals_from_digests(
    digests: list[dict[str, Any]],
    rules: EnvironmentRules,
) -> list[dict[str, Any]]:
    existing_diff_ids = {r.id for r in rules.forbidden_diff_patterns}
    existing_audit_ids = {r.id for r in rules.custom_audit_rules}
    existing_paths = set(rules.forbidden_path_patterns)

    proposals_by_id: dict[str, dict[str, Any]] = {}

    def _add_proposal(prop_id: str, item: dict[str, Any], d_item: dict[str, Any]) -> None:
        if prop_id in existing_diff_ids or prop_id in existing_audit_ids or prop_id in existing_paths:
            return
        run_id = d_item["run_id"]
        kind = item.get("kind", "diff_rule")
        if prop_id not in proposals_by_id:
            proposals_by_id[prop_id] = {
                **item,
                "id": prop_id,
                "rule_type": canonical_rule_type(kind),
                "target_stage": target_stage_for_kind(kind),
                "project_key": d_item.get("project_key"),
                "source_task_id": d_item.get("task_id"),
                "source_run_id": run_id,
                "source_cluster_id": d_item.get("cluster_id"),
                "source_runs": [run_id],
            }
        elif run_id not in proposals_by_id[prop_id]["source_runs"]:
            proposals_by_id[prop_id]["source_runs"].append(run_id)

    for d in digests:
        verdict_low = (d.get("verdict_excerpt") or "").lower()
        checks_low = "\n".join(d.get("failed_checks") or []).lower()
        changed = d.get("changed_files") or []

        # 1. Phát hiện tự ý sửa cấu hình native hooks / user_defines trong pubspec.yaml
        if "user_defines" in verdict_low or ("pubspec.yaml" in changed and "sqlite3" in verdict_low and "source:" in verdict_low):
            _add_proposal(
                "no_pubspec_user_defines_override",
                {
                    "kind": "diff_rule",
                    "pattern": r"^\+\s*user_defines\s*:",
                    "message": "Chặn tự ý thêm hooks.user_defines trong pubspec.yaml làm thay đổi liên kết native toàn cục",
                    "rationale": "Phát hiện từ lịch sử Verifier chặn bản vá tự ý ghi đè cấu hình native hook sqlite3 trong pubspec.yaml.",
                },
                d,
            )

        # 2. Phát hiện lỗi BlendMode.srcIn đè mất chi tiết SVG nhiều lớp / có nền đặc
        if "blendmode.srcin" in verdict_low and ".svg" in verdict_low:
            _add_proposal(
                "review_svg_srcin_tint",
                {
                    "kind": "audit_rule",
                    "severity": "medium",
                    "pattern": r"ColorFilter\.mode\([^)]*BlendMode\.srcIn\)",
                    "extensions": [".dart"],
                    "description": "Cảnh báo dùng BlendMode.srcIn trực tiếp trên tài nguyên SVG đa lớp có nền đặc",
                    "rationale": "Phát hiện từ lịch sử Verifier chặn lỗi áp ColorFilter BlendMode.srcIn làm mất lớp đồ họa bên trong icon SVG.",
                },
                d,
            )

        # 3. Phát hiện print() debug lọt vào mã nguồn production
        if "avoid_print" in checks_low or "print(" in verdict_low:
            _add_proposal(
                "no_debug_print_in_diff",
                {
                    "kind": "diff_rule",
                    "pattern": r"^\+\s*print\s*\(",
                    "message": "Không đưa lệnh print() debug trực tiếp vào bản vá production",
                    "rationale": "Phát hiện cảnh báo avoid_print / lệnh print() trong quá trình kiểm tra bản vá.",
                },
                d,
            )

        # 4. Phát hiện chạm vào file lock / cấu hình ký ứng dụng nhạy cảm
        for ch_file in changed:
            if ch_file.endswith(("key.properties", "ExportOptions.plist")) and ch_file not in existing_paths:
                _add_proposal(
                    ch_file,
                    {
                        "kind": "forbidden_path",
                        "pattern": ch_file,
                        "rationale": f"Phát hiện bản vá chạm vào tệp cấu hình ký/phát hành nhạy cảm '{ch_file}'.",
                    },
                    d,
                )

    return list(proposals_by_id.values())


def sample_recent_runs(
    runs_dir: Path | str = "data/runs",
    env_rules_path: Path | str | None = None,
    limit: int = 25,
    project_key: str | None = None,
    preloaded_reports: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Lấy mẫu kiểm tra các lượt chạy gần nhất và đề xuất nâng cấp luật môi trường (Giai đoạn 5)."""
    rdir = Path(runs_dir)
    rules = load_environment_rules(env_rules_path)
    if preloaded_reports is not None:
        raw_reports = list(preloaded_reports)
    else:
        raw_reports = _load_raw_run_reports(rdir, limit=max(limit, 50) if project_key else limit)
    digests = [_extract_run_digest(r) for r in raw_reports]
    if project_key:
        digests = [d for d in digests if d.get("project_key") == project_key][:limit]

    completed_count = sum(1 for d in digests if d["overall_status"] in COMPLETED_RUN_STATUSES and not d["dry_run"])
    verifier_rejected_count = sum(1 for d in digests if d["overall_status"] == "VERIFICATION_FAILED")
    failed_count = sum(
        1 for d in digests
        if d["overall_status"] in FAILED_RUN_STATUSES and d["overall_status"] != "VERIFICATION_FAILED"
    )
    self_healed_count = sum(1 for d in digests if d["self_healed"])

    flaw_runs = [
        d for d in digests
        if d["overall_status"] in FAILED_RUN_STATUSES or d["self_healed"] or d["diff_violations"]
    ]
    proposals = _heuristic_proposals_from_digests(digests, rules)

    return {
        "summary": {
            "total_sampled": len(digests),
            "completed_live": completed_count,
            "verifier_rejected": verifier_rejected_count,
            "other_failed": failed_count,
            "self_healed": self_healed_count,
            "flaws_detected": len(flaw_runs),
            "proposals_count": len(proposals),
        },
        "proposals": proposals,
        "flaw_runs": flaw_runs[:12],
        "sampled_runs": digests[:15],
        "active_rules": rules.model_dump(mode="json"),
    }


def _validate_ai_proposals(raw_items: list[Any], rules: EnvironmentRules) -> list[dict[str, Any]]:
    existing_ids = {r.id for r in rules.forbidden_diff_patterns} | {r.id for r in rules.custom_audit_rules}
    existing_paths = set(rules.forbidden_path_patterns)
    valid: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        rid = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(item.get("id") or "").strip())
        pattern = str(item.get("pattern") or "").strip()
        if not kind or not pattern:
            continue
        if kind in ("diff_rule", "audit_rule"):
            if not rid or rid in existing_ids:
                continue
            try:
                re.compile(pattern)
            except re.error:
                continue
            valid.append(
                {
                    "id": rid,
                    "kind": kind,
                    "pattern": pattern,
                    "severity": str(item.get("severity") or "medium"),
                    "extensions": item.get("extensions") if isinstance(item.get("extensions"), list) else [".dart", ".py", ".ts", ".js"],
                    "message": str(item.get("message") or item.get("description") or rid),
                    "description": str(item.get("description") or item.get("message") or ""),
                    "rationale": str(item.get("rationale") or "Đề xuất từ AI Agent phân tích mẫu lỗi Giai đoạn 5."),
                    "source_runs": item.get("source_runs") if isinstance(item.get("source_runs"), list) else [],
                }
            )
        elif kind == "forbidden_path" and pattern not in existing_paths:
            valid.append(
                {
                    "id": rid or pattern,
                    "kind": "forbidden_path",
                    "pattern": pattern,
                    "rationale": str(item.get("rationale") or "Đường dẫn nhạy cảm đề xuất khóa bởi AI Agent."),
                    "source_runs": item.get("source_runs") if isinstance(item.get("source_runs"), list) else [],
                }
            )
    return valid


def propose_rules_with_ai_agent(
    runs_dir: Path | str = "data/runs",
    env_rules_path: Path | str | None = None,
    agent_settings: Optional[AgentSettings] = None,
    limit: int = 15,
    project_key: str | None = None,
) -> dict[str, Any]:
    """Dùng CLI Agent (read-only sandbox) đọc các lượt chạy lỗi/bị chặn thực tế để đề xuất Rule mới cho Giai đoạn 5."""
    sampled = sample_recent_runs(runs_dir=runs_dir, env_rules_path=env_rules_path, limit=limit, project_key=project_key)
    rules = load_environment_rules(env_rules_path)
    flaw_runs = sampled["flaw_runs"]
    if not flaw_runs:
        return {
            **sampled,
            "ai_executed": False,
            "ai_message": "Không có lượt chạy lỗi hoặc cảnh báo nào trong mẫu hiện tại — môi trường đang vận hành tốt.",
        }

    cfg = agent_settings or load_agent_settings()
    preferred_id = cfg.routing.get("verifier") or cfg.default_agent
    profile = cfg.profiles.get(preferred_id) if preferred_id and preferred_id != "none" else None
    if not profile:
        return {
            **sampled,
            "ai_executed": False,
            "ai_message": "Chưa cấu hình Agent cho bộ phân tích Giai đoạn 5.",
        }

    with tempfile.TemporaryDirectory(prefix="crashpilot-stage5-") as tmp:
        tmp_dir = Path(tmp)
        digest_file = tmp_dir / "morning_flaws.json"
        digest_file.write_text(
            json.dumps(
                {"flaw_runs": flaw_runs[:8], "existing_rules": rules.model_dump(mode="json")},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        prompt = (
            f"Bạn là Kiến trúc sư Hardening Môi trường (Giai đoạn 5 — Nâng cấp gian bếp). "
            f"Đọc dữ liệu mẫu lỗi thực tế tại '{digest_file.resolve()}' (chỉ đọc, không sửa file). "
            "Dựa trên các lỗi bị Verifier từ chối hoặc lỗi cơ học lặp lại mà CHƯA có trong existing_rules, "
            "hãy đề xuất tối đa 4 quy tắc phòng ngừa tự động.\n"
            "BẮT BUỘC chỉ trả về một mảng JSON hợp lệ (không thêm văn bản ngoài JSON) theo mẫu:\n"
            '[{"id":"rule_id","kind":"diff_rule|audit_rule|forbidden_path","pattern":"regex_or_glob",'
            '"severity":"medium","message":"Mô tả lỗi ngắn gọn","rationale":"Lý do từ run thực tế","source_runs":["run_id"]}]'
        )
        resolved = _build_resolved_command(preferred_id, profile, profile.model, prompt, digest_file)
        if not resolved.installed:
            return {
                **sampled,
                "ai_executed": False,
                "ai_message": f"CLI Agent '{preferred_id}' chưa được cài đặt trên máy.",
            }

        res = run_agent(
            resolved.command,
            tmp_dir,
            writable=False,
            inputs=[digest_file],
            variables={"CI": "1"},
            timeout=min(resolved.timeout_sec, 120),
            credential_names=profile.credential_env,
            proxy_port=profile.loopback_proxy_port,
        )
        ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        clean_out = ansi_re.sub("", (res.stdout or "")).strip()
        ai_props: list[dict[str, Any]] = []
        m_json = re.search(r"\[[\s\S]*\]", clean_out)
        if res.returncode == 0 and m_json:
            try:
                parsed = json.loads(m_json.group(0))
                if isinstance(parsed, list):
                    ai_props = _validate_ai_proposals(parsed, rules)
            except Exception:
                ai_props = []

        merged = list(sampled["proposals"])
        seen_ids = {p["id"] for p in merged}
        for p in ai_props:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                merged.append(p)

        sampled["proposals"] = merged
        sampled["summary"]["proposals_count"] = len(merged)
        return {
            **sampled,
            "ai_executed": True,
            "ai_agent": f"{resolved.agent_id} ({resolved.label})",
            "ai_exit_code": res.returncode,
            "ai_added_count": len(ai_props),
            "ai_message": (
                f"AI Agent ({resolved.label}) đã phân tích {len(flaw_runs[:8])} lượt lỗi và đề xuất thêm {len(ai_props)} quy tắc."
                if res.returncode == 0
                else f"AI Agent trả về mã lỗi {res.returncode}."
            ),
        }


def _next_proposal_number(db) -> int:
    from app.db.models import RuleProposalModel

    max_n = 0
    for (pid,) in db.query(RuleProposalModel.proposal_id).all():
        m = re.match(r"^PROP-(\d+)$", str(pid or ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def sync_rule_proposals_to_db(
    db,
    proposals: list[dict[str, Any]],
    *,
    project_key: Optional[str] = None,
    task_by_cluster: Optional[dict[str, str]] = None,
    task_by_run: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Lưu bền vững các Rule Proposals vào SQLite, gắn Source Task & Source Run, giữ nguyên trạng thái đã duyệt/từ chối."""
    from app.db.models import RuleProposalModel

    existing_rows = db.query(RuleProposalModel).all()
    by_rule_key: dict[tuple[str, str], RuleProposalModel] = {
        (r.project_key or "", r.rule_key): r for r in existing_rows
    }
    next_n = _next_proposal_number(db)
    now = datetime.now(timezone.utc)
    t_by_cls = task_by_cluster or {}
    t_by_run = task_by_run or {}
    dirty = False

    for p in proposals:
        rkey = str(p.get("id") or p.get("pattern") or "").strip()
        if not rkey:
            continue
        pk = str(p.get("project_key") or project_key or "")
        s_runs = list(p.get("source_runs") or [])
        s_run_id = str(p.get("source_run_id") or (s_runs[0] if s_runs else "") or "") or None
        s_cls = str(p.get("source_cluster_id") or "")
        s_task_id = (
            p.get("source_task_id")
            or (t_by_run.get(s_run_id) if s_run_id else None)
            or (t_by_cls.get(s_cls) if s_cls else None)
        )
        kind = str(p.get("kind") or "diff_rule")
        rtype = canonical_rule_type(kind)
        tstage = target_stage_for_kind(kind)

        row = by_rule_key.get((pk, rkey)) or by_rule_key.get(("", rkey))
        if row:
            if s_task_id and not row.source_task_id:
                row.source_task_id = s_task_id
                dirty = True
            if s_run_id and not row.source_run_id:
                row.source_run_id = s_run_id
                dirty = True
            if s_runs:
                new_runs_json = json.dumps(s_runs, ensure_ascii=False)
                if row.source_runs_json != new_runs_json:
                    row.source_runs_json = new_runs_json
                    dirty = True
            if row.status == "pending_review":
                new_payload = json.dumps(p, ensure_ascii=False)
                new_rat = str(p.get("rationale") or row.rationale or "")
                if row.payload_json != new_payload or row.rationale != new_rat:
                    row.payload_json = new_payload
                    row.rationale = new_rat
                    row.updated_at = now
                    dirty = True
        else:
            pid = f"PROP-{next_n}"
            next_n += 1
            row = RuleProposalModel(
                proposal_id=pid,
                rule_key=rkey,
                project_key=pk or None,
                source_task_id=s_task_id,
                source_run_id=s_run_id,
                source_runs_json=json.dumps(s_runs, ensure_ascii=False),
                rule_type=rtype,
                target_stage=tstage,
                scope="global",
                payload_json=json.dumps(p, ensure_ascii=False),
                rationale=str(p.get("rationale") or ""),
                status="pending_review",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            by_rule_key[(pk, rkey)] = row
            dirty = True

    if dirty:
        db.commit()
    q = db.query(RuleProposalModel)
    if project_key:
        q = q.filter((RuleProposalModel.project_key == project_key) | (RuleProposalModel.project_key.is_(None)))
    rows = q.order_by(RuleProposalModel.created_at.desc()).all()
    return [serialize_rule_proposal(r) for r in rows]


def serialize_rule_proposal(r) -> dict[str, Any]:
    try:
        payload = json.loads(r.payload_json or "{}")
    except Exception:
        payload = {}
    try:
        s_runs = json.loads(r.source_runs_json or "[]")
    except Exception:
        s_runs = []
    return {
        "proposal_id": r.proposal_id,
        "id": r.rule_key,
        "rule_key": r.rule_key,
        "project_key": r.project_key,
        "source_task_id": r.source_task_id,
        "source_run_id": r.source_run_id,
        "source_runs": s_runs,
        "rule_type": r.rule_type,
        "target_stage": r.target_stage,
        "scope": r.scope,
        "kind": payload.get("kind") or r.rule_type,
        "pattern": payload.get("pattern") or "",
        "severity": payload.get("severity") or "medium",
        "extensions": payload.get("extensions") or [".dart", ".py", ".ts", ".js"],
        "message": payload.get("message") or payload.get("description") or r.rule_key,
        "description": payload.get("description") or payload.get("message") or "",
        "rationale": r.rationale or payload.get("rationale") or "",
        "status": r.status,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def decide_rule_proposal(
    db,
    proposal_id: str,
    decision: str,
    *,
    scope: str = "global",
    env_rules_path: Path | str | None = None,
) -> dict[str, Any]:
    """Phê duyệt (approve) hoặc Từ chối (reject) một Rule Proposal, tự động ghi YAML + Change History."""
    from app.db.models import RuleProposalModel

    row = db.query(RuleProposalModel).filter(RuleProposalModel.proposal_id == proposal_id).first()
    if not row:
        row = db.query(RuleProposalModel).filter(RuleProposalModel.rule_key == proposal_id).first()
    if not row:
        raise ValueError(f"Không tìm thấy đề xuất '{proposal_id}'.")

    act = (decision or "").strip().lower()
    now = datetime.now(timezone.utc)
    if act == "reject":
        row.status = "rejected"
        row.decided_at = now
        row.updated_at = now
        db.commit()
        return {"decided": True, "status": "rejected", "proposal": serialize_rule_proposal(row)}

    if act != "approve":
        raise ValueError("Hành động không hợp lệ (chỉ chấp nhận 'approve' hoặc 'reject').")

    payload = json.loads(row.payload_json or "{}")
    eff_proj = row.project_key if scope == "project" else None
    rt = canonical_rule_type(row.rule_type)
    apply_res: dict[str, Any]
    if rt == "forbidden_diff_patterns":
        apply_res = add_diff_rule(
            DiffRule(
                id=row.rule_key,
                pattern=str(payload.get("pattern") or ""),
                message=str(payload.get("message") or payload.get("description") or row.rule_key),
                project_key=eff_proj,
            ),
            path=env_rules_path,
            db=db,
            action="approve_proposal",
            source_proposal_id=row.proposal_id,
            source_task_id=row.source_task_id,
            source_run_id=row.source_run_id,
        )
    elif rt == "custom_audit_rules":
        apply_res = add_custom_audit_rule(
            CustomAuditRule(
                id=row.rule_key,
                severity=str(payload.get("severity") or "medium"),
                pattern=str(payload.get("pattern") or ""),
                extensions=payload.get("extensions") or [".dart", ".py", ".ts", ".js"],
                description=str(payload.get("description") or payload.get("message") or ""),
                project_key=eff_proj,
            ),
            path=env_rules_path,
            db=db,
            action="approve_proposal",
            source_proposal_id=row.proposal_id,
            source_task_id=row.source_task_id,
            source_run_id=row.source_run_id,
        )
    elif rt == "forbidden_path_patterns":
        apply_res = add_forbidden_path_pattern(
            str(payload.get("pattern") or row.rule_key),
            path=env_rules_path,
            db=db,
            project_key=eff_proj,
            action="approve_proposal",
            source_proposal_id=row.proposal_id,
            source_task_id=row.source_task_id,
            source_run_id=row.source_run_id,
        )
    elif rt == "extra_mechanical_checks":
        apply_res = add_extra_mechanical_check(
            payload.get("command") or payload.get("pattern") or "",
            path=env_rules_path,
            db=db,
            project_key=eff_proj,
            action="approve_proposal",
            source_proposal_id=row.proposal_id,
            source_task_id=row.source_task_id,
            source_run_id=row.source_run_id,
        )
    else:
        raise ValueError(f"Loại luật không hỗ trợ phê duyệt: {row.rule_type}")

    row.status = "approved"
    row.scope = "project" if eff_proj else "global"
    row.decided_at = now
    row.updated_at = now
    db.commit()
    return {
        "decided": True,
        "status": "approved",
        "change_id": apply_res.get("change_id"),
        "proposal": serialize_rule_proposal(row),
        "rules": apply_res.get("rules"),
    }
