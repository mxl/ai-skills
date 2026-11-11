# Cloud Offline Audit and Eviction

Read when auditing offline cloud copies or when any provider root is discovered during cleanup. Apply the parent [skill](../SKILL.md)'s guard, privacy, measurement and exact-approval gates; the bundled collector does not implement eviction.

## Cloud Offline Audit

Provider discovery automatically invokes `cloud_metadata_collect.py` for each root under the guard: up to 1000 entries, depth 3 and a 10-second cooperative traversal budget per root, within the outer module timeout. Dataless directories are skipped rather than hydrated. The explicit `cloud-metadata --root <root>` CLI supports bounded follow-up, with hard limits of 10000 entries, depth 64 and 60 seconds. A single blocking kernel metadata call may outlast a cooperative budget; the outer audit runtime provides process-level timeout/cancellation.

Public item IDs use per-run HMAC, not predictable filename hashes; do not treat them as stable cross-run identities or executable paths. Missing measurements remain unknown, with known lower bounds separately labelled. Complete enumeration is not complete cloud verification: sync, pins, conflicts and remote-preserving eviction still need provider-specific evidence. Do not ask the user to request an already authorized safe metadata audit again.

For every discovered provider in `cloud-offline-audit` and `cleanup`:

1. Enumerate item-level offline copies instead of stopping at provider-root totals. Record the provider/domain, exact target, allocated bytes, logical bytes, sync state, local state, and evidence source.
2. Research the provider-specific eviction mechanism available on the current Mac. Prefer documented Finder/provider Remove Download or online-only semantics with verified API access. File Provider extension APIs are not universal administrative access to every domain. `brctl evict` is private/version-dependent; missing help does not prove absence, but neither is it a stable public contract. Do not use a real mutation merely as a capability probe.
3. Before proposing eviction, verify no upload/sync errors using provider/Finder metadata or supported tooling. Verify that `Remove Download`, `Free up space` or `Make online-only` removes only the local copy while preserving the object in the synchronized namespace. Never substitute `rm`, Trash or namespace deletion. Estimate benefit from allocated blocks, not logical size.
4. Aggregate candidates by provider and reviewed folder without double-counting parent and child targets. Show the candidate list even when eviction cannot yet be automated.
5. Offer verified targets in a separate `cloud-local eviction` confirmation group. Each option must identify one provider and exact target set, estimated allocated benefit, offline-access loss, recovery method, and verification plan.
6. After an approved eviction, verify that the remote object remains present, the local item becomes a placeholder or online-only object, and allocated local bytes decrease. Reconcile failures or unchanged targets explicitly.

For an approved iCloud eviction:

1. Re-check exact-target allocated and logical sizes, `isUploaded`, `isUploading`, `isDownloading`, conflicts, keep-downloaded state, and filesystem capacity immediately before action.
2. Use the specifically reviewed and approved eviction method. A private `brctl` command requires explicit version/capability review; Foundation or Finder also requires verified scope and sync evidence.
3. If an operation fails or times out, reconcile partial/asynchronous state before trying another method. Do not stack fallback actions or restart `bird` as routine cleanup. Stop a known content-reading source first when handling accidental hydration; the no-hydration guard does not cancel an existing transfer.
4. Verify allocated bytes fall, logical bytes and namespace remain, and provider metadata still reports the item uploaded and not trashed. Folder-level `isDownloaded=1` alone does not prove descendants remain materialized.

If item-level status or supported eviction semantics cannot be verified, classify the target `AMBIGUOUS`, include it in the report, and provide exact provider UI steps or the narrow next audit needed. Do not silently omit it or guess a filesystem deletion command.

The cloud module is `PARTIAL` when a cloud root is discovered but item-level offline candidates were not enumerated, their sync/local state was not investigated, or a remote-preserving eviction mechanism was not researched.
