#!/bin/sh

set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/common.sh"

module_name=caches
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

emit_cache_candidate() {
  owner=$1
  target_path=$2
  review_action=$3
  recovery=$4

  case "$target_path" in
    /*) ;;
    *) record_failure "$owner returned a non-absolute cache path"; return 0 ;;
  esac
  while [ "$target_path" != / ] && [ "${target_path%/}" != "$target_path" ]; do
    target_path=${target_path%/}
  done
  case "$target_path" in
    /|*/.|*/..|*/./*|*/../*|*//*) record_failure "$owner returned an unsafe cache root"; return 0 ;;
  esac
  probe=$target_path
  while [ "$probe" != / ]; do
    if [ -L "$probe" ]; then
      # Darwin's root-owned aliases are not user-configured redirections.
      link_target=$(/usr/bin/readlink "$probe" 2>/dev/null || true)
      case "$probe:$link_target" in
        /var:private/var|/var:/private/var|/tmp:private/tmp|/tmp:/private/tmp|/etc:private/etc|/etc:/private/etc) ;;
        *) record_failure "$owner cache root has a symlink component"; return 0 ;;
      esac
    fi
    probe=$(dirname -- "$probe")
  done
  if [ ! -d "$target_path" ] || [ ! -x "$target_path" ]; then
    record_failure "$owner configured cache directory is absent or inaccessible"
    return 0
  fi
  physical_root=$(CDPATH='' cd -P -- "$target_path" 2>/dev/null && pwd -P) || {
    record_failure "$owner configured cache directory could not be resolved"
    return 0
  }
  physical_home=$(CDPATH='' cd -P -- "${HOME:-/}" 2>/dev/null && pwd -P) || {
    record_failure "Home directory identity unavailable"
    return 0
  }
  # Firmlink aliases may have different physical path strings but the same object.
  root_identity=$(/usr/bin/stat -f '%d:%i' "$physical_root" 2>/dev/null) || {
    record_failure "$owner cache root identity unavailable"
    return 0
  }
  home_identity=$(/usr/bin/stat -f '%d:%i' "$physical_home" 2>/dev/null) || {
    record_failure "Home directory identity unavailable"
    return 0
  }
  if [ "$physical_root" = / ] || [ "$physical_root" = "$physical_home" ] || [ "$root_identity" = "$home_identity" ]; then
    record_failure "$owner returned an unsafe cache root"
    return 0
  fi
  allocated=$(allocated_bytes "$target_path" || true)
  logical=$(logical_bytes "$target_path" || true)
  display_path=$(redact_home_path "$target_path")
  if [ -z "$allocated" ] || [ -z "$logical" ]; then
    record_failure "$owner cache could not be fully measured"
    item_status=PARTIAL
    confidence=low
  else
    item_status=MEASURED
    confidence=high
  fi

  printf '{"schema_version":"1","record_type":"cleanup_candidate","module":"caches","status":%s,"path":%s,"owner":%s,"allocated_bytes":%s,"logical_bytes":%s,"estimated_reclaimable_bytes":null,"classification":"REVIEW-ONLY","proposed_action":null,"review_action":%s,"owner_activity":"unknown","scope_state":"unverified","verification_state":"required","evidence":"Configured root measured; owner activity, installed-version cleanup semantics and action scope not verified","recovery":%s,"risk":"cleanup may discard shared or in-use entries; regeneration requires download or rebuild","confidence":%s,"actionable":false,"sensitive":true}\n' \
    "$(json_string "$item_status")" "$(json_string "$display_path")" "$(json_string "$owner")" "${allocated:-null}" "${logical:-null}" "$(json_string "$review_action")" "$(json_string "$recovery")" "$(json_string "$confidence")"
}

export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_AUTOREMOVE=1
for tool_name in brew npm pnpm go uv; do
  if ! command -v "$tool_name" >/dev/null 2>&1; then
    printf '{"schema_version":"1","record_type":"capability","module":"caches","status":"UNAVAILABLE","tool":%s,"path":null,"sensitive":false}\n' "$(json_string "$tool_name")"
    continue
  fi
  case "$tool_name" in
    brew) set -- brew --cache; owner=Homebrew; review_action="Preview installed-version cleanup with autoremove disabled; old kegs and logs are a separate scope"; recovery="redownload from configured taps; old versions may be unavailable" ;;
    npm) set -- npm config get cache; owner=npm; review_action="Inspect content cache separately from npx environments; verify is a mutation, not an audit"; recovery="redownload from configured registries; private access may be required" ;;
    pnpm) set -- pnpm store path; owner=pnpm; review_action="Verify supported shared-store prune scope and active projects"; recovery="redownload from configured registries; private access may be required" ;;
    go) set -- go env GOCACHE; owner=Go; review_action="Check go, gopls and editor writers; build cache is separate from module and fuzz state"; recovery="rebuild compilation cache" ;;
    uv) set -- uv cache dir; owner=uv; review_action="Prefer version-supported prune after lock and link-mode checks; disclose centralized environments"; recovery="redownload or rebuild; linked or centralized environments need separate review" ;;
  esac
  if configured_path=$("$@" 2>/dev/null) && [ -n "$configured_path" ]; then
    emit_cache_candidate "$owner" "$configured_path" "$review_action" "$recovery"
  else
    record_failure "$owner configured cache path unavailable"
  fi
done

evidence="Configured cache measurement only; all targets require ownership, activity, version and action-scope review; no executable actions produced"
if [ -n "$failure_evidence" ]; then
  evidence="$evidence; incomplete scope: $failure_evidence"
fi
emit_module_status "$module_name" "$status" "$evidence"
