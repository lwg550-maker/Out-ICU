"""Explicitly user-authorized IO-only continuation; original signed runs immutable."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    __package__='project_b'

from .m0_recovery_execute import sha256,durable_json,append_event,read_events,bound_path
from .en_production_v4 import (ProductionLedger as ParentLedger,
    validate_authorization as validate_parent,physical_ceiling,_stat_signature,read)
from .recovery_production import context_path,remaining_budget
from .development_run import _encoded,context_labels
from .io_recovery_v5 import SafeRecoveryWriteGuard

ROOT=Path(__file__).resolve().parents[2]
AUTH_REL='audit/development/io_race_resume_v5'
RUN_REL='audit/development/recovery_runs/io_race_resume_v5'
PRIOR=11401
CEILING=15703
_AUTH_CACHE=None


def validate_authorization(root,*,full=False):
    """Current explicit user instruction supplements, never edits, the old signature."""
    global _AUTH_CACHE
    root=Path(root).resolve()
    if root!=ROOT:raise PermissionError('PROJECT_B_ONLY')
    validate_parent(root,full=full)
    manifest_path=root/AUTH_REL/'RESUME_MANIFEST.json'
    if _AUTH_CACHE and not full:
        manifest,stamps,names=_AUTH_CACHE
        if _stat_signature(root,names)==stamps:return manifest
    manifest=read(manifest_path)
    if (manifest['authorization_basis']!='CURRENT_USER_INSTRUCTION'
        or manifest['status']!='AUTHORIZED_IO_ONLY_CONTINUATION'
        or manifest['source_run_directory']!='audit/development/recovery_runs/en_solver_recovery_v4'
        or manifest['run_directory']!=RUN_REL or manifest['prior_actual_attempts']!=PRIOR
        or manifest['scientific_objective_changed'] is not False
        or manifest['temporal_unseal_authorized'] is not False
        or manifest['physical_call_maximum']!=CEILING
        or len(manifest['success_reuse'])!=10896 or len(manifest['completed_contexts'])!=27):
        raise PermissionError('IO_RESUME_AUTHORIZATION_DRIFT')
    for name,digest in manifest['bindings'].items():
        if sha256(bound_path(root,name))!=digest:raise PermissionError('IO_RESUME_BINDING_DRIFT: '+name)
    review=read(root/AUTH_REL/'VALIDATION.json')
    if review['status']!='PASS' or review['research_fits']!=0:
        raise PermissionError('IO_FIX_TESTS_REQUIRED')
    remaining_budget(manifest['resource_charges'],[])
    names=list(manifest['bindings'])+[str(manifest_path.relative_to(root))]
    _AUTH_CACHE=(manifest,_stat_signature(root,names),names)
    return manifest


def pending_context_labels(labels,completed):
    return [label for label in labels if label!='full_development' and label not in completed]


class AttemptCounter:
    def __init__(self,out):
        self.out=Path(out);self.out.mkdir(parents=True,exist_ok=True)
    def charge(self,task,lineage):
        import msvcrt
        with (self.out/'GLOBAL_ATTEMPTS.lock').open('a+b') as stream:
            stream.seek(0,2)
            if stream.tell()==0:stream.write(b'0');stream.flush()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
            try:
                events=read_events(self.out/'GLOBAL_ATTEMPTS.jsonl')
                if any(row['task']==task for row in events):raise RuntimeError('DUPLICATE_OR_INTERRUPTED_ATTEMPT')
                if PRIOR+len(events)>=CEILING:raise RuntimeError('GLOBAL_ACTUAL_CEILING')
                row={'action':'CHARGE_START','task':task,'attempt_id':'io_race_resume_v5:'+task,
                     'prior_attempts':PRIOR,'total_actual_attempts':PRIOR+len(events)+1,**lineage}
                append_event(self.out/'GLOBAL_ATTEMPTS.jsonl',row);return row
            finally:
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)


class ProductionLedger(ParentLedger):
    def __init__(self,directory,fingerprint,run,lineage,reuse):
        super().__init__(directory,fingerprint,run,lineage,reuse,prior=PRIOR,ceiling=CEILING)
        self.counter=AttemptCounter(run)


def run_context(root,run,fingerprint,context,lineage,reuse):
    from .io_context_v5 import run_one_context
    from .development_parallel import load_context
    label=context['context'];directory=context_path(run,label)
    ledger=ProductionLedger(directory,fingerprint,run,lineage,reuse)
    ledger.context=label;ledger.guard=SafeRecoveryWriteGuard(run)
    with ledger:
        if load_context(directory,fingerprint,label) is not None:
            return {'context':label,'cached':True,'new_calls':0}
        payload=run_one_context(ledger=ledger,project_root=root,**context)
        durable_json(directory/'CONTEXT.json',{'fingerprint':fingerprint,'context':label,
            'sha256':hashlib.sha256(_encoded(payload)).hexdigest(),'payload':payload},exclusive=True)
    return {'context':label,'completed':True,'new_calls':ledger.attempts}


def import_completed_contexts(root,run,fingerprint,manifest):
    from .development_parallel import load_context
    from .development_accounting import frozen_plan
    plan=frozen_plan();imported=[]
    for label,item in manifest['completed_contexts'].items():
        source=bound_path(root,item['path'])
        if sha256(source)!=item['sha256']:raise RuntimeError('COMPLETED_CONTEXT_DRIFT')
        record=read(source);payload=load_context(source.parent,record['fingerprint'],label)
        directory=context_path(run,label)
        ledger=ProductionLedger(directory,fingerprint,run,manifest['old_attempt_lineage'],manifest['success_reuse'])
        states,n=ledger._read()
        if n or set(states)!={key for key in plan if key.startswith(label+'|')} or set(states.values())!={'SUCCESS'}:
            raise RuntimeError('COMPLETED_CONTEXT_REUSE_INCOMPLETE')
        durable_json(directory/'CONTEXT.json',{'fingerprint':fingerprint,'context':label,
            'sha256':hashlib.sha256(_encoded(payload)).hexdigest(),'payload':payload,
            'reused_from':item},exclusive=True)
        imported.append(label)
    durable_json(Path(run)/'COMPLETED_CONTEXT_REUSE.json',{
        'status':'PASS','contexts':imported,'count':len(imported),'new_fit_calls':0},exclusive=True)
    return imported


def reconcile(run,fingerprint,manifest):
    from .development_accounting import build_receipt,read_skips
    states={};skips={};attempts=0
    for label in context_labels():
        directory=context_path(run,label)
        ledger=ProductionLedger(directory,fingerprint,run,manifest['old_attempt_lineage'],manifest['success_reuse'])
        current,n=ledger._read();states.update(current);attempts+=n
        skips.update(read_skips(directory,fingerprint))
    charges=read_events(Path(run)/'GLOBAL_ATTEMPTS.jsonl')
    if len(charges)!=attempts or PRIOR+attempts>CEILING:
        raise RuntimeError('GLOBAL_LOCAL_COUNT_MISMATCH')
    result=build_receipt(states,skips)
    result.update(prior_actual_attempts=PRIOR,new_actual_attempts=attempts,
        total_actual_attempts=PRIOR+attempts,reused=len(manifest['success_reuse']),
        logical_objects=15198,physical_ceiling=CEILING)
    return result


def coordinate_stage(root,run):
    import concurrent.futures as futures
    import multiprocessing as mp
    from .development_run import iter_development_contexts
    manifest=validate_authorization(root,full=True)
    fingerprint=sha256(Path(root)/AUTH_REL/'RESUME_MANIFEST.json')
    try:
        imported=import_completed_contexts(root,run,fingerprint,manifest)
        contexts=list(iter_development_contexts(root));by_label={c['context']:c for c in contexts}
        pending=pending_context_labels(context_labels(),manifest['completed_contexts'])
        durable_json(Path(run)/'PROGRESS.json',{'status':'RUNNING','completed_outer_contexts':imported,
            'remaining_outer_contexts':pending,'temporal_unsealed':False})
        pool=futures.ProcessPoolExecutor(max_workers=12,mp_context=mp.get_context('spawn'));success=False
        try:
            submitted={pool.submit(run_context,str(root),str(run),fingerprint,by_label[label],
                manifest['old_attempt_lineage'],manifest['success_reuse']):label for label in pending}
            for future in futures.as_completed(submitted):
                result=future.result();imported.append(result['context'])
                durable_json(Path(run)/'PROGRESS.json',{'status':'RUNNING','completed_outer_contexts':imported,
                    'completed_outer_n':len(imported),'temporal_unsealed':False})
            pool.submit(run_context,str(root),str(run),fingerprint,by_label['full_development'],
                manifest['old_attempt_lineage'],manifest['success_reuse']).result()
            counts=reconcile(run,fingerprint,manifest)
            durable_json(Path(run)/'FIT_RECONCILIATION.json',counts,exclusive=True)
            if counts['status']!='RECONCILED':raise RuntimeError('RECONCILIATION_HOLD')
            from .development_postfit import evaluate
            result=evaluate(root,run,fingerprint,by_label['full_development'],SafeRecoveryWriteGuard(run))
            durable_json(Path(run)/'POSTFIT_RESULT.json',result,exclusive=True)
            if result['status']!='COMPLETE':raise RuntimeError('POSTFIT_HOLD')
            success=True
            return {'status':'COMPLETE','total_calls':counts['total_actual_attempts'],'postfit':result}
        finally:
            pool.shutdown(wait=success,cancel_futures=True)
    except BaseException:
        durable_json(Path(run)/'ROOT_CAUSE.json',{'status':'HOLD','exception_chain':traceback.format_exc()},exclusive=True)
        raise


def run_stage(root):
    root=Path(root).resolve();wall=time.monotonic();cpu=time.process_time()
    manifest=validate_authorization(root,full=True)
    run=bound_path(root,RUN_REL);run.mkdir(parents=True,exist_ok=True)
    if (run/'STAGE_STARTS.jsonl').exists() or (run/'ROOT_CAUSE.json').exists():
        raise RuntimeError('INSPECT_EXISTING_RESUME_BEFORE_REPLAY')
    lock=run/'EXTERNAL_CONTROLLER.lock'
    with lock.open('x') as stream:stream.write(str(os.getpid()));stream.flush();os.fsync(stream.fileno())
    complete=False
    try:
        budget=remaining_budget(manifest['resource_charges'],[])
        lineage={'authorization_basis':'CURRENT_USER_INSTRUCTION',
            'resume_manifest_sha256':sha256(root/AUTH_REL/'RESUME_MANIFEST.json'),
            'original_signature_sha256':manifest['original_signature_sha256'],
            'prior_actual_attempts':PRIOR,'physical_ceiling':CEILING,'temporal_unsealed':False}
        durable_json(run/'LINEAGE.json',lineage,exclusive=True)
        append_event(run/'STAGE_STARTS.jsonl',{'stage':'resume_remaining','budget':budget,**lineage})
        durable_json(run/'NEW_RUN_STATE.json',{'status':'RUNNING','stage':'resume_remaining',**lineage})
        command=[str(root/'.venv-a5/Scripts/python.exe'),'-B',str(Path(__file__).resolve()),'--child',str(root)]
        result=supervise_v2(command,run,'resume_remaining',
            wall_limit=budget['remaining_wall_seconds']-(time.monotonic()-wall),
            cpu_limit=budget['remaining_cpu_seconds']-(time.process_time()-cpu),workers=12)
        result['accounting_cpu_seconds']=result['child_cpu_seconds']+time.process_time()-cpu+1
        result['accounting_wall_seconds']=time.monotonic()-wall+1
        validate_authorization(root,full=True)
        durable_json(run/'resume_remaining_FINAL_STOPPED.json',result,exclusive=True)
        append_event(run/'STAGES.jsonl',result);complete=True
        durable_json(run/'NEW_RUN_STATE.json',{'status':'COMPLETE' if result['outcome']=='SUCCESS' else 'HOLD',
            'stage':'resume_remaining',**lineage})
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
    from .m0_recovery_execute import ChildJob
    from .io_recovery_v5 import disk_used
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
                      'controller_cpu_seconds':time.process_time()-cpu_start,'actual_attempts':PRIOR+len(read_events(run/'GLOBAL_ATTEMPTS.jsonl')),**a}
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
                    'actual_attempts':PRIOR+len(read_events(run/'GLOBAL_ATTEMPTS.jsonl')),'poll_seconds':.25,'possible_polling_overshoot':True}
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
    parser=argparse.ArgumentParser()
    parser.add_argument('--verify-authorization',action='store_true')
    parser.add_argument('--stage',choices=['resume_remaining'])
    parser.add_argument('--child')
    args=parser.parse_args()
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[key]='1'
    if args.child:
        if sys.stdin.readline().strip()!='GO':raise PermissionError('SUPERVISOR_GO_REQUIRED')
        root=Path(args.child).resolve()
        manifest=validate_authorization(root,full=True)
        result=coordinate_stage(root,bound_path(root,RUN_REL))
        durable_json(bound_path(root,RUN_REL)/'resume_remaining_RESULT.json',result,exclusive=True)
    elif args.verify_authorization:
        manifest=validate_authorization(ROOT,full=True)
        print(json.dumps({'status':'CURRENT_USER_AUTHORIZATION_AND_BINDINGS_VALID',
            'reused':len(manifest['success_reuse']),'completed_contexts':len(manifest['completed_contexts'])}))
    elif args.stage:
        print(json.dumps(run_stage(ROOT)))
    else:
        parser.error('--verify-authorization or --stage required')
