#!/usr/bin/env bash
# Publishes a digest to Telegram: first the DM, then the channel, identical content.
# For each destination: sendDocument (the .md file + caption from TL;DR), then the
# digest as sendRichMessage (split on section/post boundaries only if it is too long).
# Fallback: sendMessage with parse_mode=HTML in <=4000-char chunks.
#
# Usage (from the repo root):
#   ./scripts/publish-telegram.sh digests/ai-it/YYYY-MM-DD.md            # publish
#   ./scripts/publish-telegram.sh digests/ai-it/YYYY-MM-DD.md --dry-run  # no network
#   ./scripts/publish-telegram.sh digests/ai-it/YYYY-MM-DD.md --dm-only  # DM only
#
# Env: TELEGRAM_BOT_TOKEN, TELEGRAM_DM_CHAT_ID, TELEGRAM_CHANNEL_ID.
# Never prints the token. Logs every response. Exit 0 = everything sent, 1 = some step failed.

set -u

DIGEST_FILE="" DRY=0 DM_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --dm-only) DM_ONLY=1 ;;
    *) DIGEST_FILE="$arg" ;;
  esac
done
if [ -z "$DIGEST_FILE" ] || [ ! -f "$DIGEST_FILE" ]; then
  echo "usage: $0 <digest.md> [--dry-run] [--dm-only]" >&2
  exit 2
fi
for tool in curl jq python3; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done
if [ "$DRY" = 0 ]; then
  for v in TELEGRAM_BOT_TOKEN TELEGRAM_DM_CHAT_ID TELEGRAM_CHANNEL_ID; do
    [ -n "${!v:-}" ] || { echo "missing env: $v" >&2; exit 2; }
  done
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
python3 -I "$SCRIPT_DIR/tg_prepare.py" "$DIGEST_FILE" "$WORK" || { echo "prepare failed" >&2; exit 2; }

CAPTION="$(cat "$WORK/caption.txt")"
NPARTS="$(jq 'length' "$WORK/parts.json")"
echo "digest: $DIGEST_FILE, rich parts: $NPARTS"
echo "caption: $CAPTION"

if [ "$DRY" = 1 ]; then
  for ((p = 0; p < NPARTS; p++)); do
    echo "part $((p + 1)): $(jq -r ".[$p] | length" "$WORK/parts.json") chars," \
         "fallback chunks: $(jq ".[$p] | length" "$WORK/fallback.json")"
  done
  echo "RESULT: dry run, nothing sent"
  exit 0
fi

API="https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN"
RICH_OK=1
FAILS=0

ok_of() { printf '%s' "$1" | jq -r 'if type == "object" then (.ok | tostring) else "false" end' 2>/dev/null || echo "false"; }

# $1 = label, $2 = response body. Returns 0 if ok:true.
check() {
  local label="$1" resp="$2"
  if [ -z "$resp" ]; then
    echo "[$label] network error (empty response)"
    return 1
  fi
  if [ "$(ok_of "$resp")" = "true" ]; then
    echo "[$label] ok message_id=$(printf '%s' "$resp" | jq -r '.result.message_id // "?"' 2>/dev/null)"
    return 0
  fi
  echo "[$label] FAILED: $resp"
  return 1
}

send_document() {
  curl -s -X POST "$API/sendDocument" \
    --form-string "chat_id=$1" \
    -F "document=@$DIGEST_FILE" \
    --form-string "caption=$CAPTION"
}

send_rich() { # $1 dest, $2 part index
  jq -c --arg chat "$1" --argjson i "$2" \
    '{chat_id: $chat, rich_message: {markdown: .[$i]}}' "$WORK/parts.json" \
    | curl -s -X POST "$API/sendRichMessage" -H "Content-Type: application/json" --data-binary @-
}

send_html_part() { # $1 dest, $2 part index, $3 label
  local n text resp
  n="$(jq ".[$2] | length" "$WORK/fallback.json")"
  for ((c = 0; c < n; c++)); do
    text="$(jq -r ".[$2][$c]" "$WORK/fallback.json")"
    resp="$(curl -s -X POST "$API/sendMessage" \
      --data-urlencode "chat_id=$1" \
      --data-urlencode "text=$text" \
      --data-urlencode "parse_mode=HTML" \
      --data-urlencode "disable_web_page_preview=true")"
    check "$3 fallback chunk $((c + 1))/$n" "$resp" || FAILS=$((FAILS + 1))
    sleep 1
  done
}

DESTS=("DM:$TELEGRAM_DM_CHAT_ID")
[ "$DM_ONLY" = 0 ] && DESTS+=("CHANNEL:$TELEGRAM_CHANNEL_ID")

for entry in "${DESTS[@]}"; do
  name="${entry%%:*}"
  dest="${entry#*:}"

  resp="$(send_document "$dest")"
  if ! check "$name sendDocument" "$resp"; then
    if [ -z "$resp" ]; then sleep 3; resp="$(send_document "$dest")"; check "$name sendDocument retry" "$resp" || FAILS=$((FAILS + 1)); else FAILS=$((FAILS + 1)); fi
  fi
  sleep 1

  for ((p = 0; p < NPARTS; p++)); do
    label="$name part $((p + 1))/$NPARTS"
    if [ "$RICH_OK" = 1 ]; then
      resp="$(send_rich "$dest" "$p")"
      if [ -z "$resp" ]; then
        echo "[$label sendRichMessage] network error (empty response), retrying once"
        sleep 3
        resp="$(send_rich "$dest" "$p")"
      fi
      if check "$label sendRichMessage" "$resp"; then
        sleep 1
        continue
      fi
      if [ -z "$resp" ]; then
        FAILS=$((FAILS + 1))
        sleep 1
        continue
      fi
      if printf '%s' "$resp" | grep -qiE 'method not found|unknown method|"error_code":404'; then
        echo "[$label] Bot API has no sendRichMessage: switching to sendMessage/HTML for the rest of this run"
        RICH_OK=0
      fi
    fi
    send_html_part "$dest" "$p" "$label"
    sleep 1
  done
done

if [ "$FAILS" = 0 ]; then
  echo "RESULT: ok"
  exit 0
fi
echo "RESULT: $FAILS step(s) failed, see log above"
exit 1
