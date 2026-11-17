# AgriScout – storage ranges and hot-climate evidence (sources)

Researched 5 Oct 2026 for the DHT11 "Storage conditions" card section.
Every number below comes from the quoted source. Re-open each link and confirm before it goes into the knowledge base.

**Verification (2026-10-05)** – the sources used by the card (`data/conditions_seed.json`, `verified_note`):

| Source | How checked | Result |
|---|---|---|
| UC Davis Tomato (node/8181) | by hand in a browser (site blocks automated fetch) | all wording in section A matches |
| UC Davis Bell Pepper (node/8026) | by hand in a browser (site blocks automated fetch) | all wording in section A matches |
| FAO Bulletin 152, section 3.2 | automated fetch of the page | Q10 sentence matches word for word |
| Amha et al. (2025), AJFAND 25(9) | by hand in a browser (PDF not readable by automated fetch) | confirmed, with the precisions in section C |

## A. Ideal storage ranges (what the card compares against)

| Crop | Value | Exact wording in source | Source |
|---|---|---|---|
| Tomato, mature-green | 12.5–15 °C | "12.5-15°C (55-60°F)" | UC Davis Postharvest Technology Center, Tomato – Recommendations for Maintaining Postharvest Quality. https://postharvest.ucdavis.edu/node/8181 |
| Tomato, firm-ripe | 7–10 °C, 3–5 days only | "7-10°C (44-50°F) for 3-5 days" | same page |
| Tomato, humidity | 90–95 % RH | "90-95%" | same page |
| Tomato, chilling injury | below 10 °C for > 2 weeks, or 5 °C for > 6–8 days | "at temperatures below 10°C (50°F) if held for longer than 2 weeks or at 5°C (41°F) for longer than 6-8 days" | same page |
| Pepper (bell/green) | best at 7.5 °C (3–5 weeks) | "Storage at 7.5°C (45°F) is best for maximum shelf-life (3-5 weeks)" | UC Davis Postharvest Technology Center, Bell Pepper. https://postharvest.ucdavis.edu/node/8026 |
| Pepper, above 7.5 °C | more water loss | "Peppers stored above 7.5°C (45°F) suffer more water loss and shrivel." | same page |
| Pepper, cold limit | 5 °C for max. 2 weeks | "peppers can be stored at 5°C (41°F) for 2 weeks, and although this reduces water loss, chilling injury will begin to appear after that period." | same page |
| Pepper, humidity | > 95 % RH | ">95%" | same page |
| Pepper (second source) | 45 °F (≈7 °C), 90–95 % RH, not below 40 °F (≈4.4 °C), up to 3 weeks | "Optimal storage temperature is 45°F with 90-95% relative humidity." / "do not store peppers below 40°F." | Univ. of Kentucky Center for Crop Diversification, Post-Harvest Handling Bell Peppers (CCD-IG-14). https://ccd.uky.edu/sites/default/files/2024-12/ccd-ig-14_post-harvest-handling-of-bell-peppers.pdf |

**Correction to earlier advice:** the UC Davis pepper page does **not** say "7.5 ± 0.5 °C" or "chilling injury below 7 °C". It says 7.5 °C is best, 5 °C is acceptable for up to 2 weeks, and above 7.5 °C peppers lose more water.

## B. Rules for a hot climate without cold storage

| Rule | Exact wording in source | Source |
|---|---|---|
| Spoilage speeds up 2–3× per 10 °C (Q10) | "The rate of deterioration of perishables however increases two to three-fold with every 10 °C increase in temperature" | FAO Agricultural Services Bulletin 152, *The role of post-harvest management in assuring the quality and safety of horticultural produce*, section 3.2. https://www.fao.org/4/y5431e/y5431e04.htm |
| Tomatoes ripened above 25 °C colour poorly | "Tomatoes allowed to ripen off-the-vine above 25°C (77°F) will develop a more yellow and less red color and will be softer." | UC Davis Tomato page (above) |

Worked examples (factor = 2^(ΔT/10) to 3^(ΔT/10), ΔT = reading − top of ideal range). These are rough estimates: the FAO rule is applied over a wide temperature span.

| Crop | Reading | ΔT | Spoils roughly |
|---|---|---|---|
| Tomato (top 15 °C) | 28 °C | 13 °C | 2.5–4× faster |
| Tomato | 31 °C | 16 °C | 3–6× faster |
| Pepper (top 7.5 °C) | 28 °C | 20.5 °C | 4–9.5× faster |
| Pepper | 31 °C | 23.5 °C | 5–13× faster |

## C. Zambia / Southern Africa context

| Finding | Exact wording / figures | Source |
|---|---|---|
| Lusaka open market losses | Tomato quality loss 68.6 %; causes include mishandling 22.9 %, **high temperatures 20.1 %**, lack of storage facilities 17 %; 23 % of retailers cope by "keeping the produce cool to avoid damage by the sun" | Mwamba, J. (2025). *Estimation of post-harvest losses on vegetable value chain in Lusaka open market.* University of Zambia. https://dspace.unza.zm/handle/123456789/9391 |
| Lusaka temperatures | Long-term average daily max/min: Sep 29/14 °C, **Oct 31/18 °C**, Nov 29/17 °C (converted from 84/57, 88/64, 84/63 °F) | weather2travel.com, Lusaka climate (data from CRU Univ. of East Anglia, Met Office, KNMI). https://www.weather2travel.com/zambia/lusaka/climate/ |
| Our own measurement | Indoors, 5 Oct 2026, 10:18: 28–29 °C, 57 % RH (DHT11 on the tank) | AgriScout test log |
| South African smallholders | 73.07 % of tomatoes substandard at farm level; losses higher in open fields than tunnels | Molelekoa et al. (2025). *Quality and quantity losses of tomatoes grown by small-scale farmers under different production systems.* Univ. of Pretoria. https://repository.up.ac.za/items/024b600e-163d-40e6-8169-2ad10cc26569 |
| Zero-energy cool chamber (ZECC) | **Mean maximum** temperature 5.7–6.8 °C lower than room storage (5.66 °C Addis Ababa, 6.82 °C Woliso; measured during the kale storage period); RH up to 85 % vs **at most** ~40–45 % in the room (40.5 % / 44.5 % were the highest room values); tomato shelf life, room storage → ZECC: **Addis Ababa 16 → 24 days, Woliso 12 → 22 days** (two trial sites) | Amha, B. et al. (2025). *Enhancing the shelf life of kale and tomato using a zero-energy cooling chamber in Ethiopia.* AJFAND 25(9). https://ajfand.net/Volume25/No9/Amha25545.pdf |

## D. Gaps (not found in a reliable source)

- Monthly average **humidity** for Lusaka: not found from a reliable source. Use our own DHT11 measurements instead.
- ZECC trials **in Zambia or Southern Africa** with numbers: not found. The figures above are from Ethiopia (East Africa).
- A Zambian (ZARI / Ministry of Agriculture) storage guideline with temperature ranges: not found online.

## E. Proposed card rules (for Claude Code)

- **Tomato**: verdict against 12.5–15 °C; also show "ripe fruit: 7–10 °C for 3–5 days". Too cold below 10 °C (chilling injury risk if kept > 2 weeks). Above 25 °C add: "Tomatoes ripening above 25 °C turn more yellow and softer." RH target 90–95 %.
- **Pepper**: best 7.5 °C. Above 7.5 °C: "more water loss and shrivelling". Below 5 °C: "too cold - chilling injury risk". 5–7.5 °C: "OK for up to 2 weeks". RH target > 95 %; DHT11 reading ≥ 90 % → "at least 90 % - the DHT11 can't confirm the > 95 % target".
- **Above the ideal**: show the Q10 estimate "about X–Y× faster than in cold storage" (FAO), labelled as an estimate.
- **Low-cost actions** (no numbers): harvest early morning; shade and airflow, never in direct sun; damp sack when the air is dry; evaporative cooler / zero-energy cool chamber (Ethiopian trial: mean maximum temperature 5.7–6.8 °C lower, RH up to 85 % vs at most about 40–45 % in the room, tomato shelf life 16 → 24 days in Addis Ababa and 12 → 22 days in Woliso).
- Climate is advice only: it never changes the scan status or LED feedback.
