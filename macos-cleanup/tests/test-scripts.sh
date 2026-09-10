#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SKILL_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/macos-cleanup-tests.XXXXXX")
trap 'rm -rf "$TEST_ROOT"' EXIT INT TERM

assert_jsonl() {
  output=$1
  printf '%s\n' "$output" | osascript -l JavaScript -e '
    ObjC.import("Foundation");
    var data = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
    var text = $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js;
    text.split("\n").filter(function (line) { return line.length > 0; })
      .forEach(function (line) { JSON.parse(line); });
  '
}

mkdir -p "$TEST_ROOT/home"
newline_name=$(printf 'line\nbreak')
newline_path="$TEST_ROOT/home/$newline_name"
: > "$newline_path"
space_unicode_path="$TEST_ROOT/home/space пример"
: > "$space_unicode_path"

path_output=$(HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-paths.sh" --exact "$newline_path" "$space_unicode_path")
assert_jsonl "$path_output"
path_records=$(printf '%s\n' "$path_output" | grep -c '"record_type"')
[ "$path_records" -eq 3 ]

empty_cloud_output=$(HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-cloud.sh")
assert_jsonl "$empty_cloud_output"
printf '%s\n' "$empty_cloud_output" | grep -q '"record_type":"module_status".*"status":"COMPLETE"'
if printf '%s\n' "$empty_cloud_output" | grep -q '"record_type":"cloud_root"'; then
  printf '%s\n' 'empty cloud inventory unexpectedly reported a provider' >&2
  exit 1
fi

mkdir -p "$TEST_ROOT/home/Library/CloudStorage/GoogleDrive-user@example.com"
cloud_output=$(HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-cloud.sh")
assert_jsonl "$cloud_output"
if printf '%s\n' "$cloud_output" | grep -q 'user@example.com'; then
  printf '%s\n' 'cloud account identifier was not redacted' >&2
  exit 1
fi
printf '%s\n' "$cloud_output" | grep -q '"record_type":"cloud_root".*"status":"DISCOVERED".*"allocated_bytes":null.*"logical_bytes":null.*"classification":"AMBIGUOUS"'
printf '%s\n' "$cloud_output" | grep -q '"record_type":"module_status".*"status":"PARTIAL".*bounded guarded item metadata attempted automatically'
printf '%s\n' "$cloud_output" | grep -q '"inventory_scope":"bounded-item-metadata"'

chmod 000 "$TEST_ROOT/home/Library/CloudStorage"
cloud_unreadable_output=$(HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-cloud.sh")
chmod 700 "$TEST_ROOT/home/Library/CloudStorage"
assert_jsonl "$cloud_unreadable_output"
printf '%s\n' "$cloud_unreadable_output" | grep -q '"record_type":"module_status".*"status":"PARTIAL".*CloudStorage provider domains could not be listed'

mkdir -p "$TEST_ROOT/home/Library/Mobile Documents/com~apple~CloudDocs"
cloud_partial_output=$(PATH="$SKILL_DIR/tests/fixtures:$PATH" HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-cloud.sh")
assert_jsonl "$cloud_partial_output"
printf '%s\n' "$cloud_partial_output" | grep -q '"record_type":"module_status".*"status":"PARTIAL".*iCloud sync status was unavailable'

diskutil_log="$TEST_ROOT/diskutil.log"
capability_output=$(PATH="$SKILL_DIR/tests/fixtures:$PATH" DISKUTIL_LOG="$diskutil_log" HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-capabilities.sh" "$TEST_ROOT/home")
assert_jsonl "$capability_output"
grep -q '^apfs listSnapshots /TestVolume$' "$diskutil_log"
printf '%s\n' "$capability_output" | grep -q '"record_type":"snapshot".*"volume_device":"disk-test"'

capability_output=$(HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-capabilities.sh" "$TEST_ROOT/home")
assert_jsonl "$capability_output"
capacity_records=$(printf '%s\n' "$capability_output" | grep -c '"record_type":"capacity"')
[ "$capacity_records" -eq 1 ]
printf '%s\n' "$capability_output" | grep -q '"record_type":"capacity".*"sensitive":false'

mkdir -p "$TEST_ROOT/home/private-path"
: > "$TEST_ROOT/home/private-path/item"
chmod 000 "$TEST_ROOT/home/private-path"
path_partial_output=$(HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-paths.sh" --exact "$TEST_ROOT/home/private-path")
chmod 700 "$TEST_ROOT/home/private-path"
assert_jsonl "$path_partial_output"
printf '%s\n' "$path_partial_output" | grep -q '"record_type":"path".*"status":"PERMISSION-DENIED"'
printf '%s\n' "$path_partial_output" | grep -q '"record_type":"module_status".*"status":"PARTIAL".*path .* could not be fully measured'

fixture_modules="$TEST_ROOT/modules"
mkdir -p "$fixture_modules"
for module_name in capabilities paths caches cloud; do
  ln -s "$SKILL_DIR/tests/fixtures/audit-module" "$fixture_modules/audit-$module_name.sh"
done

orchestrator_output="$TEST_ROOT/orchestrator-complete"
MACOS_CLEANUP_MODULE_DIR="$fixture_modules" HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-all.sh" --target "$TEST_ROOT/home" --output "$orchestrator_output" --timeout 2 >/dev/null
orchestrator_inventory=$(cat "$orchestrator_output/inventory.jsonl")
assert_jsonl "$orchestrator_inventory"
[ "$(printf '%s\n' "$orchestrator_inventory" | grep -c '"record_type":"orchestrator_module"')" -eq 4 ]
printf '%s\n' "$orchestrator_inventory" | grep -q '"record_type":"audit_summary".*"status":"PARTIAL".*"actionable_candidate_count":0.*"cloud_root_count":1.*"cloud_targets_actionable":false'
printf '%s\n' "$orchestrator_inventory" | grep -q '"child_module":"cloud".*"observed_status":"PARTIAL"'
grep -q '^## Review-Only Candidates$' "$orchestrator_output/report.md"
grep -q 'test cache clean' "$orchestrator_output/report.md"

cloud_only_output="$TEST_ROOT/orchestrator-cloud-only"
MACOS_CLEANUP_MODULE_DIR="$fixture_modules" HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-all.sh" --target "$TEST_ROOT/home" --output "$cloud_only_output" --timeout 2 --modules cloud >/dev/null
cloud_only_inventory=$(cat "$cloud_only_output/inventory.jsonl")
assert_jsonl "$cloud_only_inventory"
[ "$(printf '%s\n' "$cloud_only_inventory" | grep -c '"record_type":"orchestrator_module"')" -eq 1 ]
printf '%s\n' "$cloud_only_inventory" | grep -q '"record_type":"audit_summary".*"expected_modules":"cloud"'

nonzero_output="$TEST_ROOT/orchestrator-nonzero"
TEST_PATH_MODE=nonzero MACOS_CLEANUP_MODULE_DIR="$fixture_modules" HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-all.sh" --target "$TEST_ROOT/home" --output "$nonzero_output" --timeout 2 >/dev/null
nonzero_inventory=$(cat "$nonzero_output/inventory.jsonl")
assert_jsonl "$nonzero_inventory"
printf '%s\n' "$nonzero_inventory" | grep -q '"record_type":"path"'
printf '%s\n' "$nonzero_inventory" | grep -q '"record_type":"orchestrator_module".*"status":"PARTIAL".*"child_module":"paths".*"exit_code":3.*"final_status_seen":false.*"outcome":"nonzero-exit"'

timeout_output="$TEST_ROOT/orchestrator-timeout"
TEST_CACHE_MODE=timeout MACOS_CLEANUP_MODULE_DIR="$fixture_modules" HOME="$TEST_ROOT/home" "$SKILL_DIR/scripts/audit-all.sh" --target "$TEST_ROOT/home" --output "$timeout_output" --timeout 1 >/dev/null 2>/dev/null
timeout_inventory=$(cat "$timeout_output/inventory.jsonl")
assert_jsonl "$timeout_inventory"
printf '%s\n' "$timeout_inventory" | grep -q '"record_type":"cleanup_candidate"'
printf '%s\n' "$timeout_inventory" | grep -q '"record_type":"orchestrator_module".*"status":"PARTIAL".*"child_module":"caches".*"timed_out":true.*"final_status_seen":false.*"outcome":"timeout"'

printf '%s\n' 'macos-cleanup script tests passed'
