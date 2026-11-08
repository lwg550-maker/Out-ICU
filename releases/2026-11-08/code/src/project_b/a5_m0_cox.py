"""Unpenalized 180-day Efron/Breslow Cox contract for synthetic Task05 tests."""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping, Sequence


def fit_m0(matrix20: Sequence[Sequence[float]], time: Sequence[float], event: Sequence[int],
           spec: Mapping[str, Any]) -> dict[str, Any]:
    if not spec.get("synthetic_fixture", False):
        raise PermissionError("Task05 permits only synthetic M0 fitting")
    import numpy as np
    import pandas as pd
    from lifelines import CoxPHFitter
    from .a5_common import validate_surv180_arrays
    validate_surv180_arrays(time, event)
    x = np.asarray(matrix20, dtype=float)
    t = np.asarray(time, dtype=float)
    e = np.asarray(event)
    if x.ndim != 2 or x.shape[1] != 20 or len(x) != len(t) or e.shape != t.shape or not np.isfinite(x).all() or not np.isfinite(t).all() or np.any(t < 0) or np.any(t > 180) or not set(e.tolist()) <= {0, 1} or np.sum(e) < 1:
        raise ValueError("M0 requires the exact 20-column C2 design")
    columns = [f"c{index:02d}" for index in range(20)]
    frame = pd.DataFrame(x, columns=columns)
    frame["__time"] = t
    frame["__event"] = e.astype(int)
    fitter = CoxPHFitter(baseline_estimation_method="breslow")
    fitter.fit(
        frame,
        duration_col="__time",
        event_col="__event",
        show_progress=False,
        fit_options={"precision": 1e-14, "r_precision": 1e-15, "max_steps": 1000},
    )
    from .a5_en_cox import _efron_loglik_gradient
    beta = fitter.params_.to_numpy(dtype=float)
    variance = fitter.variance_matrix_.to_numpy(dtype=float)
    if not np.isfinite(beta).all() or not np.isfinite(variance).all():
        raise ValueError("M0_NUMERICAL_HOLD: nonfinite coefficient or information")
    try:
        information = np.linalg.inv(variance)
        eigen = np.linalg.eigvalsh((information + information.T) / 2)
    except np.linalg.LinAlgError as exc:
        raise ValueError("M0_NUMERICAL_HOLD: singular observed information") from exc
    _, gradient = _efron_loglik_gradient(beta, x, t, e)
    gradient_inf = float(np.max(np.abs(gradient)))
    event_n = int(np.sum(e))
    if not np.isfinite(information).all() or not np.isfinite(eigen).all() or eigen[0] <= 0 or not math.isfinite(gradient_inf) or gradient_inf >= 1e-4 or gradient_inf / event_n >= 2e-7:
        raise ValueError("M0_NUMERICAL_HOLD: information or gradient acceptance failure")
    model = {"model_id": "M0", "backend": fitter, "columns": columns,
             "ties": "efron", "baseline": "breslow", "fit_call_count": 1, "synthetic_only": True,
             "acceptance_diagnostics": {"event_n": event_n, "gradient_inf": gradient_inf,
                                        "gradient_inf_per_event": gradient_inf / event_n,
                                        "information_min_eigenvalue": float(eigen[0])}}
    predict_native_risk(model, x)
    return model


def alias_m1_en(m0_model: Mapping[str, Any]) -> dict[str, Any]:
    return {"model_id": "M1_EN", "object": m0_model,
            "provenance": "SYMBOLIC_ALIAS_OF_M0",
            "additional_fit_call_count": 0, "max_identity_difference": 0.0}


def predict_native_risk(model: Mapping[str, Any], matrix: Sequence[Sequence[float]],
                        horizons: Sequence[int] = (90, 180)) -> list[list[float]]:
    import numpy as np
    import pandas as pd
    from .a5_calibration import validate_native_risk
    if model.get("model_id") == "M1_EN":
        model = model["object"]
    horizons = np.asarray(horizons, dtype=float)
    if horizons.shape != (2,) or not np.isfinite(horizons).all() or horizons[0] < 0 or horizons[0] >= horizons[1]:
        raise ValueError("native horizons must be two finite increasing nonnegative values")
    x = np.asarray(matrix, dtype=float)
    if x.ndim != 2 or not np.isfinite(x).all():
        raise ValueError("native prediction matrix is invalid")
    fitter = model["backend"]
    if model.get("model_id") == "RSF":
        functions = fitter.predict_cumulative_hazard_function(x)
        hazards = np.zeros((len(x), 2), dtype=float)
        for i, fn in enumerate(functions):
            indices = np.searchsorted(fn.x, horizons, side="right") - 1
            for j, idx in enumerate(indices):
                hazards[i, j] = 0.0 if idx < 0 else float(fn.y[idx])
    elif model.get("model_id") == "GBST":
        times = np.asarray(model["baseline_event_times"], dtype=float)
        cumulative = np.asarray(model["baseline_cumulative_hazard"], dtype=float)
        idx = np.searchsorted(times, horizons, side="right") - 1
        baseline = np.asarray([0.0 if i < 0 else cumulative[i] for i in idx])
        hazards = np.exp(fitter.predict(x))[:, None] * baseline[None, :]
    else:
        frame = pd.DataFrame(x, columns=model["columns"])
        partial = fitter.predict_partial_hazard(frame).to_numpy(dtype=float)
        baseline_frame = fitter.baseline_cumulative_hazard_
        times = baseline_frame.index.to_numpy(dtype=float)
        cumulative = baseline_frame.iloc[:, 0].to_numpy(dtype=float)
        idx = np.searchsorted(times, horizons, side="right") - 1
        baseline = np.asarray([0.0 if i < 0 else cumulative[i] for i in idx])
        hazards = partial[:, None] * baseline[None, :]
    risks = (1.0 - np.exp(-hazards)).tolist()
    validate_native_risk(risks)
    return [[float(value) for value in row] for row in risks]

