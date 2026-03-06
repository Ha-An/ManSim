from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from urllib.parse import quote
import webbrowser
from pathlib import Path

import hydra
from hydra.core.hydra_config import HydraConfig
from omegaconf import DictConfig, OmegaConf

from manufacturing_sim.simulation.scenarios.manufacturing.run import run


def _open_artifact(path: Path) -> None:
    if not path.exists():
        return
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open_new_tab(path.resolve().as_uri())
    except Exception:
        try:
            webbrowser.open_new_tab(path.resolve().as_uri())
        except Exception:
            pass


def _open_url(url: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open_new_tab(url)
    except Exception:
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            pass


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _pick_streamlit_port(preferred_port: int, range_start: int, range_end: int) -> int | None:
    if not _is_port_open(preferred_port):
        return preferred_port
    for port in range(range_start, range_end + 1):
        if not _is_port_open(port):
            return port
    return None


def _launch_streamlit_dashboard(
    *,
    app_path: Path,
    events_path: Path,
    preferred_port: int,
    range_start: int,
    range_end: int,
) -> str | None:
    port = _pick_streamlit_port(preferred_port, range_start, range_end)
    if port is None:
        return None

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
    ]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(app_path.parent.parent.parent.parent.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    time.sleep(1.2)
    url = f"http://localhost:{port}?events_path={quote(str(events_path))}"
    _open_url(url)
    return url


@hydra.main(config_path="conf", config_name="config", version_base="1.3")
def main(cfg: DictConfig) -> None:
    # Scenario config + policy-mode config are flattened into one payload
    # so scenario.run(...) and world/decision modules can read everything.
    experiment_cfg = OmegaConf.to_container(cfg.experiment, resolve=True)
    global_seed = cfg.get("seed", None)
    if global_seed is not None:
        experiment_cfg["seed"] = int(global_seed)
    experiment_cfg["decision"] = OmegaConf.to_container(cfg.get("decision", {}), resolve=True)
    experiment_cfg["heuristic_rules"] = OmegaConf.to_container(cfg.get("heuristic_rules", {}), resolve=True)
    runtime_output_dir = Path(HydraConfig.get().runtime.output_dir)
    result = run(experiment_cfg=experiment_cfg, output_dir=runtime_output_dir)
    print(json.dumps(result["kpi"], indent=2))

    auto_open = bool(cfg.get("ui", {}).get("auto_open_results", False))
    if auto_open:
        artifact_names = list(cfg.get("ui", {}).get("open_artifacts", []))
        for artifact_name in artifact_names:
            _open_artifact(runtime_output_dir / artifact_name)

    auto_open_streamlit = bool(cfg.get("ui", {}).get("auto_open_streamlit", False))
    if auto_open_streamlit:
        app_path = Path(__file__).resolve().parent / "scenarios" / "manufacturing" / "viz" / "replay_app.py"
        events_path = Path(result["events_path"])
        port_cfg = cfg.get("ui", {}).get("streamlit_port_range", {})
        range_start = int(port_cfg.get("start", 8505))
        range_end = int(port_cfg.get("end", 8555))
        preferred_port = int(cfg.get("ui", {}).get("streamlit_preferred_port", 8505))
        _launch_streamlit_dashboard(
            app_path=app_path,
            events_path=events_path,
            preferred_port=preferred_port,
            range_start=range_start,
            range_end=range_end,
        )


if __name__ == "__main__":
    main()
