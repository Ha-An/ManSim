"""Production plateau stopping; does not gate SGD or roll back a policy."""
from __future__ import annotations

import math
import statistics
from typing import Any


def validate_early_stopping(config: dict[str, Any], *, max_iterations: int, seed_count: int) -> None:
    if not config.get("enabled", False):
        return
    for key in ("min_iterations", "patience_iterations", "consecutive_checks"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"training.early_stopping.{key} must be a positive integer.")
    if config["min_iterations"] > max_iterations:
        raise ValueError("Early stopping minimum exceeds policy_iterations.")
    threshold = float(config.get("paired_ci_upper_threshold", float("nan")))
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("paired_ci_upper_threshold must be finite and nonnegative.")
    if seed_count < 2:
        raise ValueError("Production early stopping requires at least two paired screening seeds.")


class ProductionEarlyStopping:
    def __init__(self, config: dict[str, Any]):
        self.config = dict(config)
        self.best_iteration = -1
        self.best_products: dict[int, float] = {}
        self.history: list[dict[str, Any]] = []

    def observe(self, iteration: int, products: dict[int, float]) -> dict[str, Any]:
        if not self.config.get("enabled", False):
            return {}
        if not products or any(not math.isfinite(value) for value in products.values()):
            raise ValueError("Early stopping requires finite completed-product observations.")
        if self.history and iteration <= self.history[-1]["iteration"]:
            raise ValueError("Screening observations must have increasing iterations.")
        if self.best_products and products.keys() != self.best_products.keys():
            raise ValueError("Early stopping must compare exactly the same screening seeds.")
        if len(products) < 2:
            raise ValueError("Paired stopping CI requires at least two seeds.")
        mean = statistics.fmean(products.values())
        improved = not self.best_products or mean > statistics.fmean(self.best_products.values())
        reference = self.best_iteration
        differences = [products[seed] - self.best_products[seed] for seed in sorted(products)] if self.best_products else []
        delta = statistics.fmean(differences) if differences else None
        upper = (delta + 1.96 * statistics.stdev(differences) / math.sqrt(len(differences))) if differences else None
        if improved:
            self.best_iteration = iteration
            self.best_products = dict(products)
        record = {
            "iteration": iteration, "early_stop_reference_iteration": reference if reference >= 0 else None,
            "early_stop_paired_mean": delta, "early_stop_paired_ci_upper": upper,
            "early_stop_best_iteration": self.best_iteration,
            "early_stop_stagnant_iterations": iteration - self.best_iteration,
            "early_stop_low_gain": bool(not improved and upper is not None
                                        and upper < float(self.config["paired_ci_upper_threshold"])),
        }
        self.history.append(record)
        recent = self.history[-self.config["consecutive_checks"]:]
        record["early_stop_triggered"] = bool(
            iteration >= self.config["min_iterations"]
            and record["early_stop_stagnant_iterations"] >= self.config["patience_iterations"]
            and len(recent) == self.config["consecutive_checks"]
            and all(row["early_stop_low_gain"] for row in recent)
        )
        return dict(record)
