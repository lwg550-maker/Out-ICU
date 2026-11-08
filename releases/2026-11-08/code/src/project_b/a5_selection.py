"""Synthetic five-fold candidate scoring and failure accounting."""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Mapping, Sequence

from .a5_common import candidate_eligibility, classify_failure, validate_fit_receipt
from .a5_en_cox import _efron_loglik_gradient, en_grid, select_en_candidate


class _ProjectFailure(ValueError):
    pass


def candidate_for_fold(family: str, candidate: Mapping[str, Any], *, n_features: int) -> dict[str, Any]:
    if family != "RSF":
        return dict(candidate)
    from .a5_rsf import rsf_grid
    order = candidate.get("declared_order")
    if not isinstance(order, int) or order not in range(8):
        raise ValueError("RSF candidate identity drift")
    adapted = rsf_grid(n_features)[order]
    if any(candidate.get(key) != adapted[key] for key in ("sample_fraction", "min_samples_leaf", "declared_order")):
        raise ValueError("RSF candidate identity drift")
    return adapted


def conditional_en_lambdas(matrix, time, event, core_beta, *, core_n: int = 20):
    import numpy as np
    x = np.asarray(matrix, dtype=float)
    t = np.asarray(time, dtype=float)
    e = np.asarray(event, dtype=int)
    core = np.asarray(core_beta, dtype=float)
    if x.ndim != 2 or len(x) != len(t) or len(e) != len(t) or x.shape[1] < core_n or core.shape != (core_n,) or e.sum() <= 0:
        raise ValueError("invalid conditional core fit for lambda_max")
    if x.shape[1] == core_n:
        maximum = 0.0
    else:
        beta = np.zeros(x.shape[1]); beta[:core_n] = core
        _, score = _efron_loglik_gradient(beta, x, t, e)
        maximum = float(np.max(np.abs(score[core_n:])))
    result = []
    for candidate in en_grid():
        lambda_max = maximum / (int(e.sum()) * candidate["alpha"])
        result.append({**candidate, "lambda_max": lambda_max,
                       "lambda": lambda_max * candidate["lambda_ratio"]})
    return result


def _select(family: str, eligible: list[dict[str, Any]]) -> dict[str, Any]:
    if family == "EN":
        return select_en_candidate(eligible)
    best = min(row["brier"] for row in eligible)
    tied = [row for row in eligible if row["brier"] <= best + 1e-8]
    if family == "RSF":
        return min(tied, key=lambda row: (-row["min_samples_leaf"], row["mtry"], row["sample_fraction"], row["declared_order"]))
    if family == "GBST":
        return min(tied, key=lambda row: (row["max_depth"], row["n_estimators"], row["learning_rate"], row["declared_order"]))
    raise ValueError("unknown family")


def evaluate_candidate_grid(family: str, candidates: Sequence[Mapping[str, Any]],
                            folds: Sequence[Mapping[str, Any]], fit_fold: Callable, *,
                            n_features: int | None = None) -> dict[str, Any]:
    """Score complete five-fold candidates by pooled patient-level Brier."""
    expected = {"EN": 15, "RSF": 8, "GBST": 12}
    if family not in expected or len(candidates) != expected[family] or len(folds) != 5:
        raise ValueError("candidate grid or fold count drift")
    from .a5_gbst import gbst_grid
    from .a5_rsf import rsf_grid
    if family == "RSF" and (not isinstance(n_features, int) or n_features < 1):
        raise ValueError("RSF grid requires feature width")
    reference = en_grid() if family == "EN" else gbst_grid() if family == "GBST" else rsf_grid(n_features)
    if sorted((dict(row) for row in candidates), key=lambda row: row.get("declared_order", -1)) != reference:
        raise ValueError("frozen candidate grid drift")
    orders = [row.get("declared_order") for row in candidates]
    if set(orders) != set(range(len(candidates))):
        raise ValueError("candidate order is missing or duplicated")
    identities = []
    for fold in folds:
        ids, y = list(fold["ids"]), list(fold["outcomes"])
        if not ids or len(ids) != len(y) or not set(y) <= {0, 1}:
            raise ValueError("invalid inner validation fold")
        identities.extend(ids)
    if len(set(identities)) != len(identities):
        raise ValueError("inner validation identities overlap")
    ledger = {"planned": len(candidates) * 5, "attempted": 0, "succeeded": 0,
              "failed": 0, "skipped": 0, "failures": []}
    rows = []
    for position, candidate in enumerate(candidates):
        losses = []
        nonzero = []
        for fold_no, fold in enumerate(folds, 1):
            ledger["attempted"] += 1
            try:
                result = fit_fold(candidate, fold_no, fold)
                if list(result["ids"]) != list(fold["ids"]):
                    raise _ProjectFailure("PATIENT_FOLD_COLUMN_CHANGE")
                risks = list(result["risk180"])
                if len(risks) != len(fold["ids"]) or any(not math.isfinite(float(p)) or not 0 <= float(p) <= 1 for p in risks):
                    raise _ProjectFailure("INVALID_NATIVE_PROBABILITY")
                count = result.get("nonzero_count", 0)
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise _ProjectFailure("MISSING_PROVENANCE")
            except (ValueError, RuntimeError, FloatingPointError, PermissionError) as exc:
                classified = []
                for token in re.findall(r"(?<![A-Z0-9_])[A-Z][A-Z0-9_]*(?![A-Z0-9_])", str(exc)):
                    try:
                        classified.append((token, classify_failure(token)))
                    except ValueError:
                        continue
                if any(scope == "EXECUTION_ERROR" for _, scope in classified):
                    raise
                if classified:
                    code, scope = next(((code, scope) for code, scope in classified if scope == "PROJECT_HOLD"), classified[0])
                    ledger["failed"] += 1
                    ledger["skipped"] += 5 - fold_no + 5 * (len(candidates) - position - 1)
                    ledger["failures"].append({"candidate_order": candidate["declared_order"], "fold": fold_no, "reason": str(exc)})
                    validate_fit_receipt(ledger)
                    return {"status": scope, "project_failure": code if scope == "PROJECT_HOLD" else None,
                            "failure_code": code, "selected": None,
                            "eligible_candidates": [], "eligible_n": 0, "ledger": ledger}
                if isinstance(exc, PermissionError):
                    raise
                ledger["failed"] += 1
                ledger["skipped"] += 5 - fold_no
                ledger["failures"].append({"candidate_order": candidate["declared_order"], "fold": fold_no, "reason": str(exc)})
                break
            ledger["succeeded"] += 1
            losses.extend((float(p) - int(y))**2 for p, y in zip(risks, fold["outcomes"]))
            nonzero.append(count)
        else:
            rows.append({**candidate, "brier": sum(losses) / len(losses), "nonzero_counts": nonzero,
                         "validation_patient_n": len(losses), "complete_folds": 5})
    validate_fit_receipt(ledger)
    status = candidate_eligibility(family, eligible_n=len(rows), declared_n=len(candidates), complete_candidate=bool(rows))
    return {"status": status, "selected": _select(family, rows) if status == "PASS" else None,
            "eligible_candidates": rows, "eligible_n": len(rows), "ledger": ledger}
