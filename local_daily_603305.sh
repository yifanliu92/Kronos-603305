#!/bin/zsh
# local_daily_603305.sh
# Runs auto_report_guard_603305.py directly (no LLM dependency) and pushes result to Telegram.
# This bypasses the OpenClaw cron model-call bottleneck.
#
# Installed by: OpenClaw (PacinoAI) 2026-06-08
#
# Usage: installed via crontab for every-10-min intraday
#
# Guard: only run on weekdays (Mon=1 .. Fri=5)
dow=$(date '+%u')
if [[ $dow -gt 5 ]]; then
  echo "[GUARD] weekend (day=$dow), exiting"
  exit 0
fi

# Environment:
export PATH="$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$HOME/Desktop/Kronos" || exit 1

# Config
CREDENTIALS_FILE="$HOME/.config/kronos/telegram.env"

if [[ ! -r "$CREDENTIALS_FILE" ]]; then
  echo "[CONFIG_ERROR] missing credentials file: $CREDENTIALS_FILE" >&2
  exit 1
fi

source "$CREDENTIALS_FILE"

if [[ -z "${BOT_TOKEN:-}" || -z "${CHAT_ID:-}" ]]; then
  echo "[CONFIG_ERROR] BOT_TOKEN or CHAT_ID missing" >&2
  exit 1
fi

LOG_DIR="$HOME/Desktop/Kronos/local_cron_logs"

mkdir -p "$LOG_DIR"

ts=$(date "+%Y%m%d_%H%M%S")
log="$LOG_DIR/local_daily_603305_${ts}.log"

{
    echo "===== local_daily_603305 start $(date '+%Y-%m-%d %H:%M:%S') ====="
    
    # Run the script
    output=$(python3 auto_report_guard_603305.py 2>&1)
    rc=$?
    echo "$output"
    echo "===== local_daily_603305 end rc=$rc ====="
    
    if [[ $rc -eq 0 ]]; then
        # Check if output contains IDEMPOTENT_SKIP — if slot already done, skip TG push
        if echo "$output" | grep -q '\[IDEMPOTENT_SKIP\]'; then
            echo "[SKIP_TG_PUSH] slot already generated in this minute"
        else
            # Send to Telegram via Bot API
            # URL-encode the output for Telegram
            encoded=$(echo "$output" | python3 -c "
import sys, urllib.parse
text = sys.stdin.read().strip()
if text:
    print(urllib.parse.quote(text))
" 2>/dev/null)
            if [[ -n "$encoded" ]]; then
                echo "[TG_PUSH] sending to Telegram..."
                tg_response=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
                    -d "chat_id=${CHAT_ID}" \
                    -d "text=${encoded}" \
                    -w "\n%{http_code}" 2>&1)
                echo "[TG_PUSH] response: $tg_response"
            else
                echo "[TG_PUSH_ERROR] url-encode failed, output empty"
            fi
        fi
    else
        echo "[ERROR] script failed with rc=$rc"
        # Send failure notification
        err_msg="⚠️ 603305 本地脚本运行失败（RC=${rc}）%0A时间：$(date '+%Y-%m-%d %H:%M:%S')%0A请检查日志：${log}"
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" \
            -d "text=${err_msg}" >/dev/null 2>&1
    fi
} >> "$log" 2>&1

exit $rc
