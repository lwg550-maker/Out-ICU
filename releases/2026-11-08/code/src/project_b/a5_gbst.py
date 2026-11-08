"""Frozen scikit-survival GBST adapter for synthetic Task05 tests."""

from __future__ import annotations

import itertools
from typing import Any, Mapping, Sequence


def gbst_grid() -> list[dict[str, Any]]:
    rows = []
    for order, (depth, learning_rate, estimators) in enumerate(
            itertools.product((1, 2, 3), (0.03, 0.07), (250, 500))):
        rows.append({"max_depth": depth, "learning_rate": learning_rate,
                     "n_estimators": estimators, "subsample": 0.80,
                     "max_features": 1.0, "min_samples_leaf": 64,
                     "loss": "coxph", "early_stopping": False,
                     "declared_order": order})
    return rows


def fit_gbst(matrix: Sequence[Sequence[float]], time: Sequence[float], event: Sequence[int],
             candidate: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    if not spec.get("synthetic_fixture", False):
        raise PermissionError("Task05 permits only synthetic GBST fitting")
    import numpy as np
    import sksurv
    from sksurv.ensemble import GradientBoostingSurvivalAnalysis
    from .a5_common import backend_random_state, validate_surv180_arrays
    validate_surv180_arrays(time, event)
    if sksurv.__version__ != "0.28.0":
        raise RuntimeError("scikit-survival version drift")
    if candidate.get("max_depth") not in (1, 2, 3) or candidate.get("learning_rate") not in (0.03, 0.07) or candidate.get("n_estimators") not in (250, 500) or candidate.get("subsample", 0.8) != 0.8 or candidate.get("max_features", 1.0) != 1.0 or candidate.get("min_samples_leaf", 64) != 64 or candidate.get("loss", "coxph") != "coxph" or candidate.get("early_stopping", False) is not False:
        raise ValueError("GBST candidate grid drift")
    seed = int(spec["random_state"])
    backend_seed = backend_random_state(seed)
    estimator = GradientBoostingSurvivalAnalysis(
        loss="coxph", n_estimators=int(candidate["n_estimators"]),
        learning_rate=float(candidate["learning_rate"]),
        subsample=0.80, max_depth=int(candidate["max_depth"]),
        min_samples_leaf=64, random_state=backend_seed,
    )
    y = np.array([(bool(e), float(t)) for e, t in zip(event, time)],
                 dtype=[("event", "?"), ("time", "<f8")])
    x = np.asarray(matrix, dtype=float)
    estimator.fit(x, y)
    scores = estimator.predict(x)
    times = np.asarray(time, dtype=float)
    events = np.asarray(event, dtype=bool)
    event_times = np.unique(times[events])
    if len(event_times) == 0 or not np.isfinite(scores).all():
        raise ValueError("GBST Breslow baseline is not estimable")
    weights = np.exp(scores)
    if not np.isfinite(weights).all() or np.any(weights <= 0):
        raise ValueError("GBST risk scores cannot define a finite baseline")
    increments = [np.sum(events & (times == t)) / np.sum(weights[times >= t]) for t in event_times]
    baseline = np.cumsum(increments)
    if not np.isfinite(baseline).all():
        raise ValueError("GBST Breslow baseline is nonfinite")
    return {"model_id": "GBST", "backend": estimator, "parameters": dict(candidate),
            "synthetic_only": True, "model_seed_64": seed, "backend_random_state": backend_seed,
            "baseline_event_times": event_times.tolist(), "baseline_cumulative_hazard": baseline.tolist()}

