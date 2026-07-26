---
name: deal-voice
description: Greg's writing voice and hard rules for the Board & Brew deal documents (the Gart brief, the decision map, the operator terms). Load this BEFORE writing or editing any BBRG deal document, drafting deal correspondence, or reviewing document language. Every edit pass must end with the checks at the bottom.
---

# Deal voice: Board & Brew documents

The reader is a fund senior at Gart coming in cold. The bar: an MD reads it and thinks the author is articulate and precise. Every sentence carries a fact, a term, or an instruction. Anything else gets cut.

## Hard bans (automated check required)

- **No em dashes or en dashes** anywhere in visible text. Use commas, semicolons, or new sentences.
- **No "not X, it's Y" rhetorical constructions.** Substantive short contrasts are allowed only when both halves carry information ("delay costs the sellers, not us").
- **Banned words and framings:** "payday" (talk value creation and the asset), "purgatory", "dead ends", "juicy", "sweep" (no pressure framing on minority partners), any meta language about the document or its reader ("for a reader coming in cold", "this brief shows").
- **No editorial or commentary sentences.** No cleverness for its own sake. Headlines state the point in plain words; never metaphor headlines ("the machine runs", "money stays clean", "stacks up").

## Facts and framing rules

- Never state Tom's personal ~$4M income. The anchor is the audited ~$1.4M of BBRG distributions to his 55%.
- Say "unknown until diligence review", never "not promised" or "not guaranteed".
- Money statements must be unambiguous. Keep the three pieces distinct everywhere: cash wired at closing ($4M to $5M), the note ($6M to $7M), the earnout ($8M to $10M). "Firm" means cash plus note ($10M to $12M) and must be labeled as such; never let a subtotal read as day-one cash.
- Rate raises are structural upside with timing unknown per contract; never sell them as guaranteed.
- Minority partners get the collaborative framing: their timeline, run their stores or sell on the same terms later. Never adversarial language.
- Titles are plain descriptions ("asset summary and acquisition plan"), never slogans.
- Every number cites its source: FDD item, FA section, Exhibit B, USPTO reg, IRC section, or the Q1 2026 sales report.
- Compound modifiers are written open, matching the docs: "cross conditioned", "store by store", "bottom up".

## Editing workflow

1. Grep the exact current text before any python string replacement; report misses instead of silently skipping.
2. Edit the scratchpad artifact body, then rebuild the standalone repo file by wrapping (doctype + head, split on `</style>`).
3. Run the checks below on the rendered text (tags stripped). All must pass before publish, send, or commit.

## Pre-publish checks

```python
import re
txt = re.sub(r'<[^>]+>', ' ', open(path).read())
assert '–' not in txt and '—' not in txt          # no en/em dashes
for w in ['payday','purgatory','juicy','for a reader']:
    assert w not in txt.lower(), w
# review every remaining ", not " hit by hand; keep only substantive contrasts
[print(m.group(0)) for m in re.finditer(r'[^.]*, not [^.]*\.', txt)]
```

Never commit `fdd.pdf` or any seller document to the repo. Scratchpad only.
