"""Authorization routing only; numerical fitting is exactly the existing V4 API."""
from pathlib import Path
import numpy as np
from .en_checked_v4 import fit_numeric_once, validate_persisted_numeric
from .m0_recovery_execute import sha256


def _production(x,t,e,spec,alpha=0.,lam=0.,core_beta=None,model_id='M0'):
    from .io_resume_v5 import ROOT,RUN_REL,validate_authorization
    validate_authorization(spec['project_root'])
    directory=Path(spec['numerical_evidence_directory']).resolve()
    if not directory.is_relative_to(ROOT/RUN_REL/'contexts'):
        raise PermissionError('AUTHORIZED_RESUME_RUN_ONLY')
    anchor=None if core_beta is None else np.r_[core_beta,np.zeros(len(x[0])-20)]
    model,proof=fit_numeric_once(x,t,e,directory,alpha=alpha,
                               scientific_lambda=lam,reference_beta=anchor)
    return {'model_id':model_id,'backend':model,'columns':list(model.params_.index),
        'numerical_acceptance':proof['acceptance'],
        'numerical_evidence':{'path':str((directory/'NUMERICAL_EVIDENCE.json').relative_to(
            spec['ledger_directory'])).replace('\\','/'),'sha256':sha256(directory/'NUMERICAL_EVIDENCE.json')},
        'core_ridge_lambda':proof['core_ridge_lambda'],'acceptance_diagnostics':proof['acceptance'],
        'synthetic_only':False,'internal_center':model._norm_mean.to_dict(),
        'internal_scale':model._norm_std.to_dict()}


def fit_m0(x,t,e,spec):
    return _production(x,t,e,spec)


def fit_m2_en(x,t,e,factors,candidate,spec):
    if list(factors)!=[0]*20+[1]*(len(x[0])-20):
        raise ValueError('EXTENSION_PENALTY_SELECTOR_DRIFT')
    return _production(x,t,e,spec,float(candidate['alpha']),float(candidate['lambda']),
                       spec['m0_core_beta'],'M2_EN')
