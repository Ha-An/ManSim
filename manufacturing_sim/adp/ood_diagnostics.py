from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .compact import CompactMCBatch
from .model import require_torch


@dataclass
class OODSupportBank:
    reference_vectors: Any
    center: Any
    scale: Any
    distance_threshold: float
    reference_count: int
    calibration_count: int


def _masked_mean(values: Any, mask: Any, *, dimensions: tuple[int, ...]) -> Any:
    weights = mask.to(values.dtype)
    while weights.ndim < values.ndim:
        weights = weights.unsqueeze(-1)
    return (values * weights).sum(dim=dimensions) / weights.sum(dim=dimensions).clamp_min(1.0)


def compact_afterstate_signatures(batch: CompactMCBatch, indices: Any | None = None) -> Any:
    """Return a fixed, model-independent state-action support signature."""

    torch = require_torch()
    if indices is None:
        indices = torch.arange(len(batch), dtype=torch.long)
    elif not torch.is_tensor(indices):
        indices = torch.as_tensor(indices, dtype=torch.long)
    global_features = batch.global_features.index_select(0, indices)
    workers = batch.worker_features.index_select(0, indices)
    tasks = batch.task_features.index_select(0, indices)
    pairs = batch.pair_features.index_select(0, indices)
    worker_mask = batch.worker_mask.index_select(0, indices)
    task_mask = batch.task_mask.index_select(0, indices)
    feasibility = batch.feasibility.index_select(0, indices)
    selected = batch.selected_assignment_mask.index_select(0, indices)

    worker_mean = _masked_mean(workers, worker_mask, dimensions=(1,))
    task_mean = _masked_mean(tasks, task_mask, dimensions=(1,))
    feasible_pair_mean = _masked_mean(pairs, feasibility, dimensions=(1, 2))
    selected_pair_mean = _masked_mean(pairs, selected, dimensions=(1, 2))
    worker_count = worker_mask.sum(dim=1).clamp_min(1).to(torch.float32)
    task_count = task_mask.sum(dim=1).clamp_min(1).to(torch.float32)
    feasible_count = feasibility.sum(dim=(1, 2)).to(torch.float32)
    selected_count = selected.sum(dim=(1, 2)).to(torch.float32)
    action_context = torch.stack(
        [
            selected_count / worker_count,
            feasible_count / (worker_count * task_count).clamp_min(1.0),
        ],
        dim=1,
    )
    return torch.cat(
        [
            global_features,
            worker_mean,
            task_mean,
            feasible_pair_mean,
            selected_pair_mean,
            action_context,
        ],
        dim=1,
    ).to(torch.float32)


def _sample_indices(count: int, sample_count: int, *, seed: int) -> Any:
    torch = require_torch()
    if count <= 0 or sample_count <= 0:
        return torch.zeros((0,), dtype=torch.long)
    generator = torch.Generator(device="cpu")
    generator.manual_seed(int(seed))
    return torch.randperm(count, generator=generator)[: min(count, sample_count)]


def _minimum_distances(query: Any, reference: Any, *, device: Any, chunk_size: int = 512) -> Any:
    torch = require_torch()
    if not len(query) or not len(reference):
        return torch.zeros((len(query),), dtype=torch.float32)
    reference_device = reference.to(device)
    dimension_scale = math.sqrt(max(1, int(reference.shape[1])))
    rows = []
    with torch.no_grad():
        for start in range(0, len(query), max(1, int(chunk_size))):
            chunk = query[start : start + chunk_size].to(device)
            rows.append((torch.cdist(chunk, reference_device).amin(dim=1) / dimension_scale).cpu())
    return torch.cat(rows) if rows else torch.zeros((0,), dtype=torch.float32)


def build_ood_support_bank(
    batch: CompactMCBatch,
    *,
    device: Any,
    seed: int,
    reference_samples: int = 1024,
    calibration_samples: int = 1024,
    quantile: float = 0.95,
) -> OODSupportBank | None:
    torch = require_torch()
    if len(batch) < 4:
        return None
    requested = max(2, int(reference_samples)) + max(2, int(calibration_samples))
    indices = _sample_indices(len(batch), requested, seed=seed)
    split = min(max(2, int(reference_samples)), max(2, len(indices) // 2))
    reference_indices = indices[:split]
    calibration_indices = indices[split:]
    if len(calibration_indices) < 2:
        return None
    reference = compact_afterstate_signatures(batch, reference_indices)
    calibration = compact_afterstate_signatures(batch, calibration_indices)
    combined = torch.cat([reference, calibration], dim=0)
    center = combined.mean(dim=0)
    scale = combined.std(dim=0, unbiased=False).clamp_min(0.05)
    standardized_reference = (reference - center) / scale
    standardized_calibration = (calibration - center) / scale
    calibration_distances = _minimum_distances(
        standardized_calibration,
        standardized_reference,
        device=device,
    )
    threshold = float(
        torch.quantile(
            calibration_distances,
            min(1.0, max(0.0, float(quantile))),
        ).item()
    )
    return OODSupportBank(
        reference_vectors=standardized_reference.cpu(),
        center=center.cpu(),
        scale=scale.cpu(),
        distance_threshold=threshold,
        reference_count=len(reference_indices),
        calibration_count=len(calibration_indices),
    )


def evaluate_ood_selected_actions(
    model: Any,
    batch: CompactMCBatch,
    support: OODSupportBank | None,
    *,
    device: Any,
    batch_size: int,
    seed: int,
    evaluation_samples: int = 4096,
) -> dict[str, Any]:
    torch = require_torch()
    empty = {
        "available": False,
        "ood_selection_rate": None,
        "ood_overestimation_excess": None,
        "evaluated_selection_count": 0,
        "ood_selection_count": 0,
        "in_support_selection_count": 0,
        "support_distance_threshold": None,
    }
    if support is None or model is None or not len(batch):
        return empty
    eligible = torch.nonzero(batch.value_policy_selected, as_tuple=False).flatten()
    if not len(eligible):
        return empty
    chosen = _sample_indices(len(eligible), int(evaluation_samples), seed=seed)
    indices = eligible.index_select(0, chosen)
    signatures = compact_afterstate_signatures(batch, indices)
    standardized = (signatures - support.center) / support.scale
    distances = _minimum_distances(
        standardized,
        support.reference_vectors,
        device=device,
    )
    ood_mask = distances > float(support.distance_threshold)

    predictions = []
    model.eval()
    with torch.no_grad():
        index_list = indices.tolist()
        for start in range(0, len(index_list), max(1, int(batch_size))):
            model_inputs, _targets = batch.model_batch(
                index_list[start : start + batch_size],
                device,
            )
            predictions.append(model(**model_inputs).detach().cpu())
    predicted = torch.cat(predictions) if predictions else torch.zeros((0,), dtype=torch.float32)
    targets = batch.targets.index_select(0, indices).to(torch.float32)
    errors = predicted - targets
    ood_count = int(ood_mask.sum().item())
    in_support_mask = ~ood_mask
    in_support_count = int(in_support_mask.sum().item())
    excess = None
    if ood_count and in_support_count:
        excess = float(errors[ood_mask].mean().item() - errors[in_support_mask].mean().item())
    return {
        "available": True,
        "ood_selection_rate": float(ood_mask.to(torch.float32).mean().item()),
        "ood_overestimation_excess": excess,
        "evaluated_selection_count": len(indices),
        "ood_selection_count": ood_count,
        "in_support_selection_count": in_support_count,
        "support_distance_threshold": float(support.distance_threshold),
    }
