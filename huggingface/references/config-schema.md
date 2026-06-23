# Hugging Face Homes Config

Use a small JSON file to describe the user's persistent Hugging Face cache directories.

## Purpose

This config exists so the skill can:

- inspect more than one `HF_HOME` without hardcoding paths;
- distinguish cache homes by stable user-defined names;
- generate reproducible `hf` commands with the correct target cache;
- avoid treating temporary dry-run caches as persistent state.

## Recommended Shape

```json
{
  "homes": [
    {
      "name": "default",
      "path": "/path/to/default/hf-home"
    },
    {
      "name": "external",
      "path": "/path/to/external/hf-home"
    }
  ]
}
```

## Field Rules

### `homes`

- Required.
- Must be a non-empty array.
- Each entry describes one persistent Hugging Face cache root.

### `homes[].name`

- Required.
- Short identifier used in reports and command generation.
- Choose names that are short, stable, and meaningful in the user's environment, such as `default`, `external`, `workstation`, or `archive`.
- Keep names stable once the user starts relying on them in workflows.

### `homes[].path`

- Required.
- Absolute path or a path beginning with `~`.
- Must point to the directory that should be exported as `HF_HOME`.

## Rules For Temporary Dry-Run Homes

Do not store temporary dry-run homes in this config.

For clean `hf download --dry-run` checks, create a fresh temporary directory in the operating system's standard temp location at runtime. The skill should treat that directory as ephemeral and should not add it to the persistent registry.

## Validation Guidance

Before using a configured home, the skill should:

1. Expand `~` to the user's home directory.
2. Check whether the path exists.
3. Report missing directories explicitly instead of silently skipping them.

## Non-Goals

This config is intentionally minimal. Do not add temporary run state, benchmark caches, or model-specific metadata here. Keep it focused on persistent Hugging Face cache roots.
