"""Read-only, version-bound EN observer. No production entry point or grid runner."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import traceback
import warnings

import numpy as np

NEWTON_SHA256 = "b4f2a8c727b9069d498f4475863bd472fb2eeae5eaa0d5b75de3bae1c1ae60a3"


def _json(value):
    if isinstance(value, np.ndarray):
        return _json(value.tolist())
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, (np.integer, np.bool_)):
        return value.item()
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else str(value)
    return value


def _append(path, record):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(_json(record), allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _binding():
    import lifelines
    from lifelines.fitters.coxph_fitter import SemiParametricPHFitter
    fn = SemiParametricPHFitter._newton_raphson_for_efron_model
    source, start = inspect.getsourcelines(fn)
    digest = hashlib.sha256("".join(source).encode()).hexdigest()
    if lifelines.__version__ != "0.30.0" or digest != NEWTON_SHA256:
        raise RuntimeError("observer requires inspected lifelines 0.30.0 Newton source hash")
    def line(fragment):
        matches = [start + i for i, s in enumerate(source) if fragment in s]
        if len(matches) != 1:
            raise RuntimeError("ambiguous observer line")
        return matches[0]
    return fn.__code__, line('inv_h_dot_g_T = spsolve('), line('if show_progress:'), digest


def _state(local, mapping, fixed_smoothing):
    model = local["self"]
    b = local["beta"].copy()
    scale = np.asarray(model._norm_std, float)
    center = np.asarray(model._norm_mean, float)
    external = b / scale
    g = local["g"].copy()
    h = local["h"].copy()
    a = 1.3 ** local["i"]
    penalty = float(local["elastic_net_penalty"](b, a))
    pg = np.asarray(local["d_elastic_net_penalty"](b, a), float)
    ph = np.asarray(local["dd_elastic_net_penalty"](b, a), float)
    ll = float(local["ll_"])
    unpen_ll = ll + penalty
    unpen_score = scale * (g + pg)
    E = int(mapping["E"])
    alpha, lam = float(mapping["a"]), float(mapping["lambda"])
    factors = np.asarray(mapping["penalty_factors"], float)
    scientific_penalty = lam * np.sum(factors * ((1-alpha)*external**2/2 + alpha*np.abs(external)))
    smooth_gradient = -unpen_score/E + lam*(1-alpha)*factors*external
    kkt = np.where(external != 0, np.abs(smooth_gradient + lam*alpha*factors*np.sign(external)),
                   np.maximum(0, np.abs(smooth_gradient)-lam*alpha*factors))
    frozen_kkt = np.where(np.abs(external)>1e-8,
        np.abs(smooth_gradient+lam*alpha*factors*np.sign(external)),
        np.maximum(0,np.abs(smooth_gradient)-lam*alpha*factors))
    info = -h
    finite = all(np.isfinite(x).all() for x in (b,g,h,np.asarray(ll)))
    eigen = np.linalg.eigvalsh(info) if finite else np.full(len(b), np.nan)
    eta = np.asarray(local["X"]) @ b
    # Closure evaluation is diagnostic only; no independent Newton solve is performed.
    fixed_penalty = float(local["elastic_net_penalty"](b, fixed_smoothing))
    mapped_exact_penalty = len(local["X"])*np.sum(np.asarray(model.penalizer)*
        (model.l1_ratio*np.abs(b)+(1-model.l1_ratio)*b*b/2))
    return dict(type="iteration", iteration=int(local["i"]), beta_internal=b,
        beta_external=external, internal_center=center, internal_scale_ddof1=scale,
        backend_penalized_loglik=ll, backend_penalized_gradient_internal=g,
        backend_penalized_hessian_internal=h, backend_penalized_gradient_external=scale*g,
        backend_information_external=scale[:,None]*info*scale[None,:],
        backend_penalty=penalty, backend_penalty_gradient_internal=pg,
        backend_penalty_hessian_diagonal_internal=ph, smoothing=a,
        smooth_backend_objective_per_event=-ll/E,
        fixed_smoothing=fixed_smoothing, fixed_smooth_backend_objective_per_event=(-unpen_ll+fixed_penalty)/E,
        scientific_exact_objective=-unpen_ll/E+scientific_penalty,
        scientific_exact_kkt_residual=kkt, scientific_exact_kkt_inf=float(np.max(kkt)),
        frozen_1e8_kkt_residual=frozen_kkt, frozen_1e8_kkt_inf=float(np.max(frozen_kkt)),
        core_score_inf_per_event=float(np.max(np.abs(unpen_score[factors==0]))/E),
        mapped_backend_exact_objective_per_event=(-unpen_ll+mapped_exact_penalty)/E,
        unpenalized_loglik=unpen_ll, unpenalized_gradient_external=unpen_score,
        step_size=float(local["step_size"]), previous_delta_internal=local["delta"].copy(),
        eta_min=float(np.min(eta)), eta_max=float(np.max(eta)), finite=finite,
        information_eigen_min=float(eigen.min()), information_eigen_max=float(eigen.max()),
        information_condition_2=float(np.linalg.cond(info)) if finite else float('nan'))


def fit_once(X, t, e, mapping, trace_path=None, *, initial_beta_external=None,
             fit_options=None, candidate=None, provenance=None, fixed_smoothing=100.0):
    """One explicit fit; optional durable observation, never candidate selection.

    X is the already externally prepared design. initial_beta_external is in X's
    coefficient coordinates and is converted once to backend ddof=1 coordinates.
    No patient authorization is inferred here; the caller must enforce its scope.
    A supplied trace path must not exist. Returns the native fitted object.
    """
    import pandas as pd
    from lifelines import CoxPHFitter
    X = np.asarray(X, float)
    t, e = np.asarray(t, float), np.asarray(e, int)
    if X.ndim != 2 or t.shape != (len(X),) or e.shape != t.shape:
        raise ValueError("invalid snapshot shapes")
    if mapping['n'] != len(X) or mapping['E'] != int(e.sum()):
        raise ValueError("mapping population mismatch")
    if len(mapping['backend_penalizer']) != X.shape[1] or not np.isin(e,[0,1]).all():
        raise ValueError("invalid factors/event")
    options = dict(precision=1e-10, max_steps=1000, step_size=0.95)
    options.update(fit_options or {})
    frame = pd.DataFrame(X, columns=[f'x{i:03d}' for i in range(X.shape[1])])
    frame['__time'], frame['__event'] = t,e
    model = CoxPHFitter(baseline_estimation_method='breslow',
        penalizer=np.asarray(mapping['backend_penalizer'],float), l1_ratio=mapping['a_backend'])
    initial = None if initial_beta_external is None else np.asarray(initial_beta_external,float)*X.std(axis=0,ddof=1)
    old_trace = sys.gettrace()
    code, before_solve, after_decrement, digest = _binding()
    if trace_path is not None:
        trace_path = Path(trace_path).resolve()
        project_root = Path(__file__).resolve().parents[2]
        if not trace_path.is_relative_to(project_root):
            raise PermissionError('trace output must remain inside Project_B')
        trace_path.parent.mkdir(parents=True,exist_ok=True)
        with trace_path.open('x',encoding='utf-8'):
            pass
        _append(trace_path,dict(type='header',newton_sha256=digest,mapping=mapping,
            candidate=candidate,provenance=provenance,fit_options=options,
            initial_beta_external=initial_beta_external, prior_trace_present=old_trace is not None))
    observer_errors = []
    def observer(fr, event, arg):
        if fr.f_code is not code:
            return None
        try:
            if event == 'line' and fr.f_lineno == before_solve:
                _append(trace_path,_state(fr.f_locals,mapping,fixed_smoothing))
            elif event == 'line' and fr.f_lineno == after_decrement:
                l = fr.f_locals
                _append(trace_path,dict(type='direction',iteration=l['i'],delta_internal=l['delta'].copy(),
                    norm_delta=l['norm_delta'],newton_decrement=l['newton_decrement']))
            elif event == 'return':
                _append(trace_path,dict(type='newton_exit',iteration=fr.f_locals.get('i'),
                    backend_success=fr.f_locals.get('success'), returned=arg is not None))
            elif event == 'exception':
                _append(trace_path,dict(type='backend_exception',error_type=arg[0].__name__,message=str(arg[1])))
        except Exception as error:
            observer_errors.append(repr(error))
        return observer
    caught = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            if trace_path is not None:
                sys.settrace(observer)
            model.fit(frame,duration_col='__time',event_col='__event',initial_point=initial,
                      show_progress=False,fit_options=options)
        return model
    except Exception as error:
        if trace_path is not None:
            _append(trace_path,dict(type='fit_error',error_chain=''.join(traceback.format_exception(error))))
        raise
    finally:
        sys.settrace(old_trace)
        if trace_path is not None:
            _append(trace_path,dict(type='footer',warnings=[dict(category=w.category.__name__,message=str(w.message)) for w in caught],
                observer_errors=observer_errors,trace_restored=sys.gettrace() is old_trace))
        for warning in caught:
            warnings.warn_explicit(warning.message,warning.category,warning.filename,warning.lineno)
