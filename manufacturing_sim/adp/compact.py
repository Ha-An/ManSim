from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from .model import require_torch
from .schema import GLOBAL_FEATURE_DIM, PAIR_FEATURE_DIM, TASK_FEATURE_DIM, WORKER_FEATURE_DIM


@dataclass
class CompactMCBatch:
    """CPU tensor-only Monte Carlo samples with no simulation object references."""

    global_features: Any
    worker_features: Any
    task_features: Any
    pair_features: Any
    worker_mask: Any
    task_mask: Any
    feasibility: Any
    selected_assignment_mask: Any
    value_policy_selected: Any
    targets: Any
    episode_ids: Any
    worker_counts: Any
    td_data: Any = None

    def __len__(self) -> int:
        return int(self.targets.shape[0])

    @property
    def episode_count(self) -> int:
        if not len(self):
            return 0
        return int(self.episode_ids.unique().numel())

    @property
    def memory_bytes(self) -> int:
        tensors = (
            self.global_features,
            self.worker_features,
            self.task_features,
            self.pair_features,
            self.worker_mask,
            self.task_mask,
            self.feasibility,
            self.selected_assignment_mask,
            self.value_policy_selected,
            self.targets,
            self.episode_ids,
            self.worker_counts,
        )
        return int(sum(tensor.numel() * tensor.element_size() for tensor in tensors)) + (
            self.td_data.memory_bytes if self.td_data is not None else 0
        )

    def model_batch(self, indices: list[int], device: Any) -> tuple[dict[str, Any], Any]:
        torch = require_torch()
        index = torch.as_tensor(indices, dtype=torch.long)
        inputs = {
            "global_features": self.global_features.index_select(0, index).to(device),
            "worker_features": self.worker_features.index_select(0, index).to(device),
            "task_features": self.task_features.index_select(0, index).to(device),
            "pair_features": self.pair_features.index_select(0, index).to(device),
            "worker_mask": self.worker_mask.index_select(0, index).to(device),
            "task_mask": self.task_mask.index_select(0, index).to(device),
            "feasibility": self.feasibility.index_select(0, index).to(device),
            "selected_assignment_mask": self.selected_assignment_mask.index_select(0, index).to(device),
        }
        return inputs, self.targets.index_select(0, index).to(device)


@dataclass
class CompactPairwiseMCBatch:
    """Aligned same-state action pairs and their complete Monte Carlo returns."""

    selected: CompactMCBatch
    alternative: CompactMCBatch

    def __post_init__(self) -> None:
        if len(self.selected) != len(self.alternative):
            raise ValueError("Pairwise MC batches must contain aligned sample counts.")

    def __len__(self) -> int:
        return len(self.selected)

    @property
    def episode_count(self) -> int:
        if not len(self):
            return 0
        return int(self.selected.episode_ids.unique().numel())

    @property
    def memory_bytes(self) -> int:
        return self.selected.memory_bytes + self.alternative.memory_bytes

    def model_batch(
        self,
        indices: list[int],
        device: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], Any]:
        selected_inputs, selected_targets = self.selected.model_batch(indices, device)
        alternative_inputs, alternative_targets = self.alternative.model_batch(indices, device)
        return selected_inputs, alternative_inputs, selected_targets - alternative_targets


def select_compact_samples(batch: CompactMCBatch, indices: list[int]) -> CompactMCBatch:
    """Select compact samples without retaining simulation-side object references."""

    torch = require_torch()
    index = torch.as_tensor(indices, dtype=torch.long)
    return CompactMCBatch(
        **{
            field.name: getattr(batch, field.name).index_select(0, index)
            for field in fields(CompactMCBatch)
            if field.name != "td_data"
        },
        td_data=batch.td_data.select(indices) if batch.td_data is not None else None,
    )


def _mc_targets(transitions: list[dict[str, Any]], gamma: float) -> list[float]:
    target = 0.0
    values: list[float] = []
    for transition in reversed(transitions):
        target = float(transition["raw_reward"]) + float(gamma) * target
        values.append(target)
    values.reverse()
    return values


def compact_episode_transitions(
    transitions: list[dict[str, Any]],
    *,
    episode_id: int,
    worker_count: int,
    gamma: float = 1.0,
) -> CompactMCBatch:
    torch = require_torch()
    states = [transition["post_state"] for transition in transitions]
    sample_count = len(states)
    max_workers = max(1, max((state.worker_features.shape[0] for state in states), default=0))
    max_tasks = max(1, max((state.task_features.shape[0] for state in states), default=0))
    global_features = torch.zeros((sample_count, GLOBAL_FEATURE_DIM), dtype=torch.float32)
    workers = torch.zeros((sample_count, max_workers, WORKER_FEATURE_DIM), dtype=torch.float32)
    tasks = torch.zeros((sample_count, max_tasks, TASK_FEATURE_DIM), dtype=torch.float32)
    pairs = torch.zeros((sample_count, max_workers, max_tasks, PAIR_FEATURE_DIM), dtype=torch.float32)
    worker_mask = torch.zeros((sample_count, max_workers), dtype=torch.bool)
    task_mask = torch.zeros((sample_count, max_tasks), dtype=torch.bool)
    feasibility = torch.zeros((sample_count, max_workers, max_tasks), dtype=torch.bool)
    selected_assignment_mask = torch.zeros((sample_count, max_workers, max_tasks), dtype=torch.bool)
    for index, state in enumerate(states):
        state_workers = int(state.worker_features.shape[0])
        state_tasks = int(state.task_features.shape[0])
        global_features[index] = torch.as_tensor(state.global_features, dtype=torch.float32)
        if state_workers:
            workers[index, :state_workers] = torch.as_tensor(state.worker_features, dtype=torch.float32)
            worker_mask[index, :state_workers] = True
        if state_tasks:
            tasks[index, :state_tasks] = torch.as_tensor(state.task_features, dtype=torch.float32)
            pairs[index, :state_workers, :state_tasks] = torch.as_tensor(state.pair_features, dtype=torch.float32)
            feasibility[index, :state_workers, :state_tasks] = torch.as_tensor(state.feasibility, dtype=torch.bool)
            selected_assignment_mask[index, :state_workers, :state_tasks] = torch.as_tensor(
                state.selected_assignment_mask, dtype=torch.bool
            )
            task_mask[index, :state_tasks] = True
        else:
            task_mask[index, 0] = True
    return CompactMCBatch(
        global_features=global_features,
        worker_features=workers,
        task_features=tasks,
        pair_features=pairs,
        worker_mask=worker_mask,
        task_mask=task_mask,
        feasibility=feasibility,
        selected_assignment_mask=selected_assignment_mask,
        value_policy_selected=torch.as_tensor(
            [bool(transition.get("value_policy_selected", False)) for transition in transitions],
            dtype=torch.bool,
        ),
        targets=torch.as_tensor(_mc_targets(transitions, gamma), dtype=torch.float32),
        episode_ids=torch.full((sample_count,), int(episode_id), dtype=torch.int32),
        worker_counts=torch.full((sample_count,), int(worker_count), dtype=torch.int16),
    )


def merge_compact_batches(episodes: list[CompactMCBatch]) -> CompactMCBatch:
    torch = require_torch()
    if not episodes:
        return CompactMCBatch(
            global_features=torch.zeros((0, GLOBAL_FEATURE_DIM), dtype=torch.float32),
            worker_features=torch.zeros((0, 1, WORKER_FEATURE_DIM), dtype=torch.float32),
            task_features=torch.zeros((0, 1, TASK_FEATURE_DIM), dtype=torch.float32),
            pair_features=torch.zeros((0, 1, 1, PAIR_FEATURE_DIM), dtype=torch.float32),
            worker_mask=torch.zeros((0, 1), dtype=torch.bool),
            task_mask=torch.zeros((0, 1), dtype=torch.bool),
            feasibility=torch.zeros((0, 1, 1), dtype=torch.bool),
            selected_assignment_mask=torch.zeros((0, 1, 1), dtype=torch.bool),
            value_policy_selected=torch.zeros((0,), dtype=torch.bool),
            targets=torch.zeros((0,), dtype=torch.float32),
            episode_ids=torch.zeros((0,), dtype=torch.int32),
            worker_counts=torch.zeros((0,), dtype=torch.int16),
        )
    sample_count = sum(len(episode) for episode in episodes)
    max_workers = max(int(episode.worker_features.shape[1]) for episode in episodes)
    max_tasks = max(int(episode.task_features.shape[1]) for episode in episodes)
    merged = CompactMCBatch(
        global_features=torch.zeros((sample_count, GLOBAL_FEATURE_DIM), dtype=torch.float32),
        worker_features=torch.zeros((sample_count, max_workers, WORKER_FEATURE_DIM), dtype=torch.float32),
        task_features=torch.zeros((sample_count, max_tasks, TASK_FEATURE_DIM), dtype=torch.float32),
        pair_features=torch.zeros((sample_count, max_workers, max_tasks, PAIR_FEATURE_DIM), dtype=torch.float32),
        worker_mask=torch.zeros((sample_count, max_workers), dtype=torch.bool),
        task_mask=torch.zeros((sample_count, max_tasks), dtype=torch.bool),
        feasibility=torch.zeros((sample_count, max_workers, max_tasks), dtype=torch.bool),
        selected_assignment_mask=torch.zeros((sample_count, max_workers, max_tasks), dtype=torch.bool),
        value_policy_selected=torch.zeros((sample_count,), dtype=torch.bool),
        targets=torch.zeros((sample_count,), dtype=torch.float32),
        episode_ids=torch.zeros((sample_count,), dtype=torch.int32),
        worker_counts=torch.zeros((sample_count,), dtype=torch.int16),
    )
    offset = 0
    for episode in episodes:
        count = len(episode)
        end = offset + count
        worker_width = int(episode.worker_features.shape[1])
        task_width = int(episode.task_features.shape[1])
        merged.global_features[offset:end] = episode.global_features
        merged.worker_features[offset:end, :worker_width] = episode.worker_features
        merged.task_features[offset:end, :task_width] = episode.task_features
        merged.pair_features[offset:end, :worker_width, :task_width] = episode.pair_features
        merged.worker_mask[offset:end, :worker_width] = episode.worker_mask
        merged.task_mask[offset:end, :task_width] = episode.task_mask
        merged.feasibility[offset:end, :worker_width, :task_width] = episode.feasibility
        merged.selected_assignment_mask[offset:end, :worker_width, :task_width] = episode.selected_assignment_mask
        merged.value_policy_selected[offset:end] = episode.value_policy_selected
        merged.targets[offset:end] = episode.targets
        merged.episode_ids[offset:end] = episode.episode_ids
        merged.worker_counts[offset:end] = episode.worker_counts
        offset = end
    td_episodes = [episode.td_data for episode in episodes if len(episode)]
    if any(data is not None for data in td_episodes):
        if any(data is None for data in td_episodes):
            raise ValueError("Cannot merge MC-only and TD replay samples.")
        from .td import CompactTDData

        merged.td_data = CompactTDData.merge(td_episodes)
    return merged


def merge_pairwise_mc_batches(
    batches: list[CompactPairwiseMCBatch],
) -> CompactPairwiseMCBatch:
    return CompactPairwiseMCBatch(
        selected=merge_compact_batches([batch.selected for batch in batches]),
        alternative=merge_compact_batches([batch.alternative for batch in batches]),
    )


def stratified_episode_split(
    batch: CompactMCBatch,
    *,
    validation_fraction: float,
    rng: Any,
) -> tuple[list[int], list[int]]:
    episode_workers: dict[int, int] = {}
    for episode_id, worker_count in zip(batch.episode_ids.tolist(), batch.worker_counts.tolist()):
        episode_workers.setdefault(int(episode_id), int(worker_count))
    by_worker: dict[int, list[int]] = {}
    for episode_id, worker_count in episode_workers.items():
        by_worker.setdefault(worker_count, []).append(episode_id)
    validation_episodes: set[int] = set()
    for episode_ids in by_worker.values():
        ordered = sorted(episode_ids)
        rng.shuffle(ordered)
        count = 0 if len(ordered) <= 1 else max(1, round(len(ordered) * float(validation_fraction)))
        count = min(len(ordered) - 1, count)
        validation_episodes.update(ordered[:count])
    train_indices: list[int] = []
    validation_indices: list[int] = []
    for index, episode_id in enumerate(batch.episode_ids.tolist()):
        (validation_indices if int(episode_id) in validation_episodes else train_indices).append(index)
    return train_indices, validation_indices
