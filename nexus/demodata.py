"""One-click demo dataset.

A clearly-labelled sample (three fictional storylines, three cases) so a fresh install shows the feed, relationship
graph, entity dossiers, a case and triage working immediately — before any
sources are configured. Everything is fictional and tagged ``source='demo'`` so
it's easy to spot and delete. Idempotent: re-running adds nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from nexus.models import Analysis, RawItem, ThreatLevel
from nexus.storage import (
    add_bookmark,
    add_case_term,
    add_requirement,
    create_case,
    save_analysis,
    upsert_item,
)

logger = logging.getLogger("nexus.demodata")

_TL = {
    "none": ThreatLevel.NONE, "low": ThreatLevel.LOW, "medium": ThreatLevel.MEDIUM,
    "high": ThreatLevel.HIGH, "critical": ThreatLevel.CRITICAL,
}

# Three interlinked, entirely fictional storylines so the feed, relationship
# graph, entity dossiers, triage and pivots all have something meaningful to
# show. Every organisation, person and identifier is invented; domains use the
# reserved ``.example`` TLD and wallets are obviously synthetic.
# (ext_id, title, content, summary, threat, people, orgs, locations, identifiers)
_ITEMS: list[tuple] = [
    # --- Storyline 1: the Nightjar breach of Acme Corp -----------------------
    ("nj-1", "Acme Corp confirms major data breach",
     "Acme Corp disclosed a breach exposing customer records; its CISO is leading the response.",
     "Acme Corp confirms a customer-data breach and opens an incident response.",
     "high", ["Jane Doe"], ["Acme Corp"], ["Berlin"], []),
    ("nj-2", "Nightjar Group claims the Acme intrusion",
     "A threat actor calling itself Nightjar Group claimed responsibility, posting a contact address.",
     "Nightjar Group claims the Acme breach and provides a contact address.",
     "critical", [], ["Nightjar Group", "Acme Corp"], [], ["nightjar@mail.example"]),
    ("nj-3", "Stolen Acme data appears on a dark-web market",
     "Listings allegedly from the Acme breach were offered for cryptocurrency.",
     "Alleged Acme data listed for sale; payment requested to a crypto wallet.",
     "high", [], ["Nightjar Group", "Acme Corp"], [], ["bc1qdemo00walletexample00nightjar"]),
    ("nj-4", "CVE-2026-0001 exploited in the Acme intrusion",
     "Analysts attribute initial access to a vulnerability tracked as CVE-2026-0001.",
     "Initial access tied to CVE-2026-0001 in Acme's perimeter.",
     "high", [], ["Acme Corp"], [], ["CVE-2026-0001"]),
    ("nj-5", "Phishing domain targets Acme staff",
     "A look-alike domain was used to harvest employee credentials ahead of the breach.",
     "A look-alike phishing domain targeted Acme employees.",
     "medium", [], ["Acme Corp"], [], ["acme-login.example"]),
    ("nj-6", "Acme CISO Jane Doe issues a statement",
     "Jane Doe said affected customers will be notified and credentials reset.",
     "Acme's CISO outlines the response and customer notifications.",
     "low", ["Jane Doe"], ["Acme Corp"], ["Berlin"], []),
    ("nj-7", "Nightjar linked to an earlier Globex incident",
     "Researchers connect Nightjar Group's tooling to a prior intrusion at Globex Inc.",
     "Nightjar Group's tooling overlaps with an earlier Globex Inc incident.",
     "medium", [], ["Nightjar Group", "Globex Inc"], [], []),
    ("nj-8", "Contact address linked to operator 'John Roe'",
     "OSINT researchers tie the Nightjar contact address to an online persona, John Roe.",
     "The Nightjar contact address is linked to a persona named John Roe.",
     "medium", ["John Roe"], ["Nightjar Group"], [], ["nightjar@mail.example"]),
    ("nj-9", "Regulator opens inquiry into Acme breach disclosure timeline",
     "A data-protection authority asked Acme Corp to explain a nine-day gap before disclosure.",
     "Regulator questions why Acme waited nine days to disclose the breach.",
     "medium", ["Jane Doe"], ["Acme Corp", "Data Protection Authority"], ["Berlin"], []),
    ("nj-10", "Second wallet tied to Nightjar ransom payments",
     "Blockchain analysts trace funds from the first Nightjar wallet into a second address.",
     "A second crypto wallet receives funds from the Nightjar ransom address.",
     "high", [], ["Nightjar Group"], [],
     ["bc1qdemo00walletexample00nightjar", "bc1qdemo00walletexample00second"]),
    ("nj-11", "Globex Inc patches the same CVE after Acme disclosure",
     "Globex Inc confirmed it applied the fix for CVE-2026-0001 across its edge devices.",
     "Globex patches CVE-2026-0001 following the Acme breach news.",
     "low", [], ["Globex Inc"], ["Lisbon"], ["CVE-2026-0001"]),
    ("nj-12", "John Roe persona active on a cybercrime forum since 2024",
     "Forum archives show the John Roe handle advertising initial-access services.",
     "The John Roe persona has advertised initial-access services since 2024.",
     "high", ["John Roe"], ["Nightjar Group"], [], ["jroe_access"]),
    ("nj-13", "Acme customers report follow-on phishing emails",
     "Customers received emails impersonating Acme support and linking to acme-login.example.",
     "Follow-on phishing reuses the acme-login look-alike domain against customers.",
     "medium", [], ["Acme Corp"], [], ["acme-login.example"]),
    ("nj-14", "Security vendor publishes Nightjar indicators of compromise",
     "A vendor report lists hashes, domains and the CVE used by Nightjar Group.",
     "Vendor publishes Nightjar IOCs including the CVE and phishing domain.",
     "medium", [], ["Nightjar Group", "Brightline Security"], [],
     ["CVE-2026-0001", "acme-login.example"]),
    ("nj-15", "Nightjar threatens to leak data of a second victim",
     "The group's leak site added a countdown naming Initech Ltd as its next victim.",
     "Nightjar names Initech Ltd as a new victim with a leak countdown.",
     "critical", [], ["Nightjar Group", "Initech Ltd"], [], []),
    ("nj-16", "Initech Ltd says it is investigating Nightjar claim",
     "Initech Ltd stated it has engaged incident responders and found no evidence yet.",
     "Initech investigates the Nightjar claim; no confirmed compromise so far.",
     "medium", ["Sam Patel"], ["Initech Ltd"], ["Dublin"], []),
    # --- Storyline 2: Silt Spider ransomware against port logistics ----------
    ("tw-1", "Ransomware disrupts Harborline Logistics terminals",
     "Harborline Logistics halted container scheduling after a ransomware attack on its operations network.",
     "Ransomware halts container scheduling at Harborline Logistics.",
     "critical", [], ["Harborline Logistics"], ["Rotterdam"], []),
    ("tw-2", "Silt Spider named as suspected operator of Harborline attack",
     "Incident responders link the ransom note format to the Silt Spider group.",
     "Responders attribute the Harborline ransomware to Silt Spider.",
     "high", [], ["Silt Spider", "Harborline Logistics"], ["Rotterdam"], []),
    ("tw-3", "Exposed VPN appliance used as entry point (CVE-2026-1442)",
     "Initial access came through an unpatched VPN appliance vulnerable to CVE-2026-1442.",
     "Harborline's unpatched VPN (CVE-2026-1442) was the entry point.",
     "high", [], ["Harborline Logistics"], [], ["CVE-2026-1442"]),
    ("tw-4", "Shipping delays ripple to regional retailers",
     "Retailers warn of delivery delays as Harborline terminals remain offline for a third day.",
     "Harborline outage causes knock-on delivery delays for retailers.",
     "medium", [], ["Harborline Logistics", "Northwind Foods"], ["Rotterdam", "Antwerp"], []),
    ("tw-5", "Harborline COO Maria Lind: restoration from backups under way",
     "Maria Lind said core systems are being rebuilt from offline backups; no ransom paid.",
     "Harborline restoring from offline backups; says no ransom will be paid.",
     "low", ["Maria Lind"], ["Harborline Logistics"], ["Rotterdam"], []),
    ("tw-6", "Silt Spider leak site lists Harborline shipping manifests",
     "The group published sample manifests and set a seven-day deadline.",
     "Silt Spider leaks sample Harborline manifests with a 7-day deadline.",
     "high", [], ["Silt Spider", "Harborline Logistics"], [], ["siltspider-leaks.example"]),
    ("tw-7", "Port authority raises cyber alert level for all operators",
     "The port authority asked all terminal operators to patch CVE-2026-1442 within 48 hours.",
     "Port authority orders urgent patching of CVE-2026-1442 port-wide.",
     "medium", [], ["Port Authority"], ["Rotterdam"], ["CVE-2026-1442"]),
    ("tw-8", "Silt Spider tooling overlaps with Nightjar Group",
     "Analysts note shared loader code between Silt Spider and Nightjar Group samples.",
     "Shared loader code suggests a link between Silt Spider and Nightjar.",
     "high", [], ["Silt Spider", "Nightjar Group", "Brightline Security"], [], []),
    ("tw-9", "Phishing lure impersonates Harborline delivery notices",
     "Fake 'delivery rescheduled' emails link to harborline-portal.example to steal logins.",
     "Fake Harborline delivery notices phish credentials via a look-alike portal.",
     "medium", [], ["Harborline Logistics"], [], ["harborline-portal.example"]),
    ("tw-10", "Harborline terminals partially back online",
     "Two of five terminals resumed operations; full recovery expected within a week.",
     "Harborline partially restores terminal operations.",
     "low", ["Maria Lind"], ["Harborline Logistics"], ["Rotterdam"], []),
    ("tw-11", "Insurer flags rising ransomware claims in logistics sector",
     "A cyber insurer reports a sharp rise in logistics-sector ransomware claims this quarter.",
     "Insurer reports rising ransomware claims across logistics.",
     "low", [], ["Meridian Insurance"], [], []),
    ("tw-12", "Silt Spider affiliate recruitment post discovered",
     "A forum post signed 'tidewater' recruits affiliates and cites the Harborline attack.",
     "Silt Spider recruits affiliates, boasting of the Harborline attack.",
     "high", [], ["Silt Spider"], [], ["tidewater"]),
    # --- Storyline 3: the Copperleaf disinformation network ------------------
    ("cl-1", "Viral posts claim Northwind Foods product recall",
     "Dozens of accounts shared an image of a supposed Northwind Foods recall notice.",
     "A fake Northwind Foods recall notice spreads rapidly online.",
     "medium", [], ["Northwind Foods"], [], []),
    ("cl-2", "Northwind Foods denies any recall, calls notice fake",
     "Northwind Foods said no recall was issued and the notice image was fabricated.",
     "Northwind Foods denies the recall and calls the notice fabricated.",
     "low", ["Alex Moreno"], ["Northwind Foods"], ["Singapore"], []),
    ("cl-3", "Researchers map 'Copperleaf' network of coordinated accounts",
     "Researchers identified 140 accounts posting the recall image within minutes of each other.",
     "A coordinated 'Copperleaf' network of ~140 accounts pushed the fake recall.",
     "high", [], ["Copperleaf Network", "Northwind Foods", "Open Signal Lab"], [], []),
    ("cl-4", "Copperleaf accounts share creation dates and profile images",
     "Most accounts were created in the same week and reuse AI-generated profile photos.",
     "Copperleaf accounts share creation dates and synthetic profile photos.",
     "medium", [], ["Copperleaf Network", "Open Signal Lab"], [], ["@copperleaf_news"]),
    ("cl-5", "Fake recall linked to short-selling activity",
     "Unusual options activity on Northwind Foods preceded the first recall posts by an hour.",
     "Options trading on Northwind spiked an hour before the fake recall.",
     "high", [], ["Northwind Foods", "Copperleaf Network"], [], []),
    ("cl-6", "Copperleaf network pivots to a second brand",
     "The same accounts began amplifying a false contamination story about Fabrikam Drinks.",
     "Copperleaf pivots to a false contamination claim about Fabrikam Drinks.",
     "medium", [], ["Copperleaf Network", "Fabrikam Drinks"], [], ["@copperleaf_news"]),
    ("cl-7", "Platform removes 120 Copperleaf accounts",
     "The platform suspended most of the network for coordinated inauthentic behaviour.",
     "Platform suspends 120 Copperleaf accounts for coordinated behaviour.",
     "low", [], ["Copperleaf Network"], [], []),
    ("cl-8", "Domain hosting the recall image registered days earlier",
     "The image was first hosted on northwind-recall.example, registered four days before.",
     "Recall image hosted on a domain registered four days before the campaign.",
     "medium", [], ["Northwind Foods", "Copperleaf Network"], [], ["northwind-recall.example"]),
    ("cl-9", "Open Signal Lab publishes Copperleaf takedown analysis",
     "The lab's report details the network's timing patterns and reused assets.",
     "Open Signal Lab details Copperleaf's timing patterns and reused assets.",
     "low", ["Priya Shah"], ["Open Signal Lab", "Copperleaf Network"], [], []),
    ("cl-10", "Northwind Foods shares recover after debunk",
     "Northwind Foods' share price recovered most losses once the recall was debunked.",
     "Northwind shares recover after the fake recall is debunked.",
     "none", [], ["Northwind Foods"], ["Singapore"], []),
    # --- Background noise (realistic low-signal items) ------------------------
    ("bg-1", "Weekly vulnerability roundup: 37 new CVEs",
     "This week's roundup covers browser, VPN and CMS vulnerabilities, including CVE-2026-1442.",
     "Weekly roundup of 37 new CVEs, including CVE-2026-1442.",
     "low", [], [], [], ["CVE-2026-1442"]),
    ("bg-2", "Conference talk: measuring coordinated inauthentic behaviour",
     "A talk outlines statistical signals for spotting coordinated account networks.",
     "Talk outlines signals for detecting coordinated account networks.",
     "none", ["Priya Shah"], ["Open Signal Lab"], ["Lisbon"], []),
    ("bg-3", "Logistics firms urged to segment operational networks",
     "An industry body published guidance on separating IT and operational networks.",
     "Industry guidance urges network segmentation for logistics operators.",
     "none", [], ["Port Authority"], [], []),
    ("bg-4", "Brightline Security hires new threat intelligence lead",
     "Brightline Security announced a new head of threat intelligence.",
     "Brightline Security announces a new threat-intelligence lead.",
     "none", [], ["Brightline Security"], [], []),
]


def demo_loaded(conn) -> bool:
    """True if the demo dataset is already present."""
    return conn.execute(
        "SELECT 1 FROM items WHERE source = 'demo' LIMIT 1"
    ).fetchone() is not None


def load_demo_data(conn) -> dict:
    """Insert the demo dataset (items + a sample case). Idempotent."""
    if demo_loaded(conn):
        return {"loaded": False, "items": 0, "note": "Demo data is already loaded."}
    now = datetime.now(timezone.utc)
    count = 0
    for i, (ext, title, content, summary, threat, ppl, orgs, locs, ids) in enumerate(_ITEMS):
        iid, _ = upsert_item(conn, RawItem(
            source="demo", external_id=ext, title=title, content=content,
            url=f"https://example.com/demo/{ext}", author="Demo feed",
            published_at=now - timedelta(hours=i * 3 + 1),
        ))
        save_analysis(conn, iid, Analysis(
            threat_level=_TL.get(threat, ThreatLevel.NONE), summary=summary,
            entity_groups={"people": ppl, "organizations": orgs,
                           "locations": locs, "identifiers": ids},
            entities=[]))
        count += 1
    def _bookmark(ext: str, case_id: int) -> None:
        row = conn.execute(
            "SELECT id FROM items WHERE source = 'demo' AND external_id = ?", (ext,)
        ).fetchone()
        if row:
            add_bookmark(conn, int(row["id"]), case_id)

    cid = create_case(
        conn, "Operation Nightjar (demo)",
        "Sample investigation: who is behind the Acme breach, and who is next.", "high",
    )
    for word in ("Nightjar", "Acme Corp"):
        add_case_term(conn, cid, word)
    add_requirement(conn, "What is Nightjar Group targeting next?", priority=1, case_id=cid)
    add_requirement(conn, "Is John Roe the operator behind Nightjar?", priority=2, case_id=cid)
    for ext in ("nj-1", "nj-2", "nj-8", "nj-15"):
        _bookmark(ext, cid)

    cid2 = create_case(
        conn, "Port ransomware: Silt Spider (demo)",
        "Sample investigation: the Harborline Logistics outage and its links to Nightjar.",
        "high",
    )
    for word in ("Silt Spider", "Harborline"):
        add_case_term(conn, cid2, word)
    add_requirement(conn, "Which other port operators are exposed to CVE-2026-1442?",
                    priority=1, case_id=cid2)
    for ext in ("tw-1", "tw-3", "tw-8"):
        _bookmark(ext, cid2)

    cid3 = create_case(
        conn, "Copperleaf influence network (demo)",
        "Sample investigation: a coordinated fake-recall campaign against consumer brands.",
        "medium",
    )
    for word in ("Copperleaf", "Northwind"):
        add_case_term(conn, cid3, word)
    add_requirement(conn, "Who benefits financially from the fake recalls?",
                    priority=1, case_id=cid3)
    for ext in ("cl-3", "cl-5"):
        _bookmark(ext, cid3)
    conn.commit()
    logger.info("Demo data loaded: %d items, case #%s", count, cid)
    return {"loaded": True, "items": count, "case_id": cid}
