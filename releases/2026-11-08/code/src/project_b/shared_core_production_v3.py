"""Manually signed EN recovery lineage. All prior sources and runs stay read-only."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    __package__='project_b'
from .m0_recovery_execute import sha256,durable_json,append_event,read_events,bound_path
from .development_run import DurableTasks,_encoded,context_labels
from .recovery_production import RecoveryWriteGuard,remaining_budget,context_path

ROOT=Path(__file__).resolve().parents[2]
AUTH_REL='audit/development/shared_core_ridge_v3'
RUN_REL='audit/development/recovery_runs/shared_core_ridge_v3'
FAILED_CONTEXT='outer_repeat=2|outer_fold=5'
_AUTH_CACHE=None


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def physical_ceiling(logical,prior,reused):
    if not 0<=reused<=logical or prior<reused:raise ValueError('COUNT_INPUT')
    return logical-reused+prior


def utc(value):
    from datetime import datetime
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z',value):
        raise PermissionError('INVALID_UTC')
    try:return datetime.fromisoformat(value[:-1]+'+00:00')
    except ValueError as exc:raise PermissionError('INVALID_UTC') from exc


def validate_signature_record(signature,expected_sha,reviewed_at):
    keys={'schema_version','researcher_id','approved_at_utc','authorization_draft_path','authorization_draft_sha256','approved'}
    if (set(signature)!=keys or signature['schema_version']!='1.0' or signature['approved'] is not True
        or signature['authorization_draft_path']!=AUTH_REL+'/RECOVERY_AUTHORIZATION_DRAFT.json'
        or signature['authorization_draft_sha256']!=expected_sha
        or not isinstance(signature['researcher_id'],str) or not signature['researcher_id'].strip()
        or utc(signature['approved_at_utc'])<utc(reviewed_at)):
        raise PermissionError('NEW_MANUAL_EN_RECOVERY_SIGNATURE_REQUIRED')


def _stat_signature(root,names):
    rows=[]
    for name in names:
        stat=bound_path(root,name).stat()
        rows.append((name,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
    return tuple(rows)


def validate_authorization(root,*,full=False):
    """Read-only closed-world validation. Metadata cache avoids rehashing traces per fit."""
    global _AUTH_CACHE
    root=Path(root).resolve()
    if root!=ROOT:raise PermissionError('PROJECT_B_ONLY')
    directory=root/AUTH_REL
    signature_path=directory/'RECOVERY_RESEARCHER_SIGNATURE.json'
    if not signature_path.is_file():raise PermissionError('NEW_MANUAL_EN_RECOVERY_SIGNATURE_REQUIRED')
    draft_path=directory/'RECOVERY_AUTHORIZATION_DRAFT.json'
    review_path=directory/'RECOVERY_IMPLEMENTATION_REVIEW.json'
    if _AUTH_CACHE and not full:
        draft,stamps,names=_AUTH_CACHE
        if _stat_signature(root,names)==stamps:return draft
    signature,draft,review=read(signature_path),read(draft_path),read(review_path)
    validate_signature_record(signature,sha256(draft_path).upper(),review['reviewed_at_utc'])
    if (review['status']!='PASS_IMPLEMENTATION_TESTED' or review['draft_sha256']!=sha256(draft_path).upper()
        or draft['status']!='DRAFT_FOR_RESEARCHER_SIGNATURE' or draft['logical_trainable_objects']!=15198
        or draft['prior_actual_attempts']!=629 or draft['recovery_run_directory']!=RUN_REL
        or draft['physical_trainable_call_maximum']!=physical_ceiling(15198,629,len(draft['success_reuse']))
        or draft['authorized_stages']!=['failed_context','remaining_contexts']
        or draft['temporal_unseal_authorized'] is not False or draft['shared_core_lambda']!=1e-4):
        raise PermissionError('EN_RECOVERY_AUTHORITY_SCHEMA_DRIFT')
    for name,digest in draft['bindings'].items():
        if sha256(bound_path(root,name))!=digest.lower():raise PermissionError('EN_RECOVERY_BINDING_DRIFT: '+name)
    from .m2_en_production_v2 import validate_authorization as validate_prior_authorization
    validate_prior_authorization(root,full=True)
    if len(draft['success_reuse'])!=212 or any(not any('|'+m+'|' in task for m in ('M1_RSF','M2_RSF','M1_GBST','M2_GBST')) for task in draft['success_reuse']):
        raise PermissionError('ONLY_REVIEWED_UNCHANGED_TREE_REUSES')
    remaining_budget(draft['resource_charges'],[])
    names=list(draft['bindings'])+[AUTH_REL+'/'+p.name for p in (signature_path,draft_path,review_path)]
    _AUTH_CACHE=(draft,_stat_signature(root,names),names)
    return draft


def check_stage(stage,history,verification):
    if stage not in ('failed_context','remaining_contexts'):raise RuntimeError('INVALID_STAGE')
    if stage=='failed_context':
        if history:raise RuntimeError('STAGE_NOT_REPLAYABLE')
    elif (len(history)!=1 or history[0].get('stage')!='failed_context'
          or history[0].get('status')!='FINAL_STOPPED' or history[0].get('outcome')!='SUCCESS'
          or not verification or verification.get('status')!='PASS'
          or verification.get('context')!=FAILED_CONTEXT):
        raise RuntimeError('FAILED_CONTEXT_VERIFIED_SUCCESS_REQUIRED')


class AttemptCounterV2:
    def __init__(self,out,prior=629,ceiling=15615):
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True);self.prior=prior;self.ceiling=ceiling
    def charge(self,task,lineage):
        import msvcrt
        with (self.out/'GLOBAL_ATTEMPTS.lock').open('a+b') as stream:
            stream.seek(0,2)
            if stream.tell()==0:stream.write(b'0');stream.flush()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
            try:
                events=read_events(self.out/'GLOBAL_ATTEMPTS.jsonl')
                if any(r['task']==task for r in events):raise RuntimeError('GLOBAL_DUPLICATE_OR_INTERRUPTED')
                if self.prior+len(events)>=self.ceiling:raise RuntimeError('GLOBAL_ACTUAL_CEILING')
                row={'action':'CHARGE_START','task':task,'attempt_id':'shared_core_ridge_v3:'+task,
                     'prior_attempts':self.prior,'total_actual_attempts':self.prior+len(events)+1,**lineage}
                append_event(self.out/'GLOBAL_ATTEMPTS.jsonl',row);return row
            finally:stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)


class ProductionLedger(DurableTasks):
    def __init__(self,directory,fingerprint,run,lineage,reuse,prior=629,ceiling=15615):
        super().__init__(directory,fingerprint)
        self.run=Path(run);self.lineage=lineage;self.reuse=reuse
        self.counter=AttemptCounterV2(run,prior,ceiling)
    def _read(self):
        states,n=super()._read()
        for task,item in self.reuse.items():
            label=task.split('|M',1)[0]
            if context_path(self.run,label).resolve()!=self.path.resolve():continue
            source=bound_path(ROOT,item['path'])
            if sha256(source)!=item['sha256']:raise RuntimeError('REUSE_SOURCE_DRIFT')
            if task in states:raise RuntimeError('REUSED_OBJECT_HAS_NEW_START')
            target=self.path/'results'/(hashlib.sha256(task.encode()).hexdigest()+'.json')
            target.parent.mkdir(exist_ok=True)
            if not target.exists():
                with target.open('xb') as stream:stream.write(source.read_bytes());stream.flush();os.fsync(stream.fileno())
            if sha256(target)!=item['sha256']:raise RuntimeError('IMPORTED_CHECKPOINT_DRIFT')
            states[task]='SUCCESS';self.hashes[task]=item['sha256']
        from .shared_core_ridge_v3 import validate_persisted_numeric
        for task,status in states.items():
            if status=='SUCCESS' and any('|' + m + '|' in task for m in ('M0','M2_EN')) and 'recalibration=' not in task:
                path=self.path/'results'/(hashlib.sha256(task.encode()).hexdigest()+'.json')
                validate_persisted_numeric(self.path,read(path))
        return states,n
    def execute(self,task,operation):
        if self.state.get(task)=='SUCCESS':
            result=super().execute(task,operation)
            if any('|' + m + '|' in task for m in ('M0','M2_EN')) and 'recalibration=' not in task:
                from .shared_core_ridge_v3 import validate_persisted_numeric
                validate_persisted_numeric(self.path,result)
            return result
        if task in self.state:raise RuntimeError('RECORDED_FAILURE_OR_UNRESOLVED_NOT_REPLAYABLE')
        from .development_accounting import read_skips
        if task in read_skips(self.path,self.fingerprint):raise RuntimeError('SKIPPED_TASK_NOT_REPLAYABLE')
        if self.guard:self.guard.check_fit()
        self.counter.charge(task,self.lineage.get(task,{'parent_attempt_id':None,'retry_kind':'PREVIOUSLY_UNSTARTED'}))
        def checked_operation():
            result=operation()
            if any('|' + m + '|' in task for m in ('M0','M2_EN')) and 'recalibration=' not in task:
                from .shared_core_ridge_v3 import validate_persisted_numeric
                validate_persisted_numeric(self.path,result)
            return result
        return super().execute(task,checked_operation)


def run_context(root,run,fingerprint,context,lineage,reuse):
    from .shared_core_context_v3 import run_one_context
    from .development_parallel import load_context
    label=context['context'];directory=context_path(run,label)
    ledger=ProductionLedger(directory,fingerprint,run,lineage,reuse)
    ledger.context=label;ledger.guard=RecoveryWriteGuard(run)
    with ledger:
        existing=load_context(directory,fingerprint,label)
        if existing is not None:return {'context':label,'cached':True}
        payload=run_one_context(ledger=ledger,project_root=root,**context)
        durable_json(directory/'CONTEXT.json',{'fingerprint':fingerprint,'context':label,
            'sha256':hashlib.sha256(_encoded(payload)).hexdigest(),'payload':payload},exclusive=True)
    return {'context':label,'completed':True,'new_calls':ledger.attempts}


def verify_context(run,fingerprint,label,lineage,reuse):
    from .development_parallel import load_context
    from .development_accounting import frozen_plan,read_skips
    directory=context_path(run,label);payload=load_context(directory,fingerprint,label)
    if payload is None or set(payload['models'])!={'M0','M1_EN','M2_EN','M1_RSF','M2_RSF','M1_GBST','M2_GBST'}:
        raise RuntimeError('COMPLETE_CONTEXT_REQUIRED')
    ledger=ProductionLedger(directory,fingerprint,run,lineage,reuse)
    states,n=ledger._read();skips=read_skips(directory,fingerprint)
    expected={k for k in frozen_plan() if k.startswith(label+'|')}
    if (set(states)&set(skips) or set(states)|set(skips)!=expected or 'START' in states.values()
        or payload['models']['M2_EN']['inner']['status']!='PASS'):
        raise RuntimeError('CONTEXT_RECONCILIATION_REQUIRED')
    return {'status':'PASS','context':label,'fingerprint':fingerprint,'context_sha256':sha256(directory/'CONTEXT.json'),
            'attempted':n,'reused':sum(k in states for k in reuse),'skipped':len(skips),
            'new_en_successes_numerically_checked':sum(v=='SUCCESS' and any('|' + m + '|' in k for m in ('M0','M2_EN')) and 'recalibration=' not in k for k,v in states.items())}


def reconcile(run,fingerprint,lineage,reuse,ceiling):
    from .development_accounting import build_receipt,read_skips
    states={};skips={};attempts=0
    for label in context_labels():
        directory=context_path(run,label)
        if not directory.exists():continue
        ledger=ProductionLedger(directory,fingerprint,run,lineage,reuse)
        current,n=ledger._read();attempts+=n
        if any(not task.startswith(label+'|') for task in current):raise RuntimeError('CONTEXT_IDENTITY_DRIFT')
        states.update(current);skips.update(read_skips(directory,fingerprint))
    charged=len(read_events(Path(run)/'GLOBAL_ATTEMPTS.jsonl'))
    if charged!=attempts or charged+629>ceiling:raise RuntimeError('GLOBAL_LOCAL_COUNT_MISMATCH')
    result=build_receipt(states,skips)
    result.update(prior_actual_attempts=629,new_actual_attempts=charged,total_actual_attempts=629+charged,
                  reused=sum(k in states for k in reuse),logical_objects=15198,physical_ceiling=ceiling)
    return result


def coordinate_stage(root,run,stage):
    import concurrent.futures as futures
    import multiprocessing as mp
    from .development_run import iter_development_contexts
    draft=validate_authorization(root,full=True)
    fingerprint=sha256(Path(root)/AUTH_REL/'RECOVERY_AUTHORIZATION_DRAFT.json')
    contexts=list(iter_development_contexts(root));lineage=draft['old_attempt_lineage'];reuse=draft['success_reuse']
    failed=next(c for c in contexts if c['context']==FAILED_CONTEXT)
    try:
        if stage=='failed_context':
            result=run_context(root,run,fingerprint,failed,lineage,reuse)
            verified=verify_context(run,fingerprint,FAILED_CONTEXT,lineage,reuse)
            durable_json(Path(run)/'FAILED_CONTEXT_VERIFIED.json',verified,exclusive=True)
            return {'status':'COMPLETE','context_result':result,'verification':verified}
        if stage!='remaining_contexts':raise RuntimeError('INVALID_STAGE')
        pool=futures.ProcessPoolExecutor(max_workers=12,mp_context=mp.get_context('spawn'));success=False
        try:
            pending=[pool.submit(run_context,str(root),str(run),fingerprint,c,lineage,reuse)
                     for c in contexts[:-1] if c['context']!=FAILED_CONTEXT]
            completed=[f.result() for f in futures.as_completed(pending)]
            completed.append(pool.submit(run_context,str(root),str(run),fingerprint,contexts[-1],lineage,reuse).result())
            counts=reconcile(run,fingerprint,lineage,reuse,draft['physical_trainable_call_maximum'])
            durable_json(Path(run)/'FIT_RECONCILIATION.json',counts,exclusive=True)
            if counts['status']!='RECONCILED':raise RuntimeError('RECONCILIATION_HOLD')
            from .development_postfit import evaluate
            result=evaluate(root,run,fingerprint,contexts[-1],RecoveryWriteGuard(run))
            durable_json(Path(run)/'POSTFIT_RESULT.json',result,exclusive=True)
            if result['status']!='COMPLETE':raise RuntimeError('POSTFIT_HOLD')
            success=True
            return {'status':'COMPLETE','contexts':completed,'postfit':result,'total_calls':counts['total_actual_attempts']}
        finally:pool.shutdown(wait=success,cancel_futures=True)
    except BaseException:
        durable_json(Path(run)/'ROOT_CAUSE.json',{'status':'PROJECT_HOLD','stage':stage,
                     'exception_chain':traceback.format_exc()},exclusive=True)
        raise


def run_stage(root,stage):
    root=Path(root).resolve();wall_start=time.monotonic();cpu_start=time.process_time()
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[key]='1'
    draft=validate_authorization(root,full=True)  # No lock/run writes before manual signature validates.
    run=bound_path(root,draft['recovery_run_directory']);run.mkdir(parents=True,exist_ok=True)
    history=read_events(run/'STAGES.jsonl')
    verification=read(run/'FAILED_CONTEXT_VERIFIED.json') if (run/'FAILED_CONTEXT_VERIFIED.json').exists() else None
    check_stage(stage,history,verification)
    if (run/'ROOT_CAUSE.json').exists():raise RuntimeError('PREVIOUS_FAILURE_STOP')
    if verification:
        if verification['context_sha256']!=sha256(context_path(run,FAILED_CONTEXT)/'CONTEXT.json'):
            raise RuntimeError('FAILED_CONTEXT_VERIFICATION_DRIFT')
        verify_context(run,sha256(root/AUTH_REL/'RECOVERY_AUTHORIZATION_DRAFT.json'),FAILED_CONTEXT,
                       draft['old_attempt_lineage'],draft['success_reuse'])
    lock=run/'EXTERNAL_CONTROLLER.lock'
    with lock.open('x') as stream:stream.write(str(os.getpid()));stream.flush();os.fsync(stream.fileno())
    complete=False
    try:
        lineage={'recovery_draft_sha256':sha256(root/AUTH_REL/'RECOVERY_AUTHORIZATION_DRAFT.json'),
                 'manual_signature_sha256':sha256(root/AUTH_REL/'RECOVERY_RESEARCHER_SIGNATURE.json'),
                 'prior_actual_attempts':629,'logical_objects':15198,
                 'physical_ceiling':draft['physical_trainable_call_maximum'],
                 'old_evidence_preserved':True,'temporal_unsealed':False}
        if not (run/'LINEAGE.json').exists():durable_json(run/'LINEAGE.json',lineage,exclusive=True)
        elif read(run/'LINEAGE.json')!=lineage:raise RuntimeError('NEW_RUN_LINEAGE_DRIFT')
        starts=read_events(run/'STAGE_STARTS.jsonl')
        if any(r['stage']==stage for r in starts):raise RuntimeError('STAGE_NOT_REPLAYABLE')
        budget=remaining_budget(draft['resource_charges'],history)
        append_event(run/'STAGE_STARTS.jsonl',{'stage':stage,'budget':budget,
                    'draft_sha256':sha256(root/AUTH_REL/'RECOVERY_AUTHORIZATION_DRAFT.json')})
        durable_json(run/'NEW_RUN_STATE.json',{'status':'RUNNING','stage':stage,**lineage})
        command=[str(root/'.venv-a5/Scripts/python.exe'),'-B',str(Path(__file__).resolve()),'--child',str(root),stage]
        result=supervise_v2(command,run,stage,wall_limit=budget['remaining_wall_seconds']-(time.monotonic()-wall_start),
                            cpu_limit=budget['remaining_cpu_seconds']-(time.process_time()-cpu_start),
                            workers=12 if stage=='remaining_contexts' else 1)
        result['accounting_cpu_seconds']=result['child_cpu_seconds']+time.process_time()-cpu_start+1
        result['accounting_wall_seconds']=time.monotonic()-wall_start+1
        validate_authorization(root,full=True)
        durable_json(run/(stage+'_FINAL_STOPPED.json'),result,exclusive=True)
        append_event(run/'STAGES.jsonl',result);complete=True
        durable_json(run/'NEW_RUN_STATE.json',{'status':'STAGE_COMPLETE' if result['outcome']=='SUCCESS' else 'HOLD',
                     'stage':stage,**lineage})
        return result
    finally:
        if complete:lock.unlink()


def supervise_v2(command,run,stage,*,wall_limit,cpu_limit,workers,minimum_free_bytes=100*1024**3):
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
                      'controller_cpu_seconds':time.process_time()-cpu_start,'actual_attempts':629+len(read_events(run/'GLOBAL_ATTEMPTS.jsonl')),**a}
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
                    'actual_attempts':629+len(read_events(run/'GLOBAL_ATTEMPTS.jsonl')),'poll_seconds':.25,'possible_polling_overshoot':True}
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


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--verify-signature',action='store_true')
    parser.add_argument('--stage',choices=['failed_context','remaining_contexts']);parser.add_argument('--child',nargs=2)
    args=parser.parse_args()
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):os.environ[key]='1'
    if args.child:
        if sys.stdin.readline().strip()!='GO':raise PermissionError('SUPERVISOR_GO_REQUIRED')
        root,stage=Path(args.child[0]).resolve(),args.child[1]
        draft=validate_authorization(root,full=True);run=bound_path(root,draft['recovery_run_directory'])
        result=coordinate_stage(root,run,stage);durable_json(run/(stage+'_RESULT.json'),result,exclusive=True)
    elif args.verify_signature:
        draft=validate_authorization(ROOT,full=True)
        print(json.dumps({'status':'SIGNATURE_AND_BINDINGS_VALID','run_directory':draft['recovery_run_directory']}))
    elif args.stage:print(json.dumps(run_stage(ROOT,args.stage)))
    else:parser.error('--verify-signature or --stage required')
