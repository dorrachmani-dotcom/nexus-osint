"""One-click demo dataset.

A small, clearly-labelled sample so a fresh install shows the feed, relationship
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

# A fictional breach scenario whose entities interlink, so the graph/dossier/
# pivots all have something meaningful to show. Identifiers (email/domain/CVE/
# wallet) are deliberately included to demo the contextual-pivot buttons.
# (ext_id, title, content, summary, threat, people, orgs, locations, identifiers)
_ITEMS: list[tuple] = [
    ("nj-1", "Acme Corp confirms major data breach",
     "Acme Corp disclosed a breach exposing customer records; its CISO is leading the response.",
     "Acme Corp confirms a customer-data breach and opens an incident response.",
     "high", ["Jane Doe"], ["Acme Corp"], ["Berlin"], []),
    ("nj-2", "Nightjar Group claims the Acme intrusion",
     "A threat actor calling itself Nightjar Group claimed responsibility, posting a contact address.",
     "Nightjar Group claims the Acme breach and provides a contact address.",
     "critical", [], ["Nightjar Group", "Acme Corp"], [], ["nightjar@proton.me"]),
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
     "medium", [], ["Acme Corp"], [], ["acme-login.com"]),
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
     "medium", ["John Roe"], ["Nightjar Group"], [], ["nightjar@proton.me"]),
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
            published_at=now - timedelta(hours=i * 5),
        ))
        save_analysis(conn, iid, Analysis(
            threat_level=_TL.get(threat, ThreatLevel.NONE), summary=summary,
            entity_groups={"people": ppl, "organizations": orgs,
                           "locations": locs, "identifiers": ids},
            entities=[]))
        count += 1
    cid = create_case(
        conn, "Operation Nightjar (demo)",
        "Sample investigation — safe to delete.", "high",
    )
    for word in ("Nightjar", "Acme Corp"):
        add_case_term(conn, cid, word)
    add_requirement(conn, "What is Nightjar Group targeting next?", priority=1, case_id=cid)
    for ext in ("nj-1", "nj-2"):
        row = conn.execute(
            "SELECT id FROM items WHERE source = 'demo' AND external_id = ?", (ext,)
        ).fetchone()
        if row:
            add_bookmark(conn, int(row["id"]), cid)
    conn.commit()
    logger.info("Demo data loaded: %d items, case #%s", count, cid)
    return {"loaded": True, "items": count, "case_id": cid}
