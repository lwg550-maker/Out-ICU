"""Versioned same-objective EN recovery adapter. Original M0 and evidence kept."""
from __future__ import annotations

import json
from pathlib import Path
import traceback
import warnings

import numpy as np
import pandas as pd

from .development_en_cox import penalty_mapping
from .shared_core_ridge_v3 import (
    CORE_LAMBDA, FIT_OPTIONS, acceptance,
    fit_numeric_once as fit_v3_once, validate_persisted_numeric as validate_v3)
from .m0_recovery_execute import durable_json, sha256
from .m2_en_trace import _json
from .en_solver_v4 import SafeguardedCoxPHFitter, SHARPNESS_MAX, INTERNAL_PROX_TOL


def fit_numeric_once(x, t, e, directory, *, alpha=0., scientific_lambda=0.,
                     core_n=20, reference_beta=None, max_steps=1000):
    if scientific_lambda == 0:
        return fit_v3_once(x, t, e, directory, core_n=core_n,
                           reference_beta=reference_beta, max_steps=max_steps)
    from .a5_m0_cox import predict_native_risk
    x, t, e = np.asarray(x, float), np.asarray(t, float), np.asarray(e, int)
    root = Path(__file__).resolve().parents[2]
    path = Path(directory).resolve()
    if not path.is_relative_to(root):
        raise PermissionError('PROJECT_B_ONLY')
    path.mkdir(parents=True, exist_ok=False)
    mapping = penalty_mapping(len(x), int(e.sum()), alpha, scientific_lambda,
                              core_n=core_n, active_extension_n=x.shape[1]-core_n)
    options = {**FIT_OPTIONS, 'max_steps':max_steps}
    model = SafeguardedCoxPHFitter(core_lambda=CORE_LAMBDA, core_n=core_n,
        penalizer=np.asarray(mapping['backend_penalizer']), l1_ratio=mapping['a_backend'])
    check, error, caught = None, None, []
    try:
        if not (x.ndim == 2 and x.shape[1] >= core_n and len(x) == len(t) == len(e)
                and np.isfinite(x).all() and np.isfinite(t).all()
                and np.isin(e, [0, 1]).all() and e.sum() > 0
                and np.allclose(x.std(0), 1, atol=1e-10)):
            raise ValueError('TRAINING_STANDARDIZATION_OR_INPUT_DRIFT')
        frame = pd.DataFrame(x, columns=[f'x{i:03d}' for i in range(x.shape[1])])
        frame['__time'], frame['__event'] = t, e
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            model.fit(frame, duration_col='__time', event_col='__event',
                      initial_point=None, show_progress=False, fit_options=options)
        records = [{'category':w.category.__name__, 'message':str(w.message)} for w in caught]
        check = acceptance(model.params_.to_numpy(), x, t, e, alpha, scientific_lambda,
                           core_n, model._model.numerical_success_, records, True, reference_beta)
        if not check['passed']:
            raise RuntimeError('RIDGE_NUMERICAL_REJECTION: '+','.join(check['failure_reasons']))
        predict_native_risk({'model_id':'M0', 'backend':model,
                             'columns':list(model.params_.index)}, x)
    except BaseException:
        error = traceback.format_exc()
    records = [{'category':w.category.__name__, 'message':str(w.message)} for w in caught]
    backend = model.__dict__.get('_model')
    trace = getattr(backend, 'numerical_trace_', [])
    proof = {'status':'ACCEPTED' if error is None else 'REJECTED',
        'solver_revision':'SAFEGUARDED_EN_V4', 'model_id':'M2_EN',
        'core_ridge_lambda':CORE_LAMBDA, 'core_penalty_type':'L2_ONLY',
        'extension_mapping':mapping, 'newton_exit':{
            'success':bool(getattr(backend, 'numerical_success_', False)), 'iteration':len(trace)},
        'acceptance':check or {'passed':False, 'failure_reasons':['FIT_EXCEPTION']},
        'warnings':records, 'exception_chain':error, 'fit_options':options,
        'scientific_objective_unchanged':True, 'sharpness_max':SHARPNESS_MAX,
        'internal_prox_tolerance':INTERNAL_PROX_TOL, 'numerical_trace':trace,
        'solver_sha256':sha256(Path(__file__).with_name('en_solver_v4.py'))}
    if backend is not None and hasattr(backend, 'params_'):
        proof['beta_external'] = model.params_.to_numpy()
    durable_json(path/'NUMERICAL_EVIDENCE.json', _json(proof), exclusive=True)
    if error:
        raise RuntimeError('RIDGE_NUMERICAL_REJECTION: '+str(proof['acceptance']['failure_reasons']))
    return model, _json(proof)


def _production(x, t, e, spec, alpha=0., lam=0., core_beta=None, model_id='M0'):
    from .en_production_v4 import ROOT, RUN_REL, validate_authorization
    validate_authorization(spec['project_root'])
    directory = Path(spec['numerical_evidence_directory']).resolve()
    if not directory.is_relative_to(ROOT/RUN_REL/'contexts'):
        raise PermissionError('SIGNED_RUN_ONLY')
    anchor = None if core_beta is None else np.r_[core_beta, np.zeros(len(x[0])-20)]
    model, proof = fit_numeric_once(x, t, e, directory, alpha=alpha,
                                    scientific_lambda=lam, reference_beta=anchor)
    return {'model_id':model_id, 'backend':model, 'columns':list(model.params_.index),
        'numerical_acceptance':proof['acceptance'],
        'numerical_evidence':{'path':str((directory/'NUMERICAL_EVIDENCE.json').relative_to(
            spec['ledger_directory'])).replace('\\', '/'), 'sha256':sha256(directory/'NUMERICAL_EVIDENCE.json')},
        'core_ridge_lambda':CORE_LAMBDA, 'acceptance_diagnostics':proof['acceptance'],
        'synthetic_only':False, 'internal_center':model._norm_mean.to_dict(),
        'internal_scale':model._norm_std.to_dict()}


def fit_m0(x, t, e, spec):
    return _production(x, t, e, spec)


def fit_m2_en(x, t, e, factors, candidate, spec):
    if list(factors) != [0]*20+[1]*(len(x[0])-20):
        raise ValueError('EXTENSION_PENALTY_SELECTOR_DRIFT')
    return _production(x, t, e, spec, float(candidate['alpha']),
                        float(candidate['lambda']), spec['m0_core_beta'], 'M2_EN')


def validate_persisted_numeric(directory, result):
    try:
        item = result['numerical_evidence']
        path = (Path(directory)/item['path']).resolve()
        if not path.is_relative_to(Path(directory).resolve()) or sha256(path) != item['sha256']:
            raise ValueError('proof hash')
        proof = json.loads(path.read_text())
        if proof.get('solver_revision') != 'SAFEGUARDED_EN_V4':
            return validate_v3(directory, result)
        check = proof['acceptance']
        if (proof['status'] != 'ACCEPTED' or proof['core_ridge_lambda'] != CORE_LAMBDA
                or proof['core_penalty_type'] != 'L2_ONLY' or proof['fit_options'] != FIT_OPTIONS
                or proof['exception_chain'] is not None or proof['newton_exit']['success'] is not True
                or proof['solver_sha256'] != sha256(Path(__file__).with_name('en_solver_v4.py'))
                or check != result['numerical_acceptance'] or not check['passed'] or check['failure_reasons']
                or any(w['category'] == 'ConvergenceWarning' for w in proof['warnings'])
                or proof['sharpness_max'] != SHARPNESS_MAX
                or proof['internal_prox_tolerance'] != INTERNAL_PROX_TOL
                or not np.isfinite([check['proximal_gradient_inf'],
                    check['penalized_core_score_per_event_inf'], check['scientific_objective']]).all()
                or check['proximal_gradient_inf'] > 1e-5
                or check['penalized_core_score_per_event_inf'] > 1e-5):
            raise ValueError('numerical acceptance')
        return proof
    except Exception as exc:
        raise RuntimeError('NUMERICAL_EVIDENCE_REQUIRED: '+str(exc)) from exc
