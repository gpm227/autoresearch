# BBRG Acquisition — Decision Map

Gart's LC issued, Tom pitched. Three gates — economics, trademarks, operating agreement — then the OA answer sets the closing structure.

```mermaid
%%{init: {
  "theme": "base",
  "flowchart": { "curve": "basis", "nodeSpacing": 45, "rankSpacing": 70, "padding": 12 },
  "themeVariables": {
    "fontFamily": "Georgia, 'Times New Roman', serif",
    "fontSize": "14px",
    "primaryColor": "#FDFDFB",
    "primaryTextColor": "#23272E",
    "primaryBorderColor": "#B9B4A7",
    "lineColor": "#8A8578",
    "edgeLabelBackground": "#F4F3EF",
    "clusterBkg": "#F4F3EF"
  }
}}%%
flowchart LR
    START(["<b>START</b><br/>Gart LC issued<br/>Tom pitched"]) --> P{"Tom takes buyer's case<br/>+ earnout?"}
    P -- "No — cash-only at $20M+" --> W1(["WALK"])
    P -- "Yes" --> DOCS["<b>TOM PULLS 3 DOCS FIRST</b><br/>1 · BBRG operating agreement<br/>2 · JJ&amp;N–BBRG trademark license<br/>3 · Franchise agreements + renewal files"]

    DOCS --> LIC{"Marks securable?<br/><i>JJ&amp;N sale, or license papered<br/>perpetual + CoC-proof</i>"}
    LIC -- "No" --> W2(["WALK<br/>never BBRG<br/>without marks"])
    LIC -- "Yes — each store's mark rights<br/>confirmed per its FA" --> OA{"<b>OA: drag-along?</b><br/>THE fork — sets<br/>closing structure"}

    OA -- "Drag exists · ideal" --> A4["<b>ONE SIGNING</b><br/>Tom + JJ&amp;N + partners<br/>same price, same day"] --> A5["Renewal calendar<br/>store buys, real estate"] --> A6(["<b>OUTCOME</b><br/>100% day one<br/>4% → 5% over 10 yrs"])

    OA -- "No drag, no block · base" --> B3["<b>CLOSE 1</b><br/>Tom's 55% + JJ&amp;N<br/>+ license together"] --> B4["Blended offer:<br/>45% + their 2 stores"] --> B5{"Accept?"}
    B5 -- "Yes" --> B7["Renewal calendar<br/>store buys, real estate"]
    B5 -- "Holdout" --> B6["Patience:<br/>first to sell wins"] --> B7
    B7 --> B8(["<b>OUTCOME</b><br/>same platform<br/>6–18 mo slower"])

    OA -- "Transfers blocked / ROFR · hard" --> C4["<b>FIVE-PARTY CLOSE</b><br/>Bret &amp; Mike in early<br/>everyone signs same day"] --> C5["Renewal calendar<br/>store buys, real estate"] --> C6(["<b>OUTCOME</b><br/>same platform<br/>all-or-nothing day"])

    classDef start fill:#23272E,stroke:#23272E,color:#F4F3EF
    classDef gate fill:#F4EAD3,stroke:#8A6420,color:#4A3510,stroke-width:1.5px
    classDef action fill:#E3EDF5,stroke:#2E5C7E,color:#17364E
    classDef step fill:#FDFDFB,stroke:#B9B4A7,color:#23272E
    classDef outcome fill:#E1EFE7,stroke:#1F6B47,color:#123A28,stroke-width:1.5px
    classDef walk fill:#F6E3E0,stroke:#8E2C25,color:#5A1712,stroke-width:1.5px

    class START start
    class P,LIC,OA,B5 gate
    class DOCS,A4,B3,C4 action
    class A5,B4,B6,B7,C5 step
    class A6,B8,C6 outcome
    class W1,W2 walk
```

## Diligence notes — justifies the action, or causes risk

All cites are to BBRG, LLC's actual FDD (Board & Brew, issuance date February 10, 2026) and its form Franchise Agreement. The FDD does **not** contain the operating agreement or the JJ&N Trademark License (Item 22 attaches only the FA and ADA) — those two documents must still be read before Gates 2 and 3 finally clear.

| Section | Justifies the action | Causes risk |
|---|---|---|
| **Gate 1 · Economics** | Item 21/Exhibit B: audited FY2023–25 financials — the earnout base is provable. Item 20: 25→33 outlets over three years, zero terminations, zero non-renewals — a stream stable enough to pay over time. | Item 20 also shows openings decelerating (+4, +2, +2). Price the earnout off existing-store royalties and renewal step-ups, not the growth story. |
| **Document pull** | Item 22 attaches only the FA and ADA. Item 1 names JJN as mark owner; Item 13 summarizes the license — but the OA and license are not exhibits. The two documents the deal turns on are the two the FDD doesn't carry. | The FDD speaks as of Feb 10, 2026 — later amendments and side letters aren't in it, and pricing off Item 13's summary of an unread license is how the Gate 2 risk gets missed. |
| **Gate 2 · Trademarks** | Item 13 is stronger than assumed: "Board & Brew" incontestable (reg. 5443521, §8/§15 accepted Oct 2023); license royalty-free, exclusive, 10-yr term renewable at BBRG's option for consecutive 10-yr terms, "no significant limitations." An equity purchase keeps the licensee entity intact — on disclosed terms the license should ride through. | Item 13 also discloses the kill scenario: on termination BBRG "would have the right to change the name" and franchisees keep the marks only for their existing FA terms — royalties to expiry, then the platform dies at renewal. CoC-terminability is unverifiable from the FDD; the gate stands until the license paper says otherwise. |
| **Gate 3 · Operating agreement** | Item 17(j)/FA §12.1: "No restriction on our right to assign" — no store's consent needed on any track; the fork is decided by the OA alone. Item 2 seats the table: Powers, Crutchfield, DeSanti (the latter two also manage SCBNB, the affiliate store owner). | §12.1's one condition: assignee must be "willing and financially able to assume our obligations as franchisor" — a thin acquisition vehicle invites challenge. Items 1/21 must be amended before new franchise sales resume. |
| **Track A · one signing** | Drag compels one closing; and the step is papered — Item 17(c): renewal requires the then-current form at new-franchisee fees, and the current form is already 5% + 1% Creative Fund (Items 5–6). Legacy 4% stores step up by contract. | Item 17(b): renewal is non-automatic — one 5-yr add-on, 120-day notice, 90-day holdover, no implied renewal rights. Every renewal is also the franchisee's exit; push carelessly and Item 20's zero-termination table stops being true. |
| **Track B · control first** | Close 1 lawful day one; post-close you inherit FA §12.3 (Item 17(n)) — match "any legitimate offer" for a franchisee's business. "First to sell wins" is a contract right. | §12.3 carve-outs: no ROFR on SBA-guarantor/family transfers or <100% equity transfers — a holdout can move 99% past the match right (§12.2 approval still applies). A minority fight becomes the next entry in a currently spotless Item 3. |
| **Track C · five-party close** | Partial close is void under a blocked/ROFR OA. Item 1 is why Bret & Mike come early: the seven "company-owned" outlets are held by affiliates under common ownership — the partners' entities, not BBRG's. | One holdout kills the closing; OA ROFR notice periods set the clock; each affiliate store entity needs its own purchase paper on the same day. Worst execution risk of the three tracks. |
