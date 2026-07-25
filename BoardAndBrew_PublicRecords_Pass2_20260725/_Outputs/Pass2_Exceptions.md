# Board & Brew — Public Records Pass 2 — Exceptions, Conflicts & Ambiguities
Run date: 2026-07-25 · Read-only public-records retrieval · Tools this session: WebSearch + WebFetch only (no browser automation, no screenshots, no authenticated portals).

## 0. Environment / methodology exceptions (new this pass)

- **E0-1. Pass-1 artifacts not present in this environment.** The working directory is an unrelated code repository; the pass-1 workbook, summary memo, and ER diagram were not reachable. Pass-2 outputs were therefore built fresh. The "extend the existing workbook / keep existing tab structure" instruction could not be honored literally; tab names requested for pass 2 were created new.
- **E0-2. No screenshot/PDF capture capability.** The run environment exposes only text retrieval (WebSearch/WebFetch). The instruction "every retrieval gets a screenshot or PDF capture, including null results" could not be met. Evidence is recorded as saved text/markdown notes plus the exact URL and outcome in the source log. This is a tooling GAP, flagged for a follow-up pass with a real headless browser.
- **E0-3. Authenticated / JS / paywalled portals blocked wholesale.** The following returned 403, a JS-only shell, a human-verification wall, or a login/purchase requirement and produced no machine-readable record this session: USPTO tmsearch & TSDR & Assignment Center; CA SOS bizfileonline; CA SOS trademark DB; DFPI DOCQNET; Florida Sunbiz; Trellis.law; OpenCorporates entity pages; Bizapedia entity pages; web.archive.org (Wayback, host-blocked); PACER (also excluded by the no-login/no-purchase hard constraint). Each specific attempt is logged in Pass2_SourceLog.csv as `blocked`.
- **E0-4. PACER cannot be used at all.** PACER requires an account and per-page fees. Hard constraint #2 forbids logins/purchases. All "PACER bankruptcy search" items (Track 7) are therefore GAP by rule, not merely by tooling. Follow-up path: PACER account + fee, or the free CourtListener/RECAP mirror.

## 1. Carry-forward of pass-1 open items

The pass-1 Exceptions tab was not available in this environment, so the "eight open items" could not be quoted verbatim. Reconstructed below from the task's own "resolve these / verify these" lists, which are the implied open items. Status as of this pass:

| # | Implied open item | Status this pass |
|---|---|---|
| 1 | Roster-only stores absent from ABC table (HB, Costa Mesa, Lake Forest, Thousand Oaks, Santa Ana, Cypress, Tempe, San Clemente) — resolve operator/existence | **RESOLVED (operating status)** — all 8 confirmed open; operators named for Thousand Oaks (Hubbard) & Cypress (Williams); HB operated by OC Sands LLC (SOS mirror). Health-permit records still GAP. |
| 2 | Torrance address discrepancy (21221 Hawthorne Ste 140 vs 21211 Hawthorne Unit A) | STILL OPEN — not independently resolved; ABC value (21211 Unit A) retained |
| 3 | Ladera Ranch address discrepancy (5606 vs 25606 Crown Valley Pkwy) | RESOLVED (provisional) — 25606 retained; 5606 = dropped-digit typo |
| 4 | Harbor: 3030 Harbor Blvd Costa Mesa vs 825 N Harbor Dr San Diego — same or two stores? | **RESOLVED — two distinct stores.** 825 N Harbor Dr = SDB&B LLC (real 2025 CA entity, doc B20250024014); Costa Mesa 3030 Harbor is a separate OC store (open per Yelp) |
| 5 | Formal entity behind "Board & Brew Coastal Carlsbad" (2 EDD liens) | **RESOLVED (entity)** — CCB&B LLC / Two Rippers LLC per BBB. The liens themselves still GAP |
| 6 | "Board and Brew, Inc." (~1987) predecessor — confirm/exclude | STILL OPEN — not located this pass |
| 7 | CA state trademark registration 66474 (1979) status | STILL GAP — CA SOS trademark DB is JS-only, not fetchable |
| 8 | Rosen v. JJ&N Enterprises — run to docket level | **PARTIALLY RESOLVED** — confirmed to index level (filed 2011-08-09, dismissed w/ prejudice, plaintiff Kenneth Rosen); exact docket number still GAP (UniCourt body 405-blocked) |

## 2. Conflicts / discrepancies surfaced this pass

- **X-1. BBRG name collision (material).** "BBRG" collides with **Bravo Brio Restaurant Group, Inc.** (public co., former NASDAQ: BBRG) and its many Florida "BBRG TR, LLC" / "BBRG Holdings, LLC" subsidiaries. Aggregator and Sunbiz results for "BBRG" overwhelmingly return the Bravo Brio cluster, not the Del Mar "Board and Brew Restaurant Group, LLC." Any BBRG hit must be identity-checked against CA #201202610156 / the Del Mar or San Clemente address before attribution.
  - Consequence for **Miami-Dade 2024-010471-CA-01 (BBRG, LLC et al v. Christopher Viso):** the plaintiff cluster aligns with the Florida BBRG (Bravo Brio) entities, not the California Board & Brew entity. **Provisional resolution: EXCLUDE the Del Mar entity** (SEARCH-INFERRED; Trellis/Sunbiz detail pages were blocked, so not confirmed to docket level).
  - Consequence for **OSHA:** an inspection record "Bbrg Tr, Llc" (id 1134580.015) is the Florida Bravo Brio entity, NOT a subject here.
- **X-2. BOARD & BREW SAUCE registration number.** Task lists Reg. **7192954** for BOARD & BREW SAUCE; the owner portfolio shows that mark as **serial 97561220**, registered 2023-10-17. These are consistent (a serial and its resulting registration), not a conflict — recorded for traceability.
- **X-3. Pacific Beach: liquor DENIED vs. storefront operating.** ABC shows 4516 Mission Blvd (PBB&B, LLC) as DENIED, yet the storefront traded and took a temporary SD County health closure 2024-12-10. Two public records disagree on whether the store was operating.
- **X-4. Cypress address.** Roster/ABC = 5253 Katella Ave; the opening news article = 5247 Katella. Same shopping center; digit-level discrepancy unresolved.
- **X-5. Two Rippers LLC address.** SOS-mirror principal/mailing address is 23801 Calabasas Rd #2026, Calabasas (LA County) though the store it licenses is in Carlsbad — a mailing-agent address, not the store; flagged so it is not mistaken for a location.
- **X-6. Clayton Wheeler record gap.** Press consistently names Wheeler as first B&B franchisee / Blacktop co-founder, but no fetchable SOS filing lists him as manager/member of a specific B&B LLC; the bizprofile "Clayton Wheeler" authorized-person page shows unrelated namesakes. Association is press-level only.
- **X-7. Mike Murphy & SCBNB LLC.** No public record located tying Mike Murphy to any B&B entity; SCBNB LLC (the presumed San Clemente entity) returned no fetchable filing. Both remain unresolved (GAP), not excluded.
- **X-8. New franchisee entity from PAGA data: CYPRESSBNB LLC.** The Cypress store's PAGA notice (LWDA-CM-1147303-26) names **CYPRESSBNB LLC dba Board and Brew** — an operating entity NOT on the task's franchisee list. Reconcile against the roster (Cypress operator was independently reported as Mike Williams). Added to the record; entity managers not yet retrieved (SOS blocked).
- **X-9. OSB&B, LLC PAGA location.** OSB&B is the Oceanside licensee on the ABC table, but its PAGA notice (LWDA-CM-922795-22) lists the employer location as **Cardiff-by-the-Sea (92007)** and escalated to San Diego Superior case 37-2023-00005528-CU-OE-CTL. Address basis of the filing differs from the ABC store city — noted, not reconciled.

## 3. Ambiguities resolved by reasonable default (unattended-run choices)

- Root folder dated 20260725 (retrieval date = today), though file-name examples in the brief used 20260728; retrieval-date convention followed.
- Where the roster and the ABC table disagree on an address, the ABC (primary licensing record) value is used as the workbook's canonical value and the roster value is logged as the discrepancy.

_(Sections 1, 2 will be updated as the four breadth agents report. Any two-source disagreement they surface is appended here.)_
