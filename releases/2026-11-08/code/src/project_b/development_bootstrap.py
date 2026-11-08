"""Authorized development GLOBAL_COMMON_VALID paired-bootstrap contracts."""

from __future__ import annotations

import hashlib
import json
import random
import math
from typing import Any, Iterable, Mapping, Sequence


MODELS = ("M0", "M2_EN", "M1_RSF", "M2_RSF", "M1_GBST", "M2_GBST")
REQUIRED_STREAMS = tuple(f"{model}_{stream}" for model in MODELS for stream in ("native", "recal"))
COMPARISONS = (("M2_EN-M0", "M0", "M2_EN"),
               ("M2_RSF-M1_RSF", "M1_RSF", "M2_RSF"),
               ("M2_GBST-M1_GBST", "M1_GBST", "M2_GBST"),
               ("M1_RSF-M0", "M0", "M1_RSF"),
               ("M1_GBST-M0", "M0", "M1_GBST"),
               ("M2_RSF-M0_descriptive", "M0", "M2_RSF"),
               ("M2_GBST-M0_descriptive", "M0", "M2_GBST"))
CURVE_PROBABILITIES = (0.10, 0.25, 0.50, 0.75, 0.90)


def required_output_keys() -> tuple[str, ...]:
    keys = []
    for horizon in (90, 180):
        metrics = ["brier", "logloss", "time_auc", "calibration_intercept", "calibration_slope"]
        metrics += [f"calibration_curve_{point:g}" for point in CURVE_PROBABILITIES]
        if horizon == 180:
            metrics.append("uno_c")
        keys.extend(f"stream|{stream}|{horizon}|{metric}" for stream in REQUIRED_STREAMS for metric in metrics)
        keys.extend(f"comparison|{name}_{stream}|{horizon}|{metric}"
                    for name, _, _ in COMPARISONS for stream in ("native", "recal") for metric in metrics)
    return tuple(sorted(keys))


def _seed(spec: Mapping[str, Any], attempt_id: int) -> int:
    master = int(spec.get("master_seed", 2026102405))
    master_digest = hashlib.sha256(f"A5BOOT|{master}|global".encode()).hexdigest()
    digest = hashlib.sha256(f"{master_digest}|attempt={attempt_id}".encode()).hexdigest()
    return int(digest[:16], 16)


def attempt_indices(patient_n: int, *, attempt_id: int, spec: Mapping[str, Any]) -> list[int]:
    validate_bootstrap_spec({**spec, "retrain_models": False})
    if patient_n <= 0 or isinstance(attempt_id, bool) or not isinstance(attempt_id, int) or attempt_id < 1:
        raise ValueError("bootstrap requires positive patient count and attempt ID")
    rng = random.Random(_seed(spec, attempt_id))
    return [rng.randrange(patient_n) for _ in range(patient_n)]


def bind_attempt_draw(indices: Sequence[int], *, streams: Sequence[str],
                      horizons: Sequence[int], repeats: int) -> dict[str, Any]:
    digest = hashlib.sha256(json.dumps(list(indices), separators=(",", ":")).encode()).hexdigest().upper()
    hashes = {f"{stream}|{horizon}|repeat={repeat}": digest
              for stream in streams for horizon in horizons for repeat in range(1, repeats + 1)}
    return {"draw_sha256_by_output": hashes, "shared_draw": True}


def select_global_common_valid(attempts: Iterable[Mapping[str, Any]], *, target_valid: int,
                               maximum_attempts: int, required_outputs: Sequence[str] | None = None) -> dict[str, Any]:
    import numpy as np
    if not 1 <= target_valid <= maximum_attempts:
        raise ValueError("invalid bootstrap attempt limits")
    common: list[int] = []
    retained: dict[int, Any] = {}
    rejected: dict[int, list[str]] = {}
    attempted = 0
    previous = 0
    output_keys = set(required_outputs) if required_outputs is not None else None
    if output_keys == set():
        raise ValueError("required output schema may not be empty")
    if target_valid == 2000 and (maximum_attempts != 2200 or output_keys != set(required_output_keys())):
        raise ValueError("full bootstrap requires the frozen complete output schema")
    source_iterator = iter(attempts)
    while attempted < maximum_attempts and len(common) < target_valid:
        try:
            source = next(source_iterator)
        except StopIteration:
            break
        row = dict(source); attempted += 1
        attempt_id = row.get("attempt_id")
        if isinstance(attempt_id, bool) or not isinstance(attempt_id, int) or attempt_id <= previous:
            raise ValueError("bootstrap attempt IDs must be unique, positive and ordered")
        previous = attempt_id
        failures = sorted(set(str(item) for item in row.get("failures", [])))
        if failures:
            rejected[attempt_id] = failures
            continue
        outputs = row.get("outputs")
        if not isinstance(outputs, Mapping) or not outputs:
            raise ValueError("bootstrap output schema cannot be empty")
        if output_keys is None:
            output_keys = set(outputs)
        if set(outputs) != output_keys or any(isinstance(v, bool) or not isinstance(v, (int, float, np.number)) or not math.isfinite(float(v)) for v in outputs.values()):
            raise ValueError("bootstrap output schema is incomplete or nonfinite")
        common.append(attempt_id); retained[attempt_id] = dict(outputs)
    if len(common) != target_valid:
        return {"status": "BOOTSTRAP_INTERVALS_HOLD", "common_valid_ids": common,
                "retained_outputs": {}, "rejections": rejected, "intervals": [],
                "attempted_n": attempted}
    if target_valid != 2000:
        return {"status": "INCOMPLETE_SYNTHETIC_TEST", "common_valid_ids": common,
                "retained_outputs": retained, "rejections": rejected, "intervals": [], "attempted_n": attempted}
    intervals = {key: [float(value) for value in np.percentile([retained[i][key] for i in common], [2.5, 97.5])]
                 for key in sorted(output_keys)}
    return {"status": "PASS", "common_valid_ids": common, "retained_outputs": retained,
            "rejections": rejected, "intervals": intervals, "attempted_n": attempted}


def average_repeat_metric_and_pair(repeat_losses: Mapping[str, Sequence[Sequence[float]]],
                                   *, comparison: str) -> dict[str, Any]:
    if comparison != "M2-M1":
        raise ValueError("unsupported formal comparison")
    def means(rows):
        return [sum(map(float, row)) / len(row) for row in rows]
    m1 = means(repeat_losses["M1"]); m2 = means(repeat_losses["M2"])
    if len(m1) != len(m2):
        raise ValueError("repeat counts differ")
    paired = [a - b for a, b in zip(m1, m2)]
    return {"L_M1_minus_L_M2": sum(paired) / len(paired),
            "computed_per_repeat_before_average": True, "paired_within_draw": True}


def validate_bootstrap_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    if spec.get("common_validity") != "GLOBAL_COMMON_VALID":
        raise PermissionError("analysis-specific validity sets are forbidden")
    if spec.get("retrain_models") is not False:
        raise PermissionError("bootstrap may not retrain models")
    return {"status": "PASS"}


def _curve_values(regression):
    import numpy as np
    from scipy.special import expit, logit
    knots = np.asarray(regression["curve_knots"], dtype=float)
    scales = regression["curve_term_scales"]
    beta = np.asarray(regression["regressions"]["curve"], dtype=float)
    x = logit(np.asarray(CURVE_PROBABILITIES))
    cols = [np.ones(len(x)), x]
    k2, k3 = knots[-2:]
    for knot, scale in zip(knots[:-2], scales):
        term = (np.maximum(x-knot, 0)**3 - np.maximum(x-k2, 0)**3 * (k3-knot)/(k3-k2)
                + np.maximum(x-k3, 0)**3 * (k2-knot)/(k3-k2)) / scale
        cols.append(term)
    return expit(np.column_stack(cols) @ beta)


def _validated_inputs(predictions, outcomes):
    import numpy as np
    if not isinstance(predictions, Mapping) or set(predictions) != set(REQUIRED_STREAMS):
        raise ValueError("complete frozen native and recalibrated stream set required")
    if not isinstance(outcomes, Mapping) or set(outcomes) != {"time", "event", "death_90d", "death_180d"}:
        raise ValueError("complete outcome schema required")
    event = np.asarray(outcomes["event"])
    time = np.asarray(outcomes["time"], dtype=float)
    y90, y180 = np.asarray(outcomes["death_90d"]), np.asarray(outcomes["death_180d"])
    if event.ndim != 1 or len(event) < 2 or time.shape != event.shape or y90.shape != event.shape or y180.shape != event.shape or \
            not np.isfinite(time).all() or not set(event.tolist()) <= {0, 1} or not set(y90.tolist()) <= {0, 1} or not set(y180.tolist()) <= {0, 1} or \
            np.any(time < 0) or np.any(time > 180) or np.any((event == 0) & (time != 180)) or \
            not np.array_equal(y90, (event == 1) & (time <= 90)) or not np.array_equal(y180, event == 1):
        raise ValueError("invalid or inconsistent frozen outcomes")
    arrays = {}
    for stream in REQUIRED_STREAMS:
        array = np.asarray(predictions[stream], dtype=float)
        if array.shape != (10, len(event), 2) or not np.isfinite(array).all() or np.any(array < 0) or np.any(array > 1):
            raise ValueError("invalid prediction stream shape or probability")
        if stream.endswith("_native") and np.any(array[:, :, 0] > array[:, :, 1]):
            raise ValueError("NATIVE_MONOTONICITY_FAILURE")
        arrays[stream] = array
    return arrays, {"time": time, "event": event.astype(bool), "death_90d": y90.astype(int), "death_180d": y180.astype(int)}


def uno_c_180(event, time, risks) -> float:
    """IPCW Uno C with the backend's exact tau=180 convention (event time < tau)."""
    import numpy as np
    from sksurv.metrics import concordance_index_ipcw
    event = np.asarray(event, dtype=bool)
    time = np.asarray(time, dtype=float)
    risks = np.asarray(risks, dtype=float)
    if event.ndim != 1 or time.shape != event.shape or risks.shape != event.shape or not np.isfinite(time).all() or not np.isfinite(risks).all():
        raise ValueError("UNO_C_NOT_ESTIMABLE")
    sampled = np.array(list(zip(event, time)), dtype=[("event", "?"), ("time", "<f8")])
    # Refit censoring weights in this exact resample. The backend uses t < tau
    # for index events; day-180 deaths remain events in every outcome array.
    return float(concordance_index_ipcw(sampled, sampled, risks, tau=180.0)[0])


def compute_development_attempt(predictions, outcomes, indices, project_root):
    """Compute every frozen stream on the same patient draw, repeat first."""
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from .development_calibration import fit_evaluation_regression_triplet
    arrays, outcome = _validated_inputs(predictions, outcomes)
    draw = np.asarray(indices)
    n = len(outcome["event"])
    if draw.ndim != 1 or len(draw) != n or not np.issubdtype(draw.dtype, np.integer) or np.any(draw < 0) or np.any(draw >= n):
        raise ValueError("patient draw must have one valid index per sampled patient")
    output = {}
    for stream, array in arrays.items():
        for h, horizon in enumerate((90, 180)):
            y = outcome[f"death_{horizon}d"][draw]
            if len(np.unique(y)) != 2:
                raise ValueError("BOOTSTRAP_CLASS_NOT_ESTIMABLE")
            values = {}
            for repeat in range(10):
                p = array[repeat, draw, h]
                q = np.clip(p, 1e-6, 1 - 1e-6)
                metrics = {"brier": float(np.mean((p-y)**2)),
                           "logloss": float(np.mean(-y*np.log(q)-(1-y)*np.log1p(-q))),
                           "time_auc": float(roc_auc_score(y, p))}
                if horizon == 180:
                    # The censoring distribution is re-estimated in this draw.
                    metrics["uno_c"] = uno_c_180(outcome["event"][draw], outcome["time"][draw], p)
                regression = fit_evaluation_regression_triplet(p, y, {"project_root": project_root})
                metrics["calibration_intercept"] = regression["regressions"]["intercept"][0]
                metrics["calibration_slope"] = regression["regressions"]["slope"][1]
                for point, curve in zip(CURVE_PROBABILITIES, _curve_values(regression)):
                    metrics[f"calibration_curve_{point:g}"] = float(curve)
                for key, value in metrics.items():
                    values.setdefault(key, []).append(value)
            for key, rows in values.items():
                output[f"stream|{stream}|{horizon}|{key}"] = float(np.mean(rows))
    for name, reference, candidate in COMPARISONS:
        for stream in ("native", "recal"):
            for horizon in (90, 180):
                prefix = f"stream|{reference}_{stream}|{horizon}|"
                for key in [key for key in output if key.startswith(prefix)]:
                    metric = key[len(prefix):]
                    other = f"stream|{candidate}_{stream}|{horizon}|{metric}"
                    # For losses positive values mean an improvement; other
                    # metrics use candidate minus reference.
                    delta = output[key] - output[other] if metric in {"brier", "logloss"} else output[other] - output[key]
                    output[f"comparison|{name}_{stream}|{horizon}|{metric}"] = float(delta)
    if set(output) != set(required_output_keys()) or not all(math.isfinite(value) for value in output.values()):
        raise ValueError("bootstrap output not estimable")
    return output


def paired_bootstrap(predictions, outcomes, comparison, spec):
    validate_bootstrap_spec(spec)
    from .development_authorization import validate_development_authorization
    validate_development_authorization(spec["project_root"])
    if comparison != "ALL_FORMAL_AND_DESCRIPTIVE":
        raise ValueError("the complete comparison set is required")
    if spec.get("target_valid_replicates") != 2000 or spec.get("maximum_attempts") != 2200:
        raise ValueError("frozen bootstrap limits drift")
    arrays, checked = _validated_inputs(predictions, outcomes)
    def attempts():
        for attempt_id in range(1, 2201):
            draw = attempt_indices(len(checked["event"]), attempt_id=attempt_id, spec=spec)
            try:
                outputs = compute_development_attempt(arrays, checked, draw, spec["project_root"])
                yield {"attempt_id": attempt_id, "outputs": outputs, "failures": []}
            except (ValueError, RuntimeError, FloatingPointError) as exc:
                yield {"attempt_id": attempt_id, "outputs": {}, "failures": [str(exc)]}
    result = select_global_common_valid(attempts(), target_valid=2000, maximum_attempts=2200,
                                        required_outputs=required_output_keys())
    result.update({"patient_level": True, "draw_shared_across_all_streams_horizons_repeats": True,
                   "retrained_models": False, "curve_evaluation_probabilities": list(CURVE_PROBABILITIES)})
    return result

