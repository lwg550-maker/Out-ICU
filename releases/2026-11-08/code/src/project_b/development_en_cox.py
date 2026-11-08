"""Task05 elastic-net Cox contracts for synthetic conformance only."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any, Iterable, Mapping, Sequence


ALPHAS = (0.10, 0.50, 0.90)
LAMBDA_RATIOS = (1.0, 0.25, 0.0625, 0.015625, 0.00390625)
STRICT_LIMITS = {
    "kkt_inf": 1e-5,
    "objective_abs_diff": 1e-7,
    "coefficient_max_abs_diff": 1e-4,
    "risk_max_abs_diff": 1e-5,
}


def penalty_mapping(n: int, event_n: int, alpha: float, scientific_lambda: float,
                    *, core_n: int, active_extension_n: int) -> dict[str, Any]:
    if n <= 1 or not 0 < event_n <= n or not 0 <= alpha <= 1 or scientific_lambda < 0:
        raise ValueError("invalid D3 penalty mapping inputs")
    s = math.sqrt(n / (n - 1))
    r = (1 - alpha) / (s * s) + alpha / s
    q = (event_n / n) * scientific_lambda * r
    a_backend = (alpha / s) / r
    factors = [0] * core_n + [1] * active_extension_n
    return {
        "n": n, "E": event_n, "a": alpha, "lambda": scientific_lambda,
        "s": s, "r": r, "q": q, "a_backend": a_backend,
        "penalty_factors": factors,
        "backend_penalizer": [q * value for value in factors],
        "backend_penalizer_rule": "q*penalty_factors",
        "internal_center_recorded": True,
        "internal_scale_recorded": True,
        "coefficient_conversion_path": "lifelines_public_params_already_unscaled_no_second_conversion",
        "baseline_conversion_path": "centered_linear_predictor_and_centered_breslow_baseline",
    }


def en_grid() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    order = 0
    for alpha in ALPHAS:
        for ratio in LAMBDA_RATIOS:
            rows.append({"alpha": alpha, "lambda_ratio": ratio,
                         "candidate_identity": (alpha, ratio), "declared_order": order})
            order += 1
    return rows


def select_en_candidate(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    candidates = [dict(row) for row in rows]
    if not candidates:
        raise ValueError("no EN candidates")
    best_score = min(float(row["brier"]) for row in candidates)
    tied = [row for row in candidates if float(row["brier"]) <= best_score + 1e-8]
    def key(row: Mapping[str, Any]) -> tuple[float, float, float, int]:
        median_nonzero = statistics.median(float(x) for x in row["nonzero_counts"])
        return (median_nonzero, -float(row["lambda_ratio"]), -float(row["alpha"]),
                int(row.get("declared_order", 0)))
    selected = min(tied, key=key)
    selected["candidate_identity"] = (float(selected["alpha"]), float(selected["lambda_ratio"]))
    return selected


def strict_reference_status(metrics: Mapping[str, float]) -> str:
    for key, limit in STRICT_LIMITS.items():
        value = float(metrics.get(key, math.inf))
        if not math.isfinite(value) or value > limit:
            return "M2_EN_BACKEND_HOLD"
    return "PASS"


def fit_m2_en(matrix177: Sequence[Sequence[float]], time: Sequence[float], event: Sequence[int],
              penalty_factors: Sequence[int], candidate: Mapping[str, float],
              spec: Mapping[str, Any]) -> dict[str, Any]:
    """Fit the approved lifelines adapter on an explicitly synthetic matrix."""
    from .development_authorization import validate_development_authorization
    if spec.get("synthetic_fixture") is not None:
        raise PermissionError("synthetic_fixture flag is not valid for development")
    validate_development_authorization(spec["project_root"])
    import numpy as np
    import pandas as pd
    from lifelines import CoxPHFitter
    from .a5_common import validate_surv180_arrays
    validate_surv180_arrays(time, event)
    x = np.asarray(matrix177, dtype=float)
    if x.ndim != 2 or len(x) != len(time) or len(penalty_factors) != x.shape[1]:
        raise ValueError("EN matrix/penalty dimensions do not close")
    mapping = penalty_mapping(len(time), int(sum(event)), float(candidate["alpha"]),
                              float(candidate["lambda"]), core_n=20,
                              active_extension_n=x.shape[1] - 20)
    if list(penalty_factors) != mapping["penalty_factors"]:
        raise ValueError("penalty-factor mapping drift")
    columns = [f"x{index:03d}" for index in range(x.shape[1])]
    frame = pd.DataFrame(x, columns=columns)
    frame["__time"] = np.asarray(time, dtype=float)
    frame["__event"] = np.asarray(event, dtype=int)
    fitter = CoxPHFitter(baseline_estimation_method="breslow",
                        penalizer=np.asarray(mapping["backend_penalizer"], dtype=float),
                        l1_ratio=mapping["a_backend"])
    fitter.fit(frame, duration_col="__time", event_col="__event", show_progress=False,
               fit_options={"precision": 1e-10, "max_steps": 1000})
    return {"model_id": "M2_EN", "backend": fitter, "mapping": mapping, "columns": columns,
            "internal_center": fitter._norm_mean.to_dict(),
            "internal_scale": fitter._norm_std.to_dict(),
            "synthetic_only": False, "training_provenance": "AUTHORIZED_DEVELOPMENT"}


def _efron_loglik_gradient(beta, matrix, time, event):
    """Manual Efron partial log-likelihood and gradient (no penalty)."""
    import numpy as np
    x = np.asarray(matrix, dtype=float)
    t = np.asarray(time, dtype=float)
    e = np.asarray(event, dtype=int)
    b = np.asarray(beta, dtype=float)
    eta = x @ b
    shift = float(np.max(eta))
    exp_eta = np.exp(eta - shift)
    loglik = 0.0
    gradient = np.zeros_like(b)
    for failure_time in sorted(set(t[e == 1].tolist())):
        deaths = (t == failure_time) & (e == 1)
        risk = t >= failure_time
        death_n = int(deaths.sum())
        death_exp = exp_eta[deaths]
        risk_exp = exp_eta[risk]
        death_sum = float(death_exp.sum())
        risk_sum = float(risk_exp.sum())
        death_x_exp = (x[deaths] * death_exp[:, None]).sum(axis=0)
        risk_x_exp = (x[risk] * risk_exp[:, None]).sum(axis=0)
        loglik += float(eta[deaths].sum())
        gradient += x[deaths].sum(axis=0)
        for tie_index in range(death_n):
            fraction = tie_index / death_n
            denominator_scaled = risk_sum - fraction * death_sum
            loglik -= math.log(denominator_scaled) + shift
            gradient -= (risk_x_exp - fraction * death_x_exp) / denominator_scaled
    return float(loglik), gradient


def _scientific_objective_gradient(beta, matrix, time, event, alpha, scientific_lambda,
                                   core_n=20):
    import numpy as np
    beta = np.asarray(beta, dtype=float)
    event_n = int(np.asarray(event, dtype=int).sum())
    loglik, score = _efron_loglik_gradient(beta, matrix, time, event)
    ext = beta[core_n:]
    penalty = scientific_lambda * ((1 - alpha) * float(ext @ ext) / 2 +
                                   alpha * float(np.abs(ext).sum()))
    objective = -loglik / event_n + penalty
    gradient = -score / event_n
    gradient[core_n:] += scientific_lambda * ((1 - alpha) * ext + alpha * np.sign(ext))
    return float(objective), gradient


def _kkt_inf(beta, matrix, time, event, alpha, scientific_lambda, core_n=20):
    import numpy as np
    beta = np.asarray(beta, dtype=float)
    event_n = int(np.asarray(event, dtype=int).sum())
    _, score = _efron_loglik_gradient(beta, matrix, time, event)
    smooth = -score / event_n
    ext = beta[core_n:]
    smooth[core_n:] += scientific_lambda * (1 - alpha) * ext
    residuals = list(np.abs(smooth[:core_n]))
    for value, gradient in zip(ext, smooth[core_n:]):
        if abs(value) > 1e-8:
            residuals.append(abs(gradient + scientific_lambda * alpha * math.copysign(1.0, value)))
        else:
            residuals.append(max(0.0, abs(gradient) - scientific_lambda * alpha))
    return float(max(residuals, default=0.0))


def _breslow_risk(beta, matrix, time, event, target, horizons=(90, 180)):
    import numpy as np
    x = np.asarray(matrix, dtype=float)
    t = np.asarray(time, dtype=float)
    e = np.asarray(event, dtype=int)
    eta = x @ np.asarray(beta, dtype=float)
    exp_eta = np.exp(eta)
    increments = []
    for failure_time in sorted(set(t[e == 1].tolist())):
        deaths = int(((t == failure_time) & (e == 1)).sum())
        denominator = float(exp_eta[t >= failure_time].sum())
        increments.append((failure_time, deaths / denominator))
    target_eta = np.asarray(target, dtype=float) @ np.asarray(beta, dtype=float)
    risks = []
    for horizon in horizons:
        baseline = sum(increment for failure_time, increment in increments if failure_time <= horizon)
        risks.append(1 - np.exp(-baseline * np.exp(target_eta)))
    return np.column_stack(risks)


def _strict_fixture(seed: int, alpha: float, scientific_lambda: float, n: int = 180) -> dict[str, Any]:
    import numpy as np
    from scipy.optimize import minimize
    rng = np.random.default_rng(seed)
    p, core_n = 22, 20
    raw = rng.normal(size=(n, p))
    x = (raw - raw.mean(axis=0)) / raw.std(axis=0, ddof=0)
    x += np.linspace(-0.7, 0.8, p)
    truth = np.zeros(p); truth[0] = 0.25; truth[1] = -0.2; truth[20] = 0.65; truth[21] = -0.55
    latent = rng.exponential(55.0 / np.exp(np.clip((x - x.mean(axis=0)) @ truth, -2, 2)))
    censor = rng.uniform(35, 220, size=n)
    event = (latent <= censor).astype(int)
    time = np.minimum(latent, censor)
    time = np.minimum(np.floor(time / 7) * 7, 180.0)
    event = ((event == 1) & (time <= 180)).astype(int)
    time[event == 0] = 180.0
    # Exercise the approved nonnegative-time path and Efron ties.
    first_event = int(np.flatnonzero(event)[0]); time[first_event] = 0.0
    event_n = int(event.sum())
    mapping = penalty_mapping(n, event_n, alpha, scientific_lambda,
                              core_n=core_n, active_extension_n=p - core_n)
    model = fit_m2_en(x, time, event, mapping["penalty_factors"],
                      {"alpha": alpha, "lambda": scientific_lambda},
                      {"synthetic_fixture": True})
    fitter = model["backend"]
    beta_backend = fitter.params_.to_numpy(dtype=float)
    result = minimize(
        lambda beta: _scientific_objective_gradient(beta, x, time, event, alpha,
                                                     scientific_lambda, core_n),
        beta_backend, jac=True, method="L-BFGS-B",
        options={"ftol": 1e-15, "gtol": 1e-12, "maxiter": 4000, "maxls": 100},
    )
    if not result.success:
        raise RuntimeError(f"manual strict-reference optimizer failed: {result.message}")
    beta_manual = result.x
    backend_objective, _ = _scientific_objective_gradient(
        beta_backend, x, time, event, alpha, scientific_lambda, core_n)
    manual_objective, _ = _scientific_objective_gradient(
        beta_manual, x, time, event, alpha, scientific_lambda, core_n)
    target = x[:12]
    manual_risk = _breslow_risk(beta_manual, x, time, event, target)
    from .a5_m0_cox import predict_native_risk
    backend_risk = np.asarray(predict_native_risk(model, target), dtype=float)
    coefficient_diff = float(np.max(np.abs(beta_backend - beta_manual)))
    risk_diff = np.max(np.abs(backend_risk - manual_risk), axis=0)
    metrics = {
        "kkt_inf": _kkt_inf(beta_backend, x, time, event, alpha, scientific_lambda, core_n),
        "objective_abs_diff": abs(backend_objective - manual_objective),
        "coefficient_max_abs_diff": coefficient_diff,
        "risk_max_abs_diff": float(np.max(risk_diff)),
    }
    failure_checks = {}
    # Perturb real fitted coefficients and recompute the actual Efron
    # objective, KKT residual, coefficients and Breslow risk for each gate.
    for target_metric, index, delta in (("kkt_inf", 20, 0.01),
                                         ("objective_abs_diff", 20, 0.05),
                                         ("coefficient_max_abs_diff", 0, 0.001),
                                         ("risk_max_abs_diff", 0, 0.05)):
        perturbed = beta_backend.copy(); perturbed[index] += delta
        bad_objective, _ = _scientific_objective_gradient(perturbed, x, time, event, alpha, scientific_lambda, core_n)
        bad_risk = _breslow_risk(perturbed, x, time, event, target)
        bad = {"kkt_inf": _kkt_inf(perturbed, x, time, event, alpha, scientific_lambda, core_n),
               "objective_abs_diff": abs(bad_objective - manual_objective),
               "coefficient_max_abs_diff": float(np.max(np.abs(perturbed - beta_manual))),
               "risk_max_abs_diff": float(np.max(np.abs(bad_risk - manual_risk)))}
        failure_checks[target_metric] = {"metrics": bad, "status": strict_reference_status(bad)}
    factor_sha = hashlib.sha256(json.dumps(mapping["penalty_factors"],
                                          separators=(",", ":")).encode()).hexdigest().upper()
    return {
        **{key: mapping[key] for key in ("n", "E", "a", "lambda", "s", "r", "q", "a_backend")},
        "penalty_factor_sha256": factor_sha,
        "internal_center": model["internal_center"], "internal_scale": model["internal_scale"],
        "coefficient_conversion_path": mapping["coefficient_conversion_path"],
        "baseline_conversion_path": mapping["baseline_conversion_path"],
        "manual_objective": manual_objective, "backend_objective": backend_objective,
        "manual_kkt_inf": _kkt_inf(beta_manual, x, time, event, alpha, scientific_lambda, core_n),
        "manual_coefficients": beta_manual.tolist(), "backend_coefficients": beta_backend.tolist(),
        "manual_risks_90_180": manual_risk.tolist(), "backend_risks_90_180": backend_risk.tolist(),
        "computed_failure_checks": failure_checks,
        **metrics, "risk_90_abs_diff": float(risk_diff[0]),
        "risk_180_abs_diff": float(risk_diff[1]), "thresholds": dict(STRICT_LIMITS),
        "status": strict_reference_status(metrics),
    }


def run_strict_reference_fixtures(fixture_seed: int = 2026102405) -> list[dict[str, Any]]:
    return [_strict_fixture(fixture_seed + 11, 0.10, 1e-5, 140),
            _strict_fixture(fixture_seed + 29, 0.50, 0.005, 180),
            _strict_fixture(fixture_seed + 43, 0.90, 0.02, 240)]

