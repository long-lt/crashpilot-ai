from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import re
import shlex
import yaml
from app.config_files import editing_config, write_yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_ENV_RULES_PATH = Path("config/environment_rules.yaml")


def _validate_regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"Biểu thức chính quy không hợp lệ: {exc}") from exc
    return value


def canonical_rule_type(kind: str) -> str:
    k = (kind or "").strip().lower()
    mapping = {
        "diff_rule": "forbidden_diff_patterns",
        "forbidden_diff_patterns": "forbidden_diff_patterns",
        "audit_rule": "custom_audit_rules",
        "custom_audit_rules": "custom_audit_rules",
        "forbidden_path": "forbidden_path_patterns",
        "forbidden_path_patterns": "forbidden_path_patterns",
        "extra_check": "extra_mechanical_checks",
        "extra_mechanical_checks": "extra_mechanical_checks",
    }
    return mapping.get(k, k)


def target_stage_for_kind(kind: str) -> str:
    rt = canonical_rule_type(kind)
    if rt == "forbidden_diff_patterns":
        return "Giai đoạn 4A"
    if rt == "custom_audit_rules":
        return "Giai đoạn 1"
    if rt == "forbidden_path_patterns":
        return "Giai đoạn 3 & 4A"
    if rt == "extra_mechanical_checks":
        return "Giai đoạn 3"
    return "Giai đoạn 4A"


class DiffRule(BaseModel):
    id: str
    pattern: str
    message: str
    project_key: Optional[str] = None

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        return _validate_regex(value)


class CustomAuditRule(BaseModel):
    id: str
    severity: str = "medium"
    pattern: str
    extensions: list[str] = Field(default_factory=lambda: [".dart", ".py", ".ts", ".js"])
    description: str = ""
    project_key: Optional[str] = None

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        return _validate_regex(value)


class EnvironmentRules(BaseModel):
    forbidden_path_patterns: list[str] = Field(
        default_factory=lambda: [
            ".env",
            "browser_profiles/",
            ".pem",
            ".key",
            ".keystore",
            "google-services.json",
            "GoogleService-Info.plist",
            "secrets",
        ]
    )
    forbidden_diff_patterns: list[DiffRule] = Field(default_factory=list)
    custom_audit_rules: list[CustomAuditRule] = Field(default_factory=list)
    extra_mechanical_checks: list[list[str]] = Field(default_factory=list)


def load_environment_rules(path: Path | str | None = None) -> EnvironmentRules:
    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    if not cfg_path.exists():
        return EnvironmentRules()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return EnvironmentRules.model_validate(raw)


def save_environment_rules(rules: EnvironmentRules, path: Path | str | None = None) -> Path:
    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(cfg_path, rules.model_dump(mode="json", exclude_none=True))
    return cfg_path


def read_rules_yaml_text(path: Path | str | None = None) -> str:
    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    if cfg_path.exists():
        return cfg_path.read_text(encoding="utf-8")
    return yaml.safe_dump(
        EnvironmentRules().model_dump(mode="json", exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
    )


def record_rule_change(
    db,
    *,
    action: str,
    summary: str,
    before_yaml: str,
    after_yaml: str,
    project_key: Optional[str] = None,
    rule_type: Optional[str] = None,
    rule_id: Optional[str] = None,
    source_proposal_id: Optional[str] = None,
    source_task_id: Optional[str] = None,
    source_run_id: Optional[str] = None,
) -> Optional[int]:
    if db is None or before_yaml == after_yaml:
        return None
    from app.db.models import RuleChangeHistoryModel

    entry = RuleChangeHistoryModel(
        project_key=project_key,
        action=action,
        rule_type=rule_type,
        rule_id=rule_id,
        source_proposal_id=source_proposal_id,
        source_task_id=source_task_id,
        source_run_id=source_run_id,
        summary=summary[:500],
        before_yaml=before_yaml,
        after_yaml=after_yaml,
        rolled_back=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry.id


def add_custom_audit_rule(
    rule: CustomAuditRule,
    path: Path | str | None = None,
    *,
    db=None,
    action: str = "manual_add",
    source_proposal_id: Optional[str] = None,
    source_task_id: Optional[str] = None,
    source_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Bổ sung vĩnh viễn 1 luật quét/lint mới vào môi trường (Giai đoạn 5 -> nạp về Giai đoạn 1)."""
    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    with editing_config(cfg_path):
        before_yaml = read_rules_yaml_text(cfg_path)
        rules = load_environment_rules(cfg_path)
        rules.custom_audit_rules = [
            r for r in rules.custom_audit_rules
            if not (r.id == rule.id and r.project_key == rule.project_key)
        ]
        rules.custom_audit_rules.append(rule)
        saved = save_environment_rules(rules, cfg_path)
        after_yaml = read_rules_yaml_text(cfg_path)
    change_id = record_rule_change(
        db,
        action=action,
        summary=f"Thêm luật quét code '{rule.id}'",
        before_yaml=before_yaml,
        after_yaml=after_yaml,
        project_key=rule.project_key,
        rule_type="custom_audit_rules",
        rule_id=rule.id,
        source_proposal_id=source_proposal_id,
        source_task_id=source_task_id,
        source_run_id=source_run_id,
    )
    return {
        "saved": True,
        "kind": "audit_rule",
        "change_id": change_id,
        "path": str(saved),
        "total_custom_rules": len(rules.custom_audit_rules),
        "rules": rules.model_dump(mode="json", exclude_none=True),
    }


def add_diff_rule(
    rule: DiffRule,
    path: Path | str | None = None,
    *,
    db=None,
    action: str = "manual_add",
    source_proposal_id: Optional[str] = None,
    source_task_id: Optional[str] = None,
    source_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Bổ sung vĩnh viễn 1 luật chặn git diff mới (Giai đoạn 5 -> nạp về Giai đoạn 4A Verifier)."""
    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    with editing_config(cfg_path):
        before_yaml = read_rules_yaml_text(cfg_path)
        rules = load_environment_rules(cfg_path)
        rules.forbidden_diff_patterns = [
            r for r in rules.forbidden_diff_patterns
            if not (r.id == rule.id and r.project_key == rule.project_key)
        ]
        rules.forbidden_diff_patterns.append(rule)
        saved = save_environment_rules(rules, cfg_path)
        after_yaml = read_rules_yaml_text(cfg_path)
    change_id = record_rule_change(
        db,
        action=action,
        summary=f"Thêm luật chặn diff '{rule.id}'",
        before_yaml=before_yaml,
        after_yaml=after_yaml,
        project_key=rule.project_key,
        rule_type="forbidden_diff_patterns",
        rule_id=rule.id,
        source_proposal_id=source_proposal_id,
        source_task_id=source_task_id,
        source_run_id=source_run_id,
    )
    return {
        "saved": True,
        "kind": "diff_rule",
        "change_id": change_id,
        "path": str(saved),
        "total_diff_rules": len(rules.forbidden_diff_patterns),
        "rules": rules.model_dump(mode="json", exclude_none=True),
    }


def add_forbidden_path_pattern(
    pattern: str,
    path: Path | str | None = None,
    *,
    db=None,
    project_key: Optional[str] = None,
    action: str = "manual_add",
    source_proposal_id: Optional[str] = None,
    source_task_id: Optional[str] = None,
    source_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Bổ sung vĩnh viễn 1 mẫu đường dẫn cấm chỉnh sửa (Giai đoạn 5 -> nạp về Giai đoạn 3 & 4)."""
    clean = (pattern or "").strip()
    if not clean:
        raise ValueError("Mẫu đường dẫn cấm không được để trống.")
    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    with editing_config(cfg_path):
        before_yaml = read_rules_yaml_text(cfg_path)
        rules = load_environment_rules(cfg_path)
        if clean not in rules.forbidden_path_patterns:
            rules.forbidden_path_patterns.append(clean)
        saved = save_environment_rules(rules, cfg_path)
        after_yaml = read_rules_yaml_text(cfg_path)
    change_id = record_rule_change(
        db,
        action=action,
        summary=f"Khóa đường dẫn nhạy cảm '{clean}'",
        before_yaml=before_yaml,
        after_yaml=after_yaml,
        project_key=project_key,
        rule_type="forbidden_path_patterns",
        rule_id=clean,
        source_proposal_id=source_proposal_id,
        source_task_id=source_task_id,
        source_run_id=source_run_id,
    )
    return {
        "saved": True,
        "kind": "forbidden_path",
        "change_id": change_id,
        "path": str(saved),
        "rules": rules.model_dump(mode="json", exclude_none=True),
    }


def add_extra_mechanical_check(
    command: list[str] | str,
    path: Path | str | None = None,
    *,
    db=None,
    project_key: Optional[str] = None,
    action: str = "manual_add",
    source_proposal_id: Optional[str] = None,
    source_task_id: Optional[str] = None,
    source_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Bổ sung vĩnh viễn 1 lệnh kiểm tra cơ học bắt buộc (Giai đoạn 5 -> nạp về Giai đoạn 3)."""
    tokens = shlex.split(command) if isinstance(command, str) else [str(t).strip() for t in command if str(t).strip()]
    if not tokens:
        raise ValueError("Lệnh kiểm tra cơ học không được để trống.")
    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    with editing_config(cfg_path):
        before_yaml = read_rules_yaml_text(cfg_path)
        rules = load_environment_rules(cfg_path)
        if tokens not in rules.extra_mechanical_checks:
            rules.extra_mechanical_checks.append(tokens)
        saved = save_environment_rules(rules, cfg_path)
        after_yaml = read_rules_yaml_text(cfg_path)
    cmd_str = " ".join(tokens)
    change_id = record_rule_change(
        db,
        action=action,
        summary=f"Thêm lệnh kiểm tra cơ học '{cmd_str}'",
        before_yaml=before_yaml,
        after_yaml=after_yaml,
        project_key=project_key,
        rule_type="extra_mechanical_checks",
        rule_id=cmd_str,
        source_proposal_id=source_proposal_id,
        source_task_id=source_task_id,
        source_run_id=source_run_id,
    )
    return {
        "saved": True,
        "kind": "extra_check",
        "change_id": change_id,
        "path": str(saved),
        "rules": rules.model_dump(mode="json", exclude_none=True),
    }


def delete_environment_rule(
    kind: str,
    target: str | int,
    path: Path | str | None = None,
    *,
    db=None,
    project_key: Optional[str] = None,
) -> dict[str, Any]:
    """Xóa 1 luật khỏi config/environment_rules.yaml theo loại (audit_rule, diff_rule, forbidden_path, extra_check)."""
    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    with editing_config(cfg_path):
        before_yaml = read_rules_yaml_text(cfg_path)
        rules = load_environment_rules(cfg_path)
        k = (kind or "").strip().lower()
        if k in ("audit_rule", "custom_audit_rules"):
            before = len(rules.custom_audit_rules)
            rules.custom_audit_rules = [r for r in rules.custom_audit_rules if r.id != str(target)]
            removed = len(rules.custom_audit_rules) < before
        elif k in ("diff_rule", "forbidden_diff_patterns"):
            before = len(rules.forbidden_diff_patterns)
            rules.forbidden_diff_patterns = [r for r in rules.forbidden_diff_patterns if r.id != str(target)]
            removed = len(rules.forbidden_diff_patterns) < before
        elif k in ("forbidden_path", "forbidden_path_patterns"):
            before = len(rules.forbidden_path_patterns)
            rules.forbidden_path_patterns = [p for p in rules.forbidden_path_patterns if p != str(target)]
            removed = len(rules.forbidden_path_patterns) < before
        elif k in ("extra_check", "extra_mechanical_checks"):
            removed = False
            if isinstance(target, int) or str(target).isdigit():
                idx = int(target)
                if 0 <= idx < len(rules.extra_mechanical_checks):
                    rules.extra_mechanical_checks.pop(idx)
                    removed = True
            else:
                target_str = str(target).strip()
                before = len(rules.extra_mechanical_checks)
                rules.extra_mechanical_checks = [
                    c for c in rules.extra_mechanical_checks if " ".join(c) != target_str
                ]
                removed = len(rules.extra_mechanical_checks) < before
        else:
            raise ValueError(f"Loại luật không hợp lệ: {kind}")

        saved = save_environment_rules(rules, cfg_path)
        after_yaml = read_rules_yaml_text(cfg_path)

    change_id = None
    if removed:
        change_id = record_rule_change(
            db,
            action="delete_rule",
            summary=f"Xóa luật '{target}' ({canonical_rule_type(k)})",
            before_yaml=before_yaml,
            after_yaml=after_yaml,
            project_key=project_key,
            rule_type=canonical_rule_type(k),
            rule_id=str(target),
        )
    return {
        "deleted": removed,
        "kind": k,
        "change_id": change_id,
        "path": str(saved),
        "rules": rules.model_dump(mode="json", exclude_none=True),
    }


def list_rule_change_history(db, project_key: Optional[str] = None, limit: int = 40) -> list[dict[str, Any]]:
    from app.db.models import RuleChangeHistoryModel

    q = db.query(RuleChangeHistoryModel)
    if project_key:
        q = q.filter((RuleChangeHistoryModel.project_key == project_key) | (RuleChangeHistoryModel.project_key.is_(None)))
    rows = q.order_by(RuleChangeHistoryModel.id.desc()).limit(limit).all()
    return [
        {
            "change_id": r.id,
            "project_key": r.project_key,
            "action": r.action,
            "rule_type": r.rule_type,
            "rule_id": r.rule_id,
            "source_proposal_id": r.source_proposal_id,
            "source_task_id": r.source_task_id,
            "source_run_id": r.source_run_id,
            "summary": r.summary,
            "before_yaml": r.before_yaml,
            "after_yaml": r.after_yaml,
            "rolled_back": bool(r.rolled_back),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def rollback_rule_change(
    db,
    change_id: int,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Khôi phục cấu hình environment_rules.yaml về trạng thái trước thay đổi `change_id`."""
    from app.db.models import RuleChangeHistoryModel

    target = db.query(RuleChangeHistoryModel).filter(RuleChangeHistoryModel.id == change_id).first()
    if not target:
        raise ValueError(f"Không tìm thấy bản ghi lịch sử #{change_id}.")
    parsed = yaml.safe_load(target.before_yaml) or {}
    restored_rules = EnvironmentRules.model_validate(parsed)

    cfg_path = Path(path if path is not None else DEFAULT_ENV_RULES_PATH)
    with editing_config(cfg_path):
        current_yaml = read_rules_yaml_text(cfg_path)
        saved = save_environment_rules(restored_rules, cfg_path)
        after_yaml = read_rules_yaml_text(cfg_path)

    target.rolled_back = True
    if target.source_proposal_id:
        from app.db.models import RuleProposalModel

        prop = (
            db.query(RuleProposalModel)
            .filter(RuleProposalModel.proposal_id == target.source_proposal_id)
            .first()
        )
        if prop:
            still_present = False
            rt = canonical_rule_type(target.rule_type or prop.rule_type)
            rid = str(target.rule_id or prop.rule_key or "")
            if rt == "forbidden_diff_patterns":
                still_present = any(r.id == rid for r in restored_rules.forbidden_diff_patterns)
            elif rt == "custom_audit_rules":
                still_present = any(r.id == rid for r in restored_rules.custom_audit_rules)
            elif rt == "forbidden_path_patterns":
                still_present = rid in restored_rules.forbidden_path_patterns
            elif rt == "extra_mechanical_checks":
                still_present = any(" ".join(c) == rid for c in restored_rules.extra_mechanical_checks)
            if not still_present:
                prop.status = "pending_review"
                prop.decided_at = None
                prop.updated_at = datetime.now(timezone.utc)
    db.commit()

    rb_id = record_rule_change(
        db,
        action="rollback",
        summary=f"Rollback về trước thay đổi #{change_id} ({target.summary})",
        before_yaml=current_yaml,
        after_yaml=after_yaml,
        project_key=target.project_key,
        rule_type=target.rule_type,
        rule_id=target.rule_id,
        source_proposal_id=target.source_proposal_id,
        source_task_id=target.source_task_id,
        source_run_id=target.source_run_id,
    )
    return {
        "rolled_back": True,
        "restored_from_change_id": change_id,
        "rollback_change_id": rb_id,
        "path": str(saved),
        "rules": restored_rules.model_dump(mode="json", exclude_none=True),
    }
