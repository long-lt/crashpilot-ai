from contextlib import ExitStack
import asyncio
import concurrent.futures
import json
import os
import signal
import socket
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
from pydantic import BaseModel, Field

from app.constrained_runner.isolation import isolated_command
from app.constrained_runner.models import RuntimeVerifierConfig


class RuntimeVerifyOutcome(BaseModel):
    executed: bool = False
    mode: str = "none"
    passed: bool = True
    clicks_performed: int = 0
    console_errors: list[str] = Field(default_factory=list)
    page_errors: list[str] = Field(default_factory=list)
    server_errors: list[str] = Field(default_factory=list)
    mobile_output: str = ""
    screenshot_path: Optional[str] = None
    skipped_reason: Optional[str] = None


def _owns_listener(proc: subprocess.Popen, port: int) -> bool:
    """Only accept listeners belonging to the app process group for this run."""
    if proc.poll() is not None:
        return False
    listeners = subprocess.run(
        ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True, text=True, timeout=3,
    )
    groups = set()
    for pid in listeners.stdout.split():
        try:
            groups.add(os.getpgid(int(pid)))
        except ProcessLookupError:
            continue
    return groups == {proc.pid}


def _wait_for_url(url: str, timeout_sec: int, proc: subprocess.Popen, port: int) -> bool:
    deadline = time.monotonic() + max(1, timeout_sec)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < 400 and _owns_listener(proc, port):
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


async def _run_web_cdp_async(
    repo_root: Path,
    cfg: RuntimeVerifierConfig,
    artifacts_dir: Path,
) -> RuntimeVerifyOutcome:
    from playwright.async_api import async_playwright

    outcome = RuntimeVerifyOutcome(executed=True, mode="web_cdp", passed=True)
    if not cfg.url:
        return RuntimeVerifyOutcome(
            executed=False, mode="web_cdp", passed=True, skipped_reason="Chưa cấu hình runtime_verifier.url"
        )

    resources = ExitStack()
    url = cfg.url
    proc: Optional[subprocess.Popen] = None
    try:
        if cfg.start_command:
            command = cfg.start_command
            if "{port}" in url:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    allocated = str(sock.getsockname()[1])
                url = url.replace("{port}", allocated)
                command = [arg.replace("{port}", allocated) for arg in command]
            address = urlparse(url)
            if address.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Managed web verifier requires a loopback URL")
            port = address.port or (443 if address.scheme == "https" else 80)
            command, env = resources.enter_context(isolated_command(command, repo_root, writable=True, inputs=[],
                                                                    variables={}, listen_port=port))
            proc = subprocess.Popen(
                command, start_new_session=True, env=env,
                cwd=str(repo_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if not _wait_for_url(url, cfg.startup_timeout_sec, proc, port):
                outcome.passed = False
                outcome.server_errors.append(
                    f"App không phản hồi tại {url} sau {cfg.startup_timeout_sec}s"
                )
                return outcome

        target_origin = urlparse(url)[:2]

        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("console", lambda m: outcome.console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: outcome.page_errors.append(str(e)))
            page.on(
                "response",
                lambda r: outcome.server_errors.append(f"HTTP {r.status} {r.url}") if r.status >= 500 else None,
            )

            response = await page.goto(url, wait_until="networkidle", timeout=cfg.timeout_sec * 1000)
            if not response or not response.ok or urlparse(page.url)[:2] != urlparse(url)[:2]:
                raise ValueError("Trang được kiểm thử không phản hồi thành công tại đúng địa chỉ.")

            # Fuzzing: click thử các nút/link cùng origin hiển thị trên màn hình
            max_clicks = max(0, cfg.fuzz_clicks)
            for idx in range(max_clicks):
                clickables = page.locator("button:visible:not([disabled]), [role='button']:visible, a[href]:visible")
                count = await clickables.count()
                if count == 0 or idx >= count:
                    break
                el = clickables.nth(idx)
                try:
                    href = await el.get_attribute("href")
                    if href and urlparse(urljoin(page.url, href))[:2] != target_origin:
                        continue
                    await el.click(timeout=3000)
                    outcome.clicks_performed += 1
                    await page.wait_for_timeout(250)
                except Exception:
                    continue

            shot = artifacts_dir / "runtime_verify.png"
            await page.screenshot(path=str(shot), full_page=False)
            outcome.screenshot_path = str(shot)
            await browser.close()

        if proc and not _owns_listener(proc, port):
            outcome.server_errors.append("Ứng dụng của lượt kiểm thử đã ngừng phục vụ.")
        if outcome.console_errors or outcome.page_errors or outcome.server_errors:
            outcome.passed = False
        return outcome
    except Exception as exc:
        outcome.passed = False
        outcome.page_errors.append(f"Lỗi Runtime Verifier (web_cdp): {exc}")
        return outcome
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        resources.close()


def _detect_flutter_device(
    flutter_bin: str,
    preferred_id: Optional[str] = None,
) -> tuple[Optional[str], str]:
    res = subprocess.run(
        [flutter_bin, "devices", "--machine"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if res.returncode != 0:
        return None, ""
    try:
        devices = json.loads(res.stdout)
        if preferred_id:
            for d in devices:
                if str(d.get("id")) == preferred_id:
                    return preferred_id, str(d.get("targetPlatform") or "").lower()
            return preferred_id, ""
        for d in devices:
            target = str(d.get("targetPlatform") or "").lower()
            if "ios" in target or "android" in target:
                return str(d.get("id")), target
    except Exception:
        return None, ""
    return None, ""


def _run_ios_simulator_smoke(
    flutter_bin: str,
    repo_root: Path,
    device_id: str,
    cfg: RuntimeVerifierConfig,
    artifacts_dir: Path,
    outcome: RuntimeVerifyOutcome,
    outputs: list[str],
) -> None:
    """Build thật bản iOS Simulator debug, cài đặt, mở app trên Simulator, kiểm tra sống & chụp ảnh."""
    import plistlib
    import re

    xcrun = shutil.which("xcrun")
    if not xcrun:
        outcome.passed = False
        outputs.append("[iOS Simulator] Không tìm thấy lệnh xcrun trên máy.")
        return

    build_timeout = max(int(cfg.timeout_sec or 180), 180)
    build_res = subprocess.run(
        [flutter_bin, "build", "ios", "--simulator", "--debug"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=build_timeout,
    )
    build_tail = (build_res.stdout + "\n" + build_res.stderr).strip()[-1500:]
    outputs.append(f"[flutter build ios --simulator --debug] (exit {build_res.returncode})\n{build_tail}")
    if build_res.returncode != 0:
        outcome.passed = False
        return

    sim_dir = repo_root / "build" / "ios" / "iphonesimulator"
    app_bundles = sorted(sim_dir.glob("*.app")) if sim_dir.is_dir() else []
    if not app_bundles:
        outcome.passed = False
        outputs.append(f"[iOS Simulator] Không tìm thấy file .app sau khi build tại {sim_dir}.")
        return
    app_path = app_bundles[0]

    info_plist = app_path / "Info.plist"
    bundle_id = ""
    if info_plist.is_file():
        try:
            with info_plist.open("rb") as f:
                plist_data = plistlib.load(f)
            bundle_id = str(plist_data.get("CFBundleIdentifier") or "").strip()
        except Exception:
            bundle_id = ""
    if not bundle_id:
        outcome.passed = False
        outputs.append(f"[iOS Simulator] Không đọc được CFBundleIdentifier từ {info_plist}.")
        return

    install_res = subprocess.run(
        [xcrun, "simctl", "install", device_id, str(app_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if install_res.returncode != 0:
        outcome.passed = False
        outputs.append(f"[xcrun simctl install] FAIL: {(install_res.stdout + install_res.stderr).strip()[-600:]}")
        return

    launch_res = subprocess.run(
        [xcrun, "simctl", "launch", device_id, bundle_id],
        capture_output=True,
        text=True,
        timeout=30,
    )
    launch_out = (launch_res.stdout + "\n" + launch_res.stderr).strip()
    if launch_res.returncode != 0:
        outcome.passed = False
        outputs.append(f"[xcrun simctl launch {bundle_id}] FAIL: {launch_out[-600:]}")
        return

    m_pid = re.search(r":\s*(\d+)", launch_out)
    app_pid = int(m_pid.group(1)) if m_pid else None

    # Đợi app khởi động và render trên iOS Simulator
    time.sleep(4.0)

    alive = True
    if app_pid:
        try:
            os.kill(app_pid, 0)
        except OSError:
            alive = False

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    shot_path = artifacts_dir / "runtime_4b_ios_simulator.png"
    shot_res = subprocess.run(
        [xcrun, "simctl", "io", device_id, "screenshot", str(shot_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if shot_res.returncode == 0 and shot_path.is_file():
        outcome.screenshot_path = str(shot_path)

    # Dừng app gọn gàng sau khi kiểm tra
    subprocess.run(
        [xcrun, "simctl", "terminate", device_id, bundle_id],
        capture_output=True,
        text=True,
        timeout=15,
    )

    if not alive:
        outcome.passed = False
        outputs.append(
            f"[iOS Simulator Smoke] FAIL: App {bundle_id} (PID {app_pid}) bị thoát/crash trong 4 giây đầu sau khi mở."
        )
    else:
        outputs.append(
            f"[iOS Simulator Smoke] PASS: Đã build, cài đặt và mở {bundle_id} (PID {app_pid}) "
            f"ổn định trên Simulator {device_id} (không crash khi khởi động)."
        )


def _run_flutter_mobile(
    repo_root: Path,
    cfg: RuntimeVerifierConfig,
    artifacts_dir: Path,
) -> RuntimeVerifyOutcome:
    flutter_bin = shutil.which("flutter")
    if not flutter_bin:
        return RuntimeVerifyOutcome(
            executed=False, mode="flutter_mobile", passed=False, skipped_reason="Không tìm thấy lệnh flutter trong PATH."
        )

    device_id, target_platform = _detect_flutter_device(flutter_bin, preferred_id=cfg.device_id)
    if not device_id:
        return RuntimeVerifyOutcome(
            executed=False,
            mode="flutter_mobile",
            passed=False,
            skipped_reason="Không có Android Emulator / iOS Simulator nào đang bật — không thể chạy kiểm định 4B Runtime Mobile.",
        )

    outcome = RuntimeVerifyOutcome(executed=True, mode="flutter_mobile", passed=True)
    outputs: list[str] = []

    # 1. Chạy integration_test nếu tồn tại
    target_path = repo_root / cfg.test_target
    if target_path.exists():
        res = subprocess.run(
            [flutter_bin, "test", cfg.test_target, "-d", device_id],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=cfg.timeout_sec,
        )
        tail = (res.stdout + "\n" + res.stderr).strip()[-1000:]
        outputs.append(f"[flutter test {cfg.test_target}]\n{tail}")
        if res.returncode != 0:
            outcome.passed = False

    # 2. Chạy Android Monkey Fuzzing nếu cấu hình android_package + monkey_events > 0
    adb_bin = shutil.which("adb")
    if cfg.android_package and cfg.monkey_events > 0 and adb_bin:
        m_res = subprocess.run(
            [
                adb_bin,
                "-s",
                device_id,
                "shell",
                "monkey",
                "-p",
                cfg.android_package,
                "--throttle",
                "150",
                "-v",
                str(cfg.monkey_events),
            ],
            capture_output=True,
            text=True,
            timeout=cfg.timeout_sec,
        )
        m_out = (m_res.stdout + "\n" + m_res.stderr).strip()
        outputs.append(f"[adb monkey {cfg.monkey_events} events]\n{m_out[-800:]}")
        outcome.clicks_performed = cfg.monkey_events
        if m_res.returncode != 0 or "CRASH:" in m_out or "ANR" in m_out:
            outcome.passed = False

    # 3. Nếu repo chưa có integration_test và đang chạy trên iOS Simulator -> build & launch smoke test thật
    if not outputs and "ios" in target_platform:
        _run_ios_simulator_smoke(flutter_bin, repo_root, device_id, cfg, artifacts_dir, outcome, outputs)

    if not outputs:
        outcome.executed = False
        outcome.passed = False
        outcome.skipped_reason = "Không có kiểm thử mobile nào được thực thi."
    outcome.mobile_output = "\n\n".join(outputs)
    return outcome


def run_runtime_verifier(
    repo_root: Path,
    cfg: Optional[RuntimeVerifierConfig],
    artifacts_dir: Path,
) -> RuntimeVerifyOutcome:
    """Chạy Giai đoạn 4 Runtime Verifier (Web CDP Fuzzing hoặc Flutter Mobile)."""
    if not cfg or not cfg.enabled:
        return RuntimeVerifyOutcome(executed=False, mode="disabled", passed=True, skipped_reason="runtime_verifier chưa bật")

    if cfg.mode == "flutter_mobile":
        return _run_flutter_mobile(repo_root, cfg, artifacts_dir)

    # Chạy Playwright async trên thread riêng để an toàn khi gọi bên trong asyncio event loop
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, _run_web_cdp_async(repo_root, cfg, artifacts_dir))
        return future.result(timeout=cfg.timeout_sec + cfg.startup_timeout_sec + 15)
