"""Render archived maps or an aligned inventory preview without simulation."""
from __future__ import annotations

import argparse
import copy
import html
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import yaml


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments/mfg_flow_shop_paper/results"
OUTPUT = RESULTS / "20261003_inventory_maps"
WALL = "#39464b"
SHELF = "#737f84"
STOCK = "#188b80"
EMPTY = "#dce2e5"
DOOR = "#75c77b"
PICKUP = "#507bdd"


def font(size: int):
    return ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", size)


def load_condition(fill: int) -> dict:
    study = (
        RESULTS / "20260929_095039_current_checkpoints"
        if fill == 30 else RESULTS / f"20261002_inventory_20_40/materials_{fill}"
    )
    run = study / "evaluation/runs/immediate_shared/workers_3/seed_910001"
    layout = json.loads((run / "replay_studio_layout.json").read_text(encoding="utf-8"))
    config = yaml.safe_load((run / ".hydra/config.yaml").read_text(encoding="utf-8"))["scenario"]
    snapshots = json.loads((run / "minute_snapshots.json").read_text(encoding="utf-8"))["snapshots"]
    first = snapshots[0]
    assert first["t"] == 0 and first["warehouse_material_shelf_count"] == fill
    slots = sorted((obj for obj in layout["grid"]["object_footprints"] if obj["object_type"] == "material_slot"), key=lambda obj: obj["object_id"])
    assert len(slots) == config["warehouse"]["material_shelf"]["capacity"]
    regions = []
    for region in layout["regions"]:
        sx = layout["grid"]["width_tiles"] / layout["viewport"]["width"]
        sy = layout["grid"]["height_tiles"] / layout["viewport"]["height"]
        regions.append({"name": region["label"], "x": round(region["position"]["x"] * sx),
                        "y": round(region["position"]["y"] * sy), "width": round(region["size"]["width"] * sx),
                        "height": round(region["size"]["height"] * sy)})
    warehouse = next(region for region in regions if region["name"] == "Warehouse")
    doors = [tile for tile in layout["grid"]["doors"] if warehouse["x"] <= tile["x"] < warehouse["x"]+warehouse["width"] and warehouse["y"] <= tile["y"] < warehouse["y"]+warehouse["height"]]
    return {"fill": fill, "run": run, "layout": layout, "regions": regions, "warehouse": warehouse,
            "slots": slots, "doors": doors, "workers": first["worker_tiles"],
            "restock_target": config["objective"]["throughput"]["restock_target_fill"]}


def align_condition(condition: dict, reference: dict) -> dict:
    """Reuse complete reference geometry while preserving the stock treatment."""
    if not 0 <= condition["fill"] <= len(reference["slots"]):
        raise ValueError("Initial material count exceeds the reference shelf capacity.")
    if not 0 <= condition["restock_target"] <= len(reference["slots"]):
        raise ValueError("Restock target exceeds the reference shelf capacity.")
    aligned = copy.deepcopy(reference)
    aligned.update(fill=condition["fill"], restock_target=condition["restock_target"],
                   historical_run=condition["run"], source_kind="proposed_aligned_layout",
                   asset_suffix="_aligned")
    return aligned


def render_map(condition: dict, *, zoom: bool, preserve_existing: bool = False) -> Path:
    name = f"materials_{condition['fill']}{condition.get('asset_suffix', '')}_{'warehouse' if zoom else 'full_map'}.png"
    path = OUTPUT / name
    if preserve_existing and path.exists():
        return path
    bounds = (34, 2, 32, 25) if zoom else (0, 0, 100, 70)
    ox, oy, width, height = bounds
    tile = 36 if zoom else 16
    margin = 42
    image = Image.new("RGB", (width * tile + 2 * margin, height * tile + 2 * margin), "white")
    draw = ImageDraw.Draw(image)

    def point(x, y):
        return margin + (x-ox)*tile, margin + (y-oy)*tile

    def rectangle(x, y, w, h, color, outline=None, inset=0):
        left, top = point(x, y)
        right, bottom = point(x+w, y+h)
        left, top = max(margin, left+inset), max(margin, top+inset)
        right, bottom = min(margin+width*tile, right-inset-1), min(margin+height*tile, bottom-inset-1)
        if right >= left and bottom >= top:
            draw.rectangle((left, top, right, bottom), fill=color, outline=outline)

    def label(x, y, text, size=18, color=WALL):
        px, py = point(x, y)
        if margin <= px <= margin+width*tile and margin <= py <= margin+height*tile:
            draw.text((px, py), text, font=font(size), fill=color, anchor="mm")

    for region in condition["regions"]:
        rectangle(region["x"], region["y"], region["width"], region["height"], "#f1f7f4" if region["name"] == "Warehouse" else "#f4f5f6")
    for x in range(ox, ox+width+1):
        draw.line((*point(x, oy), *point(x, oy+height)), fill="#e7ebec")
    for y in range(oy, oy+height+1):
        draw.line((*point(ox, y), *point(ox+width, y)), fill="#e7ebec")
    grid = condition["layout"]["grid"]
    for wall in grid["walls"]:
        rectangle(wall["x"], wall["y"], 1, 1, WALL)
    for door in grid["doors"]:
        rectangle(door["x"], door["y"], 1, 1, DOOR)
    occupied = {obj["object_id"] for obj in condition["slots"][:condition["fill"]]}
    for obj in grid["object_footprints"]:
        kind = obj["object_type"]
        if kind == "shelf":
            continue  # Nonblocking parent bounding box, not another obstacle.
        color = {"shelf_wall": SHELF, "shelf_low_wall": SHELF, "material_slot": STOCK if obj["object_id"] in occupied else EMPTY,
                 "machine": "#6ebdc2", "charging_dock": "#ddbf5d", "inspection_desk": "#bc9bb5"}.get(kind, "#b4c5d1")
        rectangle(obj["x"], obj["y"], obj["width"], obj["height"], color, WALL, inset=1)
        if kind == "material_slot":
            if zoom:
                label(obj["x"]+.5, obj["y"]+.5, obj["object_id"].rsplit("_", 1)[-1], 21, "white" if obj["object_id"] in occupied else WALL)
            for pickup in grid["service_tiles"][obj["object_id"]]:
                cx, cy = point(pickup["x"]+.5, pickup["y"]+.5)
                if margin <= cx <= margin+width*tile and margin <= cy <= margin+height*tile:
                    radius = 4 if zoom else 2
                    draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill=PICKUP)
        elif not zoom and kind in {"machine", "inspection_desk", "charging_dock"}:
            text = obj["object_id"].replace("charging_dock_", "").replace("inspection_desk", "DESK")
            label(obj["x"]+obj["width"]/2, obj["y"]+obj["height"]/2, text, 13 if kind == "charging_dock" else 16)
    if zoom:
        door_y = condition["doors"][0]["y"]
        label(50, door_y+2, f"Door: y={door_y}", 22)
        label(50, 25.8, "STATION 2", 23)
    else:
        for region in condition["regions"]:
            label(region["x"]+region["width"]/2, region["y"]-1, region["name"], 22)
        for worker, pos in condition["workers"].items():
            cx, cy = point(pos["x"]+.5, pos["y"]+.5)
            draw.ellipse((cx-5, cy-5, cx+5, cy+5), fill="#ad4361", outline="white")
    for x in range(ox, ox+width+1, 2 if zoom else 10):
        px, _ = point(x, oy)
        draw.text((px, margin-15), str(x), fill="#647477", font=font(17), anchor="mm")
    for y in range(oy, oy+height+1, 2 if zoom else 10):
        _, py = point(ox, y)
        draw.text((margin-15, py), str(y), fill="#647477", font=font(17), anchor="mm")
    image.save(path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aligned", action="store_true",
                        help="Preview 30 materials in the same 40-slot warehouse as the 20/40 conditions.")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    conditions = [load_condition(fill) for fill in (20, 30, 40)]
    assert conditions[0]["layout"]["grid"] == conditions[2]["layout"]["grid"]
    historical = conditions[1]
    comparison_path = OUTPUT / "warehouse_comparison.html"
    archived_path = OUTPUT / "warehouse_comparison_archived.html"
    if args.aligned:
        if not archived_path.exists() and comparison_path.exists():
            previous = comparison_path.read_text(encoding="utf-8")
            if "Archived Warehouse Maps</title>" in previous:
                archived_path.write_text(previous, encoding="utf-8")
        conditions[1] = align_condition(historical, conditions[2])
        assert all(condition["layout"]["grid"] == conditions[0]["layout"]["grid"] for condition in conditions)
    views, full, provenance = [], [], []
    for condition in conditions:
        fill, w = condition["fill"], condition["warehouse"]
        proposed = condition.get("source_kind") == "proposed_aligned_layout"
        zoom = render_map(condition, zoom=True, preserve_existing=args.aligned and not proposed)
        whole = render_map(condition, zoom=False, preserve_existing=args.aligned and not proposed)
        rows = len({obj["y"] for obj in condition["slots"]})
        source_kind = "Proposed" if proposed else "Archived"
        status = '<p class="status">Proposed layout; not yet simulated</p>' if proposed else ''
        doors = ', '.join(f'({tile["x"]}, {tile["y"]})' for tile in condition["doors"])
        views.append(f'<article data-materials="{fill}"><h2>{fill} materials</h2><p>{w["width"]} &times; {w["height"]} tiles &middot; {rows} shelf rows &middot; {len(condition["slots"])} slots</p><a href="{zoom.name}"><img src="{zoom.name}" alt="{source_kind} {fill}-material warehouse, initial stock"></a><p>Initial stock: {fill} / {len(condition["slots"])} &middot; Empty slots: {len(condition["slots"])-fill}<br>Door: {html.escape(doors)}</p>{status}</article>')
        full.append(f'<section class="full"><h2>{fill} materials: full shop floor</h2><a href="{whole.name}"><img src="{whole.name}" alt="Full 100 by 70 tile shop floor, {fill} materials"></a></section>')
        provenance.append({"condition": fill, "source_kind": condition.get("source_kind", "archived_experiment"),
                           "layout_source": str(condition["run"] / "replay_studio_layout.json"),
                           "geometry_config_source": str(condition["run"] / ".hydra/config.yaml"),
                           "stock_config_source": str(condition.get("historical_run", condition["run"]) / ".hydra/config.yaml"),
                           "simulation_executed_for_preview": False,
                           "warehouse": w, "doors": condition["doors"], "shelf_rows": rows,
                           "capacity": len(condition["slots"]), "initial_fill": fill,
                           "restock_target": condition["restock_target"],
                           "occupied_slot_ids": [slot["object_id"] for slot in condition["slots"][:fill]],
                           "empty_slot_ids": [slot["object_id"] for slot in condition["slots"][fill:]]})
    legend = ''.join(f'<span><i style="background:{color}"></i>{name}</span>' for name, color in (("Wall", WALL), ("Shelf partitions", SHELF), ("Initial material", STOCK), ("Empty shelf slot (blocked)", EMPTY), ("Door", DOOR), ("Pickup tile", PICKUP)))
    title = "Aligned Warehouse Preview" if args.aligned else "Archived Warehouse Maps"
    context = ("Aligned layout preview &middot; 3 Humanoids &middot; Initial stock (t=0)" if args.aligned else
               "Archived experiment maps &middot; Immediate Shared &middot; 3 Humanoids &middot; Seed 910001 &middot; Initial stock (t=0)")
    notice = ("All three conditions share a 26 x 15 warehouse, four shelf rows and 40 physical slots. Only initial stock and the daily restock target differ." if args.aligned else
              "20 and 40 use the same expanded warehouse. The historical 30-material run uses the smaller warehouse.")
    source_note = ("The proposed 30-material geometry is copied from the archived 40-material layout, with slots 01-30 occupied and 31-40 empty. Scenario configuration and completed experiment results are unchanged." if args.aligned else
                   "Geometry comes directly from each saved replay_studio_layout.json, not the current scenario configuration.")
    changes = ''
    if args.aligned:
        old, new = historical["warehouse"], conditions[1]["warehouse"]
        change_rows = [
            ("Warehouse footprint", f'{old["width"]} x {old["height"]}', f'{new["width"]} x {new["height"]}'),
            ("Shelf rows / physical slots", f'{len({s["y"] for s in historical["slots"]})} / {len(historical["slots"])}', '4 / 40'),
            ("Door tiles", ', '.join(f'({d["x"]}, {d["y"]})' for d in historical["doors"]), ', '.join(f'({d["x"]}, {d["y"]})' for d in conditions[1]["doors"])),
            ("Initial stock / daily restock target", '30 / 30', '30 / 30'),
            ("Empty shelf slots", '0', '10 (31-40)'),
        ]
        rows_html = ''.join(f'<tr><th scope="row">{html.escape(label)}</th><td>{html.escape(before)}</td><td>{html.escape(after)}</td></tr>' for label, before, after in change_rows)
        history_link = '<p><a href="warehouse_comparison_archived.html">Historical 20 / 30 / 40 comparison</a></p>' if archived_path.exists() else ''
        changes = f'<section class="changes"><h2>30 materials: historical vs proposed</h2><p>Layout preview only. No new simulation or training has been run.</p><table><thead><tr><th>Setting</th><th>Historical</th><th>Proposed</th></tr></thead><tbody>{rows_html}</tbody></table>{history_link}</section>'
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>ManSim | {title}</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#fff;color:#263a3d;font:15px "Segoe UI",sans-serif;letter-spacing:0}}main{{max-width:1900px;margin:auto;padding:26px}}h1{{font-size:25px;margin:0 0 8px}}h2{{font-size:19px;margin:0 0 8px}}p{{line-height:1.6;color:#526368;margin:6px 0}}.legend{{display:flex;flex-wrap:wrap;gap:12px 24px;margin:18px 0}}.legend span{{display:flex;align-items:center;gap:7px}}i{{display:inline-block;width:13px;height:13px;border:1px solid #7f8b8e}}.comparison{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;border-block:1px solid #dce2e5;padding:22px 0}}article{{min-width:0}}img{{display:block;width:100%;height:auto}}article p{{font-size:13px;overflow-wrap:anywhere}}.status{{color:#8a5c10}}.full{{max-width:1400px;margin:36px auto;border-bottom:1px solid #dce2e5;padding-bottom:24px}}.changes{{margin:26px 0}}table{{border-collapse:collapse;width:100%;table-layout:fixed}}th,td{{text-align:left;border-bottom:1px solid #dce2e5;padding:10px 12px;overflow-wrap:anywhere}}details{{padding:18px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}@media(max-width:850px){{main{{padding:18px}}.comparison{{grid-template-columns:1fr}}th,td{{padding:8px 6px}}}}</style>
<main><h1>Warehouse layout: 20 / 30 / 40 materials</h1><p>{context}</p><p><strong>{notice}</strong></p><div class="legend">{legend}</div><section class="comparison">{''.join(views)}</section>{changes}{''.join(full)}<details><summary>Geometry sources and initial-stock contract</summary><p>{source_note} Initial materials occupy shelf slots in ascending slot-ID order. Empty slots remain physical obstacles. All close-ups have identical tile bounds and scale; the full shop floor is 100 by 70 tiles.</p><pre>{html.escape(json.dumps(provenance, indent=2))}</pre></details></main></html>'''
    comparison_path.write_text(page, encoding="utf-8")
    source_name = "sources_aligned.json" if args.aligned else "sources.json"
    (OUTPUT / source_name).write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(comparison_path)


if __name__ == "__main__":
    main()
