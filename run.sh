#!/bin/bash
# CrashPilot AI — script chạy toàn bộ hệ thống + quản lý lập lịch cron.
#
# Cách dùng:
#   ./run.sh                       Mở Dashboard, kiểm tra phiên Google và chạy Autopilot ở nền.
#                                  (Tự động dừng server cũ trên cùng cổng nếu đang chạy; Ctrl+C để dừng).
#                                     Tuỳ chỉnh: CRASHPILOT_SYNC_INTERVAL_MIN=30 CRASHPILOT_PORT=8787 ./run.sh
#   ./run.sh stop                  Dừng Web Dashboard đang chạy trên cổng cấu hình
#   ./run.sh setup                 Cài venv và dependencies (lần đầu)
#   ./run.sh login                 Đăng nhập Google cho nguồn Crashlytics
#   ./run.sh sync [args...]        Đồng bộ Crashlytics -> SQLite (VD: ./run.sh sync --limit 10)
#   ./run.sh serve                 Chạy Web Dashboard (http://127.0.0.1:8787)
#   ./run.sh all                   Sync một lần rồi bật Dashboard
#   ./run.sh test                  Chạy unit tests
#   ./run.sh cron-install ["expr"] Cài lịch thức dậy hằng ngày (mặc định 08:00 "0 8 * * *" + macOS LaunchAgent)
#   ./run.sh wakeup-install [HH:MM] Đặt giờ thức dậy hằng ngày (VD: ./run.sh wakeup-install 07:30)
#   ./run.sh cron-remove           Gỡ lịch cron & LaunchAgent thức dậy
#   ./run.sh cron-status           Xem lịch thức dậy hiện tại + log gần nhất
#   ./run.sh morning-audit [--ai]  Giai đoạn 5: Lấy mẫu kiểm tra buổi sáng & đề xuất nâng cấp luật
#   ./run.sh retry-task <TASK>     Chạy tiếp task từ bước lỗi (VD: ./run.sh retry-task TASK-1012 --from-step 3)
#   ./run.sh logs                  Theo dõi log sync realtime

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
LOG_DIR="$PROJECT_DIR/data/logs"
LOG_FILE="$LOG_DIR/sync.log"
LOCK_FILE="$PROJECT_DIR/data/cycle.lock"
CRON_TAG="# crashpilot-ai-sync"
DEFAULT_CRON="0 * * * *"
SYNC_LIMIT="${CRASHPILOT_SYNC_LIMIT:-10}"

cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

require_venv() {
  if [[ ! -x "$VENV_PY" ]]; then
    echo "Chưa có môi trường. Chạy: ./run.sh setup" >&2
    exit 1
  fi
}

cmd_setup() {
  local py="${PYTHON:-python3}"
  log "Tạo virtualenv bằng $py ..."
  [[ -d .venv ]] || "$py" -m venv .venv
  "$VENV_PY" -m pip install --upgrade pip -q
  "$VENV_PY" -m pip install -e ".[dev]" -q
  [[ -f .env ]] || cp .env.example .env
  log "Setup xong. Tiếp theo: ./run.sh login"
}

cmd_login() {
  require_venv
  "$VENV_PY" -m app.main login
}

# The shell keeps fd 9 open for the duration of sync/cycle. The kernel releases
# its advisory lock when the last inherited descriptor closes.
acquire_cycle_lock() {
  exec 9>>"$LOCK_FILE"
  "$VENV_PY" -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("Một chu trình khác đang chạy.", file=sys.stderr)
    sys.exit(75)'
}

# Sync có khóa chống chạy chồng (cron chạy khi lần trước chưa xong).
cmd_sync() {
  require_venv
  acquire_cycle_lock || return $?

  if [[ $# -eq 0 ]]; then
    set -- --limit "$SYNC_LIMIT"
  fi
  log "Bắt đầu sync: $*"
  local rc=0
  "$VENV_PY" -m app.main sync "$@" || rc=$?
  if [[ $rc -eq 0 ]]; then
    log "Sync hoàn tất."
  elif [[ $rc -eq 2 ]]; then
    log "CẢNH BÁO: Session Google hết hạn — chạy './run.sh login' để đăng nhập lại."
  else
    log "Sync lỗi (exit $rc)."
  fi
  return $rc
}

browser_host_for() {
  local host="$1"
  case "$host" in
    0.0.0.0) echo "127.0.0.1" ;;
    ::) echo "[::1]" ;;
    *:*) echo "[$host]" ;;
    *) echo "$host" ;;
  esac
}

is_crashpilot_server() {
  local url="$1"
  shift
  if curl --noproxy '*' --silent --max-time 2 "$url/api/health" 2>/dev/null | grep -q '"crashpilot-ai"'; then
    return 0
  fi
  local pid cmdline
  for pid in "$@"; do
    cmdline="$(ps -ww -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$cmdline" == *"app.main"*"serve"* || "$cmdline" == *"app.api.server"* ]]; then
      return 0
    fi
  done
  return 1
}

stop_existing_server() {
  local host="${1:-${CRASHPILOT_HOST:-127.0.0.1}}"
  local port="${2:-${CRASHPILOT_PORT:-8787}}"
  local mode="${3:-restart}"
  local url="http://$(browser_host_for "$host"):$port"
  local raw_pids
  raw_pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -z "$raw_pids" ]] && return 0

  local pids=()
  local p
  for p in $raw_pids; do
    [[ "$p" != "$$" ]] && pids+=("$p")
  done
  [[ ${#pids[@]} -eq 0 ]] && return 0

  if ! is_crashpilot_server "$url" "${pids[@]}"; then
    log "Cổng $port đang được sử dụng bởi tiến trình khác (PID: ${pids[*]}) — đặt CRASHPILOT_PORT=xxxx ./run.sh để dùng cổng khác."
    exit 1
  fi

  if [[ "$mode" == "stop" ]]; then
    log "Phát hiện server CrashPilot đang chạy trên cổng $port (PID: ${pids[*]}) — đang dừng..."
  else
    log "Phát hiện server CrashPilot đang chạy trên cổng $port (PID: ${pids[*]}) — đang dừng để chạy lại..."
  fi
  kill -TERM "${pids[@]}" 2>/dev/null || true

  local i alive
  for ((i = 0; i < 40; i++)); do
    alive=0
    for p in "${pids[@]}"; do
      if kill -0 "$p" 2>/dev/null; then
        alive=1
        break
      fi
    done
    if [[ $alive -eq 0 ]] && ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      log "Đã dừng server CrashPilot trên cổng $port."
      return 0
    fi
    sleep 0.2
  done

  log "Server cũ (PID: ${pids[*]}) chưa thoát kịp — gửi SIGKILL..."
  kill -KILL "${pids[@]}" 2>/dev/null || true
  local extra_pids
  extra_pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  for p in $extra_pids; do
    if [[ "$p" != "$$" ]] && is_crashpilot_server "$url" "$p"; then
      kill -KILL "$p" 2>/dev/null || true
    fi
  done

  for ((i = 0; i < 15; i++)); do
    if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      log "Đã buộc dừng server cũ trên cổng $port."
      return 0
    fi
    sleep 0.2
  done

  log "Không thể giải phóng cổng $port. Hãy kiểm tra thủ công: lsof -nP -iTCP:$port -sTCP:LISTEN"
  exit 1
}

cmd_stop() {
  local host="${CRASHPILOT_HOST:-127.0.0.1}"
  local port="${CRASHPILOT_PORT:-8787}"
  if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    log "Không có server nào đang chạy trên cổng $port."
    return 0
  fi
  stop_existing_server "$host" "$port" "stop"
}

cmd_serve() {
  require_venv
  local host="${CRASHPILOT_HOST:-127.0.0.1}"
  local port="${CRASHPILOT_PORT:-8787}"
  stop_existing_server "$host" "$port" "restart"
  "$VENV_PY" -m app.main serve --host "$host" --port "$port"
}

cmd_all() {
  ( cmd_sync "$@" ) || log "Sync có lỗi, vẫn tiếp tục mở Dashboard."
  cmd_serve
}

cmd_test() {
  require_venv
  "$VENV_PY" -m pytest
}

# Chạy trọn vẹn 1 chu trình khép kín (có khóa chống chạy chồng):
# Nguồn (projects.yaml) -> Thu thập ngữ cảnh & Gom cụm -> Điều phối Quy trình 4 bước.
cmd_cycle() {
  require_venv
  acquire_cycle_lock || return $?

  local session_rc=0
  if [[ "${1:-}" == "--startup" ]]; then
    shift
    echo checking_session >&3
    "$VENV_PY" -m app.main check-session || session_rc=$?
    if [[ $session_rc -eq 2 ]]; then
      if [[ -t 0 ]]; then
        echo awaiting_login >&3
        cmd_login > "$(tty)" 2>&1 || return 2
        session_rc=0
      else
        log "Phiên Google hết hạn. Chạy ./run.sh login để đăng nhập lại."
        set -- --skip-browser-sync --limit "$SYNC_LIMIT" "$@"
      fi
    fi
    echo running_cycle >&3
  fi
  if [[ $# -eq 0 ]]; then
    set -- --limit "$SYNC_LIMIT"
  fi
  log "Bắt đầu chu trình Autopilot (cycle): $*"
  local rc=0
  "$VENV_PY" -m app.main cycle "$@" || rc=$?
  if [[ $rc -eq 0 ]]; then
    log "Chu trình hoàn tất."
  elif [[ $rc -eq 2 ]]; then
    log "CẢNH BÁO: Session Google hết hạn — chạy './run.sh login' để đăng nhập lại."
  else
    log "Chu trình lỗi (exit $rc)."
  fi
  if [[ $rc -eq 0 && $session_rc -eq 2 ]]; then
    return 2
  fi
  return $rc
}

# Dashboard trước, scheduler quản lý chu trình đầu và các lần chạy tiếp theo.
cmd_start() {
  local port="${CRASHPILOT_PORT:-8787}"
  local host="${CRASHPILOT_HOST:-127.0.0.1}"
  local browser_host
  browser_host="$(browser_host_for "$host")"
  local url="http://$browser_host:$port"
  stop_existing_server "$host" "$port" "restart"
  if [[ ! -x "$VENV_PY" ]] || ! "$VENV_PY" -c "import playwright, fastapi" 2>/dev/null; then
    cmd_setup
  fi
  log "Dashboard: $url | Ctrl+C để dừng"
  if [[ -z "${CRASHPILOT_NO_OPEN:-}" ]]; then
    local server_pid=$$
    (
      while kill -0 "$server_pid" 2>/dev/null; do
        if curl --noproxy '*' --fail --silent --max-time 1 "$url/api/health" >/dev/null 2>&1; then
          open "$url"
          break
        fi
        sleep 0.2
      done
    ) &
  fi
  export CRASHPILOT_STARTUP_CYCLE=1
  exec "$VENV_PY" -m app.main serve --host "$host" --port "$port"
}

cmd_wakeup_run() {
  require_venv
  local port="${CRASHPILOT_PORT:-8787}"
  local host="${CRASHPILOT_HOST:-127.0.0.1}"
  local url="http://$(browser_host_for "$host"):$port"
  if is_crashpilot_server "$url"; then
    log "[Wakeup] Server CrashPilot đang hoạt động tại $url — kích hoạt chu trình Autopilot hằng ngày..."
    cmd_cycle --limit "$SYNC_LIMIT"
  else
    log "[Wakeup] Đánh thức CrashPilot Server tại $url và chạy chu trình Autopilot đầu ngày..."
    CRASHPILOT_NO_OPEN=1 nohup "$PROJECT_DIR/run.sh" start >> "$LOG_FILE" 2>&1 &
    log "[Wakeup] Đã khởi chạy nền CrashPilot Server (PID: $!)."
  fi
}

cmd_wakeup_install() {
  require_venv
  local daily_time="${1:-08:00}"
  "$VENV_PY" -c "import sys; from app.wakeup_schedule import apply_wakeup_schedule; st = apply_wakeup_schedule({'enabled': True, 'schedule_mode': 'daily', 'daily_time': sys.argv[1]}); print('LaunchAgent:', st['launchd_plist_path'] if st['launchd_installed'] else 'N/A', '(LOADED)' if st['launchd_loaded'] else ''); print('Crontab:', st['crontab_line'] or st.get('crontab_note', 'N/A'))" "$daily_time"
  log "Đã bật lịch thức dậy hằng ngày lúc $daily_time."
}

cmd_cron_install() {
  require_venv
  local expr="${1:-0 8 * * *}"
  "$VENV_PY" -c "import sys; from app.wakeup_schedule import apply_wakeup_schedule; st = apply_wakeup_schedule({'enabled': True, 'schedule_mode': 'cron', 'cron_expr': sys.argv[1]}); print('LaunchAgent:', st['launchd_plist_path'] if st['launchd_installed'] else 'N/A', '(LOADED)' if st['launchd_loaded'] else ''); print('Crontab:', st['crontab_line'] or st.get('crontab_note', 'N/A'))" "$expr"
  log "Đã cài lịch cron thức dậy: $expr"
  log "Log: $LOG_FILE"
}

cmd_cron_remove() {
  require_venv
  "$VENV_PY" -c "from app.wakeup_schedule import apply_wakeup_schedule; apply_wakeup_schedule({'enabled': False})"
  log "Đã gỡ lịch cron & macOS LaunchAgent của crashpilot-ai."
}

cmd_cron_status() {
  require_venv
  "$VENV_PY" -c "from app.wakeup_schedule import get_wakeup_status
st = get_wakeup_status()
cfg = st['config']
print('=== Lịch thức dậy & Cron hiện tại ===')
print(f\"Trạng thái : {'ĐANG BẬT' if st['scheduler_active'] else 'ĐANG TẮT'}\")
print(f\"Chế độ     : {cfg['schedule_mode']} (giờ hằng ngày: {cfg['daily_time']}, cron: {cfg['cron_expr']})\")
print(f\"Hành động  : {cfg['action']}\")
if st['launchd_supported']:
    print(f\"LaunchAgent: {'ĐÃ NẠP (ACTIVE)' if st['launchd_loaded'] else ('ĐÃ TẠO FILE' if st['launchd_installed'] else 'CHƯA CÀI')} ({st['launchd_plist_path']})\")
print(f\"Crontab    : {st['crontab_line'] or '(chưa ghi hoặc dùng LaunchAgent macOS)'}\")
if st['launchd_supported']:
    print(f\"Gợi ý đánh thức phần cứng từ Sleep (tùy chọn): {st['pmset_wake_command']}\")
"
  echo ""
  echo "=== 20 dòng log gần nhất ($LOG_FILE) ==="
  tail -n 20 "$LOG_FILE" 2>/dev/null || echo "(chưa có log)"
}

cmd_logs() { touch "$LOG_FILE"; tail -f "$LOG_FILE"; }

usage() { sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; }

command="${1:-start}"
shift || true
case "$command" in
  start)          cmd_start ;;
  stop)           cmd_stop ;;
  setup)          cmd_setup ;;
  login)          cmd_login ;;
  sync)           cmd_sync "$@" ;;
  cycle)          cmd_cycle "$@" ;;
  startup-cycle)  cmd_cycle --startup 3>&1 >>"$LOG_FILE" 2>&1 ;;
  wakeup-run)     cmd_wakeup_run ;;
  wakeup-install) cmd_wakeup_install "$@" ;;
  wakeup-remove)  cmd_cron_remove ;;
  wakeup-status)  cmd_cron_status ;;
  morning-audit)  require_venv; "$VENV_PY" -m app.main morning-audit "$@" ;;
  retry-task|retry-step) require_venv; "$VENV_PY" -m app.main retry-task "$@" ;;
  collect)        require_venv; "$VENV_PY" -m app.main collect "$@" ;;
  clusters)       require_venv; "$VENV_PY" -m app.main clusters "$@" ;;
  execute)        require_venv; "$VENV_PY" -m app.main execute "$@" ;;
  serve)          cmd_serve ;;
  all)            cmd_all "$@" ;;
  test)           cmd_test ;;
  doctor)         require_venv; "$VENV_PY" -m app.main doctor ;;
  cron-install)   cmd_cron_install "$@" ;;
  cron-remove)    cmd_cron_remove ;;
  cron-status)    cmd_cron_status ;;
  logs)           cmd_logs ;;
  *)              usage ;;
esac
