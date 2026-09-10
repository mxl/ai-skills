#!/bin/sh

set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/common.sh"

module_name=cloud
status=COMPLETE
found=0
index=0
display_home='~'
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

emit_cloud_root() {
  provider_kind=$1
  target_path=$2
  sync_state=$3
  eviction_method=$4
  display_path=${5-$target_path}

  [ -e "$target_path" ] || return 0
  found=$((found + 1))
  index=$((index + 1))

  # Root aggregates remain unknown; bounded guarded item evidence is separate.
  allocated=null
  logical=null
  item_status=DISCOVERED
  size_relation=unknown

  domain_label="$provider_kind account $index"
  printf '{"schema_version":"1","record_type":"cloud_root","module":"cloud","status":%s,"path":%s,"owner":%s,"provider_kind":%s,"domain_label":%s,"allocated_bytes":%s,"logical_bytes":%s,"sync_state":%s,"local_state":"unknown","size_relation":%s,"eviction_method":%s,"classification":"AMBIGUOUS","proposed_action":"verify item-level sync and eviction semantics in the provider UI","recovery":"remote download if upload is complete","risk":"offline access is lost; namespace deletion may delete the remote object","confidence":"low","sensitive":true}\n' \
    "$(json_string "$item_status")" "$(json_string "$display_path")" "$(json_string "$provider_kind")" "$(json_string "$provider_kind")" "$(json_string "$domain_label")" "$allocated" "$logical" "$(json_string "$sync_state")" "$(json_string "$size_relation")" "$(json_string "$eviction_method")"
  if ! PYTHONDONTWRITEBYTECODE=1 python3 "$SCRIPT_DIR/cloud_metadata_collect.py" "$target_path" "$provider_kind" "$index"; then
    record_failure "bounded item metadata worker did not complete"
  fi
}

icloud_root="${HOME:-}/Library/Mobile Documents/com~apple~CloudDocs"
icloud_sync=unknown
if [ -d "$icloud_root" ] && command -v brctl >/dev/null 2>&1; then
  icloud_status=$(brctl status com.apple.CloudDocs 2>/dev/null)
  brctl_result=$?
  if [ "$brctl_result" -ne 0 ]; then
    record_failure "iCloud sync status was unavailable"
  elif printf '%s' "$icloud_status" | /usr/bin/grep -q 'needs-sync'; then
    icloud_sync=needs-sync
  elif printf '%s' "$icloud_status" | /usr/bin/grep -q 'caught-up'; then
    icloud_sync=caught-up
  fi
fi
emit_cloud_root icloud-drive "$icloud_root" "$icloud_sync" "Finder Remove Download or Optimize Mac Storage" "$display_home/Library/Mobile Documents/com~apple~CloudDocs"

cloud_storage="${HOME:-}/Library/CloudStorage"
if [ -d "$cloud_storage" ]; then
  if /bin/ls -1A "$cloud_storage" >/dev/null 2>&1; then
    for target_path in "$cloud_storage"/* "$cloud_storage"/.[!.]* "$cloud_storage"/..?*; do
      [ -d "$target_path" ] || continue
      base_name=$(basename "$target_path")
      case "$base_name" in
        GoogleDrive-*) provider_kind=google-drive ;;
        Dropbox*) provider_kind=dropbox ;;
        OneDrive*) provider_kind=onedrive ;;
        *Yandex*) provider_kind=yandex-disk ;;
        *) provider_kind=unknown-file-provider ;;
      esac
      display_path="$display_home/Library/CloudStorage/<$provider_kind-domain-$((index + 1))>"
      emit_cloud_root "$provider_kind" "$target_path" unknown "Provider UI or Finder action; verify semantics first" "$display_path"
    done
  else
    record_failure "CloudStorage provider domains could not be listed"
  fi
fi

mobile_documents="${HOME:-}/Library/Mobile Documents"
if [ -d "$mobile_documents" ]; then
  container_paths=$(/usr/bin/find -x "$mobile_documents" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
  find_result=$?
  if [ "$find_result" -eq 0 ]; then
    container_count=$(printf '%s\n' "$container_paths" | /usr/bin/awk 'NF {count++} END {print count+0}')
    container_status=DISCOVERED
    container_evidence="Container names withheld; measure exact user-visible containers only when needed"
  else
    record_failure "iCloud container inventory could not be listed"
    container_count=null
    container_status=PARTIAL
    container_evidence="iCloud container inventory could not be completed"
  fi
  printf '{"schema_version":"1","record_type":"cloud_container_inventory","module":"cloud","status":%s,"path":"~/Library/Mobile Documents/<containers>","owner":"iCloud","container_count":%s,"classification":"REVIEW-ONLY","evidence":%s,"sensitive":true}\n' \
    "$(json_string "$container_status")" "$container_count" "$(json_string "$container_evidence")"
fi

for target_path in "${HOME:-}/Dropbox" "${HOME:-}/OneDrive" "${HOME:-}/Google Drive" "${HOME:-}/Yandex.Disk" "${HOME:-}/Yandex.Disk.localized"; do
  [ -e "$target_path" ] || continue
  [ -L "$target_path" ] && continue
  case "$(basename "$target_path")" in
    Dropbox) provider_kind=dropbox ;;
    OneDrive) provider_kind=onedrive ;;
    "Google Drive") provider_kind=google-drive ;;
    *) provider_kind=yandex-disk ;;
  esac
  display_path="$display_home/${target_path#"${HOME:-}"/}"
  emit_cloud_root "$provider_kind" "$target_path" unknown "Provider UI; verify remote sync before local cleanup" "$display_path"
done

if [ "$found" -eq 0 ]; then
  evidence="No supported cloud roots discovered"
else
  status=PARTIAL
  evidence="Discovered $found cloud root(s); bounded guarded item metadata attempted automatically; provider sync, pinning and eviction remain unverified; account identifiers redacted"
fi
if [ -n "$failure_evidence" ]; then
  evidence="$evidence; incomplete scope: $failure_evidence"
fi
emit_module_status "$module_name" "$status" "$evidence"
