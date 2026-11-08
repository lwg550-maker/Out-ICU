"""Shared standardized-core ridge for M0/M2_EN, fixed before new patient fits."""
from __future__ import annotations
import hashlib
import inspect
import json
from pathlib import Path
import sys
import traceback
import warnings

import numpy as np

from .development_en_cox import penalty_mapping,_efron_loglik_gradient
from .m0_recovery_execute import durable_json,sha256
from .m2_en_trace import _json

CORE_LAMBDA=1e-4
FIT_OPTIONS={'step_size':.25,'precision':1e-10,'max_steps':1000}
M0_FIT_OPTIONS={'step_size':.25,'precision':1e-14,'r_precision':1e-15,'max_steps':1000}


def core_internal_penalty(n,E,sd,core_n):
    sd=np.asarray(sd,float)
    return E/n*CORE_LAMBDA/sd**2*(np.arange(len(sd))<core_n)


def objective_gradient(beta,x,t,e,alpha=0.,scientific_lambda=0.,*,core_n=20):
    beta=np.asarray(beta,float);E=int(np.sum(e))
    ll,score=_efron_loglik_gradient(beta,x,t,e)
    ext=beta[core_n:]
    objective=-ll/E+CORE_LAMBDA*(beta[:core_n]@beta[:core_n])/2
    objective+=scientific_lambda*((1-alpha)*(ext@ext)/2+alpha*np.abs(ext).sum())
    gradient=-score/E
    gradient[:core_n]+=CORE_LAMBDA*beta[:core_n]
    gradient[core_n:]+=scientific_lambda*((1-alpha)*ext+alpha*np.sign(ext))
    return float(objective),gradient


def acceptance(beta,x,t,e,alpha,lam,core_n,backend_success,records,observer_ok,reference_beta):
    beta=np.asarray(beta,float)
    objective,gradient=objective_gradient(beta,x,t,e,alpha,lam,core_n=core_n)
    smooth=gradient.copy();smooth[core_n:]-=lam*alpha*np.sign(beta[core_n:])
    z=beta[core_n:]-smooth[core_n:]
    prox=smooth.copy();prox[core_n:]=beta[core_n:]-np.sign(z)*np.maximum(np.abs(z)-lam*alpha,0)
    exact=np.abs(gradient)
    exact[core_n:]=np.where(beta[core_n:]!=0,exact[core_n:],np.maximum(np.abs(smooth[core_n:])-lam*alpha,0))
    refs={'zero':objective_gradient(np.zeros_like(beta),x,t,e,alpha,lam,core_n=core_n)[0]}
    if reference_beta is not None:
        refs['matched_shared_ridge_M0']=objective_gradient(reference_beta,x,t,e,alpha,lam,core_n=core_n)[0]
    finite=bool(np.isfinite(beta).all() and np.isfinite([objective,*gradient,*refs.values()]).all())
    why=[]
    if not backend_success:why.append('BACKEND_NONCONVERGENCE')
    if any(w['category']=='ConvergenceWarning' for w in records):why.append('CONVERGENCE_WARNING')
    if not observer_ok:why.append('OBSERVER_FAILURE')
    if not finite:why.append('NONFINITE')
    if np.abs(prox).max()>1e-5:why.append('SCIENTIFIC_STATIONARITY')
    if np.abs(gradient[:core_n]).max()>1e-5:why.append('PENALIZED_CORE_SCORE')
    if objective>min(refs.values())+1e-7:why.append('OBJECTIVE_REFERENCE')
    return {'passed':not why,'failure_reasons':why,'finite':finite,'scientific_objective':objective,
            'proximal_gradient_inf':float(np.abs(prox).max()),'penalized_core_score_per_event_inf':float(np.abs(gradient[:core_n]).max()),
            'exact_kkt_inf':float(exact.max()),'reference_objectives':refs,'core_ridge_lambda':CORE_LAMBDA}


def fit_numeric_once(x,t,e,directory,*,alpha=0.,scientific_lambda=0.,core_n=20,reference_beta=None,max_steps=1000):
    """One ungated mathematical call. Production wrappers always validate new signature."""
    import pandas as pd
    from .shared_core_solver_v3 import SharedRidgeCoxPHFitter,SharedRidgeSemiParametric
    from .a5_m0_cox import predict_native_risk
    x,t,e=np.asarray(x,float),np.asarray(t,float),np.asarray(e,int)
    path=Path(directory).resolve();root=Path(__file__).resolve().parents[2]
    if not path.is_relative_to(root):raise PermissionError('PROJECT_B_ONLY')
    path.mkdir(parents=True,exist_ok=False)
    mapping=penalty_mapping(len(x),int(e.sum()),alpha,scientific_lambda,core_n=core_n,active_extension_n=x.shape[1]-core_n)
    model_id='M0' if x.shape[1]==core_n and scientific_lambda==0 else 'M2_EN'
    options={**(M0_FIT_OPTIONS if model_id=='M0' else FIT_OPTIONS),'max_steps':max_steps}
    model=SharedRidgeCoxPHFitter(core_lambda=CORE_LAMBDA,core_n=core_n,
        penalizer=np.asarray(mapping['backend_penalizer']),l1_ratio=mapping['a_backend']) if scientific_lambda>0 else SharedRidgeCoxPHFitter(
        core_lambda=CORE_LAMBDA,core_n=core_n,penalizer=np.zeros(x.shape[1]),l1_ratio=0.)
    code=SharedRidgeSemiParametric._newton_raphson_for_efron_model.__code__
    old_trace=sys.gettrace();exit_state=None;records=[];check=None;error=None
    def observer(frame,event,arg):
        nonlocal exit_state
        if frame.f_code is not code:return None
        if event=='return':exit_state={'success':frame.f_locals.get('success') is True,'iteration':frame.f_locals.get('i')}
        return observer
    try:
        if not (x.ndim==2 and x.shape[1]>=core_n and np.isfinite(x).all()
                and np.isin(e,[0,1]).all() and e.sum()>0 and np.allclose(x.std(0),1,atol=1e-10)):
            raise ValueError('TRAINING_STANDARDIZATION_OR_INPUT_DRIFT')
        frame=pd.DataFrame(x,columns=[f'x{i:03d}' for i in range(x.shape[1])]);frame['__time']=t;frame['__event']=e
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always');sys.settrace(observer)
            try:model.fit(frame,duration_col='__time',event_col='__event',initial_point=None,show_progress=False,
                          fit_options=options)
            finally:sys.settrace(old_trace)
        records=[{'category':w.category.__name__,'message':str(w.message)} for w in caught]
        beta=model.params_.to_numpy()
        check=acceptance(beta,x,t,e,alpha,scientific_lambda,core_n,bool(exit_state and exit_state['success']),
                         records,sys.gettrace() is old_trace,reference_beta)
        if not check['passed']:raise RuntimeError('RIDGE_NUMERICAL_REJECTION: '+','.join(check['failure_reasons']))
        predict_native_risk({'model_id':'M0','backend':model,'columns':list(model.params_.index)},x)
    except BaseException:
        error=traceback.format_exc()
        if 'caught' in locals():records=[{'category':w.category.__name__,'message':str(w.message)} for w in caught]
    finally:
        sys.settrace(old_trace)
        report={'status':'ACCEPTED' if error is None else 'REJECTED','model_id':model_id,'core_ridge_lambda':CORE_LAMBDA,
            'core_penalty_type':'L2_ONLY','extension_mapping':mapping,'newton_exit':exit_state,
            'core_internal_q':core_internal_penalty(len(x),int(e.sum()),x.std(0,ddof=1),core_n),
            'acceptance':check or {'passed':False,'failure_reasons':['FIT_EXCEPTION']},'warnings':records,
            'exception_chain':error,'fit_options':options,
            'trace_restored':sys.gettrace() is old_trace,
            'solver_sha256':sha256(Path(__file__).with_name('shared_core_solver_v3.py'))}
        if hasattr(model,'params_'):report['beta_external']=model.params_.to_numpy()
        durable_json(path/'NUMERICAL_EVIDENCE.json',_json(report),exclusive=True)
    if error:raise RuntimeError('RIDGE_NUMERICAL_REJECTION: '+str(report['acceptance']['failure_reasons']))
    return model,_json(report)


def _production(x,t,e,spec,alpha=0.,lam=0.,core_beta=None,model_id='M0'):
    from .shared_core_production_v3 import ROOT,RUN_REL,validate_authorization
    validate_authorization(spec['project_root'])
    directory=Path(spec['numerical_evidence_directory']).resolve()
    if not directory.is_relative_to(ROOT/RUN_REL/'contexts'):raise PermissionError('SIGNED_RUN_ONLY')
    anchor=None if core_beta is None else np.r_[core_beta,np.zeros(len(x[0])-20)]
    model,proof=fit_numeric_once(x,t,e,directory,alpha=alpha,scientific_lambda=lam,reference_beta=anchor)
    evidence={'path':str((directory/'NUMERICAL_EVIDENCE.json').relative_to(spec['ledger_directory'])).replace('\\','/'),
              'sha256':sha256(directory/'NUMERICAL_EVIDENCE.json')}
    return {'model_id':model_id,'backend':model,'columns':list(model.params_.index),
        'numerical_acceptance':proof['acceptance'],'numerical_evidence':evidence,
        'core_ridge_lambda':CORE_LAMBDA,'acceptance_diagnostics':proof['acceptance'],'synthetic_only':False,
        'internal_center':model._norm_mean.to_dict(),'internal_scale':model._norm_std.to_dict()}


def fit_m0(x,t,e,spec):return _production(x,t,e,spec)
def fit_m2_en(x,t,e,factors,candidate,spec):
    if list(factors)!=[0]*20+[1]*(len(x[0])-20):raise ValueError('EXTENSION_PENALTY_SELECTOR_DRIFT')
    return _production(x,t,e,spec,float(candidate['alpha']),float(candidate['lambda']),spec['m0_core_beta'],'M2_EN')


def validate_persisted_numeric(directory,result):
    try:
        proof=result['numerical_evidence'];path=(Path(directory)/proof['path']).resolve()
        if not path.is_relative_to(Path(directory).resolve()) or sha256(path)!=proof['sha256']:raise ValueError('proof hash')
        report=json.loads(path.read_text());check=report['acceptance']
        if (report['status']!='ACCEPTED' or report['core_ridge_lambda']!=CORE_LAMBDA
            or report['core_penalty_type']!='L2_ONLY' or report['fit_options']!=(M0_FIT_OPTIONS if report['model_id']=='M0' else FIT_OPTIONS)
            or report['trace_restored'] is not True or report['exception_chain'] is not None
            or report['newton_exit']['success'] is not True or check['passed'] is not True
            or report['solver_sha256']!=sha256(Path(__file__).with_name('shared_core_solver_v3.py'))
            or check!=result['numerical_acceptance'] or check['failure_reasons']
            or any(w['category']=='ConvergenceWarning' for w in report['warnings'])
            or not np.isfinite([check['proximal_gradient_inf'],check['penalized_core_score_per_event_inf'],check['scientific_objective']]).all()
            or check['proximal_gradient_inf']>1e-5 or check['penalized_core_score_per_event_inf']>1e-5):raise ValueError('numerical proof')
        return report
    except Exception as exc:raise RuntimeError('NUMERICAL_EVIDENCE_REQUIRED: '+str(exc)) from exc
