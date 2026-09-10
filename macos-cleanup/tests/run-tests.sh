#!/bin/sh

set -eu

TEST_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1

python3 -m unittest discover -s "$TEST_DIR" -p 'test_*.py' -v
/bin/sh "$TEST_DIR/test-scripts.sh"
for script in "$TEST_DIR/../scripts/"*.sh "$TEST_DIR/"*.sh; do
  /bin/sh -n "$script"
done
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "$TEST_DIR/../scripts/"*.sh "$TEST_DIR/"*.sh
else
  printf '%s\n' 'shellcheck unavailable; shell syntax and regression tests completed'
fi
