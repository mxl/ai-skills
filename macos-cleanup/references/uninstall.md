# Named Application Uninstall

Apply the parent [skill](../SKILL.md)'s exact-approval and independent verification gates, the [action policy](action-policy.md), and the application/package audit requirements in the [audit guide](audit-guide.md).

Recognize `uninstall <app-name-or-exact-path> ...`. Resolve each target to exactly one bundle in `/Applications`, `~/Applications`, or an explicit path. If resolution is missing or ambiguous, ask for clarification.

1. Audit only named bundles, cask/vendor ownership, processes, helpers, services, extensions, and associated data.
2. Present one bundle option per app. Homebrew cask uninstall may run app-specific scripts; disclose recorded uninstall effects. Never use `--zap --force`.
3. After bundle selection, ask a separate multi-select question for eligible app data, one option per app plus `None`.
4. Keep synced, credential, browser-profile, database, VM, project, personal, and model data in dedicated decisions rather than generic app-data approval.
5. Use vendor uninstallers when needed for helpers or extensions. Otherwise move only the exact user-owned app bundle to Trash after exact closure verification.
6. Approval for one app never authorizes another app, helper, package, cache, or data path.
