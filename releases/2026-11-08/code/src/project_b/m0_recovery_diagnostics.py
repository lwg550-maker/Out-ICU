"""D0 fixed-point Efron and risk-set LP diagnostics. No patient fit entry point."""
from __future__ import annotations
import warnings
import numpy as np


def write_snapshot(path,arrays):
    """Exclusive creation, no object arrays; hashes bind dtype, shape and bytes."""
    import hashlib
    import json
    from pathlib import Path
    arrays={k:np.ascontiguousarray(v) for k,v in arrays.items()}
    if any(v.dtype.hasobject for v in arrays.values()): raise ValueError('object arrays forbidden')
    with Path(path).open('xb') as f: np.savez(f,**arrays)
    return {'path':str(path),'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'arrays':{k:{'shape':list(v.shape),'dtype':v.dtype.str,
                         'sha256':hashlib.sha256(json.dumps([v.dtype.str,list(v.shape)],separators=(',',':')).encode()+v.tobytes()).hexdigest()} for k,v in arrays.items()}}


def efron_loglik_score_information(X, t, e, beta):
    """Independent moment sums, including deaths/censoring on the same day."""
    X=np.asarray(X,float); t=np.asarray(t,float); e=np.asarray(e,bool); beta=np.asarray(beta,float)
    p=X.shape[1]; score=np.zeros(p); info=np.zeros((p,p)); ll=0.
    eta=X@beta
    for day in np.unique(t[e]):
        risk=t>=day; deaths=(t==day)&e; d=int(deaths.sum())
        shift=float(eta[risk].max())
        weights=np.exp(eta[risk]-shift); Z=X[risk]
        dw=np.exp(eta[deaths]-shift); D=X[deaths]
        s0=weights.sum(); s1=weights@Z; s2=Z.T@(weights[:,None]*Z)
        q0=dw.sum(); q1=dw@D; q2=D.T@(dw[:,None]*D)
        ll+=float(eta[deaths].sum()); score+=D.sum(axis=0)
        for k in range(d):
            f=k/d; den=s0-f*q0; mean=(s1-f*q1)/den
            ll-=np.log(den)+shift; score-=mean
            info+=(s2-f*q2)/den-np.outer(mean,mean)
    return float(ll),score,(info+info.T)/2


def compare_backend(X,t,e,beta):
    """Map ddof=1 internal coordinates back to external ddof=0 design."""
    import pandas as pd
    from lifelines.fitters.coxph_fitter import SemiParametricPHFitter
    X=np.asarray(X,float); t=np.asarray(t,float); e=np.asarray(e,bool); beta=np.asarray(beta,float)
    sd=X.std(axis=0,ddof=1); mean=X.mean(axis=0)
    order=np.lexsort((e.astype(int),t))
    Z=(X-mean)/sd
    h,g,ll=SemiParametricPHFitter()._get_efron_values_single(
        pd.DataFrame(Z[order]),pd.Series(t[order]),pd.Series(e[order]),
        pd.Series(np.ones(len(t))),None,beta*sd)
    manual=efron_loglik_score_information(X,t,e,beta)
    backend=(ll,g*sd,-h*sd[:,None]*sd[None,:])
    residual={}
    for name,a,b in zip(('loglik','score','information'),manual,backend):
        absolute=float(np.max(np.abs(np.asarray(a)-b)))
        residual[name]={'absolute':absolute,'relative':absolute/max(1.,float(np.max(np.abs(a))))}
    return {'passed':all(v['absolute']<=1e-8 or v['relative']<=1e-10 for v in residual.values()),
            'residuals':residual,'internal_ddof':1,'external_ddof':0,
            'internal_sd':sd.tolist(),'coordinate_mapping':'beta_internal=sd*beta_external; g_external=sd*g_internal; I_external=diag(sd)*I_internal*diag(sd)'}


def riskset_contrasts(X,t,e):
    X=np.asarray(X,float); t=np.asarray(t,float); e=np.asarray(e,bool)
    return np.concatenate([X[i]-X[t>=t[i]] for i in np.flatnonzero(e)],axis=0)


def riskset_direction(X,t,e):
    """Constraint generation with a complete final scan and relaxation dual bound."""
    from scipy.optimize import linprog, OptimizeWarning
    A=riskset_contrasts(X,t,e); n,p=A.shape
    mean=A.mean(axis=0); c=np.r_[-mean,mean]
    selected=np.unique(np.linspace(0,n-1,min(n,1000),dtype=int))
    for iteration in range(100):
        subset=A[selected]
        B=np.vstack([np.c_[-subset,subset],np.ones((1,2*p))])
        b=np.r_[np.zeros(len(subset)),1.]
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore',message='Unrecognized options detected',category=OptimizeWarning)
            result=linprog(c,A_ub=B,b_ub=b,bounds=(0,None),method='highs',
                           options={'threads':1,'time_limit':120.,'dual_feasibility_tolerance':1e-9,'primal_feasibility_tolerance':1e-9})
        if not result.success:
            return {'classification':'INCONCLUSIVE','solver_status':int(result.status),'message':result.message,'constraint_n':n}
        v=result.x[:p]-result.x[p:]; margins=A@v
        violated=np.flatnonzero(margins < -1e-9)
        if not len(violated): break
        worst=violated[np.argsort(margins[violated])[:1000]]
        updated=np.union1d(selected,worst)
        if len(updated)==len(selected): break
        selected=updated
    y=result.ineqlin.marginals
    dual_residual=float(max(0.,np.max(B.T@y-c)))
    raw_dual=float(-b@y); bound=raw_dual+dual_residual
    violation=float(max(0.,-margins.min())); strict=float(margins.max()); objective=float(margins.mean())
    classification='INCONCLUSIVE'
    if violation<=1e-8 and strict>1e-7 and objective>1e-7 and np.abs(v).sum()<=1+1e-8:
        classification='MONOTONE_LIKELIHOOD_EVIDENCE'
    elif violation<=1e-8 and abs(bound)<=1e-12 and strict<=1e-12:
        classification='NO_STRICT_DIRECTION_DETECTED'
    return {'classification':classification,'direction':v.tolist(),'constraint_n':n,
            'constraints_in_final_lp':len(subset),'constraint_generation_rounds':iteration+1,
            'full_constraint_scan':True,'worst_margin':float(margins.min()),'violation':violation,
            'strict_margin':strict,'strict_constraint_index':int(np.argmax(margins)),
            'l1_norm':float(np.abs(v).sum()),'primal_objective':objective,
            'raw_dual_upper_bound':raw_dual,'dual_upper_bound':bound,'dual_stationarity_error':dual_residual,
            'primal_dual_gap':bound-objective,'solver_status':int(result.status),'solver_message':result.message,
            'threads':1,'riskset_definition':'T_j >= T_i, including same-day censor and mutual death ties',
            'negative_result_does_not_prove_finite_MLE':True}
