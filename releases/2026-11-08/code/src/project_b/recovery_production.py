"""Explicitly signed recovery lineage, accounting and staged context coordinator.

Original Task06/A5 code, ACTIVE_RUN, locks and ledgers remain read-only.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import time
if not __package__:
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    __package__='project_b'
from .m0_recovery_execute import sha256,durable_json,append_event,read_events,bound_path

FAILED_CONTEXT='outer_repeat=1|outer_fold=5'
FAILED_OBJECT=FAILED_CONTEXT+'|M0|fixed|inner=1'


def old_attempt_states(events):
    states={}
    for row in events:
        task=row['task']; action=row['action']
        if action=='START':
            if task in states: raise RuntimeError('OLD_DUPLICATE')
            states[task]={'parent_attempt_id':'development_2008_2019:'+task,'retry_kind':'RETRY_OF_INTERRUPTED','old_status':'START'}
        elif action in ('SUCCESS','FAILURE') and task in states and states[task]['old_status']=='START':
            states[task].update(old_status=action,retry_kind='REUSE_REQUIRES_REVIEW' if action=='SUCCESS' else 'RETRY_OF_FAILURE')
            if action=='SUCCESS':states[task]['sha256']=row['sha256']
        else:raise RuntimeError('OLD_LEDGER_SEQUENCE')
    return states


def remaining_budget(charges,resources):
    cpu=float(charges['old_cpu_charge'])+float(charges['diagnostic_cpu_charge'])
    wall=float(charges['old_wall_charge'])+float(charges['diagnostic_wall_charge'])
    for record in resources:
        if record.get('status')!='FINAL_STOPPED':raise RuntimeError('UNRESOLVED_RECOVERY_RESOURCE_TAIL')
        cpu+=float(record['accounting_cpu_seconds']);wall+=float(record['accounting_wall_seconds'])
    if not 0<=cpu<800*3600 or not 0<=wall<96*3600:raise RuntimeError('DEVELOPMENT_BUDGET_EXHAUSTED')
    return {'remaining_cpu_seconds':800*3600-cpu,'remaining_wall_seconds':96*3600-wall,
            'charged_cpu_seconds':cpu,'charged_wall_seconds':wall,'old_tail_measured':False,
            'old_charge_basis':'explicit conservative accounting allocation; not measured or proved strict bound'}


def check_stage(stage,history):
    order=('failed_object','first_context','remaining_contexts')
    if stage not in order:raise RuntimeError('INVALID_STAGE')
    if any(r['stage']==stage for r in history):raise RuntimeError('STAGE_NOT_REPLAYABLE')
    index=order.index(stage)
    if len(history)!=index or any(r.get('stage')!=order[i] or r.get('status')!='FINAL_STOPPED' or r.get('outcome')!='SUCCESS' for i,r in enumerate(history)):
        raise RuntimeError('PRIOR_STAGE_NOT_APPROVED_SUCCESS')


class AttemptCounter:
    """One fsynced global charge before every actual trainable operation.

    Windows byte-range lock serializes unrelated spawned processes. A global
    charge without a matching local START is still charged and blocks replay.
    """
    def __init__(self,out,prior=15):
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True);self.prior=prior
    def charge(self,task,lineage):
        import msvcrt
        lock=self.out/'GLOBAL_ATTEMPTS.lock'
        with lock.open('a+b') as f:
            f.seek(0,2)
            if f.tell()==0:f.write(b'0');f.flush()
            f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_LOCK,1)
            try:
                path=self.out/'GLOBAL_ATTEMPTS.jsonl';rows=read_events(path)
                if any(r['task']==task for r in rows):raise RuntimeError('GLOBAL_DUPLICATE_OR_INTERRUPTED_ATTEMPT')
                if self.prior+len(rows)>=15212:raise RuntimeError('GLOBAL_ACTUAL_CEILING')
                row={'action':'CHARGE_START','task':task,'attempt_id':'recovery_v1:'+task,
                     'prior_attempts':self.prior,'total_actual_attempts':self.prior+len(rows)+1,**lineage}
                append_event(path,row);return row
            finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


def validate_recovery_authorization(root):
    root=Path(root).resolve();out=root/'audit/development/recovery_v1'
    read=lambda p:json.loads(Path(p).read_text(encoding='utf-8'))
    draft_path=out/'RECOVERY_AUTHORIZATION_DRAFT.json'
    signature=read(out/'RECOVERY_RESEARCHER_SIGNATURE.json')
    keys={'schema_version','researcher_id','approved_at_utc','authorization_draft_path','authorization_draft_sha256','approved'}
    if (set(signature)!=keys or signature.get('schema_version')!='1.0' or signature.get('approved') is not True
        or signature.get('authorization_draft_sha256')!=sha256(draft_path).upper()
        or signature.get('authorization_draft_path')!='audit/development/recovery_v1/RECOVERY_AUTHORIZATION_DRAFT.json'
        or not isinstance(signature.get('researcher_id'),str) or not signature['researcher_id'].strip()):
        raise PermissionError('NEW_MANUAL_RECOVERY_SIGNATURE_REQUIRED')
    draft=read(draft_path)
    review=read(out/'RECOVERY_INDEPENDENT_REVIEW.json')
    if (review.get('status')!='PASS_APPROVED' or review.get('authorization_draft_sha256')!=sha256(draft_path).upper()
        or review.get('critical_findings')!=0 or review.get('important_findings')!=0):
        raise PermissionError('NEW_RECOVERY_SOURCE_REVIEW_REQUIRED')
    from datetime import datetime
    import re
    def utc(value):
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z',value):raise PermissionError('INVALID_UTC')
        try:return datetime.fromisoformat(value[:-1]+'+00:00')
        except ValueError as exc:raise PermissionError('INVALID_UTC') from exc
    if utc(signature['approved_at_utc'])<utc(review.get('reviewed_at_utc')):raise PermissionError('SIGNATURE_PRECEDES_REVIEW')
    if draft.get('status')!='DRAFT_FOR_RESEARCHER_SIGNATURE':raise PermissionError('DRAFT_SCHEMA')
    for name,digest in draft['bindings'].items():
        if sha256(bound_path(root,name))!=digest:raise PermissionError('RECOVERY_BINDING_DRIFT: '+name)
    from .development_authorization import validate_development_authorization
    from .development_run import compute_run_fingerprint
    validate_development_authorization(root)
    if compute_run_fingerprint(root)!=draft['old_fingerprint']:raise PermissionError('ORIGINAL_PRODUCTION_DRIFT')
    for name,digest in read(out/'D0_PROTECTED_BEFORE.json').items():
        if sha256(bound_path(root,name))!=digest:raise PermissionError('OLD_EVIDENCE_DRIFT')
    d2=read(out/'D2_INDEPENDENT_REVIEW.json')
    if d2.get('status')!='PASS_SOLVER_REVISION_SUPPORTED' or d2.get('uniform_M0_initial_step_size_supported')!=.25:
        raise PermissionError('D2_UNIFORM_RULE_NOT_APPROVED')
    reuse=read(out/'SUCCESS_CHECKPOINT_REUSE_REVIEW.json')
    if reuse.get('status')!='PASS_APPROVED':raise PermissionError('OLD_SUCCESS_REUSE_REVIEW_REQUIRED')
    for name,digest in reuse['binding_sha256'].items():
        if sha256(bound_path(root,name))!=digest.lower():raise PermissionError('REUSE_REVIEW_BINDING_DRIFT')
    if len(draft['old_success_reuse'])!=1:raise PermissionError('ONE_OLD_SUCCESS_REQUIRED_FOR_15212_CEILING')
    if draft['logical_trainable_objects']!=15198 or draft['physical_trainable_call_maximum']!=15212 or draft['prior_actual_attempts']!=15:
        raise PermissionError('RECOVERY_COUNT_DRIFT')
    validate_budget_estimate(root,draft)
    return draft


def validate_budget_estimate(root,draft):
    root=Path(root)
    receipt=json.loads((root/'audit/development/recovery_v1/RECOVERY_BUDGET_REESTIMATE.json').read_text())
    if (receipt.get('status')!='PASS_DIMENSION_MATCHED_PLANNING_ESTIMATE' or receipt.get('within_remaining_cpu') is not True
        or receipt.get('within_remaining_wall') is not True or receipt.get('resource_charges_bound_for_arithmetic')!=draft['resource_charges']):
        raise PermissionError('RECOVERY_PLANNING_BUDGET_HOLD')
    required={'first_context_serial':1,'remaining_outer_contexts':49,'max_workers':12,'remaining_outer_waves':5,
              'full_development_serial':1,'survival_context_equivalent_wall_units':7}
    if any(receipt.get('schedule',{}).get(k)!=v for k,v in required.items()):raise PermissionError('RECOVERY_SCHEDULE_ESTIMATE_DRIFT')
    sources={'audit/a5/A5_COMPUTE_BUDGET.json','src/project_b/development_run.py','audit/development/recovery_v1/reestimate_recovery_budget.py'}
    if not sources<=set(receipt.get('source_sha256',{})):raise PermissionError('BENCHMARK_SOURCE_BINDINGS_REQUIRED')
    for name,digest in receipt['source_sha256'].items():
        if sha256(bound_path(root,name))!=digest:raise PermissionError('BENCHMARK_SOURCE_DRIFT')
    remaining=remaining_budget(draft['resource_charges'],[])
    if not (0<receipt['estimated_cpu_hours']*3600<remaining['remaining_cpu_seconds'] and
            0<receipt['estimated_wall_hours']*3600<remaining['remaining_wall_seconds']):
        raise PermissionError('RECOVERY_FORECAST_EXCEEDS_ALLOCATION')
    return receipt


def context_path(run,label):return Path(run)/'contexts'/hashlib.sha256(label.encode()).hexdigest()


from .development_run import DurableTasks

class RecoveryLedger(DurableTasks):
    def __init__(self,directory,fingerprint,run,lineage,reuse):
        super().__init__(directory,fingerprint)
        self.run=Path(run);self.lineage=lineage;self.reuse=reuse;self.context=None
        self.counter=AttemptCounter(run)
    def _read(self):
        states,n=super()._read()
        for task,item in self.reuse.items():
            if self.context is not None and not task.startswith(self.context+'|'):continue
            if context_path(self.run,task.rsplit('|',3)[0]).resolve()!=self.path.resolve():continue
            source=Path(item['path'])
            if sha256(source)!=item['sha256']:raise RuntimeError('REUSE_SOURCE_TAMPER')
            if task in states:raise RuntimeError('REUSED_OBJECT_HAS_NEW_START')
            target=self.path/'results'/(hashlib.sha256(task.encode()).hexdigest()+'.json')
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                with target.open('xb') as f:f.write(source.read_bytes());f.flush();os.fsync(f.fileno())
            if sha256(target)!=item['sha256']:raise RuntimeError('IMPORTED_CHECKPOINT_TAMPER')
            states[task]='SUCCESS';self.hashes[task]=item['sha256']
        return states,n
    def execute(self,task,operation):
        if self.state.get(task)=='SUCCESS':return super().execute(task,operation)
        if self.guard:self.guard.check_fit()
        lineage=self.lineage.get(task,{'parent_attempt_id':None,'retry_kind':'ORIGINAL_UNSTARTED_OBJECT'})
        self.counter.charge(task,lineage)
        return super().execute(task,operation)


def run_context(root,run,fingerprint,context,lineage,reuse,only_failed=False):
    from .recovery_context import run_one_context,run_m0_inner_context
    from .development_parallel import load_context
    label=context['context'];directory=context_path(run,label)
    with RecoveryLedger(directory,fingerprint,run,lineage,reuse) as ledger:
        ledger.context=label;ledger.guard=RecoveryWriteGuard(run)
        if only_failed:
            if label!=FAILED_CONTEXT:raise RuntimeError('FAILED_CONTEXT_REQUIRED')
            result=run_m0_inner_context(ledger=ledger,context=label,ids=context['ids'],core_raw=context['core_training'],
                times=context['times'],events=context['events'],project_root=root,folds=(1,))
            return {'context':label,'failed_object_completed':True,'actual_new_calls':ledger.attempts}
        existing=load_context(directory,fingerprint,label)
        if existing is not None:return {'context':label,'cached':True}
        payload=run_one_context(ledger=ledger,project_root=root,**context)
        from .development_run import _encoded
        durable_json(directory/'CONTEXT.json',{'fingerprint':fingerprint,'context':label,'sha256':hashlib.sha256(_encoded(payload)).hexdigest(),'payload':payload},exclusive=True)
        return {'context':label,'completed':True,'new_context_calls':ledger.attempts}


class RecoveryWriteGuard:
    def __init__(self,run):
        self.path=Path(run)
        self.accounting_root=next((p for p in self.path.parents if p.name=='development'),self.path)
    def check_fit(self):
        if (self.path/'ROOT_CAUSE.json').exists():raise RuntimeError('RECOVERY_ROOT_FAILURE_STOP')
    def check_write(self,size=0):
        import shutil
        from .m0_recovery_execute import disk_used
        if disk_used(self.accounting_root)+size+128*1024**2>50*1024**3:raise RuntimeError('ARTIFACT_DISK_HOLD')
        if shutil.disk_usage(self.path).free-size-128*1024**2<100*1024**3:raise RuntimeError('FREE_DISK_HOLD')


def reconcile_recovery(run,fingerprint,lineage,reuse):
    from .development_run import context_labels
    from .development_accounting import read_skips,build_receipt
    states={};skips={};local_attempts=0
    for label in context_labels():
        directory=context_path(run,label)
        if not directory.exists():continue
        ledger=RecoveryLedger(directory,fingerprint,run,lineage,reuse);ledger.context=label
        current,n=ledger._read();local_attempts+=n
        if any(not k.startswith(label+'|') for k in current):raise RuntimeError('RECOVERY_CONTEXT_IDENTITY_DRIFT')
        states.update(current);skips.update(read_skips(directory,fingerprint))
    charges=read_events(Path(run)/'GLOBAL_ATTEMPTS.jsonl')
    if len(charges)!=local_attempts:raise RuntimeError('CHARGED_BUT_UNRESOLVED_ATTEMPT')
    if len(charges)+15>15212:raise RuntimeError('RECOVERY_PHYSICAL_CEILING')
    report=build_receipt(states,skips)
    report.update(actual_attempts_including_history=15+len(charges),historical_attempts=12,diagnostic_attempts=3,
                  recovery_actual_attempts=len(charges),logical_reused=sum(k in states for k in reuse),
                  unique_completed_logical=sum(v=='SUCCESS' for v in states.values()),logical_object_ceiling=15198,physical_attempt_ceiling=15212,
                  history_preserved=True,fingerprint=fingerprint)
    return report


def coordinate_stage(root,run,stage):
    """Runs inside the externally owned Job; the supervisor owns all termination."""
    import concurrent.futures as futures
    import multiprocessing as mp
    import traceback
    from .development_run import iter_development_contexts
    root=Path(root);run=Path(run);draft=validate_recovery_authorization(root)
    contexts=list(iter_development_contexts(root));fingerprint=sha256(root/'audit/development/recovery_v1/RECOVERY_AUTHORIZATION_DRAFT.json')
    lineage=draft['old_attempt_lineage'];reuse={k:{**v,'path':str(bound_path(root,v['path']))} for k,v in draft['old_success_reuse'].items()}
    failed=next(c for c in contexts if c['context']==FAILED_CONTEXT)
    try:
        if stage=='failed_object':return run_context(root,run,fingerprint,failed,lineage,reuse,True)
        if stage=='first_context':return run_context(root,run,fingerprint,failed,lineage,reuse)
        if stage!='remaining_contexts':raise RuntimeError('INVALID_STAGE')
        pool=futures.ProcessPoolExecutor(max_workers=12,mp_context=mp.get_context('spawn'))
        success=False
        try:
            pending=[pool.submit(run_context,str(root),str(run),fingerprint,c,lineage,reuse) for c in contexts[:-1] if c['context']!=FAILED_CONTEXT]
            completed=[future.result() for future in futures.as_completed(pending)]
            completed.append(pool.submit(run_context,str(root),str(run),fingerprint,contexts[-1],lineage,reuse).result())
            counts=reconcile_recovery(run,fingerprint,lineage,reuse)
            durable_json(run/'FIT_RECONCILIATION.json',counts,exclusive=True)
            if counts['status']!='RECONCILED':raise RuntimeError('RECOVERY_FIT_RECONCILIATION_INCOMPLETE')
            from .development_postfit import evaluate
            postfit=evaluate(root,run,fingerprint,contexts[-1],RecoveryWriteGuard(run))
            durable_json(run/'POSTFIT_RESULT.json',postfit,exclusive=True)
            if postfit['status']!='COMPLETE':raise RuntimeError('POSTFIT_HOLD')
            success=True
            return {'completed':completed,'status':'COMPLETE','postfit':postfit,'actual_attempts':counts['actual_attempts_including_history']}
        finally:pool.shutdown(wait=success,cancel_futures=True)
    except BaseException:
        try:durable_json(run/'ROOT_CAUSE.json',{'stage':stage,'exception_chain':traceback.format_exc(),'status':'PROJECT_HOLD'},exclusive=True)
        finally:raise


def supervise_recovery(command,run,stage,*,wall_limit,cpu_limit,workers,minimum_free_bytes=100*1024**3):
    """Controller survives child-tree kill and queries final Job accounting."""
    import ctypes
    from ctypes import wintypes
    import subprocess
    import shutil
    import traceback
    from .m0_recovery_execute import ChildJob,disk_used
    from .development_resources import _ExtendedLimit
    if not 1<=workers<=12:raise RuntimeError('WORKER_LIMIT')
    run=Path(run);directory=run/'supervision'/stage;directory.mkdir(parents=True)
    artifact_root=next((p for p in run.parents if p.name=='development'),run)
    job=ChildJob();process=None;reason=None;error=None
    start=time.monotonic();cpu_start=time.process_time();last=None;peak_active=0
    try:
        limits=_ExtendedLimit();limits.basic.flags=0x2000|0x8|0x100|0x200
        limits.basic.active_process_limit=2+2*workers
        limits.process_memory_limit=32*1024**3;limits.job_memory_limit=32*1024**3
        if not job.k.SetInformationJobObject(job.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        env=dict(os.environ)
        for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):env[key]='1'
        with (directory/'stdout.log').open('xb') as stdout,(directory/'stderr.log').open('xb') as stderr:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,env=env,cwd=run,
                                     creationflags=subprocess.CREATE_NO_WINDOW|0x4)
            job.assign(process)
            durable_json(directory/'RESOURCE_STATUS.json',{'status':'GATED','pid':process.pid,'parent_pid':os.getpid(),'outside_job':True})
            resume=ctypes.WinDLL('ntdll').NtResumeProcess;resume.argtypes=[wintypes.HANDLE];resume.restype=ctypes.c_long
            if resume(int(process._handle))!=0:raise RuntimeError('CHILD_RESUME_FAILED')
            process.stdin.write(b'GO\n');process.stdin.flush();process.stdin.close()
            while process.poll() is None:
                a=job.accounting();wall=time.monotonic()-start;cpu=a['child_cpu_seconds']+time.process_time()-cpu_start
                peak_active=max(peak_active,a['active_child_n'])
                if (run/'ROOT_CAUSE.json').exists():reason='CHILD_ROOT_FAILURE'
                elif wall>=wall_limit:reason='WALL_BUDGET'
                elif cpu>=cpu_limit:reason='CPU_BUDGET'
                elif disk_used(artifact_root)+128*1024**2>=50*1024**3:reason='DISK_BUDGET'
                elif shutil.disk_usage(run).free<minimum_free_bytes+128*1024**2:reason='FREE_DISK_BUDGET'
                last={'status':'STOPPING' if reason else 'RUNNING','stage':stage,'reason':reason,'wall_seconds':wall,
                      'controller_cpu_seconds':time.process_time()-cpu_start,'actual_attempts':15+len(read_events(run/'GLOBAL_ATTEMPTS.jsonl')),**a}
                durable_json(directory/'RESOURCE_STATUS.json',last)
                if reason:
                    if not (run/'ROOT_CAUSE.json').exists():durable_json(run/'ROOT_CAUSE.json',last,exclusive=True)
                    job.kill();break
                time.sleep(.25)
            process.wait(timeout=15)
            a=job.accounting();wall=time.monotonic()-start;cpu=a['child_cpu_seconds']+time.process_time()-cpu_start
            result={'status':'FINAL_STOPPED','stage':stage,'outcome':'SUCCESS' if process.returncode==0 and reason is None else 'FAILURE',
                    'reason':reason,'exit_code':process.returncode,'wall_seconds':wall,'controller_cpu_seconds':time.process_time()-cpu_start,**a,
                    'accounting_cpu_seconds':cpu+1.,'accounting_wall_seconds':wall+1.,
                    'terminal_reserve':'One-second conservative charge, not measured CPU','outside_job':True,
                    'memory_job_limit_gib':32,'model_workers':workers,'os_process_ceiling':2+2*workers,
                    'peak_active_child_n':peak_active,
                    'actual_attempts':15+len(read_events(run/'GLOBAL_ATTEMPTS.jsonl')),'poll_seconds':.25,'possible_polling_overshoot':True}
            durable_json(directory/'RESOURCE_STATUS.json',result)
            durable_json(directory/'FINAL_STOPPED.json',result,exclusive=True)
            return result
    except BaseException:
        error=traceback.format_exc()
        try:
            if not (run/'ROOT_CAUSE.json').exists():durable_json(run/'ROOT_CAUSE.json',{'stage':stage,'exception_chain':error,'last_snapshot':last},exclusive=True)
        finally:raise
    finally:
        if process is not None and process.poll() is None:
            try:job.kill()
            finally:process.kill();process.wait(timeout=15)
        if process is not None and process.stdin is not None and not process.stdin.closed:process.stdin.close()
        if error is not None:
            try:
                a=job.accounting();wall=time.monotonic()-start;cpu=a['child_cpu_seconds']+time.process_time()-cpu_start
                failed={'status':'FINAL_STOPPED','stage':stage,'outcome':'FAILURE','reason':'SUPERVISOR_EXCEPTION','exception_chain':error,
                        'wall_seconds':wall,'controller_cpu_seconds':time.process_time()-cpu_start,**a,
                        'accounting_cpu_seconds':cpu+1.,'accounting_wall_seconds':wall+1.,'outside_job':True}
                durable_json(directory/'FINAL_STOPPED.json',failed,exclusive=True)
            except BaseException:pass  # Broken storage cannot persist its own error; stage stays locked.
        job.close()


def run_recovery_stage(root,stage):
    """Public production entry: requires new hash-bound manual signature."""
    import sys
    controller_wall_start=time.monotonic();controller_cpu_start=time.process_time()
    root=Path(root).resolve()
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[key]='1'
    draft=validate_recovery_authorization(root)
    run=bound_path(root,draft['recovery_run_directory']);run.mkdir(parents=True,exist_ok=True)
    lock=run/'EXTERNAL_CONTROLLER.lock'
    with lock.open('x') as f:f.write(str(os.getpid()));f.flush();os.fsync(f.fileno())
    complete=False
    try:
        history=read_events(run/'STAGES.jsonl');check_stage(stage,history)
        if (run/'ROOT_CAUSE.json').exists():raise RuntimeError('RECOVERY_FAILURE_REQUIRES_NEW_REVIEW')
        lineage={'original_active_run_sha256':draft['bindings']['audit/development/ACTIVE_RUN.json'],
                 'old_fingerprint':draft['old_fingerprint'],'recovery_draft_sha256':sha256(root/'audit/development/recovery_v1/RECOVERY_AUTHORIZATION_DRAFT.json'),
                 'prior_actual_attempts':15,'logical_objects':15198,'physical_ceiling':15212}
        if not (run/'LINEAGE.json').exists():durable_json(run/'LINEAGE.json',lineage,exclusive=True)
        elif json.loads((run/'LINEAGE.json').read_text())!=lineage:raise RuntimeError('RECOVERY_LINEAGE_DRIFT')
        budget=remaining_budget(draft['resource_charges'],history)
        # One append consumes this stage even if launch subsequently fails.
        append_event(run/'STAGE_STARTS.jsonl',{'stage':stage,'budget':budget,'draft_sha256':lineage['recovery_draft_sha256']})
        if sum(r['stage']==stage for r in read_events(run/'STAGE_STARTS.jsonl'))!=1:raise RuntimeError('STAGE_START_NOT_REPLAYABLE')
        command=[str(root/'.venv-a5/Scripts/python.exe'),'-B',str(Path(__file__).resolve()),'--child',str(root),stage]
        supervised_wall=budget['remaining_wall_seconds']-(time.monotonic()-controller_wall_start)
        supervised_cpu=budget['remaining_cpu_seconds']-(time.process_time()-controller_cpu_start)
        if supervised_wall<=0 or supervised_cpu<=0:raise RuntimeError('PRELAUNCH_RESOURCE_BUDGET_EXHAUSTED')
        result=supervise_recovery(command,run,stage,wall_limit=supervised_wall,cpu_limit=supervised_cpu,workers=12 if stage=='remaining_contexts' else 1)
        result['supervised_child_window_wall_seconds']=result['wall_seconds']
        result['supervised_controller_cpu_seconds']=result['controller_cpu_seconds']
        result['wall_seconds']=time.monotonic()-controller_wall_start
        result['controller_cpu_seconds']=time.process_time()-controller_cpu_start
        result['accounting_cpu_seconds']=result['child_cpu_seconds']+result['controller_cpu_seconds']+1.
        result['accounting_wall_seconds']=result['wall_seconds']+1.
        result['measurement_scope']='Authorization, lineage and launch through external supervision; child Job CPU counted once'
        durable_json(run/(stage+'_FINAL_STOPPED.json'),result,exclusive=True)
        append_event(run/'STAGES.jsonl',result);complete=True
        return result
    finally:
        if complete:lock.unlink()


if __name__=='__main__':
    import sys
    if len(sys.argv)==4 and sys.argv[1]=='--child':
        if sys.stdin.readline().strip()!='GO':raise RuntimeError('SUPERVISOR_REQUIRED')
        root=Path(sys.argv[2]).resolve();sys.path.insert(0,str(root/'src'))
        from project_b.recovery_production import validate_recovery_authorization,coordinate_stage,durable_json
        draft=validate_recovery_authorization(root);run=root/draft['recovery_run_directory']
        result=coordinate_stage(root,run,sys.argv[3]);durable_json(run/(sys.argv[3]+'_RESULT.json'),result,exclusive=True)
    else:
        import argparse
        parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--stage',required=True,choices=['failed_object','first_context','remaining_contexts'])
        args=parser.parse_args();sys.path.insert(0,str(Path(args.root)/'src'))
        from project_b.recovery_production import run_recovery_stage
        print(json.dumps(run_recovery_stage(args.root,args.stage),indent=2))
