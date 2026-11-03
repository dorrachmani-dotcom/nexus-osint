"""Edit -> publish (write-back): send an analyst-edited item or a write-up to
Confluence, an Obsidian vault or the knowledge pool.

Every publish goes through :mod:`nexus.publish.service` (prepare -> preview ->
explicit confirm). See docs/PUBLISHING.md.
"""

from __future__ import annotations


def publish_targets(conn) -> dict:
    """What the publish menu can offer right now (no network, no secrets)."""
    from nexus.envstore import confluence_env_name, read_env_value
    from nexus.storage.confluence_sources import list_confluence_sources
    from nexus.storage.obsidian_vaults import list_obsidian_vaults

    confluence = []
    for row in list_confluence_sources(conn):
        ready = bool(row.get("allow_publish")) and bool(
            read_env_value(confluence_env_name(row["id"])))
        confluence.append({"id": row["id"], "name": row["name"], "ready": ready,
                           "allow_publish": bool(row.get("allow_publish")),
                           "spaces": [s for s in (row.get("spaces") or "").split(",") if s]})
    vaults = [{"id": v["id"], "name": v["name"], "ready": bool(v.get("allow_write")),
               "folder": v.get("write_folder") or "Nexus"}
              for v in list_obsidian_vaults(conn)]
    return {"confluence": confluence, "vaults": vaults,
            "confluence_ready": any(c["ready"] for c in confluence),
            "vault_ready": any(v["ready"] for v in vaults)}
