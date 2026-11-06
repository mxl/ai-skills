# Native discovery and implicit invocation

Read this reference when configuring skill discovery or diagnosing a missed invocation. Routine browser actions use the main workflow; a project AGENTS.md pointer is not required to trigger this skill.

## OpenCode V2 behavior

OpenCode advertises permitted skills using their ID, name, and description. The model then calls the `skill` tool to load the selected body. Supporting references are read only when needed.

This skill's description covers ordinary browser interaction, not just concurrent tasks or remote setup. Single-task website navigation, forms, screenshots, authenticated browsing, UI checks, and natural Russian requests should select this entry point before page operations. Static HTTP fetches, backend-only tests, and API explanations are near-miss negatives.

Frontmatter deliberately contains:

```yaml
metadata:
  opencode/autoinvoke: true
```

This means the skill is eligible for advertisement. It does **not** mean OpenCode deterministically executes the skill for every matching user message. Do not describe it as a forced hook or a security boundary. `disable-model-invocation: true` or `opencode/autoinvoke: false` hides a skill from the model; those settings must not disable this entry point.

## Register once at user scope

When a user-global source already includes this repository and the skill is advertised, do not add duplicate symlinks or project-specific activation instructions.

For a new installation, OpenCode V2 supports a user-level source array, for example:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "skills": ["~/projects/ai-skills"]
}
```

This is an example, not permission to overwrite the user's configuration. Inspect only the relevant non-secret source fields, preserve other settings, and obtain approval before configuration edits. V2 watches skills/configuration; a full OpenCode restart is not normally needed. Use `/reload` only when a manual refresh is necessary.

A global skill directory/symlink under `~/.config/opencode/skills` is another supported installation method. Pick one maintained source rather than introducing redundant copies. Later sources with the same path-derived ID can override earlier definitions.

## Routing boundaries

- This skill coordinates browser/project/task ownership first.
- agent-browser supplies version-matched page-operation instructions after coordination.
- A project's test runner owns regression execution; browser skill loading does not replace it.
- Browser actions are coordinated even when the user did not mention profiles, concurrency, a viewport, or a skill name.
- A request to explain Playwright syntax or fetch static article text is not itself permission or a reason to start a browser.

## Check a missed invocation

1. Confirm the exact ID `browser-automation` is registered from the intended source.
2. Confirm its description is present, model invocation is not disabled, and the selected agent permits the `skill` action for this ID.
3. Check for a later duplicate definition overriding this source.
4. Test natural positive and near-miss negative prompts without naming the skill or adding an AGENTS.md activation pointer.
5. Record actual skill loads rather than treating a model's proposed routing as proof that it loaded the skill.

Description-based routing is model-dependent. A few successful smoke tests demonstrate those examples, not a 100% invocation guarantee. If the user requires deterministic enforcement before browser tool execution, discuss a separately approved V2 plugin/tool gate. Do not silently add a plugin, global instructions, permission changes, or browser-command interception to achieve that stronger property.

## Evidence

- [OpenCode V2 skills](https://opencode.ai/v2/docs/skills/): discovery, source arrays, frontmatter, permissions, loading, and precedence.
- [OpenCode V2 configuration](https://opencode.ai/v2/docs/config/): global/project scope and merging.

Current policy: universal coordination/recovery/launch behavior lives in this skill. Project instructions may supply project-specific facts and host safety constraints, but need not mention the skill name for native discovery to work. Existing project instructions are not automatically deleted or weakened during skill setup.
