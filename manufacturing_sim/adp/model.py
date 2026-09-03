from __future__ import annotations

import importlib
from typing import Any

import numpy as np

from .schema import GLOBAL_FEATURE_DIM, PAIR_FEATURE_DIM, TASK_FEATURE_DIM, WORKER_FEATURE_DIM, EncodedDecisionState


def require_torch() -> Any:
    try:
        return importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "simulation_based_adp requires PyTorch. Install optional dependencies with "
            ".\\.venv\\Scripts\\python.exe -m pip install -r requirements-adp.txt"
        ) from exc


def build_value_network(*, embedding_dim: int = 128, heads: int = 4, layers: int = 3) -> Any:
    torch = require_torch()
    nn = torch.nn

    class BipartiteAfterstateValueNetwork(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.global_embed = nn.Sequential(nn.Linear(GLOBAL_FEATURE_DIM, embedding_dim), nn.ReLU())
            self.worker_embed = nn.Sequential(nn.Linear(WORKER_FEATURE_DIM, embedding_dim), nn.ReLU())
            self.task_embed = nn.Sequential(nn.Linear(TASK_FEATURE_DIM, embedding_dim), nn.ReLU())
            self.pair_embed = nn.Sequential(nn.Linear(PAIR_FEATURE_DIM, embedding_dim), nn.ReLU())
            self.worker_to_task = nn.ModuleList(
                [nn.MultiheadAttention(embedding_dim, heads, batch_first=True) for _ in range(layers)]
            )
            self.task_to_worker = nn.ModuleList(
                [nn.MultiheadAttention(embedding_dim, heads, batch_first=True) for _ in range(layers)]
            )
            self.worker_norm = nn.ModuleList([nn.LayerNorm(embedding_dim) for _ in range(layers)])
            self.task_norm = nn.ModuleList([nn.LayerNorm(embedding_dim) for _ in range(layers)])
            self.value_head = nn.Sequential(
                nn.Linear(embedding_dim * 4, embedding_dim),
                nn.ReLU(),
                nn.Linear(embedding_dim, 1),
            )

        @staticmethod
        def _masked_mean(values: Any, mask: Any) -> Any:
            weights = mask.unsqueeze(-1).to(values.dtype)
            return (values * weights).sum(dim=1) / weights.sum(dim=1).clamp_min(1.0)

        def forward(
            self,
            global_features: Any,
            worker_features: Any,
            task_features: Any,
            pair_features: Any,
            worker_mask: Any,
            task_mask: Any,
            feasibility: Any,
            selected_assignment_mask: Any,
        ) -> Any:
            g = self.global_embed(global_features)
            w = self.worker_embed(worker_features)
            t = self.task_embed(task_features)
            pair = self.pair_embed(pair_features)
            pair_mask = feasibility.unsqueeze(-1).to(pair.dtype)
            w = w + (pair * pair_mask).sum(dim=2) / pair_mask.sum(dim=2).clamp_min(1.0)
            t = t + (pair * pair_mask).sum(dim=1) / pair_mask.sum(dim=1).clamp_min(1.0)
            selected_mask = selected_assignment_mask.unsqueeze(-1).to(pair.dtype)
            w = w + (pair * selected_mask).sum(dim=2)
            t = t + (pair * selected_mask).sum(dim=1)
            for w_attn, t_attn, w_norm, t_norm in zip(
                self.worker_to_task, self.task_to_worker, self.worker_norm, self.task_norm
            ):
                w_delta, _ = w_attn(w, t, t, key_padding_mask=~task_mask)
                t_delta, _ = t_attn(t, w, w, key_padding_mask=~worker_mask)
                w = w_norm(w + w_delta)
                t = t_norm(t + t_delta)
            pooled_w = self._masked_mean(w, worker_mask)
            pooled_t = self._masked_mean(t, task_mask)
            interaction = pooled_w * pooled_t
            return self.value_head(torch.cat([g, pooled_w, pooled_t, interaction], dim=-1)).squeeze(-1)

    return BipartiteAfterstateValueNetwork()


def collate_states(states: list[EncodedDecisionState], device: Any) -> dict[str, Any]:
    torch = require_torch()
    batch = len(states)
    max_workers = max(1, max(len(state.worker_ids) for state in states))
    max_tasks = max(1, max(len(state.opportunity_ids) for state in states))
    global_features = torch.zeros((batch, GLOBAL_FEATURE_DIM), dtype=torch.float32, device=device)
    workers = torch.zeros((batch, max_workers, WORKER_FEATURE_DIM), dtype=torch.float32, device=device)
    tasks = torch.zeros((batch, max_tasks, TASK_FEATURE_DIM), dtype=torch.float32, device=device)
    pairs = torch.zeros((batch, max_workers, max_tasks, PAIR_FEATURE_DIM), dtype=torch.float32, device=device)
    worker_mask = torch.zeros((batch, max_workers), dtype=torch.bool, device=device)
    task_mask = torch.zeros((batch, max_tasks), dtype=torch.bool, device=device)
    feasibility = torch.zeros((batch, max_workers, max_tasks), dtype=torch.bool, device=device)
    selected_assignment_mask = torch.zeros((batch, max_workers, max_tasks), dtype=torch.bool, device=device)
    for index, state in enumerate(states):
        worker_count = len(state.worker_ids)
        task_count = len(state.opportunity_ids)
        global_features[index] = torch.as_tensor(state.global_features, dtype=torch.float32, device=device)
        if worker_count:
            workers[index, :worker_count] = torch.as_tensor(state.worker_features, dtype=torch.float32, device=device)
            worker_mask[index, :worker_count] = True
        if task_count:
            tasks[index, :task_count] = torch.as_tensor(state.task_features, dtype=torch.float32, device=device)
            task_mask[index, :task_count] = True
            pairs[index, :worker_count, :task_count] = torch.as_tensor(state.pair_features, dtype=torch.float32, device=device)
            feasibility[index, :worker_count, :task_count] = torch.as_tensor(state.feasibility, dtype=torch.bool, device=device)
            selected_assignment_mask[index, :worker_count, :task_count] = torch.as_tensor(
                state.selected_assignment_mask, dtype=torch.bool, device=device
            )
        else:
            # A zero-valued sentinel keeps attention numerically defined when
            # no task is currently feasible for any idle worker.
            task_mask[index, 0] = True
    return {
        "global_features": global_features,
        "worker_features": workers,
        "task_features": tasks,
        "pair_features": pairs,
        "worker_mask": worker_mask,
        "task_mask": task_mask,
        "feasibility": feasibility,
        "selected_assignment_mask": selected_assignment_mask,
    }


def predict_values(model: Any, states: list[EncodedDecisionState], device: Any) -> np.ndarray:
    torch = require_torch()
    if not states:
        return np.asarray([], dtype=np.float32)
    model.eval()
    with torch.no_grad():
        batch = collate_states(states, device)
        return model(**batch).detach().cpu().numpy()
