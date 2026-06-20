# Investigator Experience — Implementation Plan

> A focused, implementation-ready plan for the three highest-leverage improvements
> to the **analyst's** day-to-day experience, mapped to the investigation
> lifecycle (collect → triage → analyze → connect → preserve → report). Each
> reduces a concrete, measurable source of friction. All three respect the
> project's principles: local-only, graceful degradation, secrets-in-`.env`,
> passive lawful OSINT only (no offensive/exploit capability).

## Why these three

| # | Feature | Friction it removes | Lifecycle stage |
| --- | --- | --- | --- |
| 1 | **Entity dossier** | "Everything about this person/org is scattered across items" | Analyze / Connect |
| 2 | **Contextual pivots** | "I have to leave the item, switch pages, and retype the target into a tool" | Pivot / Enrich |
| 3 | **Auto-triage ("needs your eyes")** | "I drown in 200 items to find the 10 that matter" | Triage |

They interlock: Feature 1 introduces an **entity index** that also powers
cross-case linking and speeds up the graph; Feature 2 launches from entities
surfaced in Feature 1 and the feed; Feature 3 routes the analyst's attention to
high-signal items whose entities link straight into Feature 1.

---

## Shared foundation: the entity index

Today, AI-extracted entities live as JSON inside each item's `analyses.entities`
row — there is **no index**, so "every item mentioning X" means a full scan. A
small index unlocks all three features and makes the existing graph faster.

**Schema** (`nexus/db.py`, idempotent migration):

```sql
CREATE TABLE IF NOT EXISTS item_entities (
  item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  name      TEXT NOT NULL,           -- display form (keeps casing)
  name_norm TEXT NOT NULL,           -- canonical key (storage._norm_entity_key)
  kind      TEXT,                    -- person | organization | location | identifier
  PRIMARY KEY (item_id, name_norm)
);
CREATE INDEX IF NOT EXISTS idx_item_entities_norm ON item_entities(name_norm);
CREATE INDEX IF NOT EXISTS idx_item_entities_item ON item_entities(item_id);
```

**Population** — in `storage.save_analysis()`, after writing `analyses`, decode the
entities (`_decode_entities`) and upsert rows into `item_entities`.

**Backfill** — on `init_db()` upgrade, if `item_entities` is empty but `analyses`
has rows, scan existing `analyses.entities` once and fill the index. Idempotent,
fail-soft (graceful degradation).

**Alias resolution** — reuse the existing `entity_aliases` table. At query time,
resolve a requested name to its canonical and include all aliases that map to it,
so "Messi" and "Lionel Messi" return one merged result.

> This foundation is the single most reusable piece of work in the plan.

---

## Feature 1 — Entity dossier

**Goal:** every entity name, everywhere in the UI, links to one page that shows
*everything* known locally about that entity.

### Storage (`storage.py`)
`entity_profile(conn, name, *, offset=0, limit=50)` returns:
- **identity** — canonical name, kind, and the alias set it resolves to.
- **stats** — items mentioning it, distinct sources, first-seen / last-seen.
- **items** — feed rows (join `item_entities` → `items` + `analyses`, newest
  first, paginated), passed through `enrich_feed_rows`.
- **connections** — top co-occurring entities (self-join `item_entities` on shared
  `item_id`, ranked by shared count), each linkable to its own dossier.
- **cases** — cases this entity appears in (via `bookmarks` → pinned items, and/or
  case tracking-term matches).

### Routes (`web/app.py`)
- `GET /entity/{name}` → renders `entity.html`.
- `GET /entity/{name}/items?offset=` → htmx "load more" (mirrors `/feed/page`).

### Template (`entity.html`)
Header (name + kind + stats) · alias panel (add/merge, reuse `/graph/alias`) ·
connections (clickable chips → other dossiers) · a compact timeline of appearances
· cases it appears in · the item list (reuse `_feed_item.html` cards, with the
existing per-card actions).

### Wiring (turns every entity into a dossier link)
Replace `/?q=<name>` entity links with `/entity/<name>` in:
`_feed_item.html`, `_item_detail.html`, and the ranked tables in
`_entity_graph.html` (and, optionally, graph node clicks).

### Edge cases
Normalize with `_norm_entity_key` (quotes/punctuation/case) · paginate large
entities · graceful empty state when no AI has run · alias loops guarded.

**Effort:** index + backfill ½d · queries ½d · route + template + wiring 1d.

---

## Feature 2 — Contextual pivots (run the right OSINT tool from any identifier)

**Goal:** from any identifier shown in an item, the entity dossier, or a graph
node, one click runs the matching **passive** OSINT tool — no page switch, no
retyping.

### Target → tool mapping (`toolguide.py`)
Add `suggest_tools(target_type)` using the existing `TOOL_CATALOG`:

| Identifier type | Suggested tools (passive recon only) |
| --- | --- |
| email | holehe, h8mail, ghunt |
| username / handle | sherlock, maigret, socialscan |
| phone | phoneinfoga |
| domain | theharvester, subfinder, dnstwist |
| instagram | toutatis |

Detect the type from the identifier value with light regex (email / phone /
domain / `@handle`), defaulting to "username" for bare tokens.

### Backend
Reuse the existing `POST /tools/run` (`tool`, `target`) → `_tool_results.html`
(findings + entity graph). Only show tools whose `is_available()` is true; for
the rest, show the one-line install hint from `toolguide`. **No exploit tools
exist or will be added** — this stays passive recon, consistent with the
assistant hardening.

### Frontend
A "Pivot ⚡" dropdown next to each identifier (feed card, item detail, dossier,
graph node). Selecting a tool does an htmx `POST /tools/run` and renders the
result into the slide-in drawer (`#item-drawer-content`).

### Edge cases
Sanitize the target before it reaches a subprocess (the adapter layer already
does, but validate type) · tool missing → install hint, never an error · long
runs → the adapter's own timeout + a spinner · findings feed the entity graph.

**Effort:** mapping + suggest helper ½d · pivot UI + drawer wiring 1d.

---

## Feature 3 — Auto-triage ("needs your eyes")

**Goal:** instead of paging through everything, the analyst opens one short,
ranked list of the items that actually warrant attention — computed from signals
the AI **already** produces, so it costs no extra tokens.

### Signal score (computed, not stored)
Combine existing per-item signals into one rank:
- best requirement relevance (`requirement_hits.score`, 0–100) — the strongest
  signal: "this answers a question you care about";
- threat level (`analyses.threat_level`);
- `contradiction` / disputed flag;
- watchlist hit;
- cross-source corroboration (`source_count` ≥ 2, from `enrich_feed_rows`).

Compute in SQL/enrich; **no new AI call**.

### Behaviour (non-destructive)
- A **"Needs your eyes (N)"** view = top unread items by signal, above a
  threshold, capped (e.g. 15). Each row shows *why* it surfaced
  ("answers Q3 · 92/100", "high threat", "disputed", "watchlist: 0xabc…").
- Low-signal items are **filtered from this view, not deleted** — they stay in the
  full feed. (Optional opt-in: auto-dismiss the very lowest signal, always
  reversible.)

### Backend / Frontend
- Route `GET /attention` (or a feed `scope=attention`) ordering by the computed
  signal with the cap, reusing `_paged_feed` plumbing and feed cards.
- Surface it prominently on the **Brief** ("Needs your eyes" block) and/or the nav,
  with the live count.
- Thresholds configurable in `meta` so an analyst can tune sensitivity.

### Edge cases
No AI / no scores → fall back to threat + watchlist + corroboration only, and say
so · explainability is mandatory (always show the reason) · never hide items
silently from the *main* feed.

**Effort:** signal query ½d · view + Brief block + explanations 1d · tuning ½d.

---

## How they interlock

```
   Auto-triage  ──surfaces──▶  high-signal items
        │                            │ entities link to
        ▼                            ▼
   the analyst's attention     Entity dossier ◀── entity index ──▶ faster graph
                                     │                                cross-case
                                     ▼ identifiers                    linking
                               Contextual pivots ──▶ passive OSINT tools
```

The **entity index** (shared foundation) is the spine: it powers the dossier,
cross-case "also seen in" linking, and accelerates the existing relationship
graph. Build it once, benefit three times.

---

## Recommended build sequence

| Sprint | Deliverable | Why first |
| --- | --- | --- |
| **1** | Entity index + backfill + **Entity dossier** | Foundation + the biggest mental-model win for an analyst |
| **2** | **Contextual pivots** | Removes the largest workflow break; launches from Sprint 1's dossier |
| **3** | **Auto-triage** | Pure win on top of existing AI signals; routes attention into Sprints 1–2 |

Each sprint ships independently and leaves the app fully working
(graceful degradation throughout).

## Testing & safety (every sprint)

- **Tests:** a unit test for each new storage query (entity_profile, signal rank,
  suggest_tools) against a seeded temp DB; a route smoke test (`200` on a seeded
  DB) for `/entity/{name}` and `/attention`. (This also closes the test gap that
  let an earlier render crash slip through.)
- **Passive-only:** pivots map exclusively to recon tools; no exploit/offensive
  capability is added — consistent with the Sherlock hardening.
- **OpSec preserved:** loopback-only, no secrets in DB/UI/logs, entity links and
  tool targets are sanitized; untrusted text rendered as text.

## Effort summary

| Item | Estimate |
| --- | --- |
| Shared: entity index + backfill | ~0.5 day |
| Feature 1: entity dossier | ~1.5 days |
| Feature 2: contextual pivots | ~1.5 days |
| Feature 3: auto-triage | ~2 days |
| Tests across all three | ~1 day |
| **Total** | **~6–7 focused days**, shippable sprint by sprint |
