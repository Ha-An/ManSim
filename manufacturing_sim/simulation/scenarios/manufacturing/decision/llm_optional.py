from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timezone
from typing import Any

from .base import DecisionModule, JobPlan, StrategyState


class OptionalLLMDecisionModule(DecisionModule):
    def __init__(self, cfg: dict[str, Any], llm_cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg
        self.llm_cfg = llm_cfg or {}
        self.enabled = bool(self.llm_cfg.get("enabled", True))
        self.provider = str(self.llm_cfg.get("provider", "openai_compatible")).strip().lower()
        self.server_url = str(self.llm_cfg.get("server_url", "http://localhost:8000/v1")).strip()
        self.model = str(self.llm_cfg.get("model", "")).strip()
        self.timeout_sec = int(self.llm_cfg.get("timeout_sec", 30))
        self.temperature = float(self.llm_cfg.get("temperature", 0.2))
        self.max_tokens = int(self.llm_cfg.get("max_tokens", 800))
        self.api_key = str(self.llm_cfg.get("api_key", "")).strip() or str(os.getenv("OPENAI_API_KEY", "")).strip()

        comm_cfg = self.llm_cfg.get("communication", {}) if isinstance(self.llm_cfg.get("communication", {}), dict) else {}
        self.communication_enabled = bool(comm_cfg.get("enabled", True))
        self.comm_rounds = max(1, int(comm_cfg.get("rounds", 2)))
        self.comm_max_transcript = max(1, int(comm_cfg.get("max_transcript_messages", 24)))

        num_agents = int((cfg.get("factory", {}) or {}).get("num_agents", 4))
        self.agent_ids = [f"A{i}" for i in range(1, num_agents + 1)]

        mem_cfg = self.llm_cfg.get("memory", {}) if isinstance(self.llm_cfg.get("memory", {}), dict) else {}
        self.memory_window_days = max(1, int(mem_cfg.get("history_window_days", 7)))
        self.include_agent_memory = bool(mem_cfg.get("include_agent_memory", True))

        self._last_discussion_trace: list[dict[str, Any]] = []
        self.shared_norms_memory: list[dict[str, Any]] = []
        self.shared_discussion_memory: list[dict[str, Any]] = []
        self.agent_memories: dict[str, list[dict[str, Any]]] = {aid: [] for aid in self.agent_ids}
        self._llm_exchange_records: list[dict[str, Any]] = []
        self._llm_call_seq = 0

        if not self.enabled:
            self._fail("decision.mode=llm but llm.enabled=false.")
        if self.provider != "openai_compatible":
            self._fail(f"Unsupported llm.provider: {self.provider}")
        if not self.server_url:
            self._fail("llm.server_url is empty.")
        if not self.model:
            self._fail("llm.model is empty.")

    def _fail(self, message: str) -> None:
        warnings.warn(f"[LLM WARNING] {message}")
        raise RuntimeError(f"[LLM WARNING] {message}")

    def consume_last_discussion_trace(self) -> list[dict[str, Any]]:
        out = list(self._last_discussion_trace)
        self._last_discussion_trace = []
        return out

    def is_communication_enabled(self) -> bool:
        return bool(self.communication_enabled)

    def get_llm_exchange_records(self) -> list[dict[str, Any]]:
        return list(self._llm_exchange_records)

    def _record_llm_exchange(self, record: dict[str, Any]) -> None:
        self._llm_exchange_records.append(record)

    def _append_bounded(self, seq: list[dict[str, Any]], item: dict[str, Any]) -> None:
        seq.append(item)
        if len(seq) > self.memory_window_days:
            del seq[: len(seq) - self.memory_window_days]

    def _memory_context(self) -> dict[str, Any]:
        agent_history: dict[str, list[dict[str, Any]]] = {}
        if self.include_agent_memory:
            for aid in self.agent_ids:
                hist = self.agent_memories.get(aid, [])
                if hist:
                    agent_history[aid] = hist[-self.memory_window_days :]
        return {
            "norms_history": self.shared_norms_memory[-self.memory_window_days :],
            "discussion_history": self.shared_discussion_memory[-self.memory_window_days :],
            "agent_history": agent_history,
        }

    def _update_memory(
        self,
        day_summary: dict[str, Any],
        updated_norms: dict[str, Any],
        transcript: list[dict[str, Any]],
        summary: str,
    ) -> dict[str, Any]:
        day = int(day_summary.get("day", len(self.shared_discussion_memory) + 1))
        norms_item = {"day": day, "norms": dict(updated_norms)}
        self._append_bounded(self.shared_norms_memory, norms_item)

        key_messages: list[str] = []
        for msg in transcript[-6:]:
            aid = str(msg.get("agent_id", "")).strip()
            utt = str(msg.get("utterance", "")).strip()
            if aid and utt:
                key_messages.append(f"{aid}: {utt}")
        discussion_item = {
            "day": day,
            "summary": summary,
            "key_messages": key_messages,
            "message_count": len(transcript),
        }
        self._append_bounded(self.shared_discussion_memory, discussion_item)

        if self.include_agent_memory:
            for aid in self.agent_ids:
                utterances = [
                    str(msg.get("utterance", "")).strip()
                    for msg in transcript
                    if str(msg.get("agent_id", "")).strip() == aid and str(msg.get("utterance", "")).strip()
                ]
                agent_item = {
                    "day": day,
                    "summary": summary,
                    "recent_utterances": utterances[-2:],
                }
                self._append_bounded(self.agent_memories[aid], agent_item)

        return {
            "day": day,
            "memory_window_days": self.memory_window_days,
            "norms_memory_size": len(self.shared_norms_memory),
            "discussion_memory_size": len(self.shared_discussion_memory),
        }

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            chunk = stripped[start : end + 1]
            try:
                parsed = json.loads(chunk)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return None
        return None

    def _call_llm_json(
        self,
        user_prompt: str,
        system_prompt: str,
        *,
        call_name: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.server_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        raw = json.dumps(body).encode("utf-8")
        headers_for_log = {"Content-Type": "application/json"}
        if self.api_key:
            headers_for_log["Authorization"] = "Bearer ***"

        started_ts = time.time()
        started_at_utc = datetime.now(timezone.utc).isoformat()
        self._llm_call_seq += 1
        call_id = self._llm_call_seq

        req_headers = {"Content-Type": "application/json"}
        if self.api_key:
            req_headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url=url, data=raw, headers=req_headers, method="POST")

        payload: dict[str, Any] | None = None
        content = ""
        error_message = ""
        status = "ok"
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            try:
                content = str(payload["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError) as exc:
                status = "error"
                error_message = f"LLM response format error: {exc}"
                self._record_llm_exchange(
                    {
                        "call_id": call_id,
                        "call_name": call_name,
                        "status": status,
                        "started_at_utc": started_at_utc,
                        "latency_sec": round(time.time() - started_ts, 3),
                        "context": context or {},
                        "request": {
                            "url": url,
                            "headers": headers_for_log,
                            "payload": body,
                        },
                        "response": payload if isinstance(payload, dict) else {},
                        "response_text": content,
                        "parsed": {},
                        "error": error_message,
                    }
                )
                self._fail(error_message)

            parsed = self._extract_json_object(content)
            if not isinstance(parsed, dict):
                status = "error"
                error_message = "Failed to parse JSON object from LLM response."
                self._record_llm_exchange(
                    {
                        "call_id": call_id,
                        "call_name": call_name,
                        "status": status,
                        "started_at_utc": started_at_utc,
                        "latency_sec": round(time.time() - started_ts, 3),
                        "context": context or {},
                        "request": {
                            "url": url,
                            "headers": headers_for_log,
                            "payload": body,
                        },
                        "response": payload if isinstance(payload, dict) else {},
                        "response_text": content,
                        "parsed": {},
                        "error": error_message,
                    }
                )
                self._fail(error_message)

            self._record_llm_exchange(
                {
                    "call_id": call_id,
                    "call_name": call_name,
                    "status": status,
                    "started_at_utc": started_at_utc,
                    "latency_sec": round(time.time() - started_ts, 3),
                    "context": context or {},
                    "request": {
                        "url": url,
                        "headers": headers_for_log,
                        "payload": body,
                    },
                    "response": payload if isinstance(payload, dict) else {},
                    "response_text": content,
                    "parsed": parsed,
                    "error": "",
                }
            )
            return parsed
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            status = "error"
            error_message = f"LLM call failed: {exc}"
            self._record_llm_exchange(
                {
                    "call_id": call_id,
                    "call_name": call_name,
                    "status": status,
                    "started_at_utc": started_at_utc,
                    "latency_sec": round(time.time() - started_ts, 3),
                    "context": context or {},
                    "request": {
                        "url": url,
                        "headers": headers_for_log,
                        "payload": body,
                    },
                    "response": payload if isinstance(payload, dict) else {},
                    "response_text": content,
                    "parsed": {},
                    "error": error_message,
                }
            )
            self._fail(error_message)

    @staticmethod
    def _as_float_map(src: Any, base: dict[str, float]) -> dict[str, float]:
        out = dict(base)
        if isinstance(src, dict):
            for k, v in src.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        return out

    @staticmethod
    def _as_int_map(src: Any, base: dict[str, int]) -> dict[str, int]:
        out = dict(base)
        if isinstance(src, dict):
            for k, v in src.items():
                try:
                    out[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        return out

    @staticmethod
    def _as_str_list(src: Any, fallback: list[str]) -> list[str]:
        if not isinstance(src, list):
            return fallback
        out: list[str] = []
        for x in src:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out if out else fallback

    def _build_strategy(self, llm_obj: dict[str, Any], fallback: StrategyState) -> StrategyState:
        bottleneck = fallback.bottleneck_station
        try:
            bottleneck = int(llm_obj.get("bottleneck_station", bottleneck))
        except (TypeError, ValueError):
            self._fail("reflect response has invalid bottleneck_station.")
        notes = self._as_str_list(llm_obj.get("notes"), fallback.notes)
        bias = self._as_float_map(llm_obj.get("priority_bias"), fallback.priority_bias)
        return StrategyState(bottleneck_station=bottleneck, notes=notes, priority_bias=bias)

    def _build_job_plan(self, llm_obj: dict[str, Any], fallback: JobPlan) -> JobPlan:
        if "task_weights" not in llm_obj or "quotas" not in llm_obj:
            self._fail("propose_jobs response missing task_weights/quotas.")
        weights = self._as_float_map(llm_obj.get("task_weights"), fallback.task_weights)
        quotas = self._as_int_map(llm_obj.get("quotas"), fallback.quotas)
        rationale = str(llm_obj.get("rationale", fallback.rationale))
        return JobPlan(task_weights=weights, quotas=quotas, rationale=rationale)

    def _build_norms(self, llm_obj: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        updated = dict(fallback)
        for k, v in llm_obj.items():
            if isinstance(k, str):
                updated[k] = v
        return updated

    def _build_urgent(self, llm_obj: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        base_updates = fallback.get("weight_updates", {})
        updates = self._as_float_map(llm_obj.get("weight_updates"), base_updates if isinstance(base_updates, dict) else {})
        return {"weight_updates": updates}

    def _prompt(self, title: str, payload: dict[str, Any], schema_hint: str) -> str:
        return (
            f"{title}\n"
            f"Input JSON:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            f"Return JSON schema:\n{schema_hint}\n"
        )

    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        fallback = StrategyState(bottleneck_station=2, notes=[], priority_bias={"flow": 1.0, "maintenance": 1.0, "quality": 1.0})
        prompt = self._prompt(
            title="Decide daily strategy from observation.",
            payload={
                "observation": observation,
                "memory": self._memory_context(),
                "fallback": fallback.__dict__,
            },
            schema_hint=(
                '{"bottleneck_station": int, '
                '"notes": [str], '
                '"priority_bias": {"flow": float, "maintenance": float, "quality": float}}'
            ),
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt="You are a manufacturing strategy planner. Return exactly one JSON object with no markdown.",
            call_name="reflect",
            context={"phase": "reflect", "day": observation.get("day")},
        )
        return self._build_strategy(llm_obj, fallback)

    def propose_jobs(
        self,
        observation: dict[str, Any],
        strategy: StrategyState,
        norms: dict[str, Any],
    ) -> JobPlan:
        fallback = JobPlan(
            task_weights={"safety": 1.0, "blocking": 1.0, "flow": 1.0, "supply": 1.0, "quality": 1.0, "maintenance": 1.0, "support": 1.0},
            quotas={"warehouse_material_runs": 20, "setup_runs": 40, "transfer_runs": 40, "inspection_runs": 35, "pm_runs": 6},
            rationale="llm_default",
        )
        prompt = self._prompt(
            title="Propose job plan for this day.",
            payload={
                "observation": observation,
                "strategy": strategy.__dict__,
                "norms": norms,
                "memory": self._memory_context(),
                "fallback": {
                    "task_weights": fallback.task_weights,
                    "quotas": fallback.quotas,
                    "rationale": fallback.rationale,
                },
            },
            schema_hint='{"task_weights": {str: float}, "quotas": {str: int}, "rationale": str}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt="You are a manufacturing operations planner. Return exactly one JSON object with no markdown.",
            call_name="propose_jobs",
            context={"phase": "propose_jobs", "day": observation.get("day")},
        )
        return self._build_job_plan(llm_obj, fallback)

    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        self._last_discussion_trace = []
        fallback = dict(norms)

        if not self.communication_enabled:
            prompt = self._prompt(
                title="Update norms after townhall discussion.",
                payload={
                    "day_summary": day_summary,
                    "norms": norms,
                    "memory": self._memory_context(),
                    "fallback": fallback,
                    "communication_enabled": False,
                },
                schema_hint='{"updated_norms": {str: any}, "summary": str}',
            )
            llm_obj = self._call_llm_json(
                user_prompt=prompt,
                system_prompt="You are a townhall moderator. Return exactly one JSON object with no markdown.",
                call_name="discuss_norm_update",
                context={"phase": "discuss_norm_update", "day": day_summary.get("day"), "communication_enabled": False},
            )
            if "updated_norms" not in llm_obj or not isinstance(llm_obj["updated_norms"], dict):
                self._fail("discussion response missing updated_norms.")
            summary = str(llm_obj.get("summary", "")).strip()
            memory_update = self._update_memory(day_summary, llm_obj["updated_norms"], [], summary)
            self._last_discussion_trace.append(
                {
                    "mode": "communication_off",
                    "summary": summary,
                    "rounds": 0,
                    "messages": 0,
                    "memory_update": memory_update,
                }
            )
            return self._build_norms(llm_obj["updated_norms"], fallback)

        transcript: list[dict[str, Any]] = []
        for ridx in range(1, self.comm_rounds + 1):
            for aid in self.agent_ids:
                trimmed = transcript[-self.comm_max_transcript :]
                agent_memory = self.agent_memories.get(aid, [])[-self.memory_window_days :] if self.include_agent_memory else []
                prompt = self._prompt(
                    title=f"Townhall round {ridx}, speaker {aid}.",
                    payload={
                        "agent_id": aid,
                        "round": ridx,
                        "day_summary": day_summary,
                        "norms": norms,
                        "shared_memory": self._memory_context(),
                        "agent_memory": agent_memory,
                        "transcript": trimmed,
                    },
                    schema_hint='{"utterance": str, "proposal": {"norm_updates": {str: any}, "weight_updates": {str: float}}}',
                )
                llm_obj = self._call_llm_json(
                    user_prompt=prompt,
                    system_prompt=(
                        "You are one agent in a manufacturing townhall discussion. "
                        "Produce one concise utterance and optional proposal as JSON."
                    ),
                    call_name="townhall_round",
                    context={"phase": "townhall_round", "day": day_summary.get("day"), "round": ridx, "agent_id": aid},
                )
                utterance = str(llm_obj.get("utterance", "")).strip()
                if not utterance:
                    self._fail("discussion utterance is empty.")
                proposal = llm_obj.get("proposal", {})
                if not isinstance(proposal, dict):
                    proposal = {}
                transcript.append({"round": ridx, "agent_id": aid, "utterance": utterance, "proposal": proposal})

        synthesis_prompt = self._prompt(
            title="Synthesize townhall transcript and update shared norms.",
            payload={
                "day_summary": day_summary,
                "norms": norms,
                "memory": self._memory_context(),
                "transcript": transcript[-self.comm_max_transcript :],
            },
            schema_hint='{"updated_norms": {str: any}, "summary": str}',
        )
        synthesis = self._call_llm_json(
            user_prompt=synthesis_prompt,
            system_prompt="You are a townhall moderator. Build consensus and return updated_norms JSON.",
            call_name="townhall_synthesis",
            context={"phase": "townhall_synthesis", "day": day_summary.get("day"), "rounds": self.comm_rounds},
        )
        if "updated_norms" not in synthesis or not isinstance(synthesis["updated_norms"], dict):
            self._fail("discussion synthesis missing updated_norms.")
        summary = str(synthesis.get("summary", "")).strip()
        memory_update = self._update_memory(day_summary, synthesis["updated_norms"], transcript, summary)
        self._last_discussion_trace = transcript + [
            {
                "role": "moderator",
                "summary": summary,
                "rounds": self.comm_rounds,
                "messages": len(transcript),
                "communication_enabled": True,
                "memory_update": memory_update,
            }
        ]
        return self._build_norms(synthesis["updated_norms"], fallback)

    def urgent_discuss(self, event: dict[str, Any], local_state: dict[str, Any]) -> dict[str, Any]:
        fallback = {"weight_updates": {}}
        prompt = self._prompt(
            title="Urgent discussion for incident response.",
            payload={
                "event": event,
                "local_state": local_state,
                "memory": self._memory_context(),
                "fallback": fallback,
            },
            schema_hint='{"weight_updates": {str: float}}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt="You are an urgent response coordinator. Return exactly one JSON object.",
            call_name="urgent_discuss",
            context={"phase": "urgent_discuss", "event_type": event.get("event_type", "")},
        )
        return self._build_urgent(llm_obj, fallback)


