---
name: domain-check
description: Use this skill whenever the user asks to check if a domain name is free, available, or unregistered, especially for .ru, .рф, or other TLDs. Trigger this skill for exact domain availability checks (e.g., "is example.ru available?", "проверь свободен ли домен пример.рф"). Do not use for generating domain name suggestions, domain registration, purchases, transfers, DNS changes, renewals, or detailed WHOIS ownership lookups.
license: MIT
compatibility: opencode
metadata:
  audience: agents
  domain: domains
---

# Domain Availability Check

Check exact domain names for availability using public registry signals (RDAP, WHOIS, and DNS diagnostics). In this skill, "free" means available/unregistered, not zero-cost.

## When To Use

Use this skill for requests like:
- "Check whether example.ru is free"
- "Is my-brand-idea.com available?"
- "проверь, свободен ли домен пример.рф"
- "find which of these exact domains are available: a.ru, b.ru, c.com"
- "exact domain availability check for these names"

Do not use this skill for:
- Generating domain-name suggestions from keywords;
- Buying, registering, transferring, or renewing domains;
- DNS zone management or configuration;
- Detailed WHOIS data (e.g., "Who owns this domain?");
- Registrar-specific API checks (e.g., "Check via REG.RU API 2").

## Requirements

- `node` available in PATH.
- Internet access to public RDAP servers and WHOIS servers (TCP port 43).

## Workflow

1. Extract exact domain names from the user's prompt.
2. If the user provides only keywords but no exact domain names, ask one short clarifying question for the exact domains to check.
3. Run the bundled script from the skill directory:

   ```bash
   node domain-check/scripts/domain-check.mjs example.ru пример.рф
   ```

   Useful options:
   - `--json`: Print machine-readable JSON only.
   - `--method auto|rdap|whois|dns`: Force a specific check method (default is `auto`).
   - `--include-dns`: Add DNS diagnostics (NS/SOA) to the result.
   - `--delay-ms <ms>`: Add delay between sequential requests to avoid rate limits.

## Interpreting Results

The script uses a routing strategy based on the TLD:
- `.ru` and `.рф` domains are checked via the official TCI WHOIS server (`whois.tcinet.ru`).
- Other TLDs use RDAP (via IANA bootstrap) if available, falling back to generic WHOIS.

**Crucial Safety Rule:** 
Never report a domain as "guaranteed registerable" or "definitely available". Always attribute the result to the source.

Correct phrasing examples:
- "According to RDAP, example.com appears to be available for registration."
- "According to TCI WHOIS, example.ru is already registered."
- "No registry record found for example.io; it appears unregistered."
- "Unable to reliably determine the availability of example.net."

If the provider returns an "unknown" or "ambiguous" status, do not mark the domain as available.

## Output Rules

- Match the user's prompt language. If the user writes in Russian, answer in Russian; otherwise, answer in the user's language.
- Return available domains first, followed by registered or errored domains.
- Keep the response concise. Do not suggest purchasing or registering unless the user explicitly asks for a separate registration workflow.

## Failure Handling

- If a network request fails or a WHOIS server is unreachable, report that the check could not be completed and include the non-sensitive error message.
- If a domain is invalid, report it as an invalid input.
