# Generation Techniques

Taxonomy of techniques for generating brandable name candidates. Use several per session for variety; do not rely on a single technique. After generating, screen with `naming-methodology.md` and verify availability via the `domain-check` script.

## 1. Affixation
Attach a fixed prefix or suffix to a keyword.
- Prefixes: `get-`, `go-`, `my-`, `try-`, `the-` -> GetShopify, GoDaddy.
- Suffixes: `-ly`, `-ify`, `-hub`, `-base`, `-kit`, `-labs`, `-io`, `-ster` -> Shopify, Grammarly, GitHub.
- Coined endings useful for availability: `-za`, `-mi`, `-vo`, `-ela`, `-ik`, `-vox`.

## 2. Compounding
Join two real words.
- Facebook, Snowflake, Firefox, Dropbox.

## 3. Blending / portmanteau
Fuse word fragments, often at an overlapping sound.
- Pinterest (pin+interest), Instagram (instant+telegram), Groupon (group+coupon).

## 4. Phonetic substitution / respelling
Swap letters for like-sounding ones, or drop vowels.
- c->k (Kasa), s->z (Bizzy), ph->f, vowel drop (Flickr, Tumblr, Lyft).
- Use sparingly: respelling can fail the "radio test" (SCRATCH: spelling-challenged).

## 5. Semantic expansion (synonyms / related terms)
Replace or add concept-adjacent words via thesaurus thinking.
- "fast" -> swift, rapid, bolt, dash. "money" -> mint, vault, ledger. "care" -> tend, nurture, soothe.

## 6. Sounds-like / rhymes
Near-homophones and rhyming pairs.
- Google<-goggle; rhyme sets bee/tree/free.

## 7. Abstract / coined words
Invent pronounceable strings, often from Latin/Greek roots or evocative phonemes.
- Kodak, Spotify, Verizon, Hulu. Lean on sound symbolism (see methodology) to match the brand feel.

## 8. Syllable / Markov-style synthesis
Generate word-like tokens with a natural consonant-vowel rhythm that read as real but are invented.
- Lumora, Stillwave, Qantel.

## 9. Description-to-name (semantic)
Map a project description/vibe directly to on-brand candidates.
- "calm meditation app" -> Stillwave, Lumora. "AI for sales" -> deal/agent/flow roots.

## 10. Hidden-signal wordplay (distinctive technique)
Embed the letters of a target concept inside a real, natural-reading word - a hybrid of blending and phonetic play. Powerful when the brand wants to hint at a concept (e.g. AI, sale, care) without saying it outright, and great for availability because the results are uncommon.
- Hide **ai** inside real words: av**ai**l (available), s**ai**l (~sale), cl**ai**m, g**ai**n, g**ai**rd (~guard), s**ai**fe (~safe), f**ai**th.
- Combine with care/help/sales roots: faithcare, gaincare, traincare, sailwise.
- Zone-aware variant: when the TLD carries a signal (`.sale`, `.ai`, `.care`), put the other concept in the name (e.g. `neuro.sale`, `agent.sale`).

## Practical generation tips
- Generate 15-40 raw candidates before filtering; quantity first.
- Prefer <= ~15 characters total; shorter is more memorable and easier to obtain.
- Avoid hyphens, numbers, and double letters across the join (e.g. care+ed).
- Don't repeat the zone's meaning in the name (no "ai" in a `.ai` domain).
- Mix techniques: a good batch contains some compounds, some coined words, and some hidden-signal plays - not 20 variants of one keyword.
- Batch the survivors into one `domain-check` call per zone for speed.
