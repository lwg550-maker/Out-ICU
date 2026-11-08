"""Diagnostic lifelines tracing and independent Efron reference library.

Patient execution requires an independently enforced authorized controller.
"""
import numpy as np


def efron_objective(X, t, event, beta):
    """Return partial log likelihood, score and observed information."""
    X = np.asarray(X, dtype=float)
    t = np.asarray(t, dtype=float)
    event = np.asarray(event, dtype=bool)
    beta = np.asarray(beta, dtype=float)
    if X.ndim != 2 or t.shape != (len(X),) or event.shape != t.shape or beta.shape != (X.shape[1],):
        raise ValueError('invalid shapes')
    if not all(np.isfinite(a).all() for a in (X, t, beta)):
        raise ValueError('nonfinite input')
    eta = X @ beta
    ll = 0.0
    score = np.zeros(X.shape[1])
    info = np.zeros((X.shape[1], X.shape[1]))
    for time in np.unique(t[event]):
        deaths = (t == time) & event
        risk = t >= time
        xr, er = X[risk], eta[risk]
        xd, ed = X[deaths], eta[deaths]
        shift = np.max(er)
        wr, wd = np.exp(er - shift), np.exp(ed - shift)
        s0, d0 = wr.sum(), wd.sum()
        s1, d1 = wr @ xr, wd @ xd
        s2 = (xr.T * wr) @ xr
        d2 = (xd.T * wd) @ xd
        count = int(deaths.sum())
        ll += ed.sum()
        score += xd.sum(axis=0)
        for k in range(count):
            frac = k / count
            den = s0 - frac * d0
            mean = (s1 - frac * d1) / den
            ll -= shift + np.log(den)
            score -= mean
            info += (s2 - frac * d2) / den - np.outer(mean, mean)
    return float(ll), score, info


def _reference_newton(X, t, event, trace_path=None):
    """Independent unpenalized Newton/Armijo reference, no pseudo inverse."""
    X = np.asarray(X, dtype=float)
    event = np.asarray(event, dtype=bool)
    count = int(event.sum())
    if count == 0:
        raise ValueError('no events')
    beta = np.zeros(X.shape[1])
    history = []
    wall_start, cpu_start = time.perf_counter(), time.process_time()

    def record(data):
        if trace_path is not None:
            data.update(wall_seconds=time.perf_counter() - wall_start,
                        cpu_seconds=time.process_time() - cpu_start)
            _append_durable(trace_path, data)

    for iteration in range(1000):
        ll, score, info = efron_objective(X, t, event, beta)
        eta = X @ beta
        finite = bool(np.isfinite(ll) and np.isfinite(score).all() and
                      np.isfinite(info).all() and np.isfinite(eta).all() and np.isfinite(beta).all())
        norm = float(np.linalg.norm(score, ord=np.inf))
        state = {'iteration': iteration, 'beta_external': beta.copy(), 'loglik': ll,
                 'score': score, 'information': info, 'score_inf': norm,
                 'score_inf_per_event': norm / count, 'eta_min': float(np.min(eta)),
                 'eta_max': float(np.max(eta)), 'finite': finite}
        record(dict(type='iteration', **state))
        if not finite:
            raise ArithmeticError('nonfinite objective')
        history.append({'iteration': iteration, 'loglik': ll, 'score_inf': norm})
        np.linalg.cholesky(info)
        if norm < 1e-4 and norm / count < 2e-7:
            record(dict(type='converged', **state))
            return {'beta': beta, 'loglik': ll, 'score': score, 'information': info, 'history': history}
        delta = np.linalg.solve(info, score)
        gain = float(score @ delta)
        update_norm = float(np.linalg.norm(delta))
        record(dict(type='search_start', newton_decrement=gain / 2, update_norm=update_norm, **state))
        alpha = 1.0
        for trial in range(30):
            candidate = beta + alpha * delta
            new_ll, _, _ = efron_objective(X, t, event, candidate)
            accepted = bool(np.isfinite(new_ll) and new_ll >= ll + 1e-4 * alpha * gain)
            record({'type': 'line_search', 'iteration': iteration, 'trial': trial,
                    'alpha': alpha, 'candidate_beta_external': candidate, 'candidate_loglik': new_ll,
                    'accepted': accepted, 'update_norm': update_norm,
                    'applied_update_norm': float(alpha * update_norm), 'newton_decrement': gain / 2,
                    'finite': bool(np.isfinite(new_ll) and np.isfinite(candidate).all())})
            if accepted:
                beta = candidate
                break
            alpha *= 0.5
        else:
            raise ArithmeticError('Armijo backtracking failed')
    raise ArithmeticError('maximum iterations reached')

# Observation is confined to the existing gradient callable: lifelines retains
# control of its own Newton solver and all convergence decisions.
import inspect
import json
import os
import time
import traceback
import warnings
from lifelines import CoxPHFitter
from lifelines.fitters.coxph_fitter import SemiParametricPHFitter
from lifelines import utils


def _json_safe(value):
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (np.integer, np.floating)):
        return _json_safe(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    return value


def _append_durable(path, record):
    with open(path, 'a', encoding='utf-8') as out:
        out.write(json.dumps(_json_safe(record), allow_nan=False, sort_keys=True) + '\n')
        out.flush()
        os.fsync(out.fileno())


class TracedSemiParametricPHFitter(SemiParametricPHFitter):
    def __init__(self, *args, trace_path, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace_path = trace_path

    def _choose_gradient_calculator(self, T, X, entries):
        original = super()._choose_gradient_calculator(T, X, entries)
        if self.strata is not None or entries is not None or np.any(np.asarray(self.penalizer) != 0):
            raise ValueError('tracing supports only unpenalized, unstratified right censoring')

        def observe(x, t, e, weights, entry, beta):
            frame = inspect.currentframe().f_back
            before = time.perf_counter()
            try:
                h, g, ll = original(x, t, e, weights, entry, beta)
                eta = np.asarray(x) @ beta
                finite = bool(np.isfinite(h).all() and np.isfinite(g).all() and np.isfinite(ll) and np.isfinite(eta).all())
                eig = np.linalg.eigvalsh(-h) if finite else np.full(len(beta), np.nan)
                delta = None
                decrement = None
                if finite:
                    try:
                        np.linalg.cholesky(-h)
                        delta = np.linalg.solve(-h, g)
                        decrement = float(g @ delta / 2)
                    except np.linalg.LinAlgError:
                        pass
                std = np.asarray(self._norm_std)
                event_count = int(np.asarray(e).sum())
                _append_durable(self.trace_path, {
                    'type': 'iteration', 'iteration': frame.f_locals.get('i'),
                    'step_size': frame.f_locals.get('step_size'), 'beta_backend': beta.copy(),
                    'beta_external': beta / std, 'loglik': ll, 'score_backend': g,
                    'hessian_backend': h, 'score_external': g * std, 'information_external': (-h) * np.outer(std, std), 'score_inf': float(np.max(np.abs(g * std))),
                    'score_inf_per_event': float(np.max(np.abs(g * std)) / event_count),
                    'information_eigenvalues_backend': eig,
                    'information_condition_backend': float(np.linalg.cond(-h)) if finite else None,
                    'eta_min': float(np.min(eta)), 'eta_max': float(np.max(eta)),
                    'update_norm': float(np.linalg.norm(delta)) if delta is not None else None,
                    'newton_decrement': decrement, 'finite': finite,
                    'elapsed_gradient_seconds': time.perf_counter() - before,
                    'cpu_seconds': time.process_time(),
                })
                return h, g, ll
            except BaseException:
                _append_durable(self.trace_path, {'type': 'gradient_exception', 'exception_chain': traceback.format_exc()})
                raise
            finally:
                del frame
        return observe


class TracedCoxPHFitter(CoxPHFitter):
    def __init__(self, *args, trace_path, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace_path = str(trace_path)

    def _fit_model_breslow(self, *args, **kwargs):
        if not utils.CensoringType.is_right_censoring(self):
            raise ValueError('only right censoring is supported')
        model = TracedSemiParametricPHFitter(penalizer=self.penalizer, l1_ratio=self.l1_ratio,
            strata=self.strata, alpha=self.alpha, label=self._label, trace_path=self.trace_path)
        start = time.perf_counter()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                try:
                    model.fit(*args, **kwargs)
                finally:
                    for item in caught:
                        _append_durable(self.trace_path, {'type': 'warning', 'message': str(item.message),
                                                         'category': item.category.__name__})
            for item in caught:
                warnings.warn_explicit(item.message, item.category, item.filename, item.lineno)
            return model
        except BaseException:
            _append_durable(self.trace_path, {'type': 'fit_exception', 'exception_chain': traceback.format_exc()})
            raise
        finally:
            _append_durable(self.trace_path, {'type': 'fit_end', 'wall_seconds': time.perf_counter()-start,
                                              'cpu_seconds': time.process_time()})


def reference_newton(X, t, event, trace_path=None):
    """Run the independent reference and durably record steps when requested."""
    started = time.perf_counter()
    try:
        result = _reference_newton(X, t, event, trace_path)
        if trace_path is not None:
            _append_durable(trace_path, {'type': 'reference_success', 'wall_seconds': time.perf_counter() - started, 'cpu_seconds': time.process_time()})
        return result
    except BaseException:
        if trace_path is not None:
            _append_durable(trace_path, {'type': 'reference_exception', 'exception_chain': traceback.format_exc(), 'wall_seconds': time.perf_counter() - started, 'cpu_seconds': time.process_time()})
        raise
