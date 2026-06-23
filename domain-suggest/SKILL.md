---
name: domain-suggest
description: Use this skill when the user asks to suggest, generate, brainstorm, invent, or come up with domain name ideas or brand name ideas for a startup, new company, product, project, side project, or rebrand. Trigger for "suggest domain names", "brainstorm domains", "domain ideas", "help me name my startup", "придумай домен", "подбери домен", "варианты доменов", "название и домен". It creates candidates and checks availability through domain-check. Do not use for exact-domain-only availability checks (use domain-check), registration, purchases, transfers, renewals, DNS changes, or WHOIS ownership lookup.
license: MIT
compatibility: opencode
metadata:
  audience: agents
  domain: domains
---

# Domain Name Suggestion

Generate brandable domain/name candidates for a startup, company, product, or project, then verify which are actually available. Generation is done by the agent using the methodology below; availability is verified deterministically by delegating to the `domain-check` skill (RDAP/WHOIS/DNS, no API keys). Never invent availability or prices.

## When To Use

Use this skill for requests like:
- "Suggest domain names for my AI sales assistant."
- "Придумай домен для нового проекта про доставку еды."
- "I need a brandable name for a fintech startup, prefer .io or .com."
- "Подбери ещё варианты доменов с word 'care'."
- "Help me name my company and find an available domain."

Do not use this skill for:
- Checking specific exact domains the user already has (use `domain-check`);
- Registering, buying, transferring, or renewing domains;
- DNS zone management or configuration;
- Detailed WHOIS ownership lookups.

If the user supplies exact domain names and only wants to know if they are free, route to `domain-check` instead.

## Requirements

- `node` available in PATH (for the bundled `domain-check` availability script).
- Internet access for RDAP/WHOIS checks.

## Workflow

1. **Brief.** Extract from the prompt: project description/industry, keywords to include/exclude, vibe/tone, target name length, language(s), preferred TLD/zones, and hard constraints (e.g., no hyphens, no "bot", no transliteration). If a critical input is missing, ask ONE short clarifying question; otherwise proceed with sensible defaults and state them.

2. **Generate.** Produce a diverse candidate pool (aim for 15-40 before filtering) using multiple techniques from `references/generation-techniques.md`: affixation, compounding, blending/portmanteau, phonetic respelling, semantic expansion, coined words, and the "hidden-signal" wordplay (embedding a concept's letters inside a real word, e.g. av**ai**l, s**ai**l, g**ai**rd). Vary techniques; do not just append a suffix to one keyword.

3. **Filter.** Screen candidates with the SMILE/SCRATCH tests and brandability rules in `references/naming-methodology.md`, and drop anything that violates the user's constraints or target length.

4. **Verify availability.** Batch-check the surviving candidates (with the user's preferred zones) by delegating to the `domain-check` script. From the repo root:

   ```bash
   node domain-check/scripts/domain-check.mjs name1.com name2.io name3.ru --concurrency 5
   ```

   Use the `domain-check` skill's interpretation and safety rules. The script streams `[free]/[taken]/[err]` lines and prints a summary.

5. **Backfill.** For taken/errored names, generate replacements and re-check until you have enough available options (default: at least 5 strong available candidates, or as many as the user asked for).

6. **Present.** Return a ranked, zone-grouped shortlist. For each name give a one-line rationale ("why it works"). Mark a top pick and a runner-up. Attribute availability to the source (RDAP/WHOIS) and never claim registration is guaranteed.

## Inputs To Capture

Description/industry - keywords (include/exclude) - vibe/tone - target length - language(s) - preferred TLD/zones (.com/.ai/.io/.ru/.рф/...) - hard constraints (no hyphens/numbers/"bot"/transliteration) - how many options wanted.

## Output Rules

- Match the user's prompt language (Russian prompt -> Russian answer).
- Group available names by zone; list available first, then notable taken ones if useful.
- One short rationale per name; mark top pick + runner-up.
- Use `domain-check` safety wording: "appears unregistered according to RDAP/WHOIS", and state that availability is not a registration guarantee.
- Do not output invented prices, trademark clearances, or social-handle availability. If relevant, recommend the user verify trademarks (USPTO/WIPO) and handles separately as a manual next step.

## References

- `references/naming-methodology.md` - name types, SMILE/SCRATCH tests, sound symbolism, TLD strategy, evaluation checklist.
- `references/generation-techniques.md` - taxonomy of generation techniques with examples, including the hidden-signal wordplay.

## Failure Handling

- If the availability script errors for some names (rate limits/timeouts), report those as "could not verify" rather than guessing, and offer to retry (optionally with `--concurrency 2`).
- If the user only gave exact domains to check, defer to `domain-check`.
