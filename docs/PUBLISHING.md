# Edit and publish (write-back)

Nexus can send your analysis back out: an item you edited, or a short write-up
built from several pinned items, goes to **Confluence**, your **Obsidian
vault** or the **knowledge pool**. Three rules always apply:

- **Nothing is sent without your click.** Every publish first shows a
  preview of exactly what will be sent or written. Only **Confirm & publish**
  sends it.
- **Writing is off by default.** Confluence and Obsidian each have their own
  opt-in switch. The read connectors work without it.
- **Collected content is never changed.** Your edits are stored as a separate
  analyst layer next to the original.

See [INGESTION.md](INGESTION.md) for how to connect Confluence and Obsidian in
the first place.

## 1. Edit an item

Open an item (the drawer) and press **Edit**. You can change:

- the **title**,
- the **summary**,
- an **analyst note** (your assessment, context, next steps).

The drawer then shows an **edited** badge with the edit time. The original
title, the AI summary and the full collected content stay visible below it.
To remove your edit, clear all three fields and save.

## 2. Compose a write-up from several items

On a case, open the **⋯** menu → **Write-ups & publish…**.

1. Tick the pinned items to include and give the write-up a title.
2. **Create draft from selected items** writes a first draft: one line per
   item with its (edited) summary and analyst note.
3. Edit the text and press **Save**. A blank line starts a new paragraph. A
   line starting with `- ` becomes a bullet.
4. Publish it from the same page. The sources (titles, links, collected dates)
   are added automatically.

## 3. Publish targets

Open **Publish** in the item drawer, or the Publish box on a write-up page.

### Confluence

Setup (once per connection, under **Sources › Confluence**):

1. Connect Confluence as described in INGESTION.md. For publishing, the
   account behind the token needs **write** permission in the target space.
   On Cloud, a scoped API token also needs write scopes.
2. Press **Test connection**. It now also reports **write permission** for
   each chosen space (yes / no / unknown).
3. Press **Allow publishing to Confluence** to turn the switch on. Turn it off
   again with **Stop publishing**.

Publishing:

- **New page:** choose the space key and, optionally, a parent page (search
  by title) and a page title.
- **Append a section to an existing page:** search for the page and pick it.
  Your section is added at the end. The rest of the page is not touched.

What is sent:

- The text is converted to Confluence storage format. **Everything is
  escaped**: no HTML or macro markup from any input reaches the page.
- Only `http(s)` links become links.
- The page includes the source link, the collected date, the case, the edit
  date, the entities and a "Collected by Nexus-OSINT" footer.

Republishing:

- Republishing updates **the same page** (version + 1). Only Nexus's own part
  is replaced, whether it created the page or appended a section. Anything
  colleagues added to the page is kept. That part is found by two invisible
  anchor macros.
- If someone saved the page in between (HTTP 409), Nexus re-reads the page
  and retries once.
- HTTP 429 / 503 are retried after `Retry-After`.
- If the page was deleted in Confluence, Nexus says so. Publish again to create
  a new page.

Cloud uses REST v2 for pages. Data Center / Server uses REST v1. Requests go
through the same network guard as the connector: internal hosts only when
allow-listed, the internal CA honoured, the token never logged.

### Obsidian vault

Setup (under **Sources › Obsidian**, on the vault's row):

1. Tick **Allow writing to the vault** and press **Save**.
2. Optionally change the folder Nexus writes into (default `Nexus`). It must
   be a plain relative folder inside the vault: no `..`, no hidden folders,
   no drive letters, at most 4 levels.

What Nexus writes:

- One Markdown note per item or write-up, **only inside that folder**.
- The folder and every folder above it must be real folders. A symlink or
  junction is refused, and the resolved path must stay inside the vault.
- Frontmatter is written with `yaml.safe_dump`. It holds `generated_by:
  Nexus-OSINT`, a `nexus_id`, the title, the source and URL, the case, tags,
  and the collected and edited dates.
- Entities become `[[wikilinks]]`, so they show up in Obsidian's graph.

Your notes are never overwritten:

- Nexus replaces a note only if its frontmatter says Nexus wrote it **for the
  same item or write-up**.
- If any other file already has the name, the note is saved as
  `Title (2).md`, `Title (3).md` and so on.
- Text you add **below the `%% nexus:end … %%` line** is kept when the note
  is republished.

The importer (**Obsidian: import vault**, and a zipped vault uploaded under
Documents) skips notes Nexus wrote itself. They are never imported again as
duplicates.

### Knowledge pool

**Add to knowledge pool** stores your edited item or write-up as a document in
the pool, so Sherlock answers from your version. Republishing replaces that
document. Nothing leaves the computer.

## 4. "Published to …"

Every publish is recorded on the item or write-up: the target, the page or
note, a link and the time. The drawer and the write-up page list them under
**Published to**. Obsidian notes open through an `obsidian://` link.

### Deleting a write-up

**Delete…** on the write-up page opens a dialog that lists every place the
write-up was published, each with a checkbox:

- **Knowledge pool**: the analyst document is removed (ticked by default).
- **Obsidian**: the note is removed (ticked by default) only if Nexus created
  it inside its own vault folder, it still carries the Nexus marker, nothing
  was typed below the `%% nexus:end %%` line and it was not changed after
  Nexus wrote it. Otherwise it is left alone and the dialog says why. Needs
  **Allow writing to the vault**.
- **Confluence**: only Nexus's own section is removed from the page; the page
  itself is never deleted and colleagues' additions stay. Needs **Allow
  publishing to Confluence**; version conflicts (409) and rate limits (429)
  are handled as when publishing.

After the delete, a result is shown per target. Anything left in place, or
not reachable (vault missing, Confluence down), keeps its publish record.

## 5. Sherlock

You can ask Sherlock, for example "publish item 42 to Confluence", "append
item 42 to the page Weekly notes", "save this write-up to Obsidian" or "add
item 42 to the knowledge pool".

- Sherlock **only prepares** the publish. The chat shows a card with the
  destination, the exact preview and a **Confirm & publish** button. Only your
  click on that card sends it.
- A card is prepared only when **your own message** names the target
  (Confluence, Obsidian / vault, knowledge pool) **and** the item or write-up
  number. Instructions hidden in collected content cannot trigger a publish,
  and the model has no action that executes one.
- For Confluence, Sherlock only uses the connection's chosen spaces, and a
  page to append to must match its title exactly.
- If the content is too long to show in full in the chat, Sherlock sends you
  to the item or write-up page to publish it there.
- The opt-in switches apply. If publishing is off, Sherlock tells you where to
  turn it on.
- A card loaded again from chat history is inert: the one-time token is gone.

## 6. When nothing is set up

The publish dialog explains what is missing and links to **Sources ›
Confluence**, **Sources › Obsidian** or **Setup**. No new `.env` keys are
needed. The Confluence token is the same one the connector already keeps in
`.env` (`CONFLUENCE_SOURCE_<id>_TOKEN`).

## Limits at a glance

| Area | Rule |
|---|---|
| Confirmation | Preview + click for every publish; preview token single-use, expires after 15 minutes |
| Confluence | Opt-in per connection; storage format fully escaped; same host only; 409 retried once; 429/503 after `Retry-After` |
| Obsidian | Opt-in per vault; only the write folder; no traversal, symlink or junction escape; never replaces a note it did not write for the same subject |
| Sherlock | Prepares only; needs your own request; no execute action exists |
