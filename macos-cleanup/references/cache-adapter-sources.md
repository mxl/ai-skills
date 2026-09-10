# Cache adapter sources

This note records the direct authoritative sources used by `scripts/cache_adapters.py`.
The adapter uses only read-only version/configuration/help probes during discovery.
It does not infer support from an unknown version and does not invoke a cleanup
command in tests.

## uv

- CLI reference: <https://docs.astral.sh/uv/reference/cli/>
- Cache concept and cleanup behavior: <https://docs.astral.sh/uv/concepts/cache/>
- Environment variables: <https://docs.astral.sh/uv/reference/environment/>
- Source consulted through Context7: <https://github.com/astral-sh/uv/blob/main/crates/uv-cli/src/lib.rs>

Tested assumptions:

- `uv --version` is the version probe and a release-shaped `uv X.Y.Z` result is
  retained as the installed version string.
- The runnable policy is stable uv `0.9.4` through the reviewed latest stable
  `0.12.12`, inclusive. `UV_LOCK_TIMEOUT` is documented as added in `0.9.4`;
  prereleases, future versions, and releases outside this reviewed window stay
  review-only. The upper bound is based on the `0.12.12` release published on
  2026-09-09: <https://github.com/astral-sh/uv/releases/tag/0.12.12>.
- `uv cache dir` reports the configured cache root.
- `uv cache prune --help` is used only as a feature probe. A successful help
  response containing the cache-prune command and `--cache-dir` is required
  before preparation. Help text alone does not establish lock semantics.
- The built command uses the documented global `--cache-dir` option and
  explicit `UV_CACHE_DIR`, `UV_NO_CONFIG=1`, `UV_NO_CACHE=0`, and
  `UV_LOCK_TIMEOUT`. It sets `UV_LINK_MODE=copy` for the bound execution
  environment. `--force` is deliberately not emitted; the owner CLI's native
  lock behavior remains authoritative.
- `UV_LINK_MODE` unset is unverified rather than equivalent to uv's default.
  Only an explicit `copy` value is accepted for this plan shape; symlink,
  hardlink, clone, unknown values, and configured `UV_PROJECT_ENVIRONMENT` are
  review-only. Config files and cache environment references are not searched.

## Go

- Official `go` command documentation, including `go clean -cache`:
  <https://pkg.go.dev/cmd/go#hdr-Remove_object_files_and_cached_files>
- Official environment command documentation, including `GOENV` and `GOWORK`:
  <https://pkg.go.dev/cmd/go#hdr-Environment_variables>
- Go toolchain documentation:
  <https://go.dev/doc/toolchain>
- Reviewed current Go command version documentation (`go1.27.1`):
  <https://pkg.go.dev/cmd/go@go1.27.1>

Tested assumptions:

- `go version` is the version probe and a `go version goX.Y[.Z] ...` result is
  retained as the installed version string.
- The runnable policy is stable Go `1.20.0` through the reviewed latest
  documented `1.27.1`, inclusive. Prereleases, future versions, and releases
  outside this reviewed window stay review-only.
- `go env GOCACHE` reports the configured build-cache root.
- `go help clean` is used only to verify that the installed command documents
  the `-cache` feature, matching the official command help guidance.
- The built command is exactly `go clean -cache` with an explicit `GOCACHE`
  environment value. Its full environment clears `GOFLAGS` and
  `GOCACHEPROG`, sets `GOTOOLCHAIN=local`, `GOENV=off`, and `GOWORK=off`, and
  does not include `-modcache` or `-fuzzcache`.

## Review-only owners

- npm cache command reference: <https://docs.npmjs.com/cli/v11/commands/npm-cache>
- pnpm store command reference: <https://pnpm.io/cli/store>
- Homebrew manpage: <https://docs.brew.sh/Manpage>

The adapter may discover npm, pnpm, or Homebrew version/configured-root
metadata, but these owners never produce a runnable plan here. Their cleanup
semantics, package/project activity, and recovery scope remain review-only.

## Writer and runtime safety

- Process activity is checked with exact-name `pgrep -x` probes for `uv` and
  `uvx`, or Go/gopls plus the concrete editor controller names `Code Helper`,
  `Cursor Helper`, `Zed`, `GoLand`, `IntelliJ IDEA`, and `Windsurf`. Exit 1
  means no matching process; a single numeric result means active; multiple
  results or any unexpected result is unknown. Full process command lines are
  not inspected.
- Public discovery establishes the native no-hydration guard before
  executable discovery, metadata reads, or creation of probe temp directories.
  Owner probes are then routed through `scripts/audit_runtime.py` with private,
  bounded temporary stdout/stderr files. Guard loss demotes evidence without
  starting another probe; cancellation propagates as `AdapterCancelled` (a
  `KeyboardInterrupt`) so CLI handling can return code 130. Probe stderr is
  transient and is not returned as evidence.
