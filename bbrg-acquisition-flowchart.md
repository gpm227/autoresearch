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

Every FDD reference does one of two jobs: **justifies the action** (the disclosure that makes the move right) or **causes risk** (the obligation or defect the move creates). The FDD only summarizes; the underlying contracts control.

| Section | Justifies the action | Causes risk |
|---|---|---|
| **Gate 1 · Economics** | Item 21 is the only provable earnings base; if it can't support $20M cash, the earnout is the bridge. Tom refusing it means his own FDD doesn't carry his price — the walk is justified by the disclosures, not posture. | The earnout rides on future royalties, and Item 20's outlet table shows how fragile that stream is — closures shrink the earnout after signing. Price it off Item 20's trend, not the current store count. |
| **Document pull** | Items 1, 13, and 17 name exactly these three contracts — OA, JJ&N license, FAs — as what controls the deal. That's why these documents come first. | Stopping at the summaries: the FDD updates annually, so anything signed since its issuance date isn't in it. Pricing off summaries imports their staleness. |
| **Gate 2 · Trademarks** | Item 13 must disclose that BBRG licenses rather than owns its marks, plus every license condition. That disclosed dependency elevates the marks to a gate with its own walk-away. | Closing on a license terminable at change of control means the acquisition can strip the brand: royalty contracts with nothing behind them, an amended Item 13 confessing it, and rescission exposure. Hence the non-negotiable walk. |
| **Gate 3 · Operating agreement** | The OA's transfer article dictates which closing structure is legally available; Item 17's contract terms confirming the franchisor can assign freely is what clears an equity close at all. | Any track triggers Item 1 and Item 21 amendments — and new franchise sales go dark until the amended FDD issues. A hidden franchisee consent right puts every store on the critical path. |
| **Track A · one signing** | The drag-along is the legal authority to compel the minority into one deal — one closing, one Item 1/21 amendment, shortest sales blackout. | The 4%→5% step cannot be imposed mid-term — forcing it early breaches the FAs and lands in Item 3. It arrives only at renewal, disclosed in Items 5–6 of the successor FDD. |
| **Track B · control first** | Unrestricted transfers mean no minority consent is needed to sell control — Close 1 is lawful day one, and the blended offer is made from control. | Operating with a 45% minority: any dispute becomes Item 3 litigation in your own FDD. Partner stores transfer under Item 17 conditions (consent, ROFR, fee). A disclosable lawsuit costs more than 6–18 months. |
| **Track C · five-party close** | With transfers blocked or ROFR'd, a partial close is void, not slow — the simultaneous five-party signing is the only structure the OA permits. | Every signature is on the critical path; one holdout kills the closing, and the OA's ROFR notice periods set the clock. Worst execution risk of the three tracks. |
