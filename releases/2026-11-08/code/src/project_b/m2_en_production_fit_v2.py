"""EN .25 adapter with compact numerical evidence before any SUCCESS/pickle.

The old adapter, full diagnostic observer and signed files are never edited.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys
import traceback
import warnings

import numpy as np

from .development_en_cox import penalty_mapping
from .m2_en_checked import evaluate_solution, NumericalAcceptanceError
from .m2_en_trace import _binding, _json
from .m0_recovery_execute import durable_json, sha256

FIT_OPTIONS = {'step_size': .25, 'precision': 1e-10, 'max_steps': 1000}


def fit_numeric_once(X, t, e, mapping, directory, *, core_n=20, reference_beta=None,
                     max_steps=1000):
    """Ungated mathematical library, caller owns authorization. No retry/selection.

    max_steps exists for synthetic early-stop tests. The production adapter
    below always uses 1000; step, precision and zero initialization are fixed.
    """
    import pandas as pd
    from lifelines import CoxPHFitter
    from lifelines.exceptions import ConvergenceWarning
    from .a5_m0_cox import predict_native_risk
    x,t,e=np.asarray(X,float),np.asarray(t,float),np.asarray(e,int)
    directory=Path(directory).resolve()
    root=Path(__file__).resolve().parents[2]
    if not directory.is_relative_to(root):raise PermissionError('PROJECT_B_EVIDENCE_ONLY')
    directory.mkdir(parents=True,exist_ok=False)
    columns=[f'x{i:03d}' for i in range(x.shape[1])]
    options={**FIT_OPTIONS,'max_steps':max_steps}
    old_trace=sys.gettrace();exit_state=None;observer_errors=[];caught=[];error=None;model=None;acceptance=None
    code,_,_,digest=_binding()
    def observer(frame,event,arg):
        nonlocal exit_state
        if frame.f_code is not code:return None
        if event=='return':
            try:
                exit_state={'backend_success':frame.f_locals.get('success') is True,
                            'iteration':int(frame.f_locals.get('i',0)),
                            'last_step':float(frame.f_locals.get('step_size',0)),
                            'returned':arg is not None}
            except Exception as exc:observer_errors.append(repr(exc))
        return observer
    try:
        if (x.ndim!=2 or t.shape!=(len(x),) or e.shape!=t.shape or mapping['n']!=len(x)
            or mapping['E']!=int(e.sum()) or list(mapping['penalty_factors'])!=[0]*core_n+[1]*(x.shape[1]-core_n)):
            raise ValueError('EN_INPUT_MAPPING_DRIFT')
        frame=pd.DataFrame(x,columns=columns);frame['__time']=t;frame['__event']=e
        model=CoxPHFitter(baseline_estimation_method='breslow',
            penalizer=np.asarray(mapping['backend_penalizer']),l1_ratio=mapping['a_backend'])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always');sys.settrace(observer)
            try:model.fit(frame,duration_col='__time',event_col='__event',initial_point=None,
                          show_progress=False,fit_options=options)
            finally:sys.settrace(old_trace)
        records=[dict(category=w.category.__name__,message=str(w.message),
                      is_convergence_warning=issubclass(w.category,ConvergenceWarning)) for w in caught]
        acceptance=evaluate_solution(x,t,e,model.params_.to_numpy(),mapping['a'],mapping['lambda'],
            core_n=core_n,backend_success=bool(exit_state and exit_state['backend_success']),
            warning_records=records,trace_complete=not observer_errors and sys.gettrace() is old_trace,
            reference_beta=reference_beta)
        if not acceptance['passed']:raise NumericalAcceptanceError(acceptance)
        native={'model_id':'M2_EN','backend':model,'columns':columns}
        risks=np.asarray(predict_native_risk(native,x))
        if not (np.isfinite(risks).all() and ((risks>=0)&(risks<=1)).all()
                and (risks[:,0]<=risks[:,1]).all()):raise RuntimeError('NATIVE_RISK_INVALID')
    except BaseException:
        error=traceback.format_exc()
    finally:
        sys.settrace(old_trace)
        records=[dict(category=w.category.__name__,message=str(w.message),
                      is_convergence_warning=issubclass(w.category,ConvergenceWarning)) for w in caught]
        evidence=dict(schema_version='1.0',status='ACCEPTED' if error is None else 'REJECTED',
            newton_source_sha256=digest,fit_options=options,zero_initialization=True,
            backend_success=bool(exit_state and exit_state['backend_success']),newton_exit=exit_state,
            observer_errors=observer_errors,trace_restored=sys.gettrace() is old_trace,
            warnings=records,acceptance=acceptance or {'passed':False,'failure_reasons':['FIT_EXCEPTION']},
            mapping=mapping,exception_chain=error,
            risk_definition='frozen right-continuous Breslow step at 90 and 180 days',
            coefficient_alteration=False,automatic_retry=False)
        if model is not None and hasattr(model,'params_'):
            evidence.update(beta_external=model.params_.to_numpy(),internal_center=model._norm_mean.to_numpy(),
                            internal_scale=model._norm_std.to_numpy())
        durable_json(directory/'NUMERICAL_EVIDENCE.json',_json(evidence),exclusive=True)
    if error is not None:
        rejection=NumericalAcceptanceError(evidence['acceptance'])
        rejection.evidence_path=str(directory/'NUMERICAL_EVIDENCE.json')
        rejection.original_exception_chain=error
        raise rejection
    return model,_json(evidence)


def fit_m2_en(matrix,time,event,penalty_factors,candidate,spec):
    """Signed production adapter; no synthetic flag or signature bypass."""
    from .m2_en_production_v2 import validate_authorization, bound_path
    root=Path(spec['project_root']).resolve()
    draft=validate_authorization(root)
    if not {'numerical_evidence_directory','ledger_directory','m0_core_beta'}<=set(spec):
        raise PermissionError('SIGNED_CONTEXT_SPEC_REQUIRED')
    run=bound_path(root,draft['recovery_run_directory'])
    directory=Path(spec['numerical_evidence_directory']).resolve()
    if not directory.is_relative_to(run/'contexts'):raise PermissionError('EVIDENCE_RUN_BOUNDARY')
    x=np.asarray(matrix,float)
    mapping=penalty_mapping(len(time),int(sum(event)),float(candidate['alpha']),float(candidate['lambda']),
                            core_n=20,active_extension_n=x.shape[1]-20)
    if list(penalty_factors)!=mapping['penalty_factors']:raise ValueError('PENALTY_FACTOR_DRIFT')
    anchor=np.r_[np.asarray(spec['m0_core_beta'],float),np.zeros(x.shape[1]-20)]
    if anchor.shape!=(x.shape[1],):raise ValueError('M0_ANCHOR_DIMENSION')
    model,evidence=fit_numeric_once(x,time,event,mapping,directory,reference_beta=anchor)
    proof=directory/'NUMERICAL_EVIDENCE.json'
    ledger_root=Path(spec['ledger_directory']).resolve()
    return {'model_id':'M2_EN','backend':model,'mapping':mapping,'columns':list(model.params_.index),
            'internal_center':model._norm_mean.to_dict(),'internal_scale':model._norm_std.to_dict(),
            'synthetic_only':False,'training_provenance':'M2_EN_SIGNED_PRODUCTION_RECOVERY_V2',
            'numerical_acceptance':evidence['acceptance'],
            'numerical_evidence':{'path':str(proof.relative_to(ledger_root)).replace('\\','/'),
                                  'sha256':sha256(proof)}}


def validate_persisted_numeric(directory,result):
    """Validate before task SUCCESS and on cached reads, not just a passed flag."""
    try:
        proof=result['numerical_evidence'];target=(Path(directory)/proof['path']).resolve()
        if not target.is_relative_to(Path(directory).resolve()) or sha256(target)!=proof['sha256']:
            raise ValueError('proof path/hash')
        evidence=json.loads(target.read_text())
        check=evidence['acceptance']
        from .m2_en_trace import NEWTON_SHA256
        finite_metrics=np.isfinite([check['proximal_gradient_inf'],check['core_score_inf_per_event'],
                                   check['scientific_objective'],*check['reference_objectives'].values()]).all()
        if (evidence['status']!='ACCEPTED' or evidence['backend_success'] is not True
            or evidence['newton_source_sha256']!=NEWTON_SHA256
            or evidence['fit_options']!=FIT_OPTIONS or evidence['zero_initialization'] is not True
            or evidence['trace_restored'] is not True or evidence['observer_errors']!=[]
            or evidence['exception_chain'] is not None
            or any(w.get('is_convergence_warning') or w['category']=='ConvergenceWarning' for w in evidence['warnings'])
            or check['passed'] is not True or check['failure_reasons']!=[]
            or not check['finite'] or not finite_metrics
            or check['proximal_gradient_inf']>1e-5 or check['core_score_inf_per_event']>1e-5
            or check['scientific_objective']>min(check['reference_objectives'].values())+1e-7
            or not np.isfinite(evidence['beta_external']).all()
            or result['numerical_acceptance']!=check):raise ValueError('acceptance proof invalid')
        return evidence
    except Exception as error:raise RuntimeError('NUMERICAL_EVIDENCE_REQUIRED: '+str(error)) from error
