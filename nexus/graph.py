"""Local entity relationship graph (a lightweight Maltego alternative).

Builds a networkx graph linking a central target to the accounts/emails/hosts
discovered by the tool adapters, then renders it to a self-contained interactive
HTML fragment with pyvis. Both libraries are imported lazily and guarded so the
rest of the platform runs even when they are not installed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from nexus.adapters.base import Finding

logger = logging.getLogger("nexus.graph")

# Shared vis-network options for both graphs. Tuned so the network spreads evenly
# and, crucially, FITS to the viewport after stabilising (pyvis otherwise leaves
# the graph stabilised in a corner). forceAtlas2 + avoidOverlap keeps dense
# clusters from collapsing into an unreadable hairball; navigation buttons add
# zoom/recenter controls; physics settle (minVelocity) instead of drifting.
_GRAPH_OPTIONS = """
{
  "autoResize": true,
  "nodes": {
    "shape": "dot",
    "borderWidth": 0,
    "font": { "color": "#e2e8f0", "size": 14, "strokeWidth": 4, "strokeColor": "#0f172a" }
  },
  "edges": {
    "color": { "color": "#475569", "opacity": 0.35, "highlight": "#f59e0b", "hover": "#f59e0b" },
    "smooth": { "enabled": true, "type": "continuous" },
    "scaling": { "min": 0.5, "max": 6 }
  },
  "physics": {
    "solver": "forceAtlas2Based",
    "forceAtlas2Based": {
      "gravitationalConstant": -60,
      "centralGravity": 0.012,
      "springLength": 140,
      "springConstant": 0.08,
      "damping": 0.6,
      "avoidOverlap": 0.7
    },
    "maxVelocity": 45,
    "minVelocity": 0.6,
    "stabilization": { "enabled": true, "iterations": 320, "updateInterval": 25, "fit": true }
  },
  "interaction": {
    "hover": true,
    "tooltipDelay": 120,
    "navigationButtons": true,
    "keyboard": true,
    "multiselect": true
  }
}
"""

# Colour per entity kind for quick visual scanning in the dashboard.
_KIND_COLOR = {
    "target": "#f59e0b",
    "account": "#38bdf8",
    "email": "#34d399",
    "host": "#a78bfa",
    "domain": "#a78bfa",
    "ip": "#f472b6",
    "phone": "#fbbf24",
    "entity": "#94a3b8",
}


# pyvis's template still links Bootstrap from a CDN even in "in_line" mode. The
# graph does not use it (no select/filter menus), so drop those tags: the
# rendered page must make no network requests at all.
_REMOTE_TAG = re.compile(
    r'<(?:link|script)\b[^>]*\b(?:href|src)="https?://[^"]*"[^>]*>(?:\s*</script>)?',
    re.IGNORECASE,
)


def _offline(html: str) -> str:
    return _REMOTE_TAG.sub("", html)


def build_graph(target: str, findings: Iterable[Finding]):
    """Return a networkx.Graph centred on `target`, or None if networkx is absent."""
    try:
        import networkx as nx
    except ImportError:
        logger.warning("networkx not installed; graph disabled.")
        return None

    graph = nx.Graph()
    graph.add_node(target, kind="target", label=target)
    for f in findings:
        if not f.value:
            continue
        graph.add_node(
            f.value,
            kind=f.kind,
            label=f.label or f.value,
            tool=f.source_tool,
        )
        graph.add_edge(target, f.value, kind=f.kind)
    return graph


def render_graph_html(target: str, findings: Iterable[Finding]) -> str | None:
    """Render the entity graph to an HTML string for embedding, or None.

    Returns None when the graph libraries are unavailable so callers can show a
    graceful "graph unavailable" message instead of crashing.
    """
    findings = list(findings)
    graph = build_graph(target, findings)
    if graph is None:
        return None

    try:
        from pyvis.network import Network
    except ImportError:
        logger.warning("pyvis not installed; graph rendering disabled.")
        return None

    net = Network(
        height="600px",
        width="100%",
        bgcolor="#0f172a",
        font_color="#e2e8f0",
        notebook=False,
        directed=False,
        cdn_resources="in_line",  # embed vis-network: the graph renders offline
    )
    for node, attrs in graph.nodes(data=True):
        kind = attrs.get("kind", "entity")
        net.add_node(
            node,
            label=attrs.get("label", node),
            color=_KIND_COLOR.get(kind, _KIND_COLOR["entity"]),
            title=f"{kind}: {node}",
            size=28 if kind == "target" else 14,
        )
    for a, b, attrs in graph.edges(data=True):
        net.add_edge(a, b, title=attrs.get("kind", ""))

    net.set_options(_GRAPH_OPTIONS)
    try:
        # generate_html avoids writing to disk.
        return _offline(net.generate_html(notebook=False))
    except Exception:
        logger.exception("pyvis failed to render graph")
        return None


# Colour per aggregated-entity kind (topic graph). Distinct from _KIND_COLOR,
# which colours the tool-finding graph above.
_ENTITY_KIND_COLOR = {
    "person": "#38bdf8",
    "organization": "#a78bfa",
    "location": "#34d399",
    "identifier": "#f472b6",
    "entity": "#94a3b8",
}


def render_entity_graph_html(graph_data: dict) -> str | None:
    """Render a topic-driven entity co-occurrence graph to an HTML fragment.

    ``graph_data`` is the dict produced by ``storage.topic_entity_graph``:
    nodes carry ``mentions`` (how often the entity is talked about) and
    ``connections`` (how many other entities it co-occurs with); edges carry a
    co-occurrence ``weight``. Node size scales with mentions so the most
    talked-about actors are visually dominant; the most connected ones sit at
    the centre of the web. Returns None when pyvis is unavailable or there is
    nothing to draw, so callers can fall back to the ranked tables alone.
    """
    nodes = graph_data.get("nodes") or []
    if not nodes:
        return None

    try:
        from pyvis.network import Network
    except ImportError:
        logger.warning("pyvis not installed; entity graph rendering disabled.")
        return None

    net = Network(
        height="600px",
        width="100%",
        bgcolor="#0f172a",
        font_color="#e2e8f0",
        notebook=False,
        directed=False,
        cdn_resources="in_line",  # embed vis-network: the graph renders offline
    )

    max_mentions = max((n["mentions"] for n in nodes), default=1) or 1
    for n in nodes:
        kind = n.get("kind", "entity")
        # Size 12..46 scaled by mention share, so "most talked-about" pops out.
        size = 12 + 34 * (n["mentions"] / max_mentions)
        net.add_node(
            n["id"],
            label=n["label"],
            color=_ENTITY_KIND_COLOR.get(kind, _ENTITY_KIND_COLOR["entity"]),
            title=(
                f"{kind} · mentioned in {n['mentions']} item(s) · "
                f"connected to {n['connections']} other entit"
                f"{'y' if n['connections'] == 1 else 'ies'}"
            ),
            size=size,
        )
    for e in graph_data.get("edges") or []:
        net.add_edge(e["source"], e["target"], value=e.get("weight", 1))

    net.set_options(_GRAPH_OPTIONS)
    try:
        return _offline(net.generate_html(notebook=False))
    except Exception:
        logger.exception("pyvis failed to render entity graph")
        return None
