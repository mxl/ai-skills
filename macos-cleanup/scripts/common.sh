#!/bin/sh

json_escape() {
  printf '%s' "$1" | LC_ALL=C /usr/bin/od -An -v -tu1 | /usr/bin/awk '
    {
      for (i = 1; i <= NF; i++) {
        byte = $i + 0
        if (byte == 8) printf "\\b"
        else if (byte == 9) printf "\\t"
        else if (byte == 10) printf "\\n"
        else if (byte == 12) printf "\\f"
        else if (byte == 13) printf "\\r"
        else if (byte == 34) printf "\\\""
        else if (byte == 92) printf "\\\\"
        else if (byte < 32) printf "\\u%04x", byte
        else printf "%c", byte
      }
    }
  '
}

json_string() {
  if [ -z "${1+x}" ] || [ "$1" = "" ]; then
    printf 'null'
  else
    printf '"%s"' "$(json_escape "$1")"
  fi
}

allocated_bytes() {
  target_path=$1
  du_output=$(/usr/bin/du -x -sk "$target_path" 2>/dev/null) || return 1
  blocks=$(printf '%s\n' "$du_output" | /usr/bin/awk 'NR==1 {print $1}')
  [ -n "$blocks" ] || return 1
  printf '%s' "$((blocks * 1024))"
}

logical_bytes() {
  target_path=$1
  du_output=$(/usr/bin/du -x -A -k -s "$target_path" 2>/dev/null) || return 1
  blocks=$(printf '%s\n' "$du_output" | /usr/bin/awk 'NR==1 {print $1}')
  [ -n "$blocks" ] || return 1
  printf '%s' "$((blocks * 1024))"
}

redact_home_path() {
  target_path=$1
  redacted_home='<home>'
  case "$target_path" in
    "${HOME:-}") printf '%s' "$redacted_home" ;;
    "${HOME:-}"/*) printf '%s/%s' "$redacted_home" "${target_path#"${HOME:-}"/}" ;;
    *) printf '%s' "$target_path" ;;
  esac
}

volume_device_for_path() {
  target_path=$1
  command -v diskutil >/dev/null 2>&1 || return 1
  command -v plutil >/dev/null 2>&1 || return 1
  target_mount=$(mount_point_for_path "$target_path" || true)
  [ -n "$target_mount" ] || return 1
  diskutil info -plist "$target_mount" 2>/dev/null | plutil -extract DeviceIdentifier raw -o - - 2>/dev/null
}

mount_point_for_path() {
  target_path=$1
  if command -v diskutil >/dev/null 2>&1 && command -v plutil >/dev/null 2>&1; then
    diskutil info -plist "$target_path" 2>/dev/null | plutil -extract MountPoint raw -o - - 2>/dev/null && return 0
  fi
  /bin/df -P "$target_path" 2>/dev/null | /usr/bin/awk 'NR==2 {print $NF}'
}

emit_module_status() {
  module_name=$1
  module_status=$2
  module_evidence=${3-}
  printf '{"schema_version":"1","record_type":"module_status","module":%s,"status":%s,"evidence":%s}\n' \
    "$(json_string "$module_name")" "$(json_string "$module_status")" "$(json_string "$module_evidence")"
}
