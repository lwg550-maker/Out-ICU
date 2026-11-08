"""Versioned EN numerical acceptance; does not replace signed production code.

The caller controls authorization, snapshots, budgets, and one-call execution.
No retry, candidate search, performance ranking, or coefficient alteration occurs.
"""
from __future__ import annotations

import json
from pathlib import Path
import warnings

import numpy as np

from .development_en_cox import STRICT_LIMITS, _efron_loglik_gradient

KKT_LIMIT = STRICT_LIMITS['kkt_inf']
ZERO_TOLERANCE = 1e-8  # Same near-zero convention as the frozen _kkt_inf.
OBJECTIVE_SLACK = STRICT_LIMITS['objective_abs_diff']


class NumericalAcceptanceError(RuntimeError):
    def __init__(self, acceptance):
        self.acceptance = acceptance
        super().__init__('M2_EN_NUMERICAL_REJECTION: ' + ','.join(acceptance['failure_reasons']))


def evaluate_solution(X, t, e, beta, alpha, scientific_lambda, *, core_n=20,
                      backend_success, warning_records, trace_complete,
                      reference_beta=None):
    """Check the frozen objective in external coefficient coordinates; no fit.

    Exact and frozen 1e-8 sign-based L1 KKT are retained as evidence. Acceptance
    uses the continuous proximal-gradient residual at unit step and a separate
    unpenalized-core score check. The proximal map is used only to calculate a
    residual; fitted coefficients are never thresholded or changed. Its zero
    set is precisely the nonsmooth KKT set for the same scientific objective.
    The tolerances are numerical, not clinical performance success thresholds.
    """
    x, t, e, beta = (np.asarray(v, dtype=float) for v in (X, t, e, beta))
    if (x.ndim != 2 or beta.shape != (x.shape[1],) or t.shape != (len(x),)
            or e.shape != t.shape or not 0 < core_n <= x.shape[1]
            or not np.isfinite(x).all() or not np.isfinite(t).all()
            or not np.isin(e, [0, 1]).all() or e.sum() <= 0
            or not 0 <= alpha <= 1 or not np.isfinite(scientific_lambda) or scientific_lambda < 0):
        raise ValueError('invalid numerical acceptance input')
    reasons = []
    if backend_success is not True:
        reasons.append('BACKEND_NONCONVERGENCE')
    if trace_complete is not True:
        reasons.append('TRACE_INCOMPLETE')
    if any(w.get('category') == 'ConvergenceWarning' or w.get('is_convergence_warning') is True
           for w in warning_records):
        reasons.append('CONVERGENCE_WARNING')
    result = dict(passed=False, failure_reasons=reasons, warning_records=warning_records,
                  backend_success=backend_success, trace_complete=trace_complete,
                  stationarity_rule='unit-step proximal-gradient of frozen scientific objective',
                  proximal_gradient_tolerance=KKT_LIMIT, core_score_tolerance=KKT_LIMIT,
                  zero_tolerance=ZERO_TOLERANCE, objective_slack=OBJECTIVE_SLACK)
    if not np.isfinite(beta).all():
        reasons.append('NONFINITE_COEFFICIENT')
        return result
    E = int(e.sum())
    loglik, score = _efron_loglik_gradient(beta, x, t, e)
    smooth = -score / E
    ext = beta[core_n:]
    smooth[core_n:] += scientific_lambda * (1 - alpha) * ext
    exact = np.abs(smooth.copy())
    exact[core_n:] = np.where(ext != 0,
        np.abs(smooth[core_n:] + scientific_lambda * alpha * np.sign(ext)),
        np.maximum(0, np.abs(smooth[core_n:]) - scientific_lambda * alpha))
    frozen = exact.copy()
    frozen[core_n:] = np.where(np.abs(ext) > ZERO_TOLERANCE,
        np.abs(smooth[core_n:] + scientific_lambda * alpha * np.sign(ext)),
        np.maximum(0, np.abs(smooth[core_n:]) - scientific_lambda * alpha))
    z = ext - smooth[core_n:]
    prox_ext = np.sign(z) * np.maximum(np.abs(z) - scientific_lambda * alpha, 0)
    proximal = smooth.copy()
    proximal[core_n:] = ext - prox_ext
    objective = float(-loglik / E + scientific_lambda *
                      ((1 - alpha) * (ext @ ext) / 2 + alpha * np.abs(ext).sum()))
    core_score = float(np.abs(score[:core_n]).max() / E)
    references = [('zero', np.zeros_like(beta))]
    if reference_beta is not None:
        ref = np.asarray(reference_beta, float)
        if ref.shape != beta.shape or not np.isfinite(ref).all():
            raise ValueError('invalid reference coefficient vector')
        references.append(('saved_M0_plus_zero_extensions', ref))
    reference_objectives = {}
    for name, ref in references:
        ll_ref, _ = _efron_loglik_gradient(ref, x, t, e)
        ref_ext = ref[core_n:]
        reference_objectives[name] = float(-ll_ref / E + scientific_lambda *
            ((1 - alpha) * (ref_ext @ ref_ext) / 2 + alpha * np.abs(ref_ext).sum()))
    finite = bool(np.isfinite([loglik, objective, core_score, *reference_objectives.values()]).all()
                  and np.isfinite(smooth).all() and np.isfinite(x @ beta).all())
    if not finite:
        reasons.append('NONFINITE_OPTIMALITY')
    else:
        if float(np.abs(proximal).max()) > KKT_LIMIT:
            reasons.append('SCIENTIFIC_KKT')
        if core_score > KKT_LIMIT:
            reasons.append('CORE_SCORE')
        if objective > min(reference_objectives.values()) + OBJECTIVE_SLACK:
            reasons.append('OBJECTIVE_WORSE_THAN_REFERENCE')
    result.update(scientific_objective=objective, exact_kkt_inf=float(exact.max()),
                  frozen_1e8_kkt_inf=float(frozen.max()), core_score_inf_per_event=core_score,
                  proximal_gradient_inf=float(np.abs(proximal).max()),
                  reference_objectives=reference_objectives, finite=finite,
                  extension_nonzero_n=int((np.abs(ext) > ZERO_TOLERANCE).sum()),
                  passed=not reasons)
    return result


def fit_checked(X, t, e, mapping, trace_path, *, core_n=20, fit_options=None,
                candidate=None, provenance=None, reference_beta=None):
    """Fit once and reject warnings/nonoptimal returns; never retry automatically."""
    from lifelines.exceptions import ConvergenceWarning
    from .m2_en_trace import fit_once
    factors = list(mapping['penalty_factors'])
    if factors != [0] * core_n + [1] * (np.asarray(X).shape[1] - core_n):
        raise ValueError('core/extension penalty boundary mismatch')
    path = Path(trace_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        model = fit_once(X, t, e, mapping, path, initial_beta_external=None,
                         fit_options=fit_options, candidate=candidate, provenance=provenance)
    records = [dict(category=w.category.__name__, message=str(w.message),
                    is_convergence_warning=issubclass(w.category, ConvergenceWarning)) for w in caught]
    footer = exit_state = None
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            row = json.loads(line)
            if row['type'] == 'newton_exit':
                exit_state = row
            footer = row
    complete = bool(footer and footer.get('type') == 'footer'
                    and footer.get('trace_restored') is True and footer.get('observer_errors') == [])
    acceptance = evaluate_solution(X, t, e, model.params_.to_numpy(), mapping['a'], mapping['lambda'],
        core_n=core_n, backend_success=bool(exit_state and exit_state.get('backend_success') is True),
        warning_records=records, trace_complete=complete, reference_beta=reference_beta)
    acceptance.update(beta_external=model.params_.to_numpy().tolist(),
                      newton_last_iteration=exit_state.get('iteration') if exit_state else None,
                      trace_warning_records=footer.get('warnings', []) if complete else [])
    if not acceptance['passed']:
        raise NumericalAcceptanceError(acceptance)
    return model, acceptance
