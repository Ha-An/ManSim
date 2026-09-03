from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from statistics import mean
from typing import Any, Iterable

from humanoidsim import expand_task_steps


class TimingConfigError(ValueError):
    """Raised when a scenario timing profile does not match HumanoidSim."""


@dataclass(frozen=True)
class TriangularDistribution:
    minimum: float
    mode: float
    maximum: float

    @classmethod
    def from_config(cls, value: Any, *, label: str) -> "TriangularDistribution":
        if not isinstance(value, dict):
            raise TimingConfigError(f"{label} must be a mapping.")
        distribution = str(value.get("type", value.get("distribution", "triangular"))).strip().lower()
        if distribution != "triangular":
            raise TimingConfigError(f"{label}.type must be 'triangular', got {distribution!r}.")
        try:
            minimum = float(value["min"])
            mode = float(value["mode"])
            maximum = float(value["max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TimingConfigError(f"{label} must define numeric min, mode, and max values.") from exc
        if not all(math.isfinite(item) for item in (minimum, mode, maximum)):
            raise TimingConfigError(f"{label} values must be finite.")
        if minimum < 0.0 or not minimum <= mode <= maximum:
            raise TimingConfigError(
                f"{label} must satisfy 0 <= min <= mode <= max; got {minimum}, {mode}, {maximum}."
            )
        return cls(minimum=minimum, mode=mode, maximum=maximum)

    @property
    def expected(self) -> float:
        return (self.minimum + self.mode + self.maximum) / 3.0

    def to_dict(self) -> dict[str, float | str]:
        return {
            "type": "triangular",
            "min": self.minimum,
            "mode": self.mode,
            "max": self.maximum,
            "expected": self.expected,
        }


def sample_triangular(
    distribution: TriangularDistribution,
    *,
    seed: int,
    namespace: str,
    sample_key: str,
) -> float:
    digest = hashlib.sha256(f"{seed}|{namespace}|{sample_key}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:16], byteorder="big", signed=False))
    return float(rng.triangular(distribution.minimum, distribution.maximum, distribution.mode))


class PrimitiveTimingResolver:
    """Resolve strict, scenario-owned timing for HumanoidSim primitive leaves."""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        scenario_type: str,
        seed: int,
        expected_task_codes: Iterable[str],
    ) -> None:
        if not isinstance(cfg, dict) or not cfg:
            raise TimingConfigError(f"Missing task_primitive_timing config for {scenario_type}.")
        self.cfg = cfg
        self.scenario_type = str(scenario_type).strip().lower()
        self.seed = int(seed)
        configured_scenario = str(cfg.get("scenario_type", "")).strip().lower()
        if configured_scenario != self.scenario_type:
            raise TimingConfigError(
                f"Timing profile scenario_type={configured_scenario!r} does not match scenario={self.scenario_type!r}."
            )
        if str(cfg.get("unit", "min")).strip().lower() != "min":
            raise TimingConfigError("task_primitive_timing.unit must be 'min'.")

        movement_cfg = cfg.get("movement", {})
        if not isinstance(movement_cfg, dict):
            raise TimingConfigError("task_primitive_timing.movement must be a mapping.")
        if str(movement_cfg.get("model", "")).strip().lower() != "per_tile_triangular":
            raise TimingConfigError("movement.model must be 'per_tile_triangular'.")
        if str(movement_cfg.get("sample_scope", "")).strip().lower() != "move":
            raise TimingConfigError("movement.sample_scope must be 'move'.")
        self.movement_distribution = TriangularDistribution.from_config(
            movement_cfg.get("per_tile_min"), label="movement.per_tile_min"
        )
        raw_multipliers = movement_cfg.get("multipliers", {})
        if not isinstance(raw_multipliers, dict):
            raise TimingConfigError("movement.multipliers must be a mapping.")
        self.movement_multipliers = {
            str(key).strip().lower(): float(value)
            for key, value in raw_multipliers.items()
            if str(key).strip()
        }
        for key, value in self.movement_multipliers.items():
            if not math.isfinite(value) or value <= 0.0:
                raise TimingConfigError(f"movement.multipliers.{key} must be positive and finite.")

        raw_tasks = cfg.get("tasks", {})
        if not isinstance(raw_tasks, dict):
            raise TimingConfigError("task_primitive_timing.tasks must be a mapping.")
        self.task_steps: dict[str, dict[str, dict[str, Any]]] = {}
        for task_code, task_cfg in raw_tasks.items():
            code = str(task_code).strip().upper()
            steps = task_cfg.get("steps", {}) if isinstance(task_cfg, dict) else {}
            if not isinstance(steps, dict):
                raise TimingConfigError(f"tasks.{code}.steps must be a mapping.")
            parsed_steps: dict[str, dict[str, Any]] = {}
            for path, step_cfg in steps.items():
                path_text = str(path).strip()
                if not path_text or not isinstance(step_cfg, dict):
                    raise TimingConfigError(f"tasks.{code}.steps contains an invalid step entry.")
                call_code = str(step_cfg.get("call_code", "")).strip().upper()
                timing_model = str(step_cfg.get("timing_model", "duration")).strip().lower()
                if timing_model not in {"duration", "movement"}:
                    raise TimingConfigError(f"tasks.{code}.steps.{path_text}.timing_model is invalid.")
                distribution = None
                if timing_model == "duration":
                    distribution = TriangularDistribution.from_config(
                        step_cfg.get("distribution"),
                        label=f"tasks.{code}.steps.{path_text}.distribution",
                    )
                parsed_steps[path_text] = {
                    "call_code": call_code,
                    "timing_model": timing_model,
                    "distribution": distribution,
                }
            self.task_steps[code] = parsed_steps

        expected_codes = {str(code).strip().upper() for code in expected_task_codes if str(code).strip()}
        configured_codes = set(self.task_steps)
        if configured_codes != expected_codes:
            missing = sorted(expected_codes - configured_codes)
            extra = sorted(configured_codes - expected_codes)
            raise TimingConfigError(f"Timing task set mismatch; missing={missing}, extra={extra}.")
        self._validate_humanoidsim_coverage(expected_codes)
        self.samples: list[dict[str, Any]] = []
        self._movement_sample_cache: dict[tuple[str, float], float] = {}

    def _validate_humanoidsim_coverage(self, task_codes: set[str]) -> None:
        for task_code in sorted(task_codes):
            rows = [
                row
                for row in expand_task_steps(task_code, {})
                if str(row.get("call_level", "")).strip().upper() == "PRIMITIVE_SKILL"
            ]
            expected = {str(row.get("path", "")): str(row.get("call_code", "")).strip().upper() for row in rows}
            configured = self.task_steps[task_code]
            if set(configured) != set(expected):
                missing = sorted(set(expected) - set(configured))
                extra = sorted(set(configured) - set(expected))
                raise TimingConfigError(f"Primitive timing coverage mismatch for {task_code}; missing={missing}, extra={extra}.")
            for path, call_code in expected.items():
                configured_call = str(configured[path]["call_code"])
                if configured_call != call_code:
                    raise TimingConfigError(
                        f"Primitive call mismatch at {path}: HumanoidSim={call_code}, timing={configured_call}."
                    )
                required_model = "movement" if call_code == "NAVIGATE_TO" else "duration"
                if configured[path]["timing_model"] != required_model:
                    raise TimingConfigError(f"Step {path} must use timing_model={required_model}.")

    def step_entry(self, task_code: str, step_path: str) -> dict[str, Any]:
        code = str(task_code).strip().upper()
        path = str(step_path).strip()
        try:
            return self.task_steps[code][path]
        except KeyError as exc:
            raise TimingConfigError(f"No primitive timing for {code}:{path}.") from exc

    def expected_step_duration(self, task_code: str, step_path: str) -> float:
        distribution = self.step_entry(task_code, step_path).get("distribution")
        return 0.0 if distribution is None else float(distribution.expected)

    def expected_task_duration(self, task_code: str) -> float:
        code = str(task_code).strip().upper()
        if code not in self.task_steps:
            raise TimingConfigError(f"No primitive timing task definition for {code}.")
        return sum(
            float(entry["distribution"].expected)
            for entry in self.task_steps[code].values()
            if entry.get("distribution") is not None
        )

    def expected_call_duration(self, task_code: str, call_code: str) -> float:
        code = str(task_code).strip().upper()
        target = str(call_code).strip().upper()
        rows = [
            entry
            for entry in self.task_steps.get(code, {}).values()
            if str(entry.get("call_code", "")).strip().upper() == target
            and entry.get("distribution") is not None
        ]
        if not rows:
            raise TimingConfigError(f"No duration timing for {code}:{target}.")
        return sum(float(entry["distribution"].expected) for entry in rows)

    def distribution_for_step(self, task_code: str, step_path: str) -> TriangularDistribution | None:
        return self.step_entry(task_code, step_path).get("distribution")

    def sample_step_duration(self, task_code: str, step_path: str, *, sample_key: str) -> float:
        distribution = self.step_entry(task_code, step_path).get("distribution")
        if distribution is None:
            return 0.0
        value = sample_triangular(
            distribution,
            seed=self.seed,
            namespace=f"{self.scenario_type}:primitive:{task_code}:{step_path}",
            sample_key=sample_key,
        )
        self.record_sample("primitive", sample_key, value, distribution, task_code=task_code, step_path=step_path)
        return value

    def sample_tile_time(self, *, sample_key: str, multiplier: float = 1.0) -> float:
        cache_key = (str(sample_key), float(multiplier))
        if cache_key in self._movement_sample_cache:
            return self._movement_sample_cache[cache_key]
        base = sample_triangular(
            self.movement_distribution,
            seed=self.seed,
            namespace=f"{self.scenario_type}:movement",
            sample_key=sample_key,
        )
        effective_multiplier = max(0.0, float(multiplier))
        value = base * effective_multiplier
        effective_distribution = TriangularDistribution(
            minimum=self.movement_distribution.minimum * effective_multiplier,
            mode=self.movement_distribution.mode * effective_multiplier,
            maximum=self.movement_distribution.maximum * effective_multiplier,
        )
        self._movement_sample_cache[cache_key] = value
        self.record_sample(
            "movement_per_tile",
            sample_key,
            value,
            effective_distribution,
            multiplier=effective_multiplier,
            base_sampled_min=base,
            base_distribution=self.movement_distribution.to_dict(),
        )
        return value

    @property
    def expected_tile_time(self) -> float:
        return self.movement_distribution.expected

    def multiplier(self, key: str, default: float = 1.0) -> float:
        return float(self.movement_multipliers.get(str(key).strip().lower(), default))

    def record_sample(
        self,
        kind: str,
        sample_key: str,
        value: float,
        distribution: TriangularDistribution,
        **details: Any,
    ) -> None:
        self.samples.append(
            {
                "kind": kind,
                "sample_key": sample_key,
                "sampled_min": float(value),
                "distribution": distribution.to_dict(),
                **details,
            }
        )

    def summary(self) -> dict[str, Any]:
        grouped: dict[str, list[float]] = {}
        for row in self.samples:
            grouped.setdefault(str(row.get("kind", "unknown")), []).append(float(row.get("sampled_min", 0.0) or 0.0))
        return {
            "scenario_type": self.scenario_type,
            "profile_fingerprint": self.profile_fingerprint,
            "sample_count": len(self.samples),
            "by_kind": {
                key: {
                    "count": len(values),
                    "mean_min": mean(values) if values else 0.0,
                    "min_min": min(values) if values else 0.0,
                    "max_min": max(values) if values else 0.0,
                }
                for key, values in sorted(grouped.items())
            },
        }

    @property
    def profile_fingerprint(self) -> str:
        payload = json.dumps(self.cfg, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
