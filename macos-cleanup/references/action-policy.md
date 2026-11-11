# Action Policy

## Action Matrix

Include every material storage class in analysis. Whether it may be proposed or executed depends on ownership, recoverability, sync state, and activity.

| Class | Analyze | Propose | Execute after approval |
|---|---|---|---|
| Rebuildable cache | Yes | Exact owner/path | Yes, through owner command or exact cache contents |
| Cloud offline copy | Yes | Exact provider/folder | Only provider-aware `Remove Download` or online-only action after sync verification |
| Personal file/media | Aggregate first | Exact item or reviewed set | Move to a unique Trash destination; cloud-synced deletion is a separate remote-delete decision |
| Application bundle | Yes | One exact app | Yes through cask/vendor uninstaller or exact bundle-to-Trash workflow |
| Application data | Yes | One exact owner/path set | Separate approval from app bundle; use app-aware cleanup when available |
| VM/emulator/simulator | Yes | One named VM/device/runtime | Yes through owning tool; disclose loss of guest/device-local state |
| Docker image/build cache | Yes | Separate Docker groups | Yes after active container/compose preflight |
| Docker volume/database | Yes | One exact volume/database | Dedicated high-risk approval; never include in generic Docker cleanup |
| Model/AI data | Yes | One exact model/repository/version | Dedicated model-aware workflow after explicit approval; never generic recursive deletion |
| Time Machine local snapshot | Yes | One exact purgeable snapshot | Dedicated system-level approval using supported `tmutil`; never promise its logical size |
| System/APFS sealed data | Yes | Supported maintenance only | Never raw-delete; use documented OS tooling or administrator handoff |
| Credentials/secrets | Count and aggregate | Exact path only on explicit request | Dedicated confirmation; never expose contents or mix with temporary-file cleanup |

An analysis without material candidates is incomplete. A proposal is not approval. An exact approval does not authorize sibling paths or related data.

## Owner-Aware Procedures

- Prefer version-supported selective pruning and warm-cache budgets over full reset. Check owner locks, action scope and regeneration cost; do not bypass busy locks. State whether recovery is local regeneration, public download, private registry, or unverified.
- Check owner processes plus known writers/dependents (for example gopls/editors for Go). Missing visibility means unknown, not idle. Do not expose full command-line arguments. A cache's measurable size does not prove owner inactivity.
- Preserve cache roots and metadata when directly clearing exact cache contents. Enumerate dotfiles safely and do not follow symlinks or cross mount points.
- Use Docker commands for Docker resources. Never delete Docker Desktop's container directory directly. Keep active containers, compose stacks, databases, and volumes separate.
- Use Xcode, `simctl`, Android Studio/`avdmanager`, UTM, or another owning tool for SDK/device/VM state. Never delete raw simulator or VM directories as generic cleanup.
- Use model-manager or repository-aware commands for model data. Inventory exact models and shared blobs before offering deletion; disclose shared-layer and redownload effects.
- Move ordinary local files to a unique `~/.Trash` destination when practical. A Trash move is reversible relocation, not reclaimed capacity until Trash is emptied through a separately approved action.
- Never request or collect an administrator password. For an approved privileged action, provide one exact narrowly scoped administrator command and describe prerequisites and untouched data.
