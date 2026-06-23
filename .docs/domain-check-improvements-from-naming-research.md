# Domain-check Improvements From Domain-Suggest Research

Дата: 2026-06-20

## Краткий вывод

`domain-check` должен остаться exact-domain availability checker'ом. Генерацию доменных идей не нужно добавлять в него: для этого создан отдельный `domain-suggest` skill. Но исследование генераторов и MCP-чекеров подсветило несколько улучшений, которые сделают `domain-check` сильнее как backend для `domain-suggest` и самостоятельный checker.

## Приоритетные улучшения

### 1. Более богатая модель результата

Сейчас JSON содержит `domain`, `asciiDomain`, `tld`, `method`, `status`, `source`, `message`, `dns`. Стоит добавить:

- `checkedAt` на каждый результат, не только на общий output;
- `confidence`: `high | medium | low`;
- `evidence`: массив коротких фактов (`RDAP 404`, `TCI WHOIS: No entries found`, `WHOIS state: REGISTERED`);
- `classification`: `standard | registered | appears_unregistered | reserved | premium_or_aftermarket | rate_limited | ambiguous`.

Зачем: domain-suggest сможет ранжировать и объяснять результаты без парсинга human-output.

### 2. `--format markdown|json|plain`

Commercial tools и MCP обычно возвращают структурированный output. Для agent workflow полезны:

- `--json` для машинного потребления;
- `--format markdown` для user-facing таблиц;
- plain streaming output оставить default.

### 3. `--file` и stdin для больших батчей

Domain generators часто создают 50-200 кандидатов. CLI сейчас принимает только argv. Добавить:

```bash
node domain-check/scripts/domain-check.mjs --file candidates.txt
printf "a.ru\nb.ru" | node domain-check/scripts/domain-check.mjs --stdin
```

Это уберёт лимиты shell-аргументов и упростит интеграцию с `domain-suggest`.

### 4. TLD-aware concurrency/rate-limit policy

Сейчас `--concurrency` общий. По материалам MCP/registrar tools rate limits различаются по TLD/provider. Улучшение:

- ограничивать concurrency per TLD/provider;
- читать `Retry-After` для RDAP `429`;
- добавлять `--rate-limit-policy conservative|balanced|fast`.

Это особенно полезно для `.sale` и нестабильных RDAP-серверов.

### 5. Cache WHOIS referral by TLD

RDAP bootstrap уже кэшируется. WHOIS discovery через `whois.iana.org` тоже стоит кэшировать:

- temp-file `domain-check-whois-referrals.json`;
- TTL 24h или 7d;
- key: TLD -> WHOIS server / no-server known.

Это снизит сетевую хрупкость для non-RDAP fallback.

### 6. TLD-specific parsers for high-value zones

Generic WHOIS parsing хрупок. Добавить явные parser rules для часто используемых зон:

- `.com`, `.net` (Verisign style)
- `.org`
- `.io`
- `.ai`
- `.co`
- `.dev`, `.app` (mostly RDAP but keep fallback)

Для `.ru/.рф` уже есть спец-маршрут на TCI WHOIS; его стоит дополнить explicit confidence/evidence.

### 7. Detect reserved / blocked / premium-like states

Нельзя сообщать такие имена как "appears unregistered". Нужно распознавать RDAP/WHOIS тексты вроде:

- reserved
- blocked
- premium
- registry reserved
- clientHold / serverHold / pendingDelete (зарегистрирован, но special state)

Возвращать `unknown` или `reserved`, не `appears_unregistered`.

### 8. Strict domain validation

Перед network call проверять:

- total length <= 253;
- labels <= 63;
- no empty labels;
- allowed ASCII labels after Punycode;
- no leading/trailing hyphen per label;
- TLD present.

Сейчас нормализация уже есть, но validation можно усилить. Это важно для генераторов, которые иногда создают странные кандидаты.

### 9. Optional `--suggested-retry` output for errored domains

Для agent workflow удобно, если error result включает машинное предложение:

```json
"suggestedRetry": { "concurrency": 2, "delayMs": 2000, "afterMs": 60000 }
```

Так `domain-suggest` сможет автоматически перезапускать проблемные TLD без ручных догадок.

### 10. Keep social handles / trademark / pricing out of domain-check

Исследование показало, что продвинутые генераторы часто смешивают domain availability, pricing, premium marketplace, social handles и trademarks. Для этого repo лучше сохранить границы:

- `domain-check`: exact domain availability only;
- `regru`: registrar-specific check only;
- `domain-suggest`: generation + orchestration;
- future `handle-check` or `trademark-screen`: separate skills, если понадобится.

## Минимальный следующий инкремент

Если делать маленький полезный PR после `domain-suggest`, я бы взял:

1. `--file` + `--stdin` input.
2. Per-result `checkedAt`, `confidence`, `evidence` in JSON.
3. WHOIS referral cache.
4. Strict validation.

Эти четыре изменения напрямую улучшают связку `domain-suggest -> domain-check`, не размывая границы skill'ов.
