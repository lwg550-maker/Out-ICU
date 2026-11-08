"""Task05 probability and supportive calibration contracts."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


def validate_native_risk(risks: Sequence[Sequence[float]]) -> dict[str, Any]:
    for index, row in enumerate(risks):
        if len(row) != 2:
            raise ValueError("native risks require 90/180 columns")
        p90, p180 = map(float, row)
        if not (math.isfinite(p90) and math.isfinite(p180) and 0 <= p90 <= p180 <= 1):
            raise ValueError(f"NATIVE_MONOTONICITY_FAILURE at row {index}")
    return {"status": "PASS", "row_n": len(risks), "clipped": False}


def clip_probability_copy(values: Sequence[float], *, purpose: str) -> list[float]:
    if purpose not in {"logloss", "logit"}:
        raise PermissionError("probability clipping is limited to logloss/logit copies")
    return [min(1 - 1e-6, max(1e-6, float(value))) for value in values]


def _fit_unpenalized_logistic(design, outcomes, *, offset=None):
    """Fit the specified unpenalized MLE only when it is finite and identifiable."""
    import numpy as np
    import warnings
    from scipy.optimize import linprog, minimize, OptimizeWarning
    from scipy.special import expit
    x = np.asarray(design, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if x.ndim != 2 or y.ndim != 1 or len(x) != len(y) or set(y.tolist()) != {0.0, 1.0} or not np.isfinite(x).all():
        raise ValueError("LOGISTIC_NOT_ESTIMABLE: invalid design or outcomes")
    norms = np.linalg.norm(x, axis=0)
    if np.any(norms == 0):
        raise ValueError("LOGISTIC_NOT_ESTIMABLE: degenerate column")
    z = x / norms
    if np.linalg.matrix_rank(z) != z.shape[1]:
        raise ValueError("LOGISTIC_NOT_ESTIMABLE: rank deficient design")
    signed = (2 * y - 1)[:, None] * z
    # A nonzero nonnegative signed margin is the LP condition for complete or
    # quasi separation. Bounded coefficients merely normalize its direction.
    # SciPy forwards this HiGHS option while warning that it is not exposed
    # in SciPy's own option list; suppress only that forwarding warning.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Unrecognized options detected:.*", category=OptimizeWarning)
        lp = linprog(-signed.sum(axis=0), A_ub=-signed, b_ub=np.zeros(len(y)),
                     bounds=[(-1.0, 1.0)] * z.shape[1], method="highs", options={"threads": 1})
    if not lp.success:
        raise RuntimeError("LOGISTIC_NOT_ESTIMABLE: separation check failed")
    if float(-lp.fun) > 1e-8:
        raise ValueError("LOGISTIC_NOT_ESTIMABLE: complete or quasi separation")
    fixed = np.zeros(len(y)) if offset is None else np.asarray(offset, dtype=float)
    if fixed.shape != y.shape or not np.isfinite(fixed).all():
        raise ValueError("LOGISTIC_NOT_ESTIMABLE: invalid offset")
    def objective(theta):
        eta = fixed + z @ theta
        return float(np.sum(np.logaddexp(0.0, eta) - y * eta))
    def gradient(theta):
        return z.T @ (expit(fixed + z @ theta) - y)
    result = minimize(objective, np.zeros(z.shape[1]), jac=gradient, method="BFGS",
                      options={"gtol": 1e-9, "maxiter": 1000})
    if not np.isfinite(result.x).all() or np.max(np.abs(gradient(result.x))) > 1e-6:
        raise RuntimeError("LOGISTIC_NOT_ESTIMABLE: optimization failed")
    p = expit(fixed + z @ result.x)
    info = z.T @ ((p * (1 - p))[:, None] * z)
    eigen = np.linalg.eigvalsh(info)
    if not np.isfinite(eigen).all() or eigen[0] <= 0 or eigen[-1] / eigen[0] > 1e12:
        raise ValueError("LOGISTIC_NOT_ESTIMABLE: singular or ill-conditioned information")
    return result.x / norms


def fit_logistic_recalibrator(inner_oof_risk: Sequence[float], outcomes: Sequence[int],
                              horizon: int, spec: Mapping[str, Any]) -> dict[str, Any]:
    if spec.get("source_role") != "inner_oof":
        raise PermissionError("recalibration may fit only inner OOF risks")
    import numpy as np
    p = np.asarray(clip_probability_copy(inner_oof_risk, purpose="logit"), dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if len(p) != len(y) or set(y.tolist()) != {0.0, 1.0}:
        raise ValueError("recalibration requires both classes")
    x = np.log(p / (1 - p))
    try:
        theta = _fit_unpenalized_logistic(np.column_stack([np.ones(len(x)), x]), y)
    except (ValueError, RuntimeError) as exc:
        raise ValueError(f"RECALIBRATION_STREAM_HOLD: {exc}") from exc
    return {"intercept": float(theta[0]), "slope": float(theta[1]),
            "horizon": int(horizon), "source_role": "inner_oof", "fit_call_count": 1}


def apply_recalibrator(model: Mapping[str, Any], risks: Sequence[float]) -> list[float]:
    output = []
    for value in clip_probability_copy(risks, purpose="logit"):
        logit = math.log(value / (1 - value))
        eta = float(model["intercept"]) + float(model["slope"]) * logit
        output.append(1 / (1 + math.exp(-eta)))
    return output


def report_recalibrated_monotonicity(risks: Sequence[Sequence[float]]) -> dict[str, Any]:
    violations = [index for index, row in enumerate(risks) if float(row[0]) > float(row[1])]
    return {"violating_row_indices": violations,
            "violation_rate": len(violations) / len(risks) if risks else 0.0,
            "unmodified_risks": [list(row) for row in risks], "projection_applied": False}


def calibration_curve_design(risks: Sequence[float], *, spline_df: int) -> dict[str, Any]:
    import numpy as np
    logits = np.log(np.asarray(clip_probability_copy(risks, purpose="logit")) /
                    (1 - np.asarray(clip_probability_copy(risks, purpose="logit"))))
    percentiles = [5, 35, 65, 95]
    return {"knot_percentiles": percentiles,
            "knots": [float(x) for x in np.percentile(logits, percentiles)],
            "spline_df": int(spline_df), "resample_specific": True}


def fit_evaluation_regression_triplet(risks: Sequence[float], outcomes: Sequence[int],
                                      spec: Mapping[str, Any]) -> dict[str, Any]:
    """Fit the frozen calibration intercept, slope and 3-df RCS curve on synthetic data."""
    if spec.get("synthetic_fixture") is not True:
        raise PermissionError("Task05 evaluation regressions are synthetic-only")
    import numpy as np
    p = np.asarray(clip_probability_copy(risks, purpose="logit"), dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if len(p) != len(y) or set(y.tolist()) != {0.0, 1.0}:
        raise ValueError("evaluation regression triplet requires both classes")
    x = np.log(p / (1.0 - p))

    def fit(design, offset=None):
        return [float(value) for value in _fit_unpenalized_logistic(design, y, offset=offset)]

    intercept = fit(np.ones((len(x), 1)), offset=x)
    slope = fit(np.column_stack([np.ones(len(x)), x]))
    knots = np.percentile(x, [5, 35, 65, 95]).astype(float)
    k_last_but_one, k_last = knots[-2], knots[-1]
    if k_last <= k_last_but_one:
        raise ValueError("calibration curve knots are not distinct")
    nonlinear = []
    for knot in knots[:-2]:
        term = (np.maximum(x - knot, 0.0) ** 3
                - np.maximum(x - k_last_but_one, 0.0) ** 3 *
                (k_last - knot) / (k_last - k_last_but_one)
                + np.maximum(x - k_last, 0.0) ** 3 *
                (k_last_but_one - knot) / (k_last - k_last_but_one))
        nonlinear.append(term)
    curve_scales = [max(float(np.std(term, ddof=0)), 1.0) for term in nonlinear]
    nonlinear = [term / scale for term, scale in zip(nonlinear, curve_scales)]
    curve = fit(np.column_stack([np.ones(len(x)), x, *nonlinear]))
    return {"status": "PASS", "regressions": {"intercept": intercept,
                                                   "slope": slope,
                                                   "curve": curve},
            "curve_knots": [float(value) for value in knots],
            "curve_term_scales": curve_scales, "spline_df": 3}

