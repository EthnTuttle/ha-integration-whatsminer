#!/usr/bin/env bash
#
# Capture a PID tuning run from Home Assistant for offline analysis.
#
# Usage:
#   HA='http://homeassistant.local:8123' TOKEN='...' \
#     ./scripts/pid-capture.sh [MINUTES] [EXTERNAL_SENSOR]
#
# Args:
#   MINUTES          lookback window in minutes (default 45)
#   EXTERNAL_SENSOR  entity_id of the external temp sensor the PID is targeting
#                    (default: auto-discover first non-miner temperature sensor)
#
# Env vars:
#   HA       Home Assistant base URL (required)
#   TOKEN    long-lived access token  (required)
#   SLUG     miner entity slug        (default: heatcore)
#
# Output:
#   pid-run-YYYYMMDD-HHMMSS.json with the history payload, plus a human
#   summary on stdout.

set -euo pipefail

: "${HA:?set HA to your Home Assistant base URL}"
: "${TOKEN:?set TOKEN to a long-lived access token}"

SLUG="${SLUG:-heatcore}"
MINUTES="${1:-45}"
EXT_SENSOR="${2:-}"

START=$(date -u -d "${MINUTES} minutes ago" +%Y-%m-%dT%H:%M:%S+00:00)
END=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

if [[ -z "$EXT_SENSOR" ]]; then
  EXT_SENSOR=$(
    curl -sfH "Authorization: Bearer $TOKEN" "$HA/api/states" \
    | python3 -c "
import sys, json
slug = '$SLUG'
cands = [s['entity_id'] for s in json.load(sys.stdin)
         if s.get('attributes', {}).get('device_class') == 'temperature'
         and slug not in s['entity_id']]
print(cands[0] if cands else '')
"
  )
fi

ENT="sensor.${SLUG}_power_limit"
ENT+=",sensor.${SLUG}_power_consumption"
ENT+=",sensor.${SLUG}_temperature"
ENT+=",sensor.${SLUG}_pid_error"
ENT+=",sensor.${SLUG}_pid_proportional"
ENT+=",sensor.${SLUG}_pid_integral"
ENT+=",sensor.${SLUG}_pid_derivative"
ENT+=",sensor.${SLUG}_pid_requested_output"
ENT+=",sensor.${SLUG}_pid_output"
ENT+=",sensor.${SLUG}_pid_target_temperature"
ENT+=",binary_sensor.${SLUG}_pid_safety_engaged"
ENT+=",binary_sensor.${SLUG}_mining_status"
[[ -n "$EXT_SENSOR" ]] && ENT+=",$EXT_SENSOR"

OUT="pid-run-$(date +%Y%m%d-%H%M%S).json"

echo "Window:   $START  →  $END  (${MINUTES} min)"
echo "Miner:    $SLUG"
echo "External: ${EXT_SENSOR:-<none — loop may not be targeting an external sensor>}"
echo "Writing:  $OUT"

curl -sfH "Authorization: Bearer $TOKEN" \
  "$HA/api/history/period/$START?end_time=$END&filter_entity_id=$ENT&minimal_response" \
  > "$OUT"

SIZE=$(wc -c < "$OUT")
SAMPLES=$(python3 -c "import json; d=json.load(open('$OUT')); print(sum(len(x) for x in d))")
SERIES=$(python3 -c "import json; d=json.load(open('$OUT')); print(len(d))")

echo ""
echo "Captured: $SERIES series, $SAMPLES total samples, $SIZE bytes"
echo ""
echo "Next: paste $OUT into chat along with:"
echo "  - current Kp / Ki / Kd / target-temp"
echo "  - min-power-step / min-adjust-interval"
echo "  - any interesting event times in the window"
