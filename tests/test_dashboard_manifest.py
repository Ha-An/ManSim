from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dashboards.manifest import build_dashboard_manifest
from dashboards.results import export_results_dashboard


class DashboardManifestTests(unittest.TestCase):
    def test_optional_artifacts_are_blank_when_files_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kpi.json").write_text(json.dumps({"scenario_type": "factory_mfg_basic", "total_products": 1}), encoding="utf-8")
            (root / "run_meta.json").write_text(json.dumps({"scenario_type": "factory_mfg_basic", "total_days": 1}), encoding="utf-8")
            (root / "daily_summary.json").write_text(json.dumps({"days": []}), encoding="utf-8")
            (root / "events.jsonl").write_text("", encoding="utf-8")
            (root / "results_dashboard.html").write_text("<html></html>", encoding="utf-8")
            (root / "kpi_dashboard.html").write_text("<html></html>", encoding="utf-8")
            (root / "gantt.html").write_text("<html></html>", encoding="utf-8")
            (root / "pre_run_diagnostics.html").write_text("<html>pre run</html>", encoding="utf-8")
            (root / "pre_run_diagnostics.json").write_text(json.dumps({"supported": True}), encoding="utf-8")
            summary = {
                "runs": [
                    {
                        "run_index": 1,
                        "output_dir": str(root),
                        "kpi_path": str(root / "kpi.json"),
                        "run_meta_path": str(root / "run_meta.json"),
                        "daily_summary_path": str(root / "daily_summary.json"),
                        "events_path": str(root / "events.jsonl"),
                    }
                ]
            }

            manifest = build_dashboard_manifest(root_output_dir=root, summary_payload=summary)
            self.assertEqual("v0.5", manifest["version"])
            self.assertEqual("factory_mfg_basic", manifest["runs"][0]["scenario_type"])
            self.assertEqual("factory_mfg_basic", manifest["runs"][0]["kpi"]["scenario_type"])
            artifacts = manifest["runs"][0]["artifacts"]

            self.assertEqual("", artifacts["run_reflection.json"])
            self.assertEqual("", artifacts["run_reflection.md"])
            self.assertEqual("", artifacts["llm_trace.html"])
            self.assertTrue(artifacts["pre_run_diagnostics.html"].endswith("pre_run_diagnostics.html"))
            self.assertTrue(artifacts["pre_run_diagnostics.json"].endswith("pre_run_diagnostics.json"))

            export_results_dashboard(
                output_dir=root,
                kpi={
                    "total_products": 1,
                    "operational_task_complexity": 2.5,
                    "cumulative_operational_complexity_over_n_days": 12.5,
                },
                manifest=manifest,
                manifest_path=root / "dashboard_manifest.json",
                current_run_id="run_01",
            )
            hub_html = (root / "results_dashboard.html").read_text(encoding="utf-8")
            self.assertNotIn("run_reflection.json", hub_html)
            self.assertNotIn("run_reflection.md", hub_html)
            self.assertIn("OTC", hub_html)
            self.assertIn("Cumulative Complexity", hub_html)
            self.assertIn("Pre-Run Diagnostics", hub_html)


if __name__ == "__main__":
    unittest.main()
