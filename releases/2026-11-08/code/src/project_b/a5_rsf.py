"""Frozen scikit-survival RSF adapter for synthetic Task05 tests."""

from __future__ import annotations

import itertools
import math
from typing import Any, Mapping, Sequence


def rsf_grid(p: int) -> list[dict[str, Any]]:
    if p < 1:
        raise ValueError("p must be positive")
    mtry = (math.ceil(math.sqrt(p)), math.ceil(p / 3))
    rows = []
    for order, (features, fraction, leaf) in enumerate(
            itertools.product(mtry, (0.632, 0.80), (32, 96))):
        rows.append({"mtry": features, "sample_fraction": fraction,
                     "min_samples_leaf": leaf, "declared_order": order})
    return rows


def backend_parameters(candidate: Mapping[str, Any], *, n_train: int) -> dict[str, Any]:
    return {"class": "RandomSurvivalForest", "version": "0.28.0",
            "n_estimators": 1000, "bootstrap": True, "max_depth": None,
            "max_features": int(candidate["mtry"]),
            "max_samples": int(math.ceil(float(candidate["sample_fraction"]) * n_train)),
            "min_samples_leaf": int(candidate["min_samples_leaf"]), "n_jobs": 1}


def fit_rsf(matrix: Sequence[Sequence[float]], time: Sequence[float], event: Sequence[int],
            candidate: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    if not spec.get("synthetic_fixture", False):
        raise PermissionError("Task05 permits only synthetic RSF fitting")
    import numpy as np
    import sksurv
    from sksurv.ensemble import RandomSurvivalForest
    from .a5_common import backend_random_state, validate_surv180_arrays
    validate_surv180_arrays(time, event)
    if sksurv.__version__ != "0.28.0":
        raise RuntimeError("scikit-survival version drift")
    x = np.asarray(matrix, dtype=float)
    if x.ndim != 2 or not np.isfinite(x).all() or int(candidate["mtry"]) not in {math.ceil(math.sqrt(x.shape[1])), math.ceil(x.shape[1] / 3)} or candidate["sample_fraction"] not in (0.632, 0.80) or candidate["min_samples_leaf"] not in (32, 96):
        raise ValueError("RSF candidate grid drift")
    params = backend_parameters(candidate, n_train=len(time))
    seed = int(spec["random_state"])
    backend_seed = backend_random_state(seed)
    estimator = RandomSurvivalForest(
        n_estimators=params["n_estimators"], bootstrap=True, max_depth=None,
        max_features=params["max_features"], max_samples=params["max_samples"],
        min_samples_leaf=params["min_samples_leaf"], n_jobs=1,
        random_state=backend_seed,
    )
    y = np.array([(bool(e), float(t)) for e, t in zip(event, time)],
                 dtype=[("event", "?"), ("time", "<f8")])
    estimator.fit(x, y)
    minimum = min(int(tree.tree_.n_node_samples[index]) for tree in estimator.estimators_
                  for index in range(tree.tree_.node_count) if tree.tree_.children_left[index] == -1)
    if minimum < params["min_samples_leaf"]:
        raise ValueError("RSF terminal leaf violates frozen sample minimum")
    return {"model_id": "RSF", "backend": estimator, "parameters": params, "synthetic_only": True,
            "model_seed_64": seed, "backend_random_state": backend_seed, "observed_terminal_leaf_minimum": minimum}

