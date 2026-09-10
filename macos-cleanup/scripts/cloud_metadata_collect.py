#!/usr/bin/env python3
"""Internal bounded item collector invoked for each discovered provider root."""

import json
import sys

sys.dont_write_bytecode = True

from cloud_inventory import inventory

PROVIDERS = {"icloud-drive", "google-drive", "dropbox", "onedrive", "yandex-disk", "unknown-file-provider"}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[1] not in PROVIDERS or not args[2].isdigit():
        sys.stderr.write("cloud metadata collector: invalid arguments\n")
        return 2
    try:
        result = inventory(args[0], max_entries=1000, max_depth=3, timeout=10.0)
    except KeyboardInterrupt:
        return 130
    except Exception:
        result = {"schema_version": "1", "record_type": "cloud_container_inventory",
                  "module": "cloud", "status": "PARTIAL", "sensitive": True,
                  "enumeration_status": "PARTIAL", "evidence": ["item metadata unavailable"]}
    result.update(provider_kind=args[1], domain_label=f"{args[1]} account {args[2]}",
                  container_count=result.get("counts", {}).get("items", 0),
                  inventory_scope="bounded-item-metadata", actionable=False)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
