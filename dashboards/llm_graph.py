from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .shell import rel_href, render_page_shell


def _load_graph_payload(graph_json_path: Path | None) -> dict[str, Any]:
    if graph_json_path is None or not graph_json_path.exists():
        return {}
    try:
        payload = json.loads(graph_json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_nodes = payload.get("nodes", [])
    rows: list[dict[str, Any]] = []
    if isinstance(raw_nodes, dict):
        for node_id, node in raw_nodes.items():
            data = dict(node) if isinstance(node, dict) else {}
            data.setdefault("id", str(node_id))
            data.setdefault("label", str(data.get("id", node_id)))
            rows.append(data)
    elif isinstance(raw_nodes, list):
        for idx, node in enumerate(raw_nodes, start=1):
            if not isinstance(node, dict):
                continue
            data = dict(node)
            data.setdefault("id", str(data.get("label", f"node_{idx}")))
            data.setdefault("label", str(data.get("id", "")))
            rows.append(data)
    return rows


def _normalize_edges(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_edges = payload.get("edges", [])
    return [dict(edge) for edge in raw_edges if isinstance(edge, dict)] if isinstance(raw_edges, list) else []


def _graph_group(node: dict[str, Any]) -> str:
    community = str(node.get("community", "")).strip()
    if community:
        return f"Community {community}"
    source = str(node.get("source_file", "")).replace("\\", "/").strip()
    if "/" in source:
        return source.split("/", 1)[0] or "Source"
    if source:
        stem = source.rsplit(".", 1)[0]
        if "_" in stem:
            return stem.split("_", 1)[0].title()
        return "Source"
    return str(node.get("file_type", node.get("type", "Concept")) or "Concept").title()


def _href(output_path: Path, target: Path | None) -> str:
    return rel_href(output_path, target) if target is not None and target.exists() else ""


def _iframe(title: str, src: str) -> str:
    if not src:
        return "<div class='panel'><p class='sub'>This graph view was not generated for this run.</p></div>"
    return (
        "<div class='panel graph-frame-wrap'>"
        f"<iframe title='{html.escape(title)}' src='{html.escape(src)}' class='graph-frame'></iframe>"
        "</div>"
    )


def _community_html(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    by_id = {str(node.get("id", "")): node for node in nodes}
    groups: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        groups.setdefault(_graph_group(node), []).append(node)
    rows: list[str] = []
    for group, group_nodes in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        ids = {str(node.get("id", "")) for node in group_nodes}
        internal = 0
        external = 0
        relations: dict[str, int] = {}
        for edge in edges:
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            if source in ids and target in ids:
                internal += 1
            elif source in ids or target in ids:
                external += 1
            if source in ids or target in ids:
                relation = str(edge.get("relation", "related_to") or "related_to")
                relations[relation] = relations.get(relation, 0) + 1
        top_nodes = sorted(group_nodes, key=lambda node: str(node.get("label", node.get("id", ""))))[:8]
        top_relations = sorted(relations.items(), key=lambda item: (-item[1], item[0]))[:5]
        rows.append(
            "<article class='community-card'>"
            f"<h3>{html.escape(group)}</h3>"
            f"<div class='community-stats'><span>{len(group_nodes)} nodes</span><span>{internal} internal edges</span><span>{external} external edges</span></div>"
            "<h4>Nodes</h4><ul>"
            + "".join(f"<li>{html.escape(str(node.get('label', node.get('id', ''))))}</li>" for node in top_nodes)
            + "</ul><h4>Relations</h4><ul>"
            + ("".join(f"<li>{html.escape(name)} <span>{count}</span></li>" for name, count in top_relations) or "<li>No relations</li>")
            + "</ul></article>"
        )
    orphan_edges = [
        edge
        for edge in edges
        if str(edge.get("source", "")) not in by_id or str(edge.get("target", "")) not in by_id
    ]
    orphan_note = f"<p class='sub'>{len(orphan_edges)} edges reference missing nodes.</p>" if orphan_edges else ""
    return "<div class='community-grid'>" + "".join(rows) + "</div>" + orphan_note


def _edges_html(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    label_by_id = {str(node.get("id", "")): str(node.get("label", node.get("id", ""))) for node in nodes}
    rows: list[str] = []
    for edge in edges:
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        relation = str(edge.get("relation", "related_to") or "related_to")
        properties = edge.get("properties", {})
        confidence = str(edge.get("confidence", "") or (properties.get("confidence", "") if isinstance(properties, dict) else ""))
        source_file = str(edge.get("source_file", "") or "")
        search_blob = " ".join([label_by_id.get(source, source), relation, label_by_id.get(target, target), confidence, source_file]).lower()
        rows.append(
            f"<tr data-search='{html.escape(search_blob)}'>"
            f"<td>{html.escape(label_by_id.get(source, source))}</td>"
            f"<td><code>{html.escape(relation)}</code></td>"
            f"<td>{html.escape(label_by_id.get(target, target))}</td>"
            f"<td>{html.escape(confidence)}</td>"
            f"<td>{html.escape(source_file)}</td>"
            "</tr>"
        )
    return (
        "<div class='edge-tools'><input id='edge-filter' type='search' placeholder='Filter edges by node, relation, confidence, or source file'></div>"
        "<div class='edge-table-wrap'><table class='edge-table'><thead><tr><th>Source</th><th>Relation</th><th>Target</th><th>Confidence</th><th>Source File</th></tr></thead>"
        "<tbody id='edge-table-body'>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _raw_json_html(graph_json: Path | None, payload: dict[str, Any], output_path: Path) -> str:
    graph_href = _href(output_path, graph_json)
    raw = json.dumps(payload, indent=2, ensure_ascii=False) if payload else "{}"
    if len(raw) > 120_000:
        raw = raw[:120_000] + "\n... truncated in dashboard preview ..."
    open_link = f"<a class='btn' href='{html.escape(graph_href)}'>Open graph.json</a>" if graph_href else ""
    return f"<div class='actions'>{open_link}</div><pre class='raw-json'>{html.escape(raw)}</pre>"


def export_llm_graph_dashboard(
    *,
    output_dir: Path | str,
    graph_html_path: Path | str | None,
    graph_json_path: Path | str | None = None,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    current_run_id: str | None = None,
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "knowledge_graph_dashboard.html"

    graph_json = Path(str(graph_json_path)).resolve() if graph_json_path else None
    graph_dir = graph_json.parent if graph_json is not None else None
    if graph_dir is None and graph_html_path:
        graph_dir = Path(str(graph_html_path)).resolve().parent
    network_html = (graph_dir / "graph.html") if graph_dir is not None else None
    tree_html = (graph_dir / "GRAPH_TREE.html") if graph_dir is not None else None
    if (network_html is None or not network_html.exists()) and graph_html_path:
        candidate = Path(str(graph_html_path)).resolve()
        if candidate.exists():
            network_html = candidate

    payload = _load_graph_payload(graph_json)
    nodes = _normalize_nodes(payload)
    edges = _normalize_edges(payload)

    network_href = _href(output_path, network_html)
    tree_href = _href(output_path, tree_html)
    graph_json_href = _href(output_path, graph_json)

    body = f"""
<style>
  .kg-tabs {{ display:flex; gap:8px; flex-wrap:wrap; margin: 14px 0; }}
  .kg-tab {{ border:1px solid #cbd8ea; background:#fff; color:#17324d; border-radius:8px; padding:9px 12px; cursor:pointer; font-weight:650; }}
  .kg-tab.active {{ background:#123c69; color:#fff; border-color:#123c69; }}
  .kg-panel {{ display:none; }}
  .kg-panel.active {{ display:block; }}
  .graph-frame-wrap {{ padding:0; overflow:hidden; }}
  .graph-frame {{ width:100%; height:calc(100vh - 285px); min-height:640px; border:0; display:block; background:#fff; }}
  .community-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; }}
  .community-card {{ border:1px solid #d8e1ef; border-radius:8px; padding:14px; background:#fff; }}
  .community-card h3 {{ margin:0 0 8px; }}
  .community-card h4 {{ margin:12px 0 4px; font-size:12px; color:#657286; text-transform:uppercase; }}
  .community-card ul {{ margin:0; padding-left:18px; }}
  .community-stats {{ display:flex; gap:8px; flex-wrap:wrap; color:#53657d; font-size:12px; }}
  .community-stats span {{ border:1px solid #e0e8f2; border-radius:999px; padding:3px 7px; background:#f7faff; }}
  .edge-tools {{ margin-bottom:10px; }}
  .edge-tools input {{ width:100%; max-width:560px; padding:10px 12px; border:1px solid #cbd8ea; border-radius:8px; font:inherit; }}
  .edge-table-wrap {{ max-height:680px; overflow:auto; border:1px solid #d8e1ef; border-radius:8px; background:#fff; }}
  .edge-table {{ width:100%; border-collapse:collapse; }}
  .edge-table th, .edge-table td {{ padding:8px 10px; border-bottom:1px solid #edf2f7; text-align:left; vertical-align:top; }}
  .edge-table th {{ position:sticky; top:0; background:#f7faff; z-index:1; }}
  .raw-json {{ max-height:720px; overflow:auto; }}
</style>
<section class='section'>
  <div class='grid cards-2'>
    <div class='card'><div class='label'>Nodes</div><div class='value'>{len(nodes)}</div><div class='sub'>Semantic nodes extracted from the LLM wiki.</div></div>
    <div class='card'><div class='label'>Edges</div><div class='value'>{len(edges)}</div><div class='sub'>Graphify relations across wiki pages and concepts.</div></div>
  </div>
</section>
<div class='actions' style='margin-bottom:14px'>
  {f"<a class='btn' href='{html.escape(network_href)}'>Open Network</a>" if network_href else ""}
  {f"<a class='btn secondary' href='{html.escape(tree_href)}'>Open Graphify Tree</a>" if tree_href else ""}
  {f"<a class='btn secondary' href='{html.escape(graph_json_href)}'>Open graph.json</a>" if graph_json_href else ""}
</div>
<section class='section'>
  <div class='panel'>
    <div class='kg-tabs' role='tablist'>
      <button class='kg-tab active' data-tab='network' type='button'>Network</button>
      <button class='kg-tab' data-tab='tree' type='button'>Tree</button>
      <button class='kg-tab' data-tab='communities' type='button'>Communities</button>
      <button class='kg-tab' data-tab='edges' type='button'>Edges</button>
      <button class='kg-tab' data-tab='raw' type='button'>Raw JSON</button>
    </div>
    <div id='tab-network' class='kg-panel active'>{_iframe("Network Knowledge Graph", network_href)}</div>
    <div id='tab-tree' class='kg-panel'>{_iframe("Graphify Tree Knowledge Graph", tree_href)}</div>
    <div id='tab-communities' class='kg-panel'>{_community_html(nodes, edges)}</div>
    <div id='tab-edges' class='kg-panel'>{_edges_html(nodes, edges)}</div>
    <div id='tab-raw' class='kg-panel'>{_raw_json_html(graph_json, payload, output_path)}</div>
  </div>
</section>
<script>
  document.querySelectorAll('.kg-tab').forEach(button => {{
    button.addEventListener('click', () => {{
      const tab = button.dataset.tab;
      document.querySelectorAll('.kg-tab').forEach(item => item.classList.toggle('active', item === button));
      document.querySelectorAll('.kg-panel').forEach(panel => panel.classList.toggle('active', panel.id === 'tab-' + tab));
    }});
  }});
  const filter = document.getElementById('edge-filter');
  if (filter) {{
    filter.addEventListener('input', () => {{
      const query = filter.value.trim().toLowerCase();
      document.querySelectorAll('#edge-table-body tr').forEach(row => {{
        row.style.display = !query || row.dataset.search.includes(query) ? '' : 'none';
      }});
    }});
  }}
</script>
"""
    html_text = render_page_shell(
        title="ManSim Knowledge Graph",
        current_page_path=output_path,
        manifest=manifest,
        manifest_path=manifest_path,
        current_artifact="graphify_graph.html",
        current_run_id=current_run_id,
        page_title="Knowledge Graph",
        page_subtitle="Graphify semantic graph built from the LLM wiki vault.",
        body_html=body,
    )
    output_path.write_text(html_text, encoding="utf-8")
    return output_path
