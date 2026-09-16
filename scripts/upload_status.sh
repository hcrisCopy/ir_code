#!/usr/bin/env bash
# =============================================================================
# upload_status.sh — 查看 push_to_remote.sh 的实时进度（只读，不干扰正在跑的传输）
#
# 用途：push_to_remote.sh 在 PowerShell 里跑时，进度屏只能用纯文本模式；
#       如果你想在**另一个窗口**盯着看，跑这个就行，它只读日志、不做任何写入。
#
# 用法（PowerShell 里）：
#   & "C:\ProgrammingEnv\msys64\usr\bin\bash.exe" ir_code/scripts/upload_status.sh
#
# 加 --watch 每 20 秒刷新一次（Ctrl-C 退出）：
#   & "C:\ProgrammingEnv\msys64\usr\bin\bash.exe" ir_code/scripts/upload_status.sh --watch
# =============================================================================

export PATH="/usr/bin:$PATH"
export MSYS_NO_PATHCONV=1

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_DATA="$(cd "$SCRIPT_DIR/../.." && pwd)/ir_data"
LOG_DIR="$LOCAL_DATA/outputs/cache/upload_logs"

WATCH=0
[ "${1:-}" = "--watch" ] && WATCH=1

fmt_bytes() {
  awk -v b="${1:-0}" 'BEGIN{
    split("B KiB MiB GiB TiB",u," ");
    i=1; while (b>=1024 && i<5) { b/=1024; i++ }
    if (i==1) printf "%d B", b; else printf "%.2f %s", b, u[i]
  }'
}

# 取日志里最后一条"像进度"的行（rsync 尾行是 --stats，不能当进度用）
last_progress() {
  local f="$1"
  [ -s "$f" ] || return 0
  tr '\r' '\n' < "$f" 2>/dev/null | awk '/[0-9]%[ \t]/ { l = $0 } END { print l }'
}

# 判断某个 state/log 文件是否属于"本次运行"。
# 起跑线文件不存在时（任务由没有 .run-start 的老版本启动），返回真、按原样显示。
is_current() {
  local f="$1" stamp="$2"
  [ -f "$stamp" ] || return 0
  [ "$f" -nt "$stamp" ] || [ "$f" -ef "$stamp" ]
}

# 没有 .run-start 时，用"正在跑/已完成的任务里最早的那个状态文件的 mtime"当替代起跑线。
# 依据：这些状态只可能由本次运行写出，所以早于它们的状态必然是上一次运行留下的。
derive_stamp() {
  local stamp="$LOG_DIR/.run-start" f ref=""
  [ -f "$stamp" ] && { echo "$stamp"; return; }
  for f in "$LOG_DIR"/*.state; do
    [ -e "$f" ] || continue
    case "$(cat "$f" 2>/dev/null)" in
      RUNNING|DONE)
        if [ -z "$ref" ] || [ "$f" -ot "$ref" ]; then ref="$f"; fi ;;
    esac
  done
  echo "$ref"
}

print_once() {
  if [ ! -d "$LOG_DIR" ]; then
    echo "还没有任何上传日志：$LOG_DIR"
    echo "（说明 push_to_remote.sh 还没跑过）"
    return
  fi
  local stamp; stamp="$(derive_stamp)"
  local any=0
  printf '%-30s %-10s %5s %12s %10s\n' "数据集" "状态" "进度" "速度" "剩余"
  printf '%-30s %-10s %5s %12s %10s\n' "------------------------------" "----------" "-----" "------------" "----------"
  local st mark pct spd eta
  for f in "$LOG_DIR"/*.state; do
    [ -e "$f" ] || continue
    local name; name="$(basename "$f" .state)"
    if is_current "$f" "$stamp"; then
      st="$(cat "$f")"
    else
      st="STALE"
    fi
    case "$st" in
      DONE)     mark="完成" ;;
      RUNNING)  mark="传输中" ;;
      FAIL:*)   mark="失败${st#FAIL:}" ;;
      STALE)    mark="-" ;;
      *)        mark="$st" ;;
    esac
    pct="-"; spd="-"; eta="-"
    local log="$LOG_DIR/$name.log"
    if [ "$st" != "STALE" ] && [ -f "$log" ] && is_current "$log" "$stamp"; then
      local line; line="$(last_progress "$log")"
      if [ -n "$line" ]; then
        pct="$(printf '%s' "$line" | grep -oE '[0-9]{1,3}%' | head -1 | tr -d '%')"
        spd="$(printf '%s' "$line" | grep -oE '[0-9.]+[kKMGTP]?B/s' | head -1)"
        eta="$(printf '%s' "$line" | grep -oE '[0-9]+:[0-9]{2}(:[0-9]{2})?' | tail -1)"
      fi
    fi
    [ "$st" = "DONE" ] && pct=100
    local pct_disp="-"
    case "$pct" in ''|'-') pct_disp="-" ;; *) pct_disp="${pct}%" ;; esac
    printf '%-30s %-10s %5s %12s %10s\n' "$name" "$mark" "$pct_disp" "${spd}" "${eta}"
    any=1
  done
  [ "$any" = "0" ] && echo "(还没有状态文件)"

  echo
  local running; running="$(grep -l '^RUNNING' "$LOG_DIR"/*.state 2>/dev/null | wc -l)"
  echo "正在传输：$running 个   日志目录：$LOG_DIR"
  echo "日志文件最后更新时间（说明还在写）："
  ls -lt --time-style=+%H:%M:%S "$LOG_DIR"/*.log 2>/dev/null | head -4 | awk '{printf "    %s  %s\n", $6, $7}'
}

if [ "$WATCH" = "1" ]; then
  while :; do
    printf '\033[H\033[2J'
    echo "== ir_data 上传进度  $(date '+%H:%M:%S') =="
    echo
    print_once
    sleep 20
  done
else
  print_once
fi
