# Nexus-OSINT — User Guide

A complete, plain-language manual for using Nexus-OSINT: a local-first open-source
intelligence workstation. Everything runs on your own machine and binds to
`127.0.0.1` — nothing is exposed to the internet, and your data never leaves your
computer unless you explicitly export it.

> New here? Read **[Getting started](#1-getting-started)** and **[The interface, page by page](#4-the-interface-page-by-page)**, then come back for the rest as you need it. There is also a built-in **Guide** page inside the app (top menu) with the same concepts.

---

## Table of contents

1. [Getting started](#1-getting-started)
2. [Core concepts](#2-core-concepts)
3. [Your first investigation in 5 minutes](#3-your-first-investigation-in-5-minutes)
4. [The interface, page by page](#4-the-interface-page-by-page)
5. [Sherlock, the in-app chat](#5-sherlock-the-in-app-chat)
6. [Common workflows](#6-common-workflows)
7. [Keyboard shortcuts](#7-keyboard-shortcuts)
8. [Privacy & security model](#8-privacy--security-model)
9. [Troubleshooting & FAQ](#9-troubleshooting--faq)

---

## 1. Getting started

Nexus runs in three ways — pick whichever suits you. Full install commands are in
the project [README](../README.md); this is the short version.

| Method | Best for | What you install |
| --- | --- | --- |
| **Desktop app** (Windows/Linux) | Non-technical users who want an icon | The packaged installer (`Nexus-Setup.exe`) — bundles everything |
| **Docker** | A reproducible, all-batteries-included box | Docker Desktop, then `docker compose up --build` |
| **Local (uvicorn)** | Developers and tinkerers | Python 3.11+, `pip install -r requirements.txt` |

However you start it, open **http://127.0.0.1:8000** in your browser. A first-run
onboarding wizard greets you, and the **Guide** page explains every concept.

### Do I need an API key?

**No.** Nexus works for free out of the box:

- **Collection** — RSS, Google News, Reddit search, and GDELT need **no key**.
- **AI analysis** — optional. Add a free Google **Gemini** key, a paid
  Anthropic / OpenAI / xAI (Grok) key, or run a **fully local** model with
  [Ollama](https://ollama.com) or an app such as LM Studio (nothing leaves your
  machine). The default provider, **Automatic**, uses whichever one is ready. With no AI at all, items are still collected, stored and
  searchable — you just don't get summaries, translations or scores.

Keys are entered on the **Settings** page, saved only to a local `.env` file,
never shown back, and never stored in the database.

### After you install — your first 3 steps

Once the app is installed and open at **http://127.0.0.1:8000**:

1. **Choose your AI (optional but recommended).** Open **Settings** and pick one:
   - *Free cloud:* paste a Google **Gemini** key (has a free tier).
   - *Fully local / offline:* choose **Local model (Ollama)** — see the
     step-by-step below. Nothing leaves your machine.
   - *Skip it:* the app still collects and searches; you just won't get AI
     summaries, translations or relevance scores.
2. **Choose what to follow.** Open **Topics**, click a ready-made bundle (e.g.
   *World News*, *Cyber Security*) or type a subject and a few terms.
3. **Scan.** Press **Scan / Run a scan**. Collected items appear in the **Feed**
   within seconds (the first scan can take a minute or two while the AI analyses).

That's the whole setup. Everything else (cases, the graph, watchlists, reports)
builds on top of these.

### Connecting a fully local AI model (Ollama) — step by step

Use this when you want analysis to run **entirely on your machine** (no cloud, no
API key, nothing leaves the computer). Two one-time steps, then it just works.

**Step 1 — Install Ollama (once).**
Download it for your OS from <https://ollama.com/download> and run the installer.
After it's installed, Ollama runs quietly in the background and serves a local
address at `http://localhost:11434` — you don't have to start anything by hand.

**Step 2 — Get a model and point Nexus at it.** You can do this entirely inside
Nexus, no terminal needed:
1. Open **Settings**.
2. In the AI-provider dropdown choose **Local model (Ollama)**.
3. Pick a model from the list (start with **Gemma 4 (E4B) — recommended** if
   unsure) and click **Download**. A progress bar shows the download; the first
   model is a few GB, so give it time. It downloads only once.
4. Click **Check** — it confirms in plain language that the local server is
   running and your model is ready.
5. Done. The next **Scan** analyses every item locally.

*(Prefer the terminal? `ollama pull gemma4:e4b` does the same as the Download
button; then just select "Local model (Ollama)" in Settings.)*

**Pick a model to match your computer:**

| Your machine | Model to choose | Download |
| --- | --- | --- |
| Older laptop (8 GB RAM) | Gemma 4 (E2B) | ~2 GB |
| Most laptops (16 GB RAM) | **Gemma 4 (E4B) — recommended** | ~4 GB |
| Strong PC (32 GB RAM) | Gemma 4 (12B) | ~9 GB |
| Workstation (64 GB / good GPU) | Gemma 4 (31B) | ~22 GB |

**If something's off:**
- *"Could not reach the local Ollama server"* → Ollama isn't running. Open the
  Ollama app once; it then stays running in the background. Click **Check** again.
- *"model is not downloaded yet"* → click **Download** in Settings (or run
  `ollama pull <model>`), wait for it to finish, then **Check**.
- A scan with the server off doesn't break anything — collection and search keep
  working, and analysis resumes automatically once Ollama is back.

### Using Grok (xAI)

1. Open <https://console.x.ai>, sign in, and add credits under **Billing**.
2. Open **API Keys** → **Create API Key** and copy the key (it starts with `xai-`).
3. In Nexus open **Settings**, paste it into **xAI (Grok) API key** and click
   **Save keys**.
4. In the AI-provider box choose **xAI (Grok)**, or leave **Automatic** (Grok is
   used when no Gemini / OpenAI / Anthropic key is set).

The default model is `grok-4.6`, xAI's current flagship (October 2026). To use
another one, for example a cheaper model for high-volume scans, set
`GROK_MODEL` in `.env` to any id listed at docs.x.ai.

### Using a local server (LM Studio, llama.cpp, vLLM, Jan, LocalAI)

If you already run models in one of these apps, Nexus can use it directly.
Nothing leaves your computer.

1. **Start the app's server.** In LM Studio: open the **Developer** tab, load a
   model, and switch **Start server** on. It listens on
   `http://localhost:1234/v1`. (llama.cpp: `llama-server -m model.gguf --port 8080`
   → `http://localhost:8080/v1`. vLLM: `http://localhost:8000/v1` — note that
   Nexus itself also uses port 8000, so start vLLM on another port.)
2. In Nexus **Settings**, choose **Local server (LM Studio, llama.cpp, vLLM…)** as
   the provider and click **Apply**.
3. Check the **Server address**, then click **Test connection**. It lists the
   models the server offers.
4. Type or pick one of them in **Model** and click **Save**. When the test turns
   green, scans are analysed by your local model.

Only servers started with an API key (for example `vllm serve … --api-key`) need
`LOCAL_LLM_API_KEY`; LM Studio, llama.cpp and Jan do not.

### Getting a daily email brief from Sherlock

Once a day Sherlock can email you a short brief: a headline, the key
developments in each open case, and what to watch, citing the items by title
with links. It uses only the items Nexus collected, and works without AI too (you
then get a plain summary). This is optional and is the only feature that emails
anything off your machine.

1. Open **Settings** and scroll to **Daily email brief (by Sherlock)**.
2. **Step 1:** choose how to send:
   - **Gmail** — needs an *App Password*, never your normal password:
     1. Turn on 2-Step Verification at <https://myaccount.google.com/security>
        (app passwords require it).
     2. Open <https://myaccount.google.com/apppasswords>, type a name such as
        *Nexus brief*, and click **Create**.
     3. Copy the 16-character password Google shows (spaces don't matter).
   - **Outlook / Microsoft 365** — your account password, or an app password if
     your account uses 2-step sign-in. Some organisations turn SMTP sign-in off;
     if the test reports a sign-in error, use an email API service instead.
   - **Resend** or **SendGrid** — create an API key with sending permission and
     verify your sender address or domain with the service.
   - **Custom SMTP** — any mail server: host, port (465 = SSL, 587 = STARTTLS),
     username and password from your mail provider.
3. **Step 2:** fill in the fields (your address, the app password or API key, and
   who should receive the brief) and click **Save email settings**. Passwords and
   keys go to `.env` only and are never shown again.
4. **Step 3:** click **Send test email** and check your inbox (and spam folder).
5. Once the test succeeds, tick **Email me the daily brief**, choose a time, and
   optionally **Run a scan first** so the brief includes fresh items. Click
   **Save**. Use **Send a test brief now** to see a real brief immediately.

Changing any email setting turns the brief off until you send a new test email.
The app must be running at the chosen time; if it was closed, the brief goes out
the next time it is open after that time (at most once a day).

### Daily case reports

On any case, open the **Reports** tab (or **Export → Daily reports…**):

- Tick **Export a daily report**, choose **PDF** or **HTML**, and choose whether
  the daily email should **link to it**, **attach it**, or not mention it.
- At the daily-brief time, a report of the case's items collected since the
  previous report is saved to `data/reports/<case>/<YYYY-MM-DD>.pdf` (HTML if no
  PDF engine is installed).
- **Generate now** creates today's report immediately.
- The tab lists every saved report with its date, item count, format and size,
  with **Open**, **Download** and **Delete**. The **Brief** page shows each case's
  newest report in a "Latest reports" strip.

---

## 2. Core concepts

- **Item** — one collected piece of content (a news article, a post, a search
  result). Stored locally in SQLite forever, until you remove it.
- **Source** — where items come from (RSS, Google News, Reddit, Telegram, a
  custom JSON API, …). Each source is optional and skipped if not configured.
- **Scan** — the action that fetches new items from every available source. Nexus
  is **trigger-based**: it does not scrape 24/7. You press **Scan**, or set an
  **auto-scan interval**, or it does a quiet *Boot Sync* at startup.
- **AI analysis** — when a provider is connected, each new item gets a summary, an
  English translation (if needed), a threat level, extracted entities
  (people/orgs/locations/identifiers), and a credibility flag.
- **Intelligence Requirement (PIR)** — a standing question you want answered
  ("Is there evidence of a data breach?"). The AI scores every item 0–100 on how
  well it answers each requirement.
- **Case** — the home for one subject of investigation. It has tracking words (a
  live feed), a pinned dossier, per-case questions, sub-cases, a timeline and a
  relationship graph.
- **Triage list** — a personal label ("Interesting", "Discard") you file items
  into, independent of cases.
- **Graceful degradation** — the guiding rule: a missing key, model or tool just
  disables that one feature. Nothing ever crashes for want of a key.

---

## 3. Your first investigation in 5 minutes

1. **Open the app** at http://127.0.0.1:8000.
2. *(Optional)* On **Settings**, paste an AI key or choose a local Ollama model.
   Skip this to run keyless.
3. On **Topics**, either click a one-click **bundle** (e.g. *Cyber Security*) or
   type a subject and a few terms you care about.
4. Press **Run a scan**. Items appear in the **Feed** within seconds.
5. Open **Cases** → create a case for your subject. Give it a one-line brief and
   (with AI on) Nexus sets up its tracking words and starter questions for you.
6. From the Feed, **pin** the items that matter into your case, add **notes**, and
   when ready **export a PDF** report.

That's a full loop: collect → triage → build a dossier → report.

---

## 4. The interface, page by page

The top navigation bar is your map. Every page is also explained inside the app's
**Guide**.

### Feed ("The River") — `/`
A reverse-chronological stream of every collected item as dense cards. Each card
shows source, time, an AI summary, translation, a threat badge, detected
entities, cross-source corroboration ("Confirmed by N sources"), and how well the
item answers your standing questions. By default the feed is **scoped to your
tracked topics**; switch to "Everything" to see the full firehose, or search to
override scoping. Cards support inline actions: **Read/Unread**, **Dismiss**,
**Bookmark**, add to a **List**, **Pin to a case**, capture **Evidence**, and open
a full **Details** drawer.

### Intel — `/intel`
The relevance-ranked view. Once you've defined **Intelligence Requirements**, this
ranks items by how well they answer what you actually care about, each with a
0–100 score and a one-line rationale. Filter by requirement, minimum score, and
date. Exports to CSV/JSON with the score column included.

### Brief — `/brief`
Your morning read: what's new across every open case since you last opened each,
top items first, with one-click pin. A live badge in the nav shows the total new
count.

### Cases — `/cases`
The investigator's hub. Each **Case** is one subject and has tabs:
- **Live feed** — items matching the case's tracking words ("word capsule"). These
  words are also searched across your sources on every scan, so adding words then
  scanning fills the case.
- **Pinned** — the curated dossier: pinned items plus your notes.
- **Questions** — per-case intelligence requirements with their scored matches.
- **Sub-cases** — focused child cases (one level deep).
- **Timeline** — pinned items laid out chronologically.

A case has a **status** (open/closed) and **priority**. Create a case from a
one-line brief and (with AI) it auto-configures its tracking words and questions.
Cases export to **PDF/HTML**, machine-readable **CSV/JSON**, and an **Obsidian
vault** (a Markdown folder with `[[wikilinks]]` between entities).

### Lists — `/lists`
Personal triage lanes ("Very interesting", "Follow up", "Discard"). File any item
into any number of lists from its card. Lists can be global or scoped to a case.

### Watchlists — `/watchlists`
Keyword / regex / crypto-wallet / phone patterns that raise an alert whenever they
appear in newly collected items. A nav badge shows unseen hits.

### Graph — `/graph`
A relationship (co-occurrence) network built from the people, organisations and
places the AI extracts: bigger dots are mentioned more, central dots connect the
most others. Available globally or scoped to a single case. Define **entity
aliases** ("Messi" → "Lionel Messi") so variants merge into one node.

### Topics — `/topics`
Choose **what** to collect: one-click curated RSS bundles, custom RSS/Atom feeds,
Google News queries, subreddits, Twitter/X queries, Telegram channels. This is
where you tell Nexus what to follow.

### Sources — `/sources/custom`
Add **any public JSON API** as a source without writing code. With an AI backend
connected, paste the API's docs or a sample response and the planner auto-fills
the configuration (base URL, auth, which field holds the text). A **Test** button
does a live fetch so you can confirm it works. **Auto-Adapt** re-maps fields
automatically if an API's response shape later drifts.

### Tools — `/tools`
Plug & Play wrappers for installed OSINT CLI tools (Sherlock, Maigret, holehe,
GHunt, PhoneInfoga, theHarvester, SpiderFoot, Toutatis, OnionSearch, and more).
Run one against a target (username, email, phone, domain) and its findings feed a
local entity graph. Tools that aren't installed are simply listed with their
one-line install step.

### Transfer — `/transfer`
**Air-gap workflow.** On an online "collection" machine, export a portable
`.nexusbundle` (everything, a single case, or just what's *new since last export*)
to a USB stick. On an offline "analysis" machine running the same app, import it to
receive every item with its analysis, scores and evidence. Re-importing is safe
(duplicates are skipped). **No secrets ever travel in a bundle.**

### Security — `/security`
A fully local defensive panel: an **egress monitor** that records every outbound
connection the app makes and flags anything that isn't an AI provider, a
configured source, or the local machine; and a **file scanner** for vetting files
you bring in (disguised executables, zip-slip/zip-bomb archives, macro documents).
Downloadable data-handling audit report included.

### Settings — `/settings`
Connect an AI provider (paste a key, or pick a local Ollama model), set the
auto-scan interval, configure the keyless translation fallback, and manage API
keys. Keys are written to `.env` only and never shown back — the page shows
**Set / Not set**.

---

## 5. Sherlock, the in-app chat

Sherlock is the floating chat (the green detective icon, bottom-left). It runs on
whichever AI provider you've configured — cloud or local Ollama — and it both
**answers** and **acts**.

**Ask it anything:**
- *How the product works* — "How do I export an air-gap bundle?"
- *Your collected data* — "What does the data say about the transfer?"

**Tell it to act** (all actions are local, constructive and reversible — it can
never delete, change settings/keys, or send your data anywhere):
- "Open a case on *<subject>*, add the matching items, and make me a PDF."
- "Add the question '…' to this case." · "Set up a sub-case for …"
- "Watch for the wallet 0x… " · "Capture evidence of the top item about …"

### Sourced answers (the "Sources" toggle)
At the bottom of the chat there are two toggles:

- **🔗 Sources** *(on by default)* — Sherlock cites the collected items its answer
  is based on, inline as `(Item N)`, and lists them as **clickable sources** under
  the answer so you can open and verify each one. Turn it off for terse answers
  with no citations. Pure how-to/product questions need no sources, so none are
  shown for those.
- **🧠 Deep** — Sherlock runs several searches over your data and reasons across
  the results before answering. Slower, but more thorough.

Sherlock has **no information about who built the software** and will decline
questions about its origin — by design.

You can **End chat** at any time; you'll be offered an opt-in **local** save of the
transcript (stored only on this machine, never sent anywhere).

---

## 6. Common workflows

### Track a subject over time
1. **Cases → New case**, give it a one-line brief.
2. With AI on, accept the auto-generated tracking words and questions (or add your
   own on the Live feed / Questions tabs).
3. **Scan.** The case's Live feed fills with matching items; the Questions tab
   shows which items answer your PIRs and how well.
4. Check **Brief** each day for "+N new" since your last visit.

### Build a dossier and report
1. From the Feed or a case's Live feed, **pin** the relevant items.
2. Add **notes** in the Pinned tab; capture **Evidence** screenshots of key
   sources.
3. **Export** → PDF (or HTML / CSV / JSON / Obsidian vault).

### Preserve a source on the Internet Archive
Pages get edited or deleted. Besides the local **Evidence** screenshot, you can
ask the Wayback Machine (web.archive.org) to keep an independent, public copy:

1. Open an item (click it to open the drawer). The **Internet Archive** box shows
   its state: *Not archived*, *archiving…*, *Archived <date>* (a link to the
   snapshot) or *Archiving failed* with the reason and a **Retry archive** button.
2. Click **Archive** to request a fresh capture, or **Find existing snapshot** to
   look up the newest copy that already exists. A capture usually takes 10-60
   seconds; the box updates by itself.
3. In a case's **Pinned** tab, **Archive all pinned items** queues every pinned
   link that is not archived yet and shows progress. Tick **Auto-archive items
   when pinned to this case** to archive new pins automatically.

The first time, a warning explains the trade-off: archiving tells the Internet
Archive which URL you are interested in and creates a **public** capture. For
sensitive investigations, use the local Evidence screenshot instead. Captures
are spaced out to respect the archive's limits (roughly a dozen a minute without
keys); adding free archive.org keys (`ARCHIVE_ORG_ACCESS_KEY` /
`ARCHIVE_ORG_SECRET_KEY`, pasted in Settings) uses the more reliable
authenticated API. The whole feature can be switched off in **Settings →
Internet Archive**. Archive links and capture times are included in the
evidence manifest, case reports and CSV/JSON case exports.

### Daily routine across machines (air-gap)
1. On the online machine: **Scan**, then **Transfer → Export**, tick *only new
   since last export*. Copy the `.nexusbundle` to a USB stick.
2. On the offline machine: **Transfer → Import** the bundle. Everything appears
   with its analysis and evidence.

### Look someone/something up with OSINT tools
1. **Tools** → pick the matching tool (username → Sherlock/Maigret, email →
   holehe, phone → PhoneInfoga, domain → theHarvester/subfinder).
2. Enter the target and run; findings feed the entity graph. (Install any missing
   tool with the one-line command shown on the card.)

---

## 7. Keyboard shortcuts

In the Feed:

| Key | Action |
| --- | --- |
| `j` / `k` | Move down / up between cards |
| `o` | Open the focused item's source |
| `b` | Bookmark the focused item |
| `r` | Mark the focused item read |

---

## 8. Privacy & security model

- **Local-only.** The server binds to `127.0.0.1`; nothing is exposed to your
  network or the internet.
- **Your keys stay yours.** API keys live only in `.env`, never in the database,
  never in the UI (set/not-set only), and a log-redaction filter scrubs them from
  every log line.
- **Outbound traffic is observable.** The Security center's egress monitor shows
  exactly what the app talks to and flags anything unexpected.
- **AI is optional and can be fully local.** Use Ollama or a local server such as
  LM Studio and analysed content never leaves the machine. Cloud providers
  (Gemini, OpenAI, Anthropic, Grok) receive item text only once you add their key.
- **Exports are deliberate.** Data only leaves your machine when *you* export a
  report or a transfer bundle — and bundles never contain secrets.
- **The daily email brief is an explicit opt-in.** When you switch it on, the
  brief (item titles, summaries and links) is sent to the mail service you
  connected and to the recipients you listed — nothing else. Saved daily case
  reports stay in your data folder and are served only on `127.0.0.1`.

---

## 9. Troubleshooting & FAQ

**The page won't open / "connection refused."**
Give the first `docker compose up --build` a minute to finish, then refresh.
Confirm Docker Desktop (or the local server) is running.

**A scan finds nothing.**
Add at least one topic on **Topics** first — without topics there's nothing to
search for. The default feed is scoped to your tracked topics.

**No summaries / translations / scores.**
Expected until you connect an AI provider on **Settings** (a cloud key or local
Ollama). Collection and search work regardless.

**The AI stops part-way through a scan.**
If a provider hits its rate/quota limit (e.g. Gemini's free tier), the scan pauses
cleanly and the remaining items are analysed on the next run.

**"Something went wrong" page.**
A single unexpected error is caught and shown as a calm page; your data is safe and
the rest of the app keeps working. Try the action again, or return to the feed.

**Evidence screenshots don't capture.**
The Evidence Vault needs a headless Chromium. It's bundled in the Docker image and
the desktop installer; for a local dev run, `playwright install chromium-headless-shell`.

**Where is my data stored?**
In a single local SQLite database under the app's data directory (the desktop app
uses `%LOCALAPPDATA%\Nexus`; a dev run uses the project's `data/` folder). Back up
that file to back up everything.
