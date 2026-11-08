"""Read-only reconstruction and fixed-point EN diagnostics. No optimizer or fit call.

Writes only new diagnosis artifacts; does not update production or recovery state.
"""
from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
import platform
import sys
import numpy as np
from .development_en_cox import penalty_mapping, _efron_loglik_gradient
from .m0_recovery_diagnostics import write_snapshot, compare_backend, efron_loglik_score_information


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, indent=2, allow_nan=False)


def fixed_penalties(beta, sd, mapping, smoothing):
    """Independent external-coordinate scientific vs n-scaled backend penalties."""
    beta=np.asarray(beta); sd=np.asarray(sd)
    a=mapping['a']; lam=mapping['lambda']; ext=beta[20:]
    scientific=lam*((1-a)*np.dot(ext,ext)/2+a*np.abs(ext).sum())
    theta=beta*sd; q=np.asarray(mapping['backend_penalizer']); ab=mapping['a_backend']
    exact=mapping['n']/mapping['E']*np.sum(q*(ab*np.abs(theta)+(1-ab)*theta**2/2))
    soft=(np.logaddexp(0,-smoothing*theta)+np.logaddexp(0,smoothing*theta))/smoothing
    smooth=mapping['n']/mapping['E']*np.sum(q*(ab*soft+(1-ab)*theta**2/2))
    grad=mapping['n']/mapping['E']*q*(ab*np.tanh(smoothing*theta/2)+(1-ab)*theta)*sd
    hdiag=mapping['n']/mapping['E']*q*(ab*smoothing/2*(1-np.tanh(smoothing*theta/2)**2)+(1-ab))*sd**2
    return dict(scientific=float(scientific),backend_exact=float(exact),backend_smooth=float(smooth),
                mapping_abs_error=float(abs(exact-scientific)),smoothing_excess=float(smooth-exact),
                smooth_gradient=grad.tolist(),smooth_hessian_diagonal=hdiag.tolist())


def spectrum(x):
    s=np.linalg.svd(x,compute_uv=False)
    tol=max(x.shape)*np.finfo(float).eps*s[0]
    return dict(rank=int((s>tol).sum()),shape=list(x.shape),sv_max=float(s[0]),sv_min=float(s[-1]),
                condition=float(s[0]/s[-1]) if s[-1]>0 else None,rank_tolerance=float(tol))


def information_spectrum(info):
    eig=np.linalg.eigvalsh(info)
    return {'eigen_min':float(eig[0]),'eigen_max':float(eig[-1]),
            'positive_definite':bool(eig[0]>0),'condition':float(eig[-1]/eig[0]) if eig[0]>0 else None}


def diagnose(root):
    from .a5_common import make_surv180,generate_inner_folds
    from .a5_selection import conditional_en_lambdas
    from .development_preprocessing import generate_c2_design,fit_preprocessor,transform_preprocessor,load_frozen_preprocessing_spec
    import lifelines, scipy, pandas
    root=Path(root).resolve(); out=root/'audit/development/m2_en_diagnosis_v1'
    out.mkdir(exist_ok=True)
    label='outer_repeat=1|outer_fold=5'
    run=root/'audit/development/recovery_runs/m0_recovery_v1'
    ctx=run/'contexts'/hashlib.sha256(label.encode()).hexdigest()
    # Explicit development allowlist, no temporal table or database read.
    data_names=['DEVELOPMENT_INDEX_OUTCOME.csv','DEVELOPMENT_C2_RAW.csv','DEVELOPMENT_EXTENDED_157.csv','OUTER_FOLD_ASSIGNMENTS.csv']
    paths=[root/'data/a4'/name for name in data_names]
    def read(p):
        with p.open(encoding='utf-8',newline='') as f:return list(csv.DictReader(f))
    index,core,ext,folds=map(read,paths)
    identities=[(int(r['subject_id']),int(r['hadm_id'])) for r in index]
    for rows in (core,ext,folds):
        assert [(int(r['subject_id']),int(r['hadm_id'])) for r in rows]==identities
    converted=[{**r,**{k:int(r[k]) for k in ('death_90d','death_180d','admin_180_eligible')},
                'death_days_after_t0':float(r['death_days_after_t0']) if r['death_days_after_t0'] else None} for r in index]
    times,events=make_surv180(converted)
    training=[i for i,r in enumerate(folds) if int(r['repeat_1_fold'])!=5]
    ids=[identities[i] for i in training]; t=np.array(times)[training]; e=np.array(events)[training]
    core=[{k:(v if k in ('gender','gcsm_category') else float(v) if v else None) for k,v in core[i].items() if k not in ('subject_id','hadm_id')} for i in training]
    ext=[{k:float(v) if v else None for k,v in ext[i].items() if k not in ('subject_id','hadm_id')} for i in training]
    assert len(ids) in (2271,2272) and sum(e)==348
    assignment=np.array(generate_inner_folds(ids,e.tolist(),{'outer_repeat':1,'outer_fold':5},{'master_seed':2026102405}))
    metadata=load_frozen_preprocessing_spec(root)
    ledger=[json.loads(line) for line in (ctx/'attempts.jsonl').read_text().splitlines()]
    statuses={r['task']:r for r in ledger if r['action'] in ('SUCCESS','FAILURE')}
    source_paths=paths+[root/'config'/n for n in ('ALGORITHM_SPEC.json','PREPROCESSING_SPEC.json','INNER_RESAMPLING_SPEC.json','A4_FIELD_DICTIONARY.csv')]
    source_paths+=list((root/'src/project_b').glob('*.py'))
    source_paths+=[ctx/'attempts.jsonl',ctx/'M2_EN_SELECTION.json',run/'ROOT_CAUSE.json']
    source_hashes={str(p.relative_to(root)):sha(p) for p in source_paths if not p.name.startswith('m2_en_')}
    backend_path=Path(lifelines.__file__).parent/'fitters/coxph_fitter.py'
    report={'schema_version':'1.0','patient_fit_count':0,'context':label,'source_hashes':source_hashes,
            'environment':{'python':sys.version,'platform':platform.platform(),'lifelines':lifelines.__version__,'numpy':np.__version__,'scipy':scipy.__version__,'pandas':pandas.__version__,'backend_source_sha256':sha(backend_path)},
            'historical_failures':[{k:r[k] for k in ('task','error_type','message')} for r in ledger if r['action']=='FAILURE' and '|M2_EN|' in r['task']],
            'historical_coefficients_available':False,'folds':[]}
    for fold in range(1,6):
        tr=np.flatnonzero(assignment!=fold).tolist(); va=np.flatnonzero(assignment==fold).tolist()
        key=f'{label}|M0|fixed|inner={fold}'
        p=ctx/'results'/f'{hashlib.sha256(key.encode()).hexdigest()}.json'
        saved=json.loads(p.read_text()); source_hashes[str(p.relative_to(root))]=sha(p)
        assert saved['ids']==[list(ids[i]) for i in va]
        if key in statuses: assert sha(p)==statuses[key]['sha256']
        design,names,design_meta=generate_c2_design(core,tr)
        rows=[dict(zip(names,row)) for row in design]
        prep=fit_preprocessor(rows,ext,tr,{**metadata,'partition_role':'inner_training','model_level':'M2'},'EN')
        x=np.array(transform_preprocessor(prep,rows,ext,tr)); xv=np.array(transform_preprocessor(prep,rows,ext,va))
        beta0=np.r_[saved['core_beta'],np.zeros(x.shape[1]-20)]; tt=t[tr]; ee=e[tr]
        candidates=conditional_en_lambdas(x,tt,ee,saved['core_beta'])
        ll,score,info=efron_loglik_score_information(x,tt,ee,beta0)
        _,_,zero_info=efron_loglik_score_information(x,tt,ee,np.zeros(x.shape[1]))
        sd=x.std(axis=0,ddof=1); xc=x-x.mean(axis=0)
        corr=np.corrcoef(x,rowvar=False); pairs=np.argwhere(np.triu(np.abs(corr)>0.95,1))
        details=[]
        for c in candidates:
            mapping=penalty_mapping(len(x),int(ee.sum()),c['alpha'],c['lambda'],core_n=20,active_extension_n=x.shape[1]-20)
            assert mapping['backend_penalizer'][:20]==[0]*20
            record={**c,'mapping':mapping,'conditional_core_kkt_inf':float(np.max(np.abs(score[:20]))/sum(ee)),
                    'conditional_extension_kkt_inf':float(max(0,np.max(np.abs(score[20:]))/sum(ee)-c['alpha']*c['lambda']))}
            key=f"{label}|M2_EN|{c['declared_order']}|inner={fold}"; status=statuses.get(key)
            record['historical_status']=status['action'] if status else 'SKIPPED'
            if status and status['action']=='SUCCESS':
                p=ctx/'results'/f'{hashlib.sha256(key.encode()).hexdigest()}.json'
                ck=json.loads(p.read_text()); assert sha(p)==status['sha256']
                assert ck['ids']==[list(ids[i]) for i in va]
                assert ck['scientific_lambda']==c['lambda'] and ck['lambda_max']==c['lambda_max']
                record['historical_result_sha256']=sha(p); record['historical_nonzero_count']=ck['nonzero_count']
                record['history_missing_fields']=[k for k in ('beta','mapping','internal_scale','internal_center') if k not in ck]
                source_hashes[str(p.relative_to(root))]=sha(p)
            record['fixed_points']=[]
            record['penalized_information']=[]
            for point,b,information in [('zero',np.zeros(x.shape[1]),zero_info),('conditional_M0',beta0,info)]:
                ridge=np.r_[np.zeros(20),np.repeat(c['lambda']*(1-c['alpha']),x.shape[1]-20)]
                z=fixed_penalties(b,sd,mapping,1.3)
                record['penalized_information'].append({'point':point,
                    'exact_smooth_part':information_spectrum(information/sum(ee)+np.diag(ridge)),
                    'backend_iteration1':information_spectrum(information/sum(ee)+np.diag(z['smooth_hessian_diagonal']))})
            for name,b in [('conditional_M0',beta0),('deterministic_nonzero',beta0+0.01*np.sin(np.arange(x.shape[1])+1))]:
                pointll,_=_efron_loglik_gradient(b,x,tt,ee)
                comparisons=[]
                for iteration in (1,10,50,100):
                    z=fixed_penalties(b,sd,mapping,1.3**iteration)
                    comparisons.append({'iteration':iteration,'scientific_objective':-pointll/sum(ee)+z['scientific'],
                                        'backend_exact_objective':-pointll/sum(ee)+z['backend_exact'],
                                        'backend_smoothed_objective':-pointll/sum(ee)+z['backend_smooth'],
                                        'mapping_abs_error':z['mapping_abs_error'],'smoothing_excess':z['smoothing_excess']})
                record['fixed_points'].append({'point':name,'comparisons':comparisons})
            details.append(record)
        snap=write_snapshot(out/f'inner_{fold}.npz',dict(X=x,t=tt,e=ee,beta0=beta0,Xvalid=xv,
                   train_ids=np.asarray(ids)[tr],valid_ids=np.asarray(ids)[va],column_names=np.asarray(prep['final_columns']),
                   penalty_factors=np.r_[np.zeros(20,dtype=int),np.ones(x.shape[1]-20,dtype=int)]))
        row={'fold':fold,'n':len(x),'event_n':int(sum(ee)),'active_extension_n':x.shape[1]-20,
             'core_design':spectrum(xc[:,:20]),'full_design':spectrum(xc),'conditional_information':spectrum(info),
             'ddof0_sd':x.std(axis=0).tolist(),'internal_sd':sd.tolist(),'variance':x.var(axis=0).tolist(),
             'max_abs':np.max(np.abs(x),axis=0).tolist(),'extreme_columns_abs_gt10':[prep['final_columns'][i] for i in np.flatnonzero(np.max(np.abs(x),axis=0)>10)],
             'exact_duplicate_pairs':[[int(i),int(j)] for i,j in pairs if np.array_equal(x[:,i],x[:,j])],
             'correlation_abs_gt095':[{'columns':[prep['final_columns'][i],prep['final_columns'][j]],'correlation':float(corr[i,j])} for i,j in pairs],
             'snapshot':snap,'preprocessor':prep,'design_metadata':design_meta,'candidates':details,
             'core_conditional_information':information_spectrum(info[:20,:20]/sum(ee)),
             'backend_fixedpoint_comparison':compare_backend(x,tt,ee,beta0),
             'backend_nonzero_comparison':compare_backend(x,tt,ee,beta0+0.01*np.sin(np.arange(x.shape[1])+1))}
        report['folds'].append(row)
        write_json(out/f'inner_{fold}_metadata.json',row)
        print('completed fixed-point fold',fold,flush=True)
    errors=[z['mapping_abs_error'] for f in report['folds'] for c in f['candidates'] for p in c['fixed_points'] for z in p['comparisons']]
    report['maximum_mapping_abs_error']=max(errors)
    report['source_hashes_unchanged']=all(sha(root/p)==h for p,h in source_hashes.items())
    report['status']='D0_FIXEDPOINT_COMPLETE' if max(errors)<1e-10 and report['source_hashes_unchanged'] else 'D0_STOP_INCONSISTENCY'
    write_json(out/'D0_REPORT.json',report)
    return report['status']


if __name__=='__main__':
    print(diagnose(sys.argv[1]))
