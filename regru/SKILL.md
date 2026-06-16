---
name: regru
description: Use this skill whenever the user asks to check REG.RU, reg.ru, regru, or Рег.ру domain availability, search whether exact domain names are free/unregistered, or verify if domains can be registered through REG.RU API 2. This skill is only for exact domain availability checks via REG.RU domain/check; use it even when the user says "free domain search" if they mean available/unregistered domains. Do not use for generated domain suggestions, domain registration, purchases, transfers, DNS changes, renewals, or WHOIS lookups.
license: MIT
compatibility: opencode
metadata:
  audience: agents
  domain: domains
---

# REG.RU Domain Availability

Check exact domain names for availability through REG.RU API 2 using the bundled CLI. In this skill, "free" means available/unregistered, not zero-cost.

## When To Use

Use this skill for requests like:

- "check whether example.ru is free on reg.ru"
- "find which of these exact domains are available through REG.RU"
- "проверь домены example.ru и example.com через Рег.ру"
- "regru domain/check for these names"

Do not use this skill for:

- generating domain-name suggestions from keywords;
- buying, registering, transferring, renewing, or deleting domains;
- DNS zone management;
- WHOIS details beyond availability;
- availability checks through another registrar.

## Requirements

- `node` available in PATH.
- Internet access to `https://api.reg.ru`.
- REG.RU partner/reseller API access. REG.RU documents `domain/check` as a partner-only function; normal client credentials can authenticate but will receive `RESELLER_AUTH_FAILED`.
- REG.RU API credentials in environment variables:
  - `REGRU_USERNAME`
  - `REGRU_PASSWORD`

`REGRU_PASSWORD` may be the REG.RU alternative API password configured in API settings.

Optional client SSL certificate auth:

- `REGRU_SSL_CERT_PATH`
- `REGRU_SSL_KEY_PATH`

If both SSL variables are set, the script sends the client certificate and key with the request. If only one is set, the script fails with a configuration error. REG.RU API 2 does not support certificate-only auth; username plus password or signature is still required.

## Workflow

1. Extract exact domain names from the user's prompt.
2. If the user gives keywords but no exact domain names, ask one short clarifying question for the exact domains to check.
3. Run the bundled script from this skill directory:

```bash
node regru/scripts/regru-domain-check.mjs example.ru example.com
```

Useful options:

```bash
node regru/scripts/regru-domain-check.mjs example.ru example.com --currency USD
node regru/scripts/regru-domain-check.mjs example.ru --premium-as-taken
node regru/scripts/regru-domain-check.mjs example.ru --json
```

The script calls:

```text
POST https://api.reg.ru/api/regru2/domain/check
```

with `output_content_type=json`, `input_format=json`, and JSON `input_data` containing a bulk `domains` array.

## Interpreting Results

Treat a domain as available only when REG.RU returns:

```text
result == "Available"
```

Other results should be reported as unavailable, invalid, unsupported, premium-only, or errored according to returned fields such as `error_code`, `result`, `is_premium`, `price`, `renew_price`, and `currency`.

REG.RU test credentials (`username=test`, `password=test`) are only for API-shape debugging. They do not return real domain availability data, so do not use them for user-facing availability decisions.

## Output Rules

Match the user's prompt language. If the user writes in Russian, answer in Russian; otherwise answer in the user's language.

Return:

- available domains first;
- unavailable, invalid, unsupported, or errored domains second;
- premium flag and registration/renewal prices when REG.RU returns them;
- a short note if any domains could not be checked;
- no credentials, tokens, certificate paths, or raw secrets.

Keep the response concise. Do not suggest purchasing or registering unless the user explicitly asks for a separate registration workflow.

## Failure Handling

If credentials are missing, tell the user which environment variables are required. Do not ask them to paste credentials into chat.

If REG.RU returns an API-level error, report the `error_code` and `error_text` if present.

If the error is `RESELLER_AUTH_FAILED`, explain that REG.RU accepted the API request but the account is not allowed to use `domain/check`; the user needs partner/reseller API access for this exact availability-check workflow.

If the network request fails, say that the availability check could not be completed and include the non-sensitive error message.
