"""Two explicitly selected default-setting diagnostic calls; never production resume."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import time
import traceback

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from project_b.m0_recovery_execute import (sha256,durable_json,append_event,read_events,
    bound_path,check_budget,disk_used,supervise)

REL='audit/development/m2_en_diagnosis_v1'
CASES={'F1':(5,1),'S1':(1,10)}
SOURCES={'src/project_b/'+n+'.py' for n in ('m2_en_diagnosis_execute','m2_en_trace',
    'm0_recovery_execute','development_resources','development_en_cox','recovery_production')}
SOURCES|={'tests/test_m2_en_diagnosis_execute.py','tests/test_m2_en_trace.py'}
INPUTS={REL+'/'+n for n in ('DIAGNOSIS_SCOPE.json','D0_REPORT.json','inner_1.npz',
    'inner_5.npz','inner_1_metadata.json','inner_5_metadata.json')}

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def validate_gate(root):
    root=Path(root).resolve(); out=root/REL
    review=read(out/'INDEPENDENT_PREFIT_DIAGNOSTIC_REVIEW.json')
    if review.get('status')!='PASS' or review.get('mapping_defect') is not False:
        raise RuntimeError('INDEPENDENT_REVIEW_REQUIRED')
    for key,required in [('source_hashes',SOURCES),('binding_sha256',INPUTS)]:
        bindings=review.get(key,{})
        if not required<=set(bindings): raise RuntimeError('REVIEW_BINDING_COVERAGE')
        for name,digest in bindings.items():
            if sha256(bound_path(root,name))!=digest.lower(): raise RuntimeError('REVIEW_HASH_DRIFT: '+name)
    scope=read(out/'DIAGNOSIS_SCOPE.json')
    required={'researcher_exact_request':'请完成针对M2_EN的失败路径做数值诊断。',
        'historical_actual_calls_at_entry':54,'diagnostic_optimization_maximum':2,
        'default_settings_only_for_reproduction':True,'per_call_wall_seconds_maximum':600,
        'diagnosis_cpu_seconds_maximum':14400,'diagnosis_wall_seconds_maximum':7200,
        'diagnosis_new_disk_bytes_maximum':2147483648,'physical_attempt_ceiling_unchanged':15212,
        'temporal_access':False,'database_access':False,'patient_deletion':False,'grid_or_objective_changes':False}
    if any(scope.get(k)!=v for k,v in required.items()): raise RuntimeError('SCOPE_DRIFT')
    d0=read(out/'D0_REPORT.json')
    if (d0.get('status')!='D0_FIXEDPOINT_COMPLETE' or d0.get('source_hashes_unchanged') is not True
        or not 0<=d0.get('maximum_mapping_abs_error',float('inf'))<1e-10):
        raise RuntimeError('D0_MAPPING_HOLD')
    for name,digest in d0['source_hashes'].items():
        if sha256(bound_path(root,name))!=digest: raise RuntimeError('D0_SOURCE_DRIFT')
    from project_b.recovery_production import validate_recovery_authorization
    validate_recovery_authorization(root)  # Read-only verification of existing signed evidence.
    budget=review.get('stage_budget_accounting',{})
    if not budget.get('basis'): raise RuntimeError('BUDGET_BASIS_REQUIRED')
    check_budget(budget,disk_used(out))
    return review

def validate_case(case,events):
    if case not in CASES: raise RuntimeError('DEFAULT_CASES_ONLY')
    starts=[r for r in events if r['action']=='START']
    terminals={r['case'] for r in events if r['action']=='FINAL_STOPPED'}
    if any(r['case']==case for r in starts): raise RuntimeError('DUPLICATE_CASE')
    if len(starts)>=2: raise RuntimeError('TWO_CALL_CEILING')
    if any(r['case'] not in terminals for r in starts): raise RuntimeError('UNRESOLVED_CALL_HOLD')
    if case=='S1' and 'F1' not in terminals: raise RuntimeError('F1_FIRST')

def claim(out,case,review_hash):
    events=read_events(Path(out)/'DIAGNOSTIC_CALLS.jsonl'); validate_case(case,events)
    directory=Path(out)/case; directory.mkdir()
    calls=1+sum(r['action']=='START' for r in events)
    append_event(Path(out)/'DIAGNOSTIC_CALLS.jsonl',dict(action='START',case=case,
        review_sha256=review_hash,historical_actual_calls=54,diagnostic_calls=calls,
        total_actual_calls=54+calls,default_zero_initialization=True,step_size=.95))
    return directory

def validate_necessity(out,case):
    if case!='S1': return
    decision=read(Path(out)/'S1_NECESSITY.json')
    if (decision.get('status')!='PROCEED' or decision.get('no_mapping_defect') is not True
        or not decision.get('rationale') or decision.get('basis') not in
        ('FAILURE_MECHANISM_REPRODUCED','RETURNED_CONVERGENCE_REQUIRES_SCIENTIFIC_AUDIT')):
        raise RuntimeError('S1_NECESSITY_REQUIRED')
    for name in ('TRACE.jsonl','RESULT.json','FINAL_STOPPED.json'):
        if decision.get('F1_hashes',{}).get(name)!=sha256(Path(out)/'F1'/name):
            raise RuntimeError('S1_F1_EVIDENCE_DRIFT')

def execute_case(root,case):
    root=Path(root).resolve()
    if root!=Path(__file__).resolve().parents[2]: raise RuntimeError('PROJECT_B_ONLY')
    out=root/REL; review=validate_gate(root)
    lock=out/'DIAGNOSTIC_CONTROLLER.lock'
    with lock.open('x') as handle:
        handle.write(str(os.getpid()));handle.flush();os.fsync(handle.fileno())
    claimed=False; finished=False; start=time.monotonic(); cpu_start=time.process_time()
    try:
        events=read_events(out/'DIAGNOSTIC_CALLS.jsonl');validate_case(case,events)
        validate_necessity(out,case)
        spent=dict(review['stage_budget_accounting'])
        for event in events:
            if event['action']=='FINAL_STOPPED':
                r=event.get('resources')
                if not r or r.get('accounting_cpu_seconds') is None: raise RuntimeError('RESOURCE_TAIL_HOLD')
                spent['wall_seconds']+=r['accounting_wall_seconds'];spent['cpu_seconds']+=r['accounting_cpu_seconds']
                for name,digest in event['artifact_hashes'].items():
                    if sha256(bound_path(out/event['case'],name))!=digest: raise RuntimeError('PRIOR_ARTIFACT_DRIFT')
        wall_limit,cpu_limit=check_budget(spent,disk_used(out))
        directory=claim(out,case,sha256(out/'INDEPENDENT_PREFIT_DIAGNOSTIC_REVIEW.json'));claimed=True
        cfg={'root':str(root),'case':case,'review_sha256':sha256(out/'INDEPENDENT_PREFIT_DIAGNOSTIC_REVIEW.json')}
        durable_json(directory/'REQUEST.json',cfg,exclusive=True)
        resources=None;error=None
        try:
            resources=supervise([str(root/'.venv-a5/Scripts/python.exe'),'-B',str(Path(__file__).resolve()),
                '--child',str(directory/'REQUEST.json')],directory,wall_limit=wall_limit,cpu_limit=cpu_limit,disk_root=out)
            validate_gate(root)
        except BaseException:
            error=traceback.format_exc()
        if resources:
            resources['accounting_wall_seconds']=time.monotonic()-start+1
            resources['accounting_cpu_seconds']=resources['child_cpu_seconds']+time.process_time()-cpu_start+1
            resources['serialization_reserve_seconds']=1
        if not (directory/'RESULT.json').exists():
            durable_json(directory/'RESULT.json',dict(status='NO_BACKEND_RETURN',exception_chain=error,
                resources=resources,diagnostic_only=True),exclusive=True)
        terminal=dict(action='FINAL_STOPPED',case=case,resources=resources,exception_chain=error,
            outcome='BACKEND_RETURNED' if resources and resources['exit_code']==0 and error is None else 'DIAGNOSTIC_FAILURE',
            scientific_acceptance=False,total_actual_calls=54+sum(r['action']=='START' for r in read_events(out/'DIAGNOSTIC_CALLS.jsonl')),
            artifact_hashes={p.name:sha256(p) for p in directory.iterdir() if p.is_file()})
        durable_json(directory/'FINAL_STOPPED.json',terminal,exclusive=True)
        append_event(out/'DIAGNOSTIC_CALLS.jsonl',terminal);finished=True
        return terminal
    finally:
        if not claimed or finished: lock.unlink()

def returned_diagnostics(data,mapping,model):
    import numpy as np
    import pandas as pd
    from project_b.development_en_cox import _efron_loglik_gradient,_scientific_objective_gradient,_kkt_inf
    x,t,e=data['X'],data['t'],data['e']; beta=model.params_.to_numpy(); E=int(e.sum())
    alpha,lam=mapping['a'],mapping['lambda']
    objective,_=_scientific_objective_gradient(beta,x,t,e,alpha,lam,core_n=20)
    anchor,_=_scientific_objective_gradient(data['beta0'],x,t,e,alpha,lam,core_n=20)
    _,score=_efron_loglik_gradient(beta,x,t,e)
    smooth=-score/E; smooth[20:]+=lam*(1-alpha)*beta[20:]
    exact=np.abs(smooth.copy()); exact[20:]=np.where(beta[20:]!=0,
        np.abs(smooth[20:]+lam*alpha*np.sign(beta[20:])),np.maximum(0,np.abs(smooth[20:])-lam*alpha))
    frame=pd.DataFrame(data['Xvalid'],columns=model.params_.index)
    risks=(1-model.predict_survival_function(frame,times=[90,180])).to_numpy().T
    tiny=np.flatnonzero((np.abs(beta)<1e-6)&(np.arange(len(beta))>=20))
    probe=[]
    if len(tiny):
        j=int(tiny[np.argmax(exact[tiny])])
        for value in (0.,-1e-6,1e-6):
            trial=beta.copy();trial[j]=value
            obj,_=_scientific_objective_gradient(trial,x,t,e,alpha,lam,core_n=20)
            probe.append(dict(column=j,original_beta=float(beta[j]),probe_beta=value,
                objective=obj,objective_minus_returned=obj-objective))
    return dict(beta=beta,internal_center=model._norm_mean.to_numpy(),internal_scale=model._norm_std.to_numpy(),
        mapping=mapping,scientific_objective=objective,exact_kkt_inf=float(exact.max()),
        frozen_1e8_kkt_inf=_kkt_inf(beta,x,t,e,alpha,lam,core_n=20),
        core_gradient_inf_per_event=float(np.max(np.abs(score[:20]))/E),
        extension_nonzero_counts={str(tol):int(np.sum(np.abs(beta[20:])>tol)) for tol in [0,1e-10,1e-8,1e-6]},
        core_anchor_objective=anchor,objective_minus_core_anchor=objective-anchor,
        tiny_coefficient_objective_probes=probe,probe_definition='fixed coordinate probes, no refit or optimization',
        native_validation_risk_90_180=risks,diagnostic_only=True,scientific_acceptance=False,
        interpretation='backend return is not scientific acceptance; no predictive performance selection')

def validate_trace(path):
    # Stream the final record; do not load all Hessians into controller memory.
    last=None
    with Path(path).open(encoding='utf-8') as handle:
        for line in handle:
            if line.strip(): last=json.loads(line)
    if (not last or last.get('type')!='footer' or last.get('trace_restored') is not True
        or last.get('observer_errors')!=[]):
        raise RuntimeError('TRACE_INCOMPLETE')
    return last

def child_main(request_path):
    if sys.stdin.readline().strip()!='GO': raise RuntimeError('SUPERVISOR_GO_REQUIRED')
    request_path=Path(request_path).resolve();cfg=read(request_path);root=Path(cfg['root']).resolve();case=cfg['case']
    if root!=Path(__file__).resolve().parents[2] or case not in CASES or request_path!=root/REL/case/'REQUEST.json':
        raise RuntimeError('REQUEST_PATH_DRIFT')
    out=root/REL; directory=request_path.parent
    try:
        review=validate_gate(root)
        validate_necessity(out,case)
        if cfg['review_sha256']!=sha256(out/'INDEPENDENT_PREFIT_DIAGNOSTIC_REVIEW.json'): raise RuntimeError('REVIEW_CHANGED')
        events=read_events(out/'DIAGNOSTIC_CALLS.jsonl')
        if sum(r['action']=='START' and r['case']==case for r in events)!=1 or any(r['action']=='FINAL_STOPPED' and r['case']==case for r in events):
            raise RuntimeError('UNCLAIMED_OR_FINISHED')
        fold,order=CASES[case];meta=read(out/f'inner_{fold}_metadata.json')
        if sha256(out/f'inner_{fold}.npz')!=meta['snapshot']['sha256']: raise RuntimeError('SNAPSHOT_DRIFT')
        import numpy as np
        from project_b.m2_en_trace import fit_once,_json
        with np.load(out/f'inner_{fold}.npz',allow_pickle=False) as raw: data={k:raw[k].copy() for k in raw.files}
        candidate=next(c for c in meta['candidates'] if c['declared_order']==order)
        model=fit_once(data['X'],data['t'],data['e'],candidate['mapping'],directory/'TRACE.jsonl',
            initial_beta_external=None,candidate=candidate,provenance=cfg)
        footer=validate_trace(directory/'TRACE.jsonl')
        result=returned_diagnostics(data,candidate['mapping'],model)
        result.update(status='BACKEND_RETURNED',warnings=footer.get('warnings'))
        durable_json(directory/'RESULT.json',_json(result),exclusive=True)
        return 0
    except BaseException:
        durable_json(directory/'RESULT.json',dict(status='DIAGNOSTIC_FAILURE',exception_chain=traceback.format_exc(),
            diagnostic_only=True,scientific_acceptance=False),exclusive=True)
        return 1

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--case',choices=tuple(CASES));group.add_argument('--child')
    args=parser.parse_args()
    if args.child: sys.exit(child_main(args.child))
    print(json.dumps(execute_case(Path(__file__).resolve().parents[2],args.case)))
