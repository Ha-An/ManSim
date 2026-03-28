from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import sys
from typing import Any


class OpenClawClient:
    """OpenClaw native-local 런타임과 워크스페이스를 관리하는 클라이언트."""

    def __init__(
        self,
        *,
        gateway_url: str,
        model: str,
        timeout_sec: int,
        api_key: str = "",
        profile_name: str = "",
        profile_config_path: str = "",
        backend: dict[str, Any] | None = None,
    ) -> None:
        self.gateway_url = str(gateway_url or "").rstrip("/")
        self.model = str(model or "").strip()
        self.timeout_sec = max(1, int(timeout_sec))
        self.api_key = str(api_key or "").strip()
        self.profile_name = str(profile_name or "mansim_repo").strip() or "mansim_repo"
        self.profile_config_path = str(profile_config_path or "").strip()
        self.backend = dict(backend or {})
        self.transport = "native_local"
        self.profile_path = self._resolve_profile_path(self.profile_config_path)
        self.runtime_profile_path = self._runtime_profile_path()
        self.runtime_root: Path | None = None
        self.runtime_workspace_root: Path | None = None
        self.runtime_workspace_aliases: dict[str, str] = {}
        self.runtime_state_root: Path | None = None
        self.runtime_facts_root: Path | None = None
        self.gateway_log_path: Path | None = None
        self._ensure_profile_seeded()
        self._normalize_backend_connectivity()
        self._sync_profile_backend()

    @staticmethod
    def _model_ref(provider: str, model: str) -> str:
        provider_id = str(provider or "").strip().lower()
        model_id = str(model or "").strip()
        if not model_id:
            return ""
        if "/" in model_id:
            return model_id
        return f"{provider_id}/{model_id}" if provider_id else model_id

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[5]

    def _repo_local_profile_path(self) -> Path:
        return self._repo_root() / "openclaw" / "profiles" / self.profile_name / "openclaw.json"

    def _runtime_profile_path(self) -> Path:
        return Path(os.path.expanduser(f"~/.openclaw-{self.profile_name}/openclaw.json"))

    def _resolve_profile_path(self, raw_path: str) -> Path:
        if raw_path:
            path = Path(raw_path)
            if not path.is_absolute():
                path = self._repo_root() / path
            return path.resolve(strict=False)
        return self._repo_local_profile_path().resolve(strict=False)

    def _ensure_profile_seeded(self) -> None:
        if self.profile_path.exists():
            return
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        if self.runtime_profile_path.exists():
            self.profile_path.write_text(self.runtime_profile_path.read_text(encoding="utf-8-sig"), encoding="utf-8")

    @staticmethod
    def _http_get_ok(url: str, *, timeout_sec: float = 3.0) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=max(1.0, float(timeout_sec))) as response:
                return int(getattr(response, "status", 0) or 0) == 200
        except Exception:
            return False


    @staticmethod
    def _probe_http_endpoint(url: str, *, timeout_sec: float = 2.5) -> dict[str, Any]:
        start = time.time()
        target = str(url or "").strip()
        result: dict[str, Any] = {
            "url": target,
            "status": None,
            "ok": False,
            "latency_ms": 0.0,
            "error": "",
            "content_type": "",
            "html_response": False,
        }
        if not target:
            result["error"] = "empty_url"
            return result
        try:
            with urllib.request.urlopen(target, timeout=max(0.5, float(timeout_sec))) as response:
                result["status"] = int(getattr(response, "status", 0) or 0)
                headers = getattr(response, "headers", None)
                content_type = ""
                if headers is not None:
                    content_type = str(headers.get("Content-Type", "") or "")
                snippet = response.read(256).decode("utf-8", errors="ignore")
                lowered = snippet.lower()
                html_response = ("<html" in lowered) or ("<!doctype html" in lowered) or ("openclaw-app" in lowered)
                result["content_type"] = content_type
                result["html_response"] = html_response
                result["ok"] = int(result["status"]) == 200 and not html_response
                if int(result["status"] or 0) == 200 and html_response:
                    result["error"] = "html_response_not_api_ready"
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency_ms"] = round((time.time() - start) * 1000.0, 2)
        if not result["ok"] and result["status"] is None and not result["error"]:
            result["error"] = "request_failed"
        return result

    def probe_runtime_health(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        """게이트웨이와 실제 백엔드가 모두 응답 가능한지 호출 직전에 확인한다."""
        timeout_sec = float(timeout_sec or 2.5)
        if timeout_sec <= 0:
            timeout_sec = 2.5

        gateway_probe: dict[str, Any] = {
            "ok": False,
            "port": self._gateway_port(),
            "checked_at": time.time(),
            "candidates": [],
            "endpoint": "",
            "last_probe": {"ok": False, "error": "not_checked"},
        }
        base_gateway = self.gateway_url.rstrip("/")
        gateway_candidates = [
            base_gateway + "/v1/models",
            base_gateway + "/v1/health",
            base_gateway + "/health",
        ]
        for candidate in gateway_candidates:
            probe = self._probe_http_endpoint(candidate, timeout_sec=timeout_sec)
            gateway_probe["candidates"].append(probe)
            if probe.get("ok", False):
                gateway_probe["ok"] = True
                gateway_probe["endpoint"] = candidate
                gateway_probe["last_probe"] = probe
                break

        if not gateway_probe.get("ok", False):
            gateway_probe["last_probe"] = gateway_probe["last_probe"] or {}
            if self._wait_for_port(self._gateway_port(), timeout_sec=min(0.75, max(0.5, timeout_sec / 2.0))):
                gateway_probe["ok"] = True
                gateway_probe["endpoint"] = base_gateway
                gateway_probe["last_probe"] = {"ok": True, "status": 200, "latency_ms": 0.0, "note": "port_open"}

        provider = str(self.backend.get("provider", "")).strip().lower()
        base_backend = self._effective_backend_base_url().rstrip("/")
        backend_probe: dict[str, Any] = {
            "provider": provider,
            "ok": provider not in {"ollama"},
            "base_url": base_backend,
            "checked_at": time.time(),
            "candidates": [],
            "endpoint": "",
            "last_probe": {"ok": False, "error": "non_ollama_not_checked"},
        }

        if provider == "ollama":
            backend_probe["ok"] = False
            backend_probe["candidates"].append({"reason": "skipped_no_base_url"})
            if base_backend:
                endpoint = base_backend + "/api/tags"
                probe = self._probe_http_endpoint(endpoint, timeout_sec=timeout_sec)
                backend_probe["candidates"].append(probe)
                backend_probe["last_probe"] = probe
                backend_probe["endpoint"] = endpoint
                if probe.get("ok", False):
                    backend_probe["ok"] = True

        overall_ok = bool(gateway_probe.get("ok", False)) and bool(backend_probe.get("ok", False))
        return {
            "ok": overall_ok,
            "checked_at": time.time(),
            "gateway": gateway_probe,
            "backend": backend_probe,
            "reason": None if overall_ok else "runtime_health_check_failed",
        }
    @staticmethod
    def _localhost_host(host: str) -> bool:
        normalized = str(host or "").strip().lower()
        return normalized in {"localhost", "127.0.0.1", "::1", "[::1]"}

    def _wsl_distro(self) -> str:
        return str(self.backend.get("wsl_distro", "")).strip() or "Ubuntu-24.04"

    def _resolve_wsl_ipv4(self) -> str:
        distro = self._wsl_distro()
        commands = [
            ["wsl", "-d", distro, "--", "bash", "-lc", "hostname -I | awk '{print $1}'"],
            ["wsl", "--", "bash", "-lc", "hostname -I | awk '{print $1}'"],
        ]
        for argv in commands:
            try:
                proc = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
            except Exception:
                continue
            if proc.returncode != 0:
                continue
            values = str(proc.stdout or "").strip().split()
            if not values:
                continue
            candidate = values[0].strip()
            if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", candidate):
                return candidate
        return ""

    def _wsl_http_get_ok(self, *, distro: str, port: int) -> bool:
        argv = [
            "wsl", "-d", distro, "--", "bash", "-lc",
            f"curl -sS -o /dev/null -w '%{{http_code}}' http://localhost:{int(port)}/api/tags",
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
        except Exception:
            return False
        return proc.returncode == 0 and str(proc.stdout or "").strip().endswith("200")

    def _ensure_wsl_ollama_proxy(self, *, listen_port: int, target_port: int, distro: str) -> bool:
        probe_url = f"http://127.0.0.1:{int(listen_port)}/api/tags"
        if self._http_get_ok(probe_url, timeout_sec=5.0):
            return True
        proxy_script = self._repo_root() / "manufacturing_sim" / "simulation" / "scenarios" / "manufacturing" / "decision" / "wsl_ollama_proxy.py"
        if not proxy_script.exists():
            return False
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    str(proxy_script),
                    "--listen-host", "127.0.0.1",
                    "--listen-port", str(int(listen_port)),
                    "--distro", distro,
                    "--target-port", str(int(target_port)),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception:
            return False
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self._http_get_ok(probe_url, timeout_sec=2.0):
                return True
            time.sleep(0.25)
        return False

    def _normalize_backend_connectivity(self) -> None:
        provider_id = str(self.backend.get("provider", "")).strip().lower()
        if provider_id != "ollama":
            return
        base_url = str(self.backend.get("base_url", "")).strip()
        if not base_url:
            return
        parts = urlsplit(base_url)
        tags_url = urlunsplit((parts.scheme or "http", parts.netloc, "/api/tags", "", ""))
        if self._http_get_ok(tags_url, timeout_sec=5.0):
            return
        port = parts.port or 11434
        distro = self._wsl_distro()
        if self._wsl_http_get_ok(distro=distro, port=port):
            if self._ensure_wsl_ollama_proxy(listen_port=port, target_port=port, distro=distro):
                self.backend["base_url"] = urlunsplit((parts.scheme or "http", f"127.0.0.1:{port}", parts.path or "", parts.query, parts.fragment))
                self.backend["resolved_via_local_proxy"] = True
                self.backend["resolved_wsl_distro"] = distro
                return
        if not self._localhost_host(parts.hostname or ""):
            return
        wsl_ipv4 = self._resolve_wsl_ipv4()
        if not wsl_ipv4:
            return
        replacement_netloc = f"{wsl_ipv4}:{port}"
        replacement_url = urlunsplit((parts.scheme or "http", replacement_netloc, parts.path or "", parts.query, parts.fragment))
        replacement_tags_url = urlunsplit((parts.scheme or "http", replacement_netloc, "/api/tags", "", ""))
        if not self._http_get_ok(replacement_tags_url, timeout_sec=5.0):
            return
        self.backend["base_url"] = replacement_url
        self.backend["resolved_from_localhost"] = True
        self.backend["resolved_wsl_ipv4"] = wsl_ipv4

    def _effective_backend_base_url(self) -> str:
        return str(self.backend.get("base_url", "")).strip()

    def _sync_profile_backend(self) -> None:
        provider_id = str(self.backend.get("provider", "")).strip().lower()
        model_id = str(self.backend.get("model", "")).strip()
        if not provider_id or not model_id or not self.profile_path.exists():
            return
        try:
            payload = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return

        model_ref = self._model_ref(provider_id, model_id)
        changed = False

        agents = payload.setdefault("agents", {})
        if not isinstance(agents, dict):
            agents = {}
            payload["agents"] = agents
            changed = True
        defaults = agents.setdefault("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
            agents["defaults"] = defaults
            changed = True
        desired_timeout = max(60, int(self.timeout_sec))
        if int(defaults.get("timeoutSeconds", 0) or 0) != desired_timeout:
            defaults["timeoutSeconds"] = desired_timeout
            changed = True
        default_model = defaults.setdefault("model", {})
        if not isinstance(default_model, dict):
            default_model = {}
            defaults["model"] = default_model
            changed = True
        if default_model.get("primary") != model_ref:
            default_model["primary"] = model_ref
            changed = True
        defaults_models = defaults.setdefault("models", {})
        if not isinstance(defaults_models, dict):
            defaults_models = {}
            defaults["models"] = defaults_models
            changed = True
        if not isinstance(defaults_models.get(model_ref), dict):
            defaults_models[model_ref] = {}
            changed = True

        agent_list = agents.setdefault("list", [])
        if not isinstance(agent_list, list):
            agent_list = []
            agents["list"] = agent_list
            changed = True
        for agent in agent_list:
            if not isinstance(agent, dict):
                continue
            agent_id = str(agent.get("id", "")).strip().lower()
            if not agent_id or agent_id == "main":
                continue
            if agent.get("model") != model_ref:
                agent["model"] = model_ref
                changed = True

        models_root = payload.setdefault("models", {})
        if not isinstance(models_root, dict):
            models_root = {}
            payload["models"] = models_root
            changed = True
        providers = models_root.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            models_root["providers"] = providers
            changed = True
        provider_cfg = providers.setdefault(provider_id, {})
        if not isinstance(provider_cfg, dict):
            provider_cfg = {}
            providers[provider_id] = provider_cfg
            changed = True

        mapped_fields = {"base_url": "baseUrl", "api_key": "apiKey", "api": "api"}
        for src_key, dst_key in mapped_fields.items():
            value = str(self.backend.get(src_key, "")).strip()
            if value and provider_cfg.get(dst_key) != value:
                provider_cfg[dst_key] = value
                changed = True

        provider_models = provider_cfg.setdefault("models", [])
        if not isinstance(provider_models, list):
            provider_models = []
            provider_cfg["models"] = provider_models
            changed = True
        model_entry = None
        for item in provider_models:
            if isinstance(item, dict) and str(item.get("id", "")).strip() == model_id:
                model_entry = item
                break
        if model_entry is None:
            model_entry = {"id": model_id}
            provider_models.append(model_entry)
            changed = True

        desired_fields: dict[str, Any] = {
            "name": str(self.backend.get("model_name", "")).strip() or model_id,
            "reasoning": bool(self.backend.get("reasoning", False)),
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }
        try:
            context_window = int(self.backend.get("context_window", 0) or 0)
        except (TypeError, ValueError):
            context_window = 0
        if context_window > 0:
            desired_fields["contextWindow"] = context_window
        try:
            max_output_tokens = int(self.backend.get("max_output_tokens", 0) or 0)
        except (TypeError, ValueError):
            max_output_tokens = 0
        if max_output_tokens > 0:
            desired_fields["maxTokens"] = max_output_tokens
        for key, value in desired_fields.items():
            if model_entry.get(key) != value:
                model_entry[key] = value
                changed = True

        if changed:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            self.profile_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _workspace_template_root(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = self._repo_root() / path
        return path.resolve(strict=False)

    @staticmethod
    def _reset_dir(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _ensure_workspace_minimum(path: Path, agent_id: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "memory" / "daily").mkdir(parents=True, exist_ok=True)
        (path / "memory" / "episodic").mkdir(parents=True, exist_ok=True)
        (path / "memory" / "semantic").mkdir(parents=True, exist_ok=True)
        (path / "facts").mkdir(parents=True, exist_ok=True)
        (path / "facts" / "report_history").mkdir(parents=True, exist_ok=True)
        (path / "beliefs" / "history").mkdir(parents=True, exist_ok=True)
        (path / "commitments" / "history").mkdir(parents=True, exist_ok=True)
        (path / "mailboxes").mkdir(parents=True, exist_ok=True)
        (path / "plans").mkdir(parents=True, exist_ok=True)
        (path / "reports").mkdir(parents=True, exist_ok=True)
        (path / "trace").mkdir(parents=True, exist_ok=True)
        user_path = path / "USER.md"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text("", encoding="utf-8")
        defaults = {
            "AGENTS.md": (
                f"# {agent_id} ???? ??\n\n"
                "- ? ??????? ?? ????? run ????.\n"
                "- ?? `USER.md`? ?? ? ??? ?? ???.\n"
                "- `facts/current_request.json`? `facts/current_response_template.json`? ??? ? ??? ?? ?? ?? ???? ??.\n"
                "- ??? ??? ?? ??? ??? ???.\n"
                "- ???? ?? ???? ???.\n"
                "- JSON ??? ??? ?? JSON? ????.\n"
                "- ??? JSON ?? ?? ??, ??, ???? ??? ??? ???.\n"
            ),
            "IDENTITY.md": (
                f"# {agent_id} ???\n\n"
                "? ??? ????? ??, ?? ??, ?? ??? ????.\n"
            ),
            "SOUL.md": (
                f"# {agent_id} ??\n\n"
                "? ??? ????? ???? ???? ?? ?? ??? ?? ??? ????.\n"
            ),
            "BOOTSTRAP.md": "# ?????\n\n??? ??? ? ???? ??? ?? ?? ??? ????.\n",
            "HEARTBEAT.md": "# ????\n\n??? ??? ????? ?? ?? ??? ??? ??? ????.\n",
            "TOOLS.md": "# ??\n\n?? ??? ??? ?? ?? ??? ????.\n",
            "MEMORY.md": (
                f"# {agent_id} ???\n\n"
                "??? ?? ???? ? ??? ????.\n"
            ),
        }
        for name, content in defaults.items():
            target = path / name
            if not target.exists():
                target.write_text(content, encoding="utf-8")
        json_defaults = {
            path / "beliefs" / "current_beliefs.json": {"beliefs": []},
            path / "commitments" / "current_commitment.json": {"commitments": []},
            path / "facts" / "current_request.json": {},
            path / "facts" / "current_response_template.json": {},
            path / "facts" / "current_report.json": {},
            path / "plans" / "current_plan.json": {},
            path / "mailboxes" / "messages.json": {"messages": []},
        }
        for json_path, payload in json_defaults.items():
            json_path.parent.mkdir(parents=True, exist_ok=True)
            if not json_path.exists():
                json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    def prepare_run_runtime(
        self,
        *,
        output_root: Path,
        worker_agent_ids: list[str],
        manager_agent_id: str,
        workspace_template_root: str,
        agent_workspace_aliases: dict[str, str] | None = None,
        runtime_agent_workspace_aliases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        run_date = str(Path(output_root).parent.name or "run").strip()
        run_label = str(Path(output_root).name or "latest").strip()
        runtime_root = Path(tempfile.gettempdir()) / "ManSim" / "openclaw_runtime" / run_date / run_label
        workspace_root = runtime_root / "workspaces"
        state_root = runtime_root / "state"
        facts_root = runtime_root / "facts"
        logs_root = runtime_root / "logs"

        self._reset_dir(runtime_root)
        workspace_root.mkdir(parents=True, exist_ok=True)
        state_root.mkdir(parents=True, exist_ok=True)
        facts_root.mkdir(parents=True, exist_ok=True)
        logs_root.mkdir(parents=True, exist_ok=True)

        template_root = self._workspace_template_root(workspace_template_root)
        base_agent_ids = [str(agent_id).strip() for agent_id in list(worker_agent_ids) + [str(manager_agent_id)] if str(agent_id).strip()]
        alias_map: dict[str, str] = {self._runtime_agent_key(agent_id): self._runtime_agent_key(agent_id) for agent_id in base_agent_ids}
        alias_overrides = dict(agent_workspace_aliases or runtime_agent_workspace_aliases or {})
        for runtime_id, alias in alias_overrides.items():
            runtime_key = self._runtime_agent_key(runtime_id)
            alias_key = self._runtime_agent_key(alias)
            if runtime_key and alias_key:
                alias_map[runtime_key] = alias_key
        self.runtime_agent_workspace_aliases = dict(alias_map)
        self.runtime_workspace_aliases = dict(alias_map)
        for workspace_alias in sorted(set(alias_map.values())):
            src = template_root / workspace_alias
            dst = workspace_root / workspace_alias
            if src.exists():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            self._ensure_workspace_minimum(dst, workspace_alias)

        profile_payload = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
        agents = profile_payload.setdefault("agents", {})
        if not isinstance(agents, dict):
            agents = {}
            profile_payload["agents"] = agents
        agent_list = agents.setdefault("list", [])
        if not isinstance(agent_list, list):
            agent_list = []
            agents["list"] = agent_list
        indexed_agents: dict[str, dict[str, Any]] = {}
        for agent in agent_list:
            if not isinstance(agent, dict):
                continue
            agent_id = str(agent.get("id", "")).strip()
            if not agent_id or agent_id.lower() == "main":
                continue
            indexed_agents[self._runtime_agent_key(agent_id)] = agent

        model_ref = self._model_ref(str(self.backend.get("provider", "")).strip().lower(), str(self.backend.get("model", "")).strip()) or self.model
        for runtime_agent_key, workspace_alias in alias_map.items():
            agent = indexed_agents.get(runtime_agent_key)
            if agent is None:
                agent = {"id": runtime_agent_key, "name": runtime_agent_key, "model": model_ref}
                agent_list.append(agent)
                indexed_agents[runtime_agent_key] = agent
            agent["workspace"] = str((workspace_root / workspace_alias).resolve())
            agent["agentDir"] = str((state_root / "agents" / runtime_agent_key / "agent").resolve())
            if agent.get("model") != model_ref:
                agent["model"] = model_ref

        self.runtime_profile_path.parent.mkdir(parents=True, exist_ok=True)

        self.runtime_profile_path.write_text(json.dumps(profile_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        self.runtime_root = runtime_root
        self.runtime_workspace_root = workspace_root
        self.runtime_workspace_aliases = dict(alias_map)
        self.runtime_state_root = state_root
        self.runtime_facts_root = facts_root
        self.gateway_log_path = logs_root / "gateway.log"
        return {
            "runtime_root": str(runtime_root.resolve()),
            "workspace_root": str(workspace_root.resolve()),
            "state_root": str(state_root.resolve()),
            "facts_root": str(facts_root.resolve()),
            "runtime_profile_path": str(self.runtime_profile_path.resolve()),
            "gateway_log_path": str(self.gateway_log_path.resolve()),
            "workspace_aliases": dict(self.runtime_agent_workspace_aliases),
        }

    @staticmethod
    def _wait_for_port(port: int, *, timeout_sec: float) -> bool:
        deadline = time.time() + max(1.0, timeout_sec)
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=1.0):
                    return True
            except OSError:
                time.sleep(0.5)
        return False

    @staticmethod
    def _locate_openclaw_cmd() -> str:
        for candidate in (
            shutil.which("openclaw.cmd"),
            shutil.which("openclaw"),
            os.path.expanduser(r"~\AppData\Roaming\npm\openclaw.cmd"),
        ):
            if candidate and Path(candidate).exists():
                return str(candidate)
        raise RuntimeError("OpenClaw executable not found. Expected openclaw.cmd on PATH or under AppData\Roaming\npm.")

    @staticmethod
    def _runtime_agent_key(agent_id: str) -> str:
        return str(agent_id or "").strip().upper()

    def _runtime_agent_workspace_alias(self, agent_id: str) -> str | None:
        normalized = self._runtime_agent_key(agent_id)
        if not normalized:
            return None
        alias = self.runtime_agent_workspace_aliases.get(normalized, normalized)
        alias = str(alias or normalized).strip()
        return alias or normalized

    def _runtime_agent_workspace(self, agent_id: str) -> Path | None:
        if self.runtime_workspace_root is None:
            return None
        alias = self._runtime_agent_workspace_alias(agent_id)
        if not alias:
            return None
        return self.runtime_workspace_root / alias

    @staticmethod
    def sanitize_session_id(session_key: str) -> str:
        raw = str(session_key or "").strip()
        if not raw:
            return "mansim-session"
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
        if not sanitized:
            sanitized = "mansim-session"
        return sanitized[:120]

    def native_agent_turn(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        agent_id: str,
        session_key: str,
        thinking: str = "minimal",
        timeout_sec: int | None = None,
    ) -> tuple[dict[str, Any], str, str, dict[str, str], dict[str, Any]]:
        cmd = self._locate_openclaw_cmd()
        sanitized_session = self.sanitize_session_id(session_key)
        runtime_agent_id = str(agent_id or "").strip()
        workspace = self._runtime_agent_workspace(runtime_agent_id)
        workspace_alias = self._runtime_agent_workspace_alias(runtime_agent_id) or runtime_agent_id
        phase_hint = ""
        if workspace is not None:
            phase_file = workspace / "facts" / "current_phase.txt"
            if phase_file.exists():
                phase_hint = str(phase_file.read_text(encoding="utf-8", errors="replace") or "").strip()
        prompt_payload = {
            "runtime_agent_id": runtime_agent_id,
            "workspace_alias": workspace_alias,
            "workspace_path": str(workspace.resolve()) if workspace is not None else "",
            "phase": phase_hint,
            "request_file": "facts/current_request.json",
            "response_template_file": "facts/current_response_template.json",
            "response_rule": "Return exactly one valid JSON object and nothing else.",
        }
        prompt_file = None
        prompt_file_label = "facts/current_native_turn.json"
        request_text = "{}\n"
        response_template_text = "{}\n"
        if workspace is not None:
            facts_dir = workspace / "facts"
            facts_dir.mkdir(parents=True, exist_ok=True)
            prompt_file = facts_dir / "current_native_turn.json"
            prompt_file.write_text(json.dumps(prompt_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            request_path = facts_dir / "current_request.json"
            response_template_path = facts_dir / "current_response_template.json"
            if request_path.exists():
                request_text = request_path.read_text(encoding="utf-8", errors="replace")
            if response_template_path.exists():
                response_template_text = response_template_path.read_text(encoding="utf-8", errors="replace")
            active_turn = [
                "# Active Turn",
                str(system_prompt or "").strip(),
                str(user_prompt or "").strip(),
                "## Request JSON",
                "```json",
                request_text.strip(),
                "```",
                "## Response Template JSON",
                "```json",
                response_template_text.strip(),
                "```",
                "## Output Rule",
                "Return exactly one valid JSON object matching the response template. No prose.",
            ]
            user_md_path = workspace / "USER.md"
            user_md_path.write_text("\n".join(part for part in active_turn if part) + "\n", encoding="utf-8")
            lowered_user = user_md_path.read_text(encoding="utf-8", errors="replace").replace(" ", "").replace("\n", "").lower()
            user_problems: list[str] = []
            if '##requestjson```json{}```' in lowered_user:
                user_problems.append('user_md_request_empty')
            if '##responsetemplatejson```json{}```' in lowered_user:
                user_problems.append('user_md_response_template_empty')
            if phase_hint and user_problems:
                raise RuntimeError(
                    'OpenClaw USER.md validation failed: '
                    + ','.join(user_problems)
                    + f' | phase={phase_hint} | agent_id={runtime_agent_id} | workspace_alias={workspace_alias} | workspace={workspace}'
                )
        else:
            prompt_file_label = "current native turn file"
        prompt_parts = [
            str(user_prompt or "").strip(),
            "Re-read USER.md, facts/current_request.json, and facts/current_response_template.json in your workspace.",
            "Return exactly one JSON object matching current_response_template.json.",
            "Do not output extra keys, prose, markdown, or acknowledgements.",
        ]
        prompt = " ".join(part for part in prompt_parts if part)
        env = os.environ.copy()
        backend_base_url = self._effective_backend_base_url()
        backend_api_key = str(self.backend.get("api_key", "")).strip()
        if backend_base_url:
            env["OLLAMA_BASE_URL"] = backend_base_url
        if backend_api_key:
            env["OLLAMA_API_KEY"] = backend_api_key
        argv = [
            cmd,
            "--profile",
            self.profile_name,
            "agent",
            "--local",
            "--agent",
            runtime_agent_id,
            "--session-id",
            sanitized_session,
            "--message",
            prompt,
            "--json",
        ]
        effective_timeout_sec = max(15, int(timeout_sec or self.timeout_sec))
        cli_timeout_sec = max(30, effective_timeout_sec) if timeout_sec is not None else max(180, effective_timeout_sec)
        process_timeout_sec = max(cli_timeout_sec + 20, effective_timeout_sec + 30)
        normalized_thinking = str(thinking or "").strip().lower()
        if normalized_thinking and normalized_thinking != "off":
            argv.extend(["--thinking", normalized_thinking])
        argv.extend([
            "--timeout",
            str(cli_timeout_sec),
        ])
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(workspace.resolve()) if workspace is not None else None,
            timeout=process_timeout_sec,
        )
        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        if completed.returncode != 0:
            raise RuntimeError(stderr or stdout or f"OpenClaw native agent turn failed with exit code {completed.returncode}.")
        try:
            response_payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError:
            balanced = self._extract_first_json_object_text(stdout)
            if balanced:
                try:
                    response_payload = json.loads(balanced)
                except json.JSONDecodeError:
                    response_payload = {"payloads": [{"text": stdout}], "meta": {"stderr": stderr, "fallback": "plain_text_stdout"}}
            else:
                response_payload = {"payloads": [{"text": stdout}], "meta": {"stderr": stderr, "fallback": "plain_text_stdout"}}
        content = self._native_response_text(response_payload) or stdout
        headers_for_log = {
            "transport": "native_local",
            "agent_id": runtime_agent_id,
            "workspace_alias": workspace_alias,
        }
        request_payload = {
            "command": "openclaw agent --local --json",
            "agent_id": runtime_agent_id,
            "workspace_alias": workspace_alias,
            "workspace_path": str(workspace.resolve()) if workspace is not None else "",
            "session_id": sanitized_session,
            "thinking": str(thinking or "off"),
            "thinking_option_used": bool(str(thinking or "").strip().lower() not in {"", "off"}),
            "message": prompt,
            "message_strategy": "workspace_file_backed_fixed_trigger",
            "message_chars": len(prompt),
            "cwd": str(workspace.resolve()) if workspace is not None else "",
            "prompt_file": str(prompt_file.resolve()) if prompt_file is not None else prompt_file_label,
            "prompt_payload": prompt_payload,
        }
        return response_payload, content, "openclaw://agent/local", headers_for_log, request_payload

    # The gateway is restarted against the freshly rendered runtime profile so the worker
    # sessions see the correct run-local workspaces and backend settings.
    # Restart against the freshly rendered runtime profile so worker ids, workspaces,
    # and run-local state always match the current simulation run.
    def restart_gateway(self) -> dict[str, Any]:
        if not self.runtime_profile_path.exists():
            raise RuntimeError(f"OpenClaw runtime profile not found: {self.runtime_profile_path}")
        cmd = self._locate_openclaw_cmd()
        port = self._gateway_port()
        log_path = self.gateway_log_path or (self._repo_root() / "openclaw" / "logs" / "gateway.runtime.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        backend_base_url = self._effective_backend_base_url()
        backend_api_key = str(self.backend.get("api_key", "")).strip()
        if backend_base_url:
            env["OLLAMA_BASE_URL"] = backend_base_url
        if backend_api_key:
            env["OLLAMA_API_KEY"] = backend_api_key
        argv = [
            cmd,
            "--profile",
            self.profile_name,
            "gateway",
            "run",
            "--port",
            str(port),
            "--auth",
            "none",
            "--bind",
            "loopback",
            "--force",
        ]
        with log_path.open("a", encoding="utf-8") as handle:
            subprocess.Popen(
                argv,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        if not self._wait_for_port(port, timeout_sec=max(15.0, float(self.timeout_sec))):
            raise RuntimeError(f"OpenClaw gateway failed to bind to port {port}.")
        return {
            "port": int(port),
            "profile_path": str(self.runtime_profile_path.resolve()),
            "log_path": str(log_path.resolve()),
            "gateway_url": self.gateway_url,
        }

    def _native_agent_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        backend_base_url = self._effective_backend_base_url()
        backend_api_key = str(self.backend.get("api_key", "")).strip()
        if backend_base_url:
            env["OLLAMA_BASE_URL"] = backend_base_url
        if backend_api_key:
            env["OLLAMA_API_KEY"] = backend_api_key
        return env

    @staticmethod
    def _extract_first_json_object_text(text: str) -> str:
        raw = str(text or "")
        start = raw.find("{")
        if start < 0:
            return ""
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(raw)):
            ch = raw[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:idx + 1]
        return ""

    @staticmethod
    def _native_response_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        raw = payload.get("text")
        if isinstance(raw, str) and raw.strip():
            return raw
        payloads = payload.get("payloads", [])
        if isinstance(payloads, list):
            for item in payloads:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        return text
        return ""


    def _gateway_port(self) -> int:
        url = self.gateway_url or "http://localhost:18789/v1"
        without_scheme = url.split("://", 1)[-1]
        host_port = without_scheme.split("/", 1)[0]
        if ":" in host_port:
            return int(host_port.rsplit(":", 1)[1])
        return 18789
    def chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        agent_id: str,
        session_key: str,
    ) -> tuple[dict[str, Any], str, str, dict[str, str], dict[str, Any]]:
        raise RuntimeError("OpenClaw chat_compat path is disabled. Use native_local only.")








