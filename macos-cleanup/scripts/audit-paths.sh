#!/bin/sh

set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/common.sh"

module_name=paths
status=COMPLETE
exact_paths=0
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

if [ "${1-}" = "--exact" ]; then
  exact_paths=1
  shift
fi

if [ "$#" -eq 0 ]; then
  emit_module_status "$module_name" FAILED "Pass one or more exact paths to measure"
  exit 2
fi

for target_path in "$@"; do
  if [ ! -e "$target_path" ]; then
    display_path=$(redact_home_path "$target_path")
    parent_path=$(dirname -- "$target_path")
    if [ -L "$target_path" ]; then
      item_status=PARTIAL
      record_failure "path $display_path has an unresolved symlink referent"
    elif [ -d "$parent_path" ] && [ -x "$parent_path" ] && [ -r "$parent_path" ]; then
      item_status=NOT-FOUND
    else
      item_status=PARTIAL
      record_failure "path $display_path absence could not be verified"
    fi
    printf '{"schema_version":"1","record_type":"path","module":"paths","status":%s,"path":%s,"allocated_bytes":null,"logical_bytes":null,"volume_device":null,"sensitive":true}\n' "$(json_string "$item_status")" "$(json_string "$display_path")"
    continue
  fi

  allocated=$(allocated_bytes "$target_path" || true)
  logical=$(logical_bytes "$target_path" || true)
  if [ -z "$allocated" ] || [ -z "$logical" ]; then
    if [ ! -r "$target_path" ] || { [ -d "$target_path" ] && [ ! -x "$target_path" ]; }; then
      item_status=PERMISSION-DENIED
    else
      item_status=PARTIAL
    fi
  else
    item_status=MEASURED
  fi

  if [ "$exact_paths" -eq 1 ]; then
    display_path=$(redact_home_path "$target_path")
  elif [ -d "$target_path" ]; then
    home_relative=${target_path#"${HOME:-}"/}
    if [ "$home_relative" != "$target_path" ] && [ "${home_relative#*/}" != "$home_relative" ]; then
      display_home='<home>'
      display_path="$display_home/${home_relative%%/*}/<redacted>"
    else
      display_path=$(redact_home_path "$target_path")
    fi
  else
    parent_path=$(dirname "$target_path")
    display_path="$(redact_home_path "$parent_path")/<redacted-item>"
  fi
  volume_device=$(volume_device_for_path "$target_path" || true)
  if [ "$item_status" != MEASURED ]; then
    record_failure "path $display_path could not be fully measured"
  fi

  printf '{"schema_version":"1","record_type":"path","module":"paths","status":%s,"path":%s,"allocated_bytes":%s,"logical_bytes":%s,"volume_device":%s,"sensitive":true}\n' \
    "$(json_string "$item_status")" "$(json_string "$display_path")" "${allocated:-null}" "${logical:-null}" "$(json_string "$volume_device")"
done

evidence="Requested paths measured without crossing mounted filesystems; output paths redacted by default"
if [ -n "$failure_evidence" ]; then
  evidence="$evidence; incomplete scope: $failure_evidence"
fi
emit_module_status "$module_name" "$status" "$evidence"
