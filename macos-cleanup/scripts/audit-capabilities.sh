#!/bin/sh

set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/common.sh"

module_name=capabilities
status=COMPLETE
failure_evidence=

record_failure() {
  failure_message=$1
  status=PARTIAL
  if [ -n "$failure_evidence" ]; then
    failure_evidence="$failure_evidence; $failure_message"
  else
    failure_evidence=$failure_message
  fi
}

target_path=${1:-${HOME:-/}}
if [ ! -e "$target_path" ]; then
  emit_module_status "$module_name" FAILED "Target path does not exist"
  exit 2
fi

target_volume=$(mount_point_for_path "$target_path" || true)
[ -n "$target_volume" ] || target_volume=$target_path

df_line=$(/bin/df -kP "$target_path" 2>/dev/null | /usr/bin/awk 'NR==2')
if [ -n "$df_line" ]; then
  total_blocks=$(printf '%s\n' "$df_line" | /usr/bin/awk '{print $2}')
  used_blocks=$(printf '%s\n' "$df_line" | /usr/bin/awk '{print $3}')
  available_blocks=$(printf '%s\n' "$df_line" | /usr/bin/awk '{print $4}')
  printf '{"schema_version":"1","record_type":"capacity","module":"capabilities","status":"COMPLETE","path":%s,"total_bytes":%s,"used_bytes":%s,"available_bytes":%s,"sensitive":false}\n' \
    "$(json_string "$(redact_home_path "$target_volume")")" "$((total_blocks * 1024))" "$((used_blocks * 1024))" "$((available_blocks * 1024))"
else
  record_failure "capacity could not be measured for the target path"
fi

for tool_name in diskutil tmutil fileproviderctl brctl brew docker xcodebuild xcrun avdmanager npm pnpm yarn bun uv python3 pipx poetry go cargo sqlite3 lsof; do
  tool_path=$(command -v "$tool_name" 2>/dev/null || true)
  if [ -n "$tool_path" ]; then
    tool_status=AVAILABLE
  else
    tool_status=UNAVAILABLE
  fi
  display_tool_path=$(redact_home_path "$tool_path")
  printf '{"schema_version":"1","record_type":"capability","module":"capabilities","status":%s,"tool":%s,"path":%s,"sensitive":true}\n' \
    "$(json_string "$tool_status")" "$(json_string "$tool_name")" "$(json_string "$display_tool_path")"
done

volume_device=$(volume_device_for_path "$target_path" || true)
if command -v diskutil >/dev/null 2>&1 && [ -z "$volume_device" ]; then
  record_failure "APFS volume identity could not be resolved"
fi

snapshot_pairs=
apfs_snapshot_status=UNAVAILABLE
if [ -n "$volume_device" ] && command -v diskutil >/dev/null 2>&1; then
  snapshot_output=$(diskutil apfs listSnapshots "$target_volume" 2>/dev/null)
  apfs_result=$?
  if [ "$apfs_result" -eq 0 ]; then
    apfs_snapshot_status=COMPLETE
    snapshot_pairs=$(printf '%s\n' "$snapshot_output" | /usr/bin/awk -F': *' '
    /^[[:space:]]*Name:/ {name=$2}
    /^[[:space:]]*Purgeable:/ {if (name != "") {print name "|" $2; name=""}}
    ')
  else
    apfs_snapshot_status=FAILED
    record_failure "APFS snapshot inventory failed for the target volume"
  fi
fi

if [ -n "$snapshot_pairs" ]; then
  printf '%s\n' "$snapshot_pairs" | while IFS='|' read -r snapshot_name purgeable; do
    case "$snapshot_name" in
      com.apple.TimeMachine.*) snapshot_owner="Time Machine"; proposed_action="supported tmutil deletion after exact approval"; risk="recovery point is permanently removed" ;;
      *) snapshot_owner="APFS"; proposed_action="supported OS maintenance only"; risk="snapshot deletion may affect recovery or system state" ;;
    esac
    printf '{"schema_version":"1","record_type":"snapshot","module":"capabilities","status":"DISCOVERED","owner":%s,"path":null,"snapshot_name":%s,"volume_device":%s,"purgeable":%s,"classification":"REVIEW-CANDIDATE","proposed_action":%s,"recovery":"snapshot history is reduced","risk":%s,"confidence":"high","sensitive":false}\n' \
      "$(json_string "$snapshot_owner")" "$(json_string "$snapshot_name")" "$(json_string "$volume_device")" "$(json_string "$purgeable")" "$(json_string "$proposed_action")" "$(json_string "$risk")"
  done
elif [ "$apfs_snapshot_status" != COMPLETE ] && command -v tmutil >/dev/null 2>&1; then
  snapshots=$(tmutil listlocalsnapshots "$target_volume" 2>/dev/null)
  tmutil_result=$?
  if [ "$tmutil_result" -eq 0 ]; then
    printf '%s\n' "$snapshots" | /usr/bin/awk 'NF {print}' | while IFS= read -r snapshot_line; do
      case "$snapshot_line" in
        Snapshots*) continue ;;
      esac
      printf '{"schema_version":"1","record_type":"snapshot","module":"capabilities","status":"DISCOVERED","owner":"Time Machine","path":null,"snapshot_name":%s,"volume_device":%s,"purgeable":null,"classification":"REVIEW-CANDIDATE","proposed_action":"supported tmutil deletion after exact approval","recovery":"backup history is reduced","risk":"recovery point is permanently removed","confidence":"high","sensitive":false}\n' \
        "$(json_string "$snapshot_line")" "$(json_string "$volume_device")"
    done
  else
    record_failure "Time Machine snapshot inventory failed for the target mount point"
  fi
fi

evidence="Runtime capability, target-volume, capacity, and snapshot discovery"
if [ -n "$failure_evidence" ]; then
  evidence="$evidence; incomplete scope: $failure_evidence"
fi
emit_module_status "$module_name" "$status" "$evidence"
