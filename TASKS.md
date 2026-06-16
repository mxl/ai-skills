# Implementation Plan: domain-check Skill

Goal: evolve `domain-check` into a no-commercial-API, read-only exact domain availability checker for `.ru`, `.рф`, and global domains.

The checker must use authoritative public registry signals where possible:

- `.ru` / `.рф`: official TCI WHOIS on `whois.tcinet.ru`.
- Global TLDs with RDAP: IANA RDAP bootstrap + registry RDAP endpoint.
- TLDs without RDAP: WHOIS fallback discovered via `whois.iana.org`.
- DNS: optional diagnostic evidence only, never generic availability proof.

Do not include REG.RU provider logic in `domain-check`. Keep `regru` separate.

## Scope

- Skill name: `domain-check`.
- Default method: `auto`.
- No required API key for default operation.
- Input: exact domain names as CLI arguments only.
- Output: normalized JSON or human-readable summary.
- Supported statuses: `registered`, `appears_unregistered`, `unknown`, `invalid`.
- Safety wording: “appears unregistered” or “no registry record found,” not “guaranteed registerable.”

## Research Conclusions To Preserve

- Official IANA WHOIS for `.ru` and `.рф` is `whois.tcinet.ru`.
- TCI WHOIS returns `state: REGISTERED...` for registered RU-zone domains.
- TCI WHOIS returns `No entries found for the selected source(s).` when no matching RU-zone record exists.
- `.рф` must be checked as Punycode, e.g. `пример.рф` -> `xn--e1afmkfd.xn--p1ai`.
- `.ru` and `.xn--p1ai` were not present in the IANA RDAP bootstrap during research, so RDAP is not the primary RU-zone path.
- DNS absence is not availability proof. Example: `example.ru` is registered but has no delegated NS/SOA records.
- Mature tools use RDAP first, WHOIS fallback, and DNS only as evidence or TLD-specific authoritative checks.
- Existing DNS-management skills are provider mutation workflows and must stay separate from availability checks.

---

## Task List

### Phase 1: Skill Definition And Scope

- [ ] **T1: Update Skill Instructions**
  - Update `domain-check/SKILL.md` from WhoisXML-only to no-API public registry checks.
  - Document method routing: `auto`, `rdap`, `whois`, `dns`.
  - State that `.ru/.рф` use `whois.tcinet.ru`.
  - State that DNS is diagnostic only unless a TLD-specific authoritative DNS rule is implemented.
  - Keep the skill exact-domain-only: no suggestions, no registration, no DNS changes, no WHOIS ownership lookup.

- [ ] **T2: Update Trigger Boundaries**
  - Trigger for exact domain availability checks: “is example.com available?”, “проверь свободен ли example.ru”.
  - Do not trigger for registrar-specific REG.RU API requests.
  - Do not trigger for domain registration, purchases, transfers, renewals, DNS changes, or DNS deliverability remediation.
  - Do not trigger for domain name brainstorming unless exact domains are supplied for checking.

### Phase 2: CLI Interface

- [ ] **T3: Replace WhoisXML Options With No-API Options**
  - Add `--method auto|rdap|whois|dns` with default `auto`.
  - Keep `--json`.
  - Keep or add `--delay-ms <number>` for pacing bulk checks.
  - Add `--timeout-ms <number>` for RDAP, WHOIS, and DNS operations.
  - Add `--include-dns` to append DNS diagnostics after authority checks.
  - Add `--confidence` to include confidence values in human output.
  - Remove default requirement for `WHOISXML_API_KEY`.
  - Remove WhoisXML-only `--mode DNS_AND_WHOIS|DNS_ONLY` from the default design.

- [ ] **T4: Preserve Input Validation And IDN Normalization**
  - Trim, lowercase, and remove trailing dots.
  - Reject URLs, paths, query strings, fragments, spaces, and empty values.
  - Convert IDNs to ASCII/Punycode using `url.domainToASCII`.
  - Deduplicate by ASCII domain.
  - Extract TLD from the ASCII domain.

### Phase 3: RDAP Implementation

- [ ] **T5: Add IANA RDAP Bootstrap Client**
  - Fetch `https://data.iana.org/rdap/dns.json`.
  - Map TLDs to RDAP base URLs.
  - Cache in memory per process.
  - Optionally persist cache in temp dir for future runs if simple.
  - Treat missing TLD entries as “no RDAP service known,” not as availability.

- [ ] **T6: Add RDAP Domain Lookup**
  - Query `<rdapBase>/domain/<asciiDomain>`.
  - Interpret HTTP `200` as `registered` with high confidence.
  - Interpret HTTP `404` as `appears_unregistered` with high confidence.
  - Interpret `429`, `5xx`, malformed JSON, or network failure as `unknown` or fallback-eligible.
  - Parse optional registrar, status, events, nameservers, and RDAP notices when available.
  - Do not expose excessive raw RDAP data in human output.

### Phase 4: WHOIS Implementation

- [ ] **T7: Add TCP WHOIS Client**
  - Use Node `net` module, TCP port 43.
  - Send `<domain>\r\n`.
  - Support timeout.
  - Decode response as UTF-8 with replacement for invalid bytes.
  - Return raw text only inside JSON if explicitly useful; keep human output concise.

- [ ] **T8: Add RU-Zone WHOIS Special Case**
  - Route `.ru` and `.xn--p1ai` to `whois.tcinet.ru` in `auto` and `whois` methods.
  - `domain:` plus `state:` -> `registered`, high confidence.
  - `No entries found for the selected source(s).` -> `appears_unregistered`, high confidence.
  - Timeout, rate-limit text, or unexpected text -> `unknown`.
  - Preserve Punycode in JSON and show Unicode input when available.

- [ ] **T9: Add WHOIS Server Discovery For Global Fallback**
  - Query `whois.iana.org` with the TLD.
  - Parse `whois:` line for the registry WHOIS server.
  - Cache TLD -> WHOIS server in memory.
  - If no WHOIS server is found, return `unknown`.

- [ ] **T10: Add Conservative WHOIS Parsing**
  - Use TLD-specific patterns for common TLDs where easy and well-known.
  - Add generic “not found” patterns: `No match`, `NOT FOUND`, `No entries found`, `Domain not found`, `not registered`, `No Data Found`.
  - Add generic registered patterns: `Domain Name:`, `domain:`, `registrar:`, `nserver:`.
  - Detect reserved/restricted/blocked text and return `unknown` or `registered` with a message, not `appears_unregistered`.
  - Treat ambiguous WHOIS output as `unknown`.

### Phase 5: DNS Diagnostics

- [ ] **T11: Add Optional DNS Diagnostic Checks**
  - Implement only when `--include-dns` or `--method dns` is used.
  - Query NS and SOA using Node `dns.promises`.
  - Optional: query A and AAAA for presence.
  - Report DNS presence as diagnostic evidence.
  - Never classify a domain as `appears_unregistered` solely because DNS records are absent.

- [ ] **T12: Add TLD-Specific Authoritative DNS Only If Needed**
  - Consider later support for GDPR ccTLD authoritative NS checks, inspired by existing tools.
  - Do not implement generic authoritative DNS availability proof in v1 unless thoroughly tested per TLD.
  - If implemented, gate it behind explicit TLD rules and confidence labels.

### Phase 6: Result Model And Output

- [ ] **T13: Define Unified Result Schema**
  - Include `domain`, `asciiDomain`, `tld`, `method`, `source`, `status`, `confidence`, `message`, `checkedAt`.
  - Include optional `evidence` array with concise source summaries.
  - Include optional `dns` diagnostics when requested.
  - Preserve machine-readable `status` values.

- [ ] **T14: Human Output**
  - Group results by `appears_unregistered`, `registered`, `unknown`, and `invalid`.
  - Use wording like “appears unregistered according to RDAP/WHOIS.”
  - Explicitly say registration is not guaranteed for `appears_unregistered` results.
  - Show source and confidence when `--confidence` is passed.
  - Keep `--json` output parseable and free of extra text.

### Phase 7: Documentation And Evals

- [ ] **T15: Update README.md**
  - Document no-API default operation.
  - Remove `WHOISXML_API_KEY` as required for `domain-check`.
  - Add examples for `.ru`, `.рф`, `.com`, and mixed checks.
  - Explain RDAP vs WHOIS vs DNS diagnostics.
  - Keep `regru` documentation separate.

- [ ] **T16: Update Evals**
  - Add RU WHOIS exact checks.
  - Add `.рф` Punycode checks.
  - Add global RDAP checks for `.com` or similar.
  - Add WHOIS fallback checks for a TLD without RDAP if stable.
  - Add DNS diagnostic negative cases showing no DNS != available.
  - Add negative cases for DNS changes, registration, suggestions, and REG.RU-specific API requests.

- [ ] **T17: Update TASKS.md As Work Progresses**
  - Mark completed tasks.
  - Add discovered edge cases.
  - Keep notes about live test domains and observed responses.

### Phase 8: Verification

- [ ] **T18: Static Verification**
  - Run `node --check domain-check/scripts/domain-check.mjs`.
  - Run `node domain-check/scripts/domain-check.mjs --help`.
  - Verify invalid inputs fail before network calls.

- [ ] **T19: Live Verification**
  - `.ru` registered: `example.ru` should return `registered` from `whois.tcinet.ru`.
  - `.ru` random: random unlikely name should return `appears_unregistered` from `whois.tcinet.ru`.
  - `.рф`: `пример.рф` should normalize to `xn--e1afmkfd.xn--p1ai` and return a WHOIS result.
  - `.com` registered: `example.com` should return `registered` via RDAP.
  - `.com` random: random unlikely name should return `appears_unregistered` via RDAP `404`.
  - DNS diagnostic: `example.ru --include-dns` should not override WHOIS registered status even if NS/SOA are absent.

- [ ] **T20: Skill Validation And Trigger Testing**
  - Run `skill_validate /Users/michaelledin/projects/ai-skills/domain-check`.
  - Run trigger evals for exact checks.
  - Ensure registrar-specific REG.RU prompts do not route to `domain-check`.
  - Ensure DNS-management prompts do not route to `domain-check`.

### Phase 9: Final Audit

- [ ] **T21: Audit For Scope Creep**
  - Verify no REG.RU provider logic exists in `domain-check/`.
  - Verify no purchase, registration, transfer, renewal, or DNS mutation paths exist.
  - Verify no commercial API key is required for default behavior.
  - Verify no secrets are printed.

- [ ] **T22: Git And Diff Review**
  - Run `git status`.
  - Review `git diff` for intended files only.
  - Summarize implemented behavior, test results, and known limitations.
