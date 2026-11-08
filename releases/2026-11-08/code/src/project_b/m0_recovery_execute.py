"""Bounded D1 controller. No automatic trial chaining or production resume."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def durable_json(path, value, exclusive=False):
    path=Path(path)
    if exclusive:
        with path.open('x',encoding='utf-8') as f:
            json.dump(value,f,indent=2,allow_nan=False); f.flush(); os.fsync(f.fileno())
    else:
        temp=path.with_suffix('.tmp')
        with temp.open('w',encoding='utf-8') as f:
            json.dump(value,f,indent=2,allow_nan=False); f.flush(); os.fsync(f.fileno())
        os.replace(temp,path)


def append_event(path,record):
    with Path(path).open('a',encoding='utf-8') as f:
        f.write(json.dumps(record,sort_keys=True,allow_nan=False)+'\n'); f.flush(); os.fsync(f.fileno())


def read_events(path):
    path=Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def validate_branch(trial,events,adjudication):
    if trial not in ('R0','R1','R2'): raise RuntimeError('INVALID_TRIAL')
    starts=[r for r in events if r['action']=='START']
    terminals={r['trial']:r for r in events if r['action']=='FINAL_STOPPED'}
    if any(r['trial']==trial for r in starts): raise RuntimeError('DUPLICATE_TRIAL')
    if len(starts)>=3: raise RuntimeError('THREE_ATTEMPT_CEILING')
    if any(r['trial'] not in terminals for r in starts): raise RuntimeError('UNFINISHED_START_HOLD')
    if trial=='R0' and starts: raise RuntimeError('R0_MUST_BE_FIRST')
    if trial=='R1':
        decision=adjudication or {}
        if 'R0' not in terminals or 'R2' in terminals: raise RuntimeError('R1_ORDER')
        if not (decision.get('status')=='ALLOW_R1' and decision.get('basis')=='EARLY_OVERSHOOT_OR_FINITE_NUMERICAL_INSTABILITY'
                and decision.get('rationale') and decision.get('monotone_evidence') is False):
            raise RuntimeError('R1_REQUIRES_EXPLICIT_TRACE_ADJUDICATION')
    if trial=='R2':
        if 'R1' in terminals and terminals['R1']['outcome']!='SUCCESS': raise RuntimeError('FAILED_R1_STOP')
        if not any(r.get('outcome')=='SUCCESS' and r.get('acceptance_passed') is True for r in terminals.values()):
            raise RuntimeError('R2_REQUIRES_ORIGINAL_ACCEPTANCE')


def check_budget(spent,disk_bytes):
    wall=float(spent['wall_seconds']); cpu=float(spent['cpu_seconds'])
    if not (0<=wall<7200 and 0<=cpu<14400): raise RuntimeError('DIAGNOSTIC_TIME_BUDGET')
    if disk_bytes+1024**2>=2*1024**3: raise RuntimeError('DIAGNOSTIC_DISK_BUDGET')
    # Historical unknown tail is retained separately; no claim of production budget proof.
    return min(600.,7200-wall),14400-cpu


def load_snapshot(path,digest):
    import numpy as np
    if sha256(path)!=digest: raise RuntimeError('SNAPSHOT_HASH_DRIFT')
    with np.load(path,allow_pickle=False) as a:
        if set(a.files)!={'X','t','e','ids','columns'}: raise RuntimeError('SNAPSHOT_SCHEMA')
        data={k:a[k].copy() for k in a.files}
    X,t,e,ids=data['X'],data['t'],data['e'],data['ids']
    if (X.shape!=(1817,20) or t.shape!=(1817,) or e.shape!=t.shape or ids.shape!=(1817,2)
        or data['columns'].shape!=(20,) or len(set(map(tuple,ids)))!=1817
        or not np.isfinite(X).all() or not np.isfinite(t).all() or not ((t>=0)&(t<=180)).all()
        or not np.isin(e,[0,1]).all() or e.sum()!=278): raise RuntimeError('SNAPSHOT_CONTENT')
    return data


def acceptance(X,t,e,beta):
    import numpy as np
    from .m0_recovery_diagnostics import efron_loglik_score_information
    X=np.asarray(X,float); t=np.asarray(t,float); e=np.asarray(e,bool); beta=np.asarray(beta,float)
    ll,score,info=efron_loglik_score_information(X,t,e,beta)
    eig=np.linalg.eigvalsh(info); norm=float(np.max(np.abs(score)))
    eta=X@beta; shift=float(np.max(eta)); relative=np.exp(eta-shift)
    baseline=np.zeros(2)
    for day in np.unique(t[e]):
        increment=float(np.sum((t==day)&e)/np.sum(relative[t>=day]))
        baseline+=increment*(np.array([90.,180.])>=day)
    risks=-np.expm1(-relative[:,None]*baseline)
    passed=bool(np.isfinite(beta).all() and np.isfinite(info).all() and np.isfinite(risks).all()
        and eig[0]>0 and norm<1e-4 and norm/int(e.sum())<2e-7
        and ((risks>=0)&(risks<=1)).all() and (risks[:,0]<=risks[:,1]).all())
    return {'passed':passed,'loglik':ll,'beta':beta.tolist(),'score_inf':norm,
            'score_inf_per_event':norm/int(e.sum()),'information_min_eigenvalue':float(eig[0]),
            'training_risks_90_180':risks.tolist(),'m1_alias_extra_fit_calls':0,
            'risk_definition':'training-only Breslow step baseline, no predictive performance metrics'}


class ChildJob:
    """Only the child is assigned; KILL_ON_JOB_CLOSE never targets the controller."""
    def __init__(self):
        from .development_resources import _ExtendedLimit
        if os.name!='nt': raise RuntimeError('WINDOWS_JOB_REQUIRED')
        self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        signatures={
            'CreateJobObjectW':([ctypes.c_void_p,wintypes.LPCWSTR],wintypes.HANDLE),
            'SetInformationJobObject':([wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD],wintypes.BOOL),
            'AssignProcessToJobObject':([wintypes.HANDLE,wintypes.HANDLE],wintypes.BOOL),
            'QueryInformationJobObject':([wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p],wintypes.BOOL),
            'TerminateJobObject':([wintypes.HANDLE,wintypes.UINT],wintypes.BOOL),
            'CloseHandle':([wintypes.HANDLE],wintypes.BOOL),
            'GetCurrentProcess':([],wintypes.HANDLE),
            'GetProcessAffinityMask':([wintypes.HANDLE,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)],wintypes.BOOL)}
        for name,(args,result) in signatures.items():
            fn=getattr(self.k,name); fn.argtypes=args; fn.restype=result
        self.handle=self.k.CreateJobObjectW(None,None)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
        try:
            process_mask=ctypes.c_size_t(); system_mask=ctypes.c_size_t()
            if not self.k.GetProcessAffinityMask(self.k.GetCurrentProcess(),ctypes.byref(process_mask),ctypes.byref(system_mask)):
                raise ctypes.WinError(ctypes.get_last_error())
            info=_ExtendedLimit(); info.basic.flags=0x2000|0x8|0x10
            # Windows venv python.exe is a launcher plus one interpreter worker.
            info.basic.active_process_limit=2; info.basic.affinity=process_mask.value & -process_mask.value
            if not self.k.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close(); raise

    def assign(self,process):
        if not self.k.AssignProcessToJobObject(self.handle,int(process._handle)): raise ctypes.WinError(ctypes.get_last_error())

    def accounting(self):
        from .development_resources import _ExtendedLimit
        class Accounting(ctypes.Structure):
            _fields_=[('user',ctypes.c_longlong),('kernel',ctypes.c_longlong),('period_user',ctypes.c_longlong),('period_kernel',ctypes.c_longlong),('faults',wintypes.DWORD),('total',wintypes.DWORD),('active',wintypes.DWORD),('terminated',wintypes.DWORD)]
        a=Accounting(); m=_ExtendedLimit()
        for kind,info in [(1,a),(9,m)]:
            if not self.k.QueryInformationJobObject(self.handle,kind,ctypes.byref(info),ctypes.sizeof(info),None):
                raise ctypes.WinError(ctypes.get_last_error())
        return {'child_cpu_seconds':(a.user+a.kernel)/1e7,'peak_child_commit_bytes':int(m.peak_job_memory_used),'active_child_n':int(a.active)}

    def kill(self):
        if not self.k.TerminateJobObject(self.handle,86): raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle: self.k.CloseHandle(self.handle); self.handle=None


def disk_used(path):
    total=0
    for p in Path(path).rglob('*'):
        if p.is_symlink(): raise RuntimeError('SYMLINK_DISK_HOLD')
        if p.is_file(): total+=p.stat().st_size
    return total


def supervise(command,directory,*,wall_limit,cpu_limit,disk_root):
    """Synchronous outside-Job supervisor, ~100ms polling; bounded overshoot disclosed."""
    directory=Path(directory); process=None; job=ChildJob(); reason=None
    wall_start=time.monotonic(); parent_cpu_start=time.process_time(); final=None
    env=dict(os.environ)
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'): env[key]='1'
    try:
        with (directory/'stdout.log').open('xb') as out,(directory/'stderr.log').open('xb') as err:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=out,stderr=err,env=env,
                                     cwd=directory,creationflags=subprocess.CREATE_NO_WINDOW|0x4)
            job.assign(process)
            # Persist ownership before letting the child proceed to imports or fits.
            durable_json(directory/'RESOURCE_STATUS.json',{'status':'CHILD_GATED','pid':process.pid,'parent_pid':os.getpid(),'parent_outside_child_job':True})
            # Popen closes the initial thread handle. Resume the suspended process
            # only after Job membership, preventing an unowned venv-shim descendant.
            resume=ctypes.WinDLL('ntdll').NtResumeProcess
            resume.argtypes=[wintypes.HANDLE]; resume.restype=ctypes.c_long
            if resume(int(process._handle))!=0: raise RuntimeError('CHILD_RESUME_FAILED')
            process.stdin.write(b'GO\n'); process.stdin.flush(); process.stdin.close()
            while process.poll() is None:
                accounting=job.accounting(); wall=time.monotonic()-wall_start
                cpu=accounting['child_cpu_seconds']+time.process_time()-parent_cpu_start
                if wall>=wall_limit: reason='TRIAL_WALL_LIMIT'
                elif cpu>=cpu_limit: reason='DIAGNOSTIC_CPU_LIMIT'
                elif disk_used(disk_root)+1024**2>=2*1024**3: reason='DIAGNOSTIC_DISK_LIMIT'
                durable_json(directory/'RESOURCE_STATUS.json',{'status':'STOPPING' if reason else 'RUNNING','reason':reason,
                    'wall_seconds':wall,'controller_cpu_seconds':time.process_time()-parent_cpu_start,**accounting})
                if reason:
                    job.kill(); break
                time.sleep(.1)
            process.wait(timeout=10)
            final={'status':'FINAL_STOPPED','outcome':'SUCCESS' if process.returncode==0 and reason is None else 'FAILURE',
                   'reason':reason,'exit_code':process.returncode,'wall_seconds':time.monotonic()-wall_start,
                   'controller_cpu_seconds':time.process_time()-parent_cpu_start,'parent_outside_child_job':True,
                   'poll_seconds':.1,'possible_polling_overshoot':True,**job.accounting()}
            durable_json(directory/'RESOURCE_STATUS.json',final)
            return final
    finally:
        # This cleanup is unconditional even when writing a HOLD/resource record fails.
        if process is not None and process.poll() is None:
            try: job.kill()
            finally:
                process.kill(); process.wait(timeout=10)
        if process is not None and process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        job.close()


def bound_path(root,name):
    path=(Path(root)/name).resolve()
    if not path.is_relative_to(Path(root).resolve()): raise RuntimeError('OUTSIDE_PROJECT_B')
    return path


def validate_gate(root):
    """Read-only gate; independent review cannot replace researcher authorization."""
    root=Path(root).resolve(); out=root/'audit/development/recovery_v1'
    read=lambda p:json.loads(Path(p).read_text(encoding='utf-8'))
    review=read(out/'D1_EXECUTION_REVIEW.json')
    if review.get('status')!='PASS_APPROVED': raise RuntimeError('D1_INDEPENDENT_REVIEW_REQUIRED')
    mandatory={'src/project_b/m0_recovery_diagnostics.py','src/project_b/m0_recovery_trace.py','src/project_b/m0_recovery_execute.py',
               'tests/test_m0_recovery_diagnostics.py','tests/test_m0_recovery_trace.py','tests/test_m0_recovery_execute.py'}
    bindings=review.get('source_hashes',{})
    if not mandatory<=set(bindings): raise RuntimeError('D1_REVIEW_SOURCE_COVERAGE')
    for name,digest in bindings.items():
        if sha256(bound_path(root,name))!=digest.lower(): raise RuntimeError('D1_REVIEW_HASH_DRIFT: '+name)
    files={'d0_report_sha256':out/'D0_REPORT.json','diagnostic_inputs_sha256':out/'DIAGNOSTIC_INPUTS.json',
           'snapshot_sha256':out/'FAILED_INNER_SNAPSHOT.npz','approval_sha256':root/'audit/development/M0_RECOVERY_EXECUTION_APPROVAL.json',
           'd0_review_sha256':out/'D0_INDEPENDENT_REVIEW.json'}
    for key,path in files.items():
        if review.get(key)!=sha256(path): raise RuntimeError('D1_REVIEW_BINDING: '+key)
    d0=read(files['d0_report_sha256']); d0review=read(files['d0_review_sha256'])
    if d0['status']!='ELIGIBLE_FOR_CONTROLLED_R0' or d0review.get('status')!='PASS' or d0review.get('next_stage')!='ELIGIBLE_FOR_CONTROLLED_R0':
        raise RuntimeError('D0_STOP_BRANCH')
    for name,digest in d0review['binding_sha256'].items():
        if sha256(bound_path(root,name))!=digest.lower(): raise RuntimeError('D0_REVIEW_HASH_DRIFT')
    approval=read(files['approval_sha256'])
    if (approval.get('diagnostic_optimization_maximum')!=3 or approval.get('prior_attempts_retained')!=12
        or approval.get('physical_trainable_call_maximum')!=15212
        or sha256(bound_path(root,approval['approved_plan_path'])).upper()!=approval['approved_plan_sha256']):
        raise RuntimeError('RECOVERY_APPROVAL_DRIFT')
    from .development_authorization import validate_development_authorization
    from .development_run import compute_run_fingerprint
    validate_development_authorization(root)
    inputs=read(files['diagnostic_inputs_sha256'])
    if compute_run_fingerprint(root)!=inputs['fingerprint']: raise RuntimeError('PRODUCTION_FINGERPRINT_DRIFT')
    protected=read(out/'D0_PROTECTED_BEFORE.json')
    for name,digest in protected.items():
        if sha256(bound_path(root,name))!=digest: raise RuntimeError('PROTECTED_HASH_DRIFT: '+name)
    snapshot=load_snapshot(files['snapshot_sha256'],review['snapshot_sha256'])
    if snapshot['columns'].tolist()!=inputs['columns']: raise RuntimeError('COLUMN_ORDER_DRIFT')
    old=[]
    for p in (root/'audit/development/runs/development_2008_2019').rglob('attempts.jsonl'): old.extend(read_events(p))
    if sum(r['action']=='START' for r in old)!=12: raise RuntimeError('PRIOR_ATTEMPTS_DRIFT')
    budget=review.get('stage_budget_accounting',{})
    if not (budget.get('basis') and budget.get('historical_tail')=='UNKNOWN' and budget.get('production_resume_budget_proven') is False):
        raise RuntimeError('RESOURCE_ACCOUNTING_SCOPE_REQUIRED')
    check_budget(budget,disk_used(out))
    return review


def claim_trial(out,trial,snapshot_hash,review_hash):
    """Caller holds the exclusive controller lock; claim consumes an attempt."""
    out=Path(out); events=read_events(out/'DIAGNOSTIC_ATTEMPTS.jsonl')
    if any(r.get('trial')==trial and r['action']=='START' for r in events): raise RuntimeError('DUPLICATE_TRIAL')
    n=sum(r['action']=='START' for r in events)
    if n>=3: raise RuntimeError('THREE_ATTEMPT_CEILING')
    directory=out/trial; directory.mkdir()
    append_event(out/'DIAGNOSTIC_ATTEMPTS.jsonl',{
        'action':'START','trial':trial,'attempt_id':'M0_RECOVERY_V1_'+trial,
        'parent_attempt_id':'development_2008_2019:outer_repeat=1|outer_fold=5|M0|fixed|inner=1',
        'logical_object_id':'outer_repeat=1|outer_fold=5|M0|fixed|inner=1',
        'snapshot_sha256':snapshot_hash,'execution_review_sha256':review_hash,
        'prior_attempts_retained':12,'diagnostic_attempts':n+1,'total_actual_attempts':12+n+1,
        'logical_trainable_objects':15198,'actual_attempt_ceiling':15212,
        'time_utc':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
    return directory


def execute_trial(root,trial):
    """Exactly one trial. Caller must have received independent approval first."""
    controller_wall_start=time.monotonic(); controller_cpu_start=time.process_time()
    root=Path(root).resolve(); out=root/'audit/development/recovery_v1'
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'): os.environ[key]='1'
    review=validate_gate(root)
    lock=out/'D1_CONTROLLER.lock'
    with lock.open('x',encoding='utf-8') as f:
        f.write(str(os.getpid())); f.flush(); os.fsync(f.fileno())
    claimed=False; final_written=False
    try:
        events=read_events(out/'DIAGNOSTIC_ATTEMPTS.jsonl')
        decision=None
        if trial=='R1':
            decision=json.loads((out/'R1_ADJUDICATION.json').read_text())
            if decision.get('trace_R0_sha256')!=sha256(out/'R0/TRACE_R0.jsonl'):
                raise RuntimeError('R1_ADJUDICATION_TRACE_DRIFT')
            if decision.get('d0_review_sha256')!=review['d0_review_sha256']: raise RuntimeError('R1_D0_REVIEW_DRIFT')
        validate_branch(trial,events,decision)
        spent=dict(review['stage_budget_accounting'])
        for r in events:
            if r['action']=='FINAL_STOPPED':
                resource=r.get('resources') or {}
                if any(resource.get(k) is None for k in ('wall_seconds','child_cpu_seconds','controller_cpu_seconds')):
                    raise RuntimeError('D1_RESOURCE_TAIL_UNRESOLVED')
                spent['wall_seconds']+=resource['accounting_wall_seconds']
                spent['cpu_seconds']+=resource['accounting_cpu_seconds']
                for filename,digest in r.get('artifact_hashes',{}).items():
                    if sha256(bound_path(out/r['trial'],filename))!=digest: raise RuntimeError('PRIOR_TRIAL_ARTIFACT_DRIFT')
        wall_limit,cpu_limit=check_budget(spent,disk_used(out))
        directory=claim_trial(out,trial,review['snapshot_sha256'],sha256(out/'D1_EXECUTION_REVIEW.json')); claimed=True
        config={'root':str(root),'trial':trial,'snapshot_sha256':review['snapshot_sha256'],
                'review_sha256':sha256(out/'D1_EXECUTION_REVIEW.json')}
        if trial=='R2':
            prior='R1' if any(r.get('trial')=='R1' and r['action']=='FINAL_STOPPED' for r in events) else 'R0'
            config['reference_against']=prior
            config['reference_result_sha256']=sha256(out/prior/'RESULT.json')
        durable_json(directory/'REQUEST.json',config,exclusive=True)
        resources=None; outcome='FAILURE'; acceptance_passed=False; error=None
        try:
            command=[str(root/'.venv-a5/Scripts/python.exe'),'-B',str(Path(__file__).resolve()),'--child',str(directory/'REQUEST.json')]
            resources=supervise(command,directory,wall_limit=wall_limit,cpu_limit=cpu_limit,disk_root=out)
            if resources['outcome']=='SUCCESS':
                result=json.loads((directory/'RESULT.json').read_text())
                acceptance_passed=result.get('acceptance',{}).get('passed') is True
                if acceptance_passed and (trial!='R2' or result.get('reference_agreement',{}).get('passed') is True): outcome='SUCCESS'
            validate_gate(root)
        except BaseException:
            error=traceback.format_exc()
        if resources is not None:
            resources['supervised_child_window_wall_seconds']=resources['wall_seconds']
            resources['wall_seconds']=time.monotonic()-controller_wall_start
            resources['controller_cpu_seconds']=time.process_time()-controller_cpu_start
            resources['accounting_wall_seconds']=resources['wall_seconds']+1.
            resources['accounting_cpu_seconds']=resources['child_cpu_seconds']+resources['controller_cpu_seconds']+1.
            resources['accounting_reserve_basis']='Disclosed one-second charge for terminal serialization/exit; not measured CPU'
        terminal={'action':'FINAL_STOPPED','trial':trial,'outcome':outcome if error is None else 'FAILURE',
                  'acceptance_passed':acceptance_passed,'resources':resources,'exception_chain':error,
                  'total_actual_attempts':12+sum(r['action']=='START' for r in read_events(out/'DIAGNOSTIC_ATTEMPTS.jsonl')),
                  'historical_tail':'UNKNOWN','production_resume_budget_proven':False,
                  'artifact_hashes':{p.name:sha256(p) for p in directory.iterdir() if p.is_file()}}
        durable_json(directory/'FINAL_STOPPED.json',terminal,exclusive=True)
        append_event(out/'DIAGNOSTIC_ATTEMPTS.jsonl',terminal); final_written=True
        return terminal
    finally:
        # A claim without a durable terminal remains locked and cannot be replayed.
        if not claimed or final_written: lock.unlink()


def child_main(request_path):
    """Internal gated child: no imports or patient access until supervisor GO."""
    if sys.stdin.readline().strip()!='GO': raise RuntimeError('SUPERVISOR_GATE_REQUIRED')
    request_path=Path(request_path).resolve(); cfg=json.loads(request_path.read_text())
    root=Path(cfg['root']).resolve(); out=root/'audit/development/recovery_v1'; trial=cfg['trial']
    if request_path!=out/trial/'REQUEST.json': raise RuntimeError('REQUEST_PATH_DRIFT')
    sys.path.insert(0,str(root/'src'))
    # __main__ cannot resolve relative imports; use the package module for shared gates.
    from project_b.m0_recovery_execute import validate_gate,load_snapshot,acceptance,append_event,durable_json,sha256
    if sha256(out/'D1_EXECUTION_REVIEW.json')!=cfg['review_sha256']: raise RuntimeError('REVIEW_CHANGED_AFTER_START')
    review=validate_gate(root)
    if cfg['snapshot_sha256']!=review['snapshot_sha256']: raise RuntimeError('SNAPSHOT_CHANGED_AFTER_START')
    records=read_events(out/'DIAGNOSTIC_ATTEMPTS.jsonl')
    if sum(r['action']=='START' and r['trial']==trial for r in records)!=1: raise RuntimeError('START_REQUIRED')
    if any(r['action']=='FINAL_STOPPED' and r['trial']==trial for r in records): raise RuntimeError('TRIAL_ALREADY_FINISHED')
    data=load_snapshot(out/'FAILED_INNER_SNAPSHOT.npz',cfg['snapshot_sha256'])
    directory=request_path.parent; trace=directory/('TRACE_'+trial+'.jsonl')
    import numpy as np
    import warnings
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            try:
                if trial in ('R0','R1'):
                    import pandas as pd
                    from project_b.m0_recovery_trace import TracedCoxPHFitter
                    frame=pd.DataFrame(data['X'],columns=data['columns']); frame['t']=data['t']; frame['e']=data['e']
                    fit=TracedCoxPHFitter(trace_path=trace,baseline_estimation_method='breslow')
                    fit.fit(frame,duration_col='t',event_col='e',show_progress=False,
                            initial_point=np.zeros(20),fit_options={'step_size':.95 if trial=='R0' else .25,'precision':1e-14,'r_precision':1e-15,'max_steps':1000})
                    beta=fit.params_.to_numpy()
                elif trial=='R2':
                    from project_b.m0_recovery_trace import reference_newton
                    solved=reference_newton(data['X'],data['t'],data['e'],trace_path=trace); beta=solved['beta']
                else: raise RuntimeError('INVALID_TRIAL')
            finally:
                for warning in caught:
                    append_event(directory/'CHILD_EVENTS.jsonl',{'type':'warning','category':warning.category.__name__,'message':str(warning.message)})
        checked=acceptance(data['X'],data['t'],data['e'],beta)
        if trial in ('R0','R1'):
            baseline=fit.baseline_cumulative_hazard_
            positions=np.searchsorted(baseline.index.to_numpy(),[90.,180.],side='right')-1
            hazard=np.array([0. if i<0 else float(baseline.iloc[i,0]) for i in positions])
            partial=fit.predict_partial_hazard(frame[data['columns'].tolist()]).to_numpy()
            native=-np.expm1(-partial[:,None]*hazard)
            checked['backend_risks_valid']=bool(np.isfinite(native).all() and ((native>=0)&(native<=1)).all() and (native[:,0]<=native[:,1]).all())
            checked['backend_manual_risk_max_abs_difference']=float(np.max(np.abs(native-checked['training_risks_90_180'])))
            checked['training_risks_90_180']=native.tolist()
            checked['passed']=checked['passed'] and checked['backend_risks_valid']
        result={'trial':trial,'snapshot_sha256':cfg['snapshot_sha256'],'fit_call_count':1,'acceptance':checked}
        if trial=='R2':
            prior=out/cfg['reference_against']/'RESULT.json'
            if sha256(prior)!=cfg['reference_result_sha256']: raise RuntimeError('REFERENCE_CANDIDATE_DRIFT')
            other=json.loads(prior.read_text())['acceptance']
            gaps={'negative_loglik_per_event_abs_difference':abs(checked['loglik']-other['loglik'])/278,
                  'coefficient_max_abs_difference':float(np.max(np.abs(np.array(checked['beta'])-other['beta']))),
                  'risk_max_abs_difference':float(np.max(np.abs(np.array(checked['training_risks_90_180'])-other['training_risks_90_180'])))}
            result['reference_agreement']={**gaps,'passed':gaps['negative_loglik_per_event_abs_difference']<=1e-7 and gaps['coefficient_max_abs_difference']<=1e-4 and gaps['risk_max_abs_difference']<=1e-5}
        durable_json(directory/'RESULT.json',result,exclusive=True)
        if not checked['passed']: raise RuntimeError('ORIGINAL_M0_ACCEPTANCE_FAILED')
    except BaseException:
        append_event(directory/'CHILD_EVENTS.jsonl',{'type':'exception','exception_chain':traceback.format_exc()})
        raise


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--child':
        child_main(sys.argv[2])
    else:
        import argparse
        parser=argparse.ArgumentParser(description='Execute exactly one independently reviewed M0 diagnostic trial')
        parser.add_argument('--root',required=True); parser.add_argument('--trial',choices=['R0','R1','R2'],required=True)
        args=parser.parse_args()
        sys.path.insert(0,str(Path(args.root)/'src'))
        from project_b.m0_recovery_execute import execute_trial
        print(json.dumps(execute_trial(args.root,args.trial),indent=2))
