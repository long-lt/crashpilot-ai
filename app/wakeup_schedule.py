"""Quản lý lập lịch thức dậy hằng ngày (Daily Wakeup Cron & macOS LaunchAgent).

Kết hợp cả 2 cơ chế thật trên hệ điều hành (Zero Mock Policy):
  1. macOS `LaunchAgent` (`~/Library/LaunchAgents/com.crashpilot.wakeup.plist` với `StartCalendarInterval`) —
     cơ chế chuẩn của macOS (không bị chặn TCC như `/usr/bin/crontab`, và nếu máy Mac đang ngủ tại thời điểm hẹn giờ,
     ngay khi mở máy/thức dậy macOS tự động đánh thức CrashPilot AI chạy bù chu trình).
  2. POSIX `crontab` (`# crashpilot-ai-sync`) — đồng bộ kèm khi hệ điều hành cho phép ghi crontab.
"""
from __future__ import annotations

import os
import platform
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from app.config_files import write_yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
RUN_SH = PROJECT_DIR / "run.sh"
LOG_FILE = PROJECT_DIR / "data" / "logs" / "sync.log"
WAKEUP_CONFIG_PATH = PROJECT_DIR / "config" / "wakeup.yaml"
CRON_TAG = "# crashpilot-ai-sync"
LAUNCHD_LABEL = "com.crashpilot.wakeup"

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CRON_FIELD_RE = re.compile(r"^[\d*/,-]+$")


class WakeupConfig(BaseModel):
    enabled: bool = True
    schedule_mode: Literal["daily", "cron"] = "daily"
    daily_time: str = "08:00"
    cron_expr: str = "0 8 * * *"
    action: Literal["ensure_server_and_cycle", "cycle_only"] = "ensure_server_and_cycle"
    enable_launchd: bool = True
    port: int = Field(default=8787, ge=1, le=65535)


def normalize_daily_time(value: str) -> tuple[int, int, str]:
    m = TIME_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"Giờ thức dậy không hợp lệ '{value}' (định dạng chuẩn HH:MM, ví dụ 08:00).")
    hour, minute = int(m.group(1)), int(m.group(2))
    return hour, minute, f"{hour:02d}:{minute:02d}"


def validate_cron_expr(expr: str) -> str:
    parts = (expr or "").strip().split()
    if len(parts) != 5 or not all(CRON_FIELD_RE.match(p) for p in parts):
        raise ValueError(f"Biểu thức cron không hợp lệ '{expr}' (cần đủ 5 trường, ví dụ '0 8 * * *').")
    return " ".join(parts)


def _expand_cron_field(token: str, min_val: int, max_val: int) -> list[int] | None:
    if token == "*":
        return None
    values: set[int] = set()
    for part in token.split(","):
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = max(1, int(step_str))
        else:
            base = part
        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            s_str, e_str = base.split("-", 1)
            start, end = int(s_str), int(e_str)
        else:
            start = end = int(base)
        for v in range(start, end + 1, step):
            if min_val <= v <= max_val:
                values.add(v)
    return sorted(values)


def cron_expr_to_launchd_calendar(cron_expr: str) -> dict | list[dict]:
    """Chuyển biểu thức Cron 5 trường sang cấu trúc StartCalendarInterval của macOS launchd."""
    m_tok, h_tok, dom_tok, mon_tok, dow_tok = validate_cron_expr(cron_expr).split()
    minutes = _expand_cron_field(m_tok, 0, 59) or [0]
    hours = _expand_cron_field(h_tok, 0, 23)
    doms = _expand_cron_field(dom_tok, 1, 31)
    months = _expand_cron_field(mon_tok, 1, 12)
    dows = _expand_cron_field(dow_tok, 0, 7)
    if dows:
        dows = sorted({0 if d == 7 else d for d in dows})

    entries: list[dict] = [{}]
    for key, vals in (
        ("Minute", minutes),
        ("Hour", hours),
        ("Day", doms),
        ("Month", months),
        ("Weekday", dows),
    ):
        if vals is None:
            continue
        entries = [{**base, key: v} for base in entries for v in vals]
        if len(entries) > 256:
            entries = entries[:256]
            break

    return entries[0] if len(entries) == 1 else entries


def launchd_plist_path(home: Path | None = None) -> Path:
    base = home or Path.home()
    return base / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def load_wakeup_config(path: Path | None = None) -> WakeupConfig:
    cfg_path = path or WAKEUP_CONFIG_PATH
    if not cfg_path.is_file():
        return WakeupConfig()
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return WakeupConfig.model_validate(raw)


def save_wakeup_config(cfg: WakeupConfig, path: Path | None = None) -> Path:
    cfg_path = path or WAKEUP_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(cfg_path, cfg.model_dump(mode="json"))
    return cfg_path


def _subcommand_for_action(action: str) -> str:
    return "wakeup-run" if action == "ensure_server_and_cycle" else "cycle"


def build_crontab_line(
    cron_expr: str,
    action: str = "ensure_server_and_cycle",
    port: int = 8787,
    project_dir: Path | None = None,
    home: Path | None = None,
) -> str:
    pdir = (project_dir or PROJECT_DIR).resolve()
    hdir = (home or Path.home()).resolve()
    log_path = pdir / "data" / "logs" / "sync.log"
    subcmd = _subcommand_for_action(action)
    safe_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return (
        f'{cron_expr} cd "{pdir}" && HOME="{hdir}" PATH="{safe_path}" '
        f'CRASHPILOT_PORT={int(port)} /bin/bash "{pdir / "run.sh"}" {subcmd} '
        f'>> "{log_path}" 2>&1 {CRON_TAG}'
    )


def build_launchd_plist_bytes(
    hour: int,
    minute: int,
    action: str = "ensure_server_and_cycle",
    port: int = 8787,
    project_dir: Path | None = None,
    home: Path | None = None,
    cron_expr: str | None = None,
) -> bytes:
    pdir = (project_dir or PROJECT_DIR).resolve()
    hdir = (home or Path.home()).resolve()
    log_path = pdir / "data" / "logs" / "sync.log"
    subcmd = _subcommand_for_action(action)
    safe_path = f"{hdir / '.local' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    cal_interval = (
        cron_expr_to_launchd_calendar(cron_expr)
        if cron_expr
        else {"Hour": int(hour), "Minute": int(minute)}
    )
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": ["/bin/bash", str(pdir / "run.sh"), subcmd],
        "WorkingDirectory": str(pdir),
        "EnvironmentVariables": {
            "HOME": str(hdir),
            "PATH": safe_path,
            "CRASHPILOT_PORT": str(int(port)),
            "CRASHPILOT_NO_OPEN": "1",
        },
        "StartCalendarInterval": cal_interval,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "RunAtLoad": False,
    }
    return plistlib.dumps(payload)


def get_current_crontab_lines() -> list[str]:
    try:
        res = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5, check=False)
    except Exception:
        return []
    if res.returncode != 0:
        return []
    return (res.stdout or "").splitlines()


def _try_write_crontab_lines(lines: list[str]) -> tuple[bool, str]:
    content = "\n".join(ln for ln in lines if ln is not None).strip()
    body = (content + "\n") if content else ""
    try:
        proc = subprocess.run(["crontab", "-"], input=body, text=True, capture_output=True, timeout=5, check=False)
    except subprocess.TimeoutExpired:
        return False, "Quá thời gian ghi crontab (macOS TCC đang chờ cấp quyền)."
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, ""


def _install_or_remove_launchd(
    *,
    enable: bool,
    hour: int = 8,
    minute: int = 0,
    cron_expr: str | None = None,
    action: str = "ensure_server_and_cycle",
    port: int = 8787,
    home: Path | None = None,
) -> tuple[bool, str]:
    if platform.system() != "Darwin":
        return False, "Chỉ hỗ trợ LaunchAgent trên macOS."

    plist_file = launchd_plist_path(home=home)
    uid = os.getuid()
    domain = f"gui/{uid}"

    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if not enable:
        plist_file.unlink(missing_ok=True)
        return True, "Đã gỡ macOS LaunchAgent."

    plist_file.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    plist_file.write_bytes(
        build_launchd_plist_bytes(
            hour,
            minute,
            action=action,
            port=port,
            home=home,
            cron_expr=cron_expr,
        )
    )

    boot = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_file)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if boot.returncode != 0:
        load_res = subprocess.run(
            ["launchctl", "load", "-w", str(plist_file)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if load_res.returncode != 0:
            return False, (boot.stderr or load_res.stderr or "Không nạp được LaunchAgent").strip()
    return True, str(plist_file)


def apply_wakeup_schedule(
    payload: dict | WakeupConfig,
    *,
    config_path: Path | None = None,
    apply_system: bool = True,
) -> dict:
    """Lưu cấu hình thức dậy hằng ngày và áp dụng thật vào macOS LaunchAgent + crontab."""
    current = load_wakeup_config(config_path)
    data = payload.model_dump() if isinstance(payload, WakeupConfig) else {**current.model_dump(), **payload}

    mode = str(data.get("schedule_mode") or "daily").strip().lower()
    if mode not in ("daily", "cron"):
        raise ValueError("schedule_mode phải là 'daily' hoặc 'cron'.")

    daily_time_raw = str(data.get("daily_time") or "08:00").strip()
    hour, minute, norm_time = normalize_daily_time(daily_time_raw)

    if mode == "daily":
        cron_expr = f"{minute} {hour} * * *"
    else:
        cron_expr = validate_cron_expr(str(data.get("cron_expr") or f"{minute} {hour} * * *"))
        parts = cron_expr.split()
        if parts[0].isdigit() and parts[1].isdigit() and parts[2:] == ["*", "*", "*"]:
            minute, hour = int(parts[0]), int(parts[1])
            norm_time = f"{hour:02d}:{minute:02d}"

    cfg = WakeupConfig(
        enabled=bool(data.get("enabled", True)),
        schedule_mode=mode,  # type: ignore[arg-type]
        daily_time=norm_time,
        cron_expr=cron_expr,
        action=str(data.get("action") or "ensure_server_and_cycle"),  # type: ignore[arg-type]
        enable_launchd=bool(data.get("enable_launchd", True)),
        port=int(data.get("port") or os.environ.get("CRASHPILOT_PORT") or 8787),
    )
    save_wakeup_config(cfg, config_path)

    crontab_note = ""
    if apply_system:
        is_macos = platform.system() == "Darwin"
        if is_macos:
            # Trên macOS, ghi /usr/bin/crontab kích hoạt hộp thoại bảo mật TCC
            # ("... would like to administer your computer") vì crontab là binary setuid chạm vào /usr/lib/cron/tabs.
            # Dùng 100% macOS LaunchAgent (~/Library/LaunchAgents) — chuẩn chính thức của Apple,
            # không cần quyền Admin/TCC và tự chạy bù ngay khi mở máy từ chế độ Sleep.
            want_launchd = cfg.enabled and cfg.enable_launchd
            launchd_ok, launchd_msg = _install_or_remove_launchd(
                enable=want_launchd,
                hour=hour,
                minute=minute,
                cron_expr=cfg.cron_expr if cfg.schedule_mode == "cron" else None,
                action=cfg.action,
                port=cfg.port,
            )
            if not launchd_ok and want_launchd:
                raise RuntimeError(f"Không thể nạp macOS LaunchAgent: {launchd_msg}")
            crontab_note = "Trên macOS hệ thống dùng LaunchAgent chuẩn của Apple (không gọi crontab để tránh hộp thoại xin quyền quản trị máy)."
        else:
            existing_lines = [ln for ln in get_current_crontab_lines() if CRON_TAG not in ln]
            if cfg.enabled:
                existing_lines.append(build_crontab_line(cfg.cron_expr, action=cfg.action, port=cfg.port))
            cron_ok, cron_err = _try_write_crontab_lines(existing_lines)
            if not cron_ok:
                raise RuntimeError(f"Không thể cài đặt lịch crontab: {cron_err}")

    status = get_wakeup_status(config_path=config_path)
    if crontab_note:
        status["crontab_note"] = crontab_note
    return status


def get_wakeup_status(*, config_path: Path | None = None) -> dict:
    """Đọc trạng thái thật 100% từ macOS LaunchAgent, crontab, pmset và config/wakeup.yaml."""
    cfg_path = config_path or WAKEUP_CONFIG_PATH
    has_saved_cfg = cfg_path.is_file()
    cfg = load_wakeup_config(cfg_path)

    cron_lines = [ln for ln in get_current_crontab_lines() if CRON_TAG in ln]
    active_cron_line = cron_lines[-1] if cron_lines else ""
    detected_cron_expr = " ".join(active_cron_line.split()[:5]) if len(active_cron_line.split()) >= 5 else ""

    plist_file = launchd_plist_path()
    launchd_installed = plist_file.is_file()
    launchd_loaded = False
    if platform.system() == "Darwin":
        chk = subprocess.run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True, text=True, timeout=5, check=False)
        launchd_loaded = chk.returncode == 0

    if not has_saved_cfg and not active_cron_line and not launchd_installed:
        cfg.enabled = False
    elif not has_saved_cfg and detected_cron_expr:
        cfg.enabled = True
        cfg.cron_expr = detected_cron_expr
        p = detected_cron_expr.split()
        if len(p) == 5 and p[0].isdigit() and p[1].isdigit() and p[2:] == ["*", "*", "*"]:
            cfg.schedule_mode = "daily"
            cfg.daily_time = f"{int(p[1]):02d}:{int(p[0]):02d}"
        else:
            cfg.schedule_mode = "cron"

    pmset_raw = ""
    if platform.system() == "Darwin":
        pm = subprocess.run(["pmset", "-g", "sched"], capture_output=True, text=True, timeout=5, check=False)
        if pm.returncode == 0:
            pmset_raw = (pm.stdout or "").strip()

    hour, minute, _ = normalize_daily_time(cfg.daily_time)
    pmset_hint_cmd = f"sudo pmset repeat wakeorpoweron MTWRFSU {hour:02d}:{minute:02d}:00"

    log_tail: list[str] = []
    if LOG_FILE.is_file():
        try:
            log_tail = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        except Exception:
            log_tail = []

    scheduler_active = bool(cfg.enabled and (launchd_loaded or launchd_installed or active_cron_line))
    return {
        "config": cfg.model_dump(mode="json"),
        "scheduler_active": scheduler_active,
        "crontab_installed": bool(active_cron_line),
        "crontab_line": active_cron_line,
        "detected_cron_expr": detected_cron_expr,
        "launchd_supported": platform.system() == "Darwin",
        "launchd_installed": launchd_installed,
        "launchd_loaded": launchd_loaded,
        "launchd_plist_path": str(plist_file),
        "pmset_schedule": pmset_raw,
        "pmset_wake_command": pmset_hint_cmd,
        "log_path": str(LOG_FILE),
        "log_tail": log_tail,
    }
