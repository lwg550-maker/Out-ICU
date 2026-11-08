"""Bounded Windows-spawn coordinator. No fitting on import."""
import concurrent.futures as futures
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import threading
from .development_run import DurableTasks, _encoded, context_labels, run_one_context
from .development_resources import ResourceGuard


def atomic_json(path, payload, guard=None):
    data=_encoded(payload)
    if guard: guard.check_write(len(data))
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with temp.open('xb') as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    os.replace(temp,path)


def verify_job(name):
    import ctypes
    from ctypes import wintypes
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenJobObjectW.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR)
    k.OpenJobObjectW.restype=wintypes.HANDLE
    k.GetCurrentProcess.restype=wintypes.HANDLE
    k.IsProcessInJob.argtypes=(wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL))
    k.CloseHandle.argtypes=(wintypes.HANDLE,)
    handle=k.OpenJobObjectW(4,False,name)
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    try:
        inside=wintypes.BOOL()
        if not k.IsProcessInJob(k.GetCurrentProcess(),handle,ctypes.byref(inside)) or not inside.value:
            raise RuntimeError('WORKER_NOT_IN_AGGREGATE_JOB')
    finally: k.CloseHandle(handle)
    return os.getpid()


class SharedQuota:
    """Cross-process reservation counter. Never releases reservations mid-session.

    Counting rewrites conservatively avoids races between measurement and writes.
    A restart measures existing bytes before creating a fresh counter.
    """
    def __init__(self, directory, lock, reserved, stop_event=None):
        self.path=Path(directory); self.lock=lock; self.reserved=reserved; self.stop_event=stop_event
    def check_fit(self):
        if self.stop_event is not None and self.stop_event.is_set(): raise RuntimeError("INDEPENDENT_REVIEW_PAUSE")
    def check_write(self, size=0):
        import shutil
        if size<0: raise ValueError('negative size')
        with self.lock:
            if self.reserved.value+size+ResourceGuard.RESERVE>ResourceGuard.DISK_LIMIT:
                raise RuntimeError('ARTIFACT_DISK_HOLD')
            if shutil.disk_usage(self.path).free-size-ResourceGuard.RESERVE<ResourceGuard.MINIMUM_FREE:
                raise RuntimeError('FREE_DISK_HOLD')
            self.reserved.value+=size


_WORKER_QUOTA=None

def initialize_worker(job_name, directory, lock, reserved, stop_event=None):
    global _WORKER_QUOTA
    verify_job(job_name)
    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        if os.environ.get(key)!='1': raise RuntimeError('THREAD_CONFIGURATION_DRIFT')
    _WORKER_QUOTA=SharedQuota(directory,lock,reserved,stop_event)


def context_directory(run_dir,label):
    return Path(run_dir)/'contexts'/hashlib.sha256(label.encode()).hexdigest()


def load_context(directory,fingerprint,label):
    path=Path(directory)/'CONTEXT.json'
    if not path.exists(): return None
    record=json.loads(path.read_text())
    if record['fingerprint']!=fingerprint or record['context']!=label or record['sha256']!=hashlib.sha256(_encoded(record['payload'])).hexdigest():
        raise ValueError('CHECKPOINT_TAMPER')
    payload=record['payload']
    for result in payload.get('models',{}).values():
        model=result.get('refit',{})
        if 'model_file' in model:
            from .development_run import _sha256_file
            model_path=(Path(directory)/model['model_file']).resolve()
            if not model_path.is_relative_to(Path(directory).resolve()) or not model_path.is_file() or _sha256_file(model_path)!=model['model_sha256']:
                raise ValueError('CHECKPOINT_TAMPER')
            sidecar=model.get('calibration_sidecar',{})
            path=(Path(directory)/sidecar.get('path','')).resolve()
            if not path.is_relative_to(Path(directory).resolve()) or not path.is_file() or _sha256_file(path)!=sidecar.get('sha256'):
                raise ValueError('CHECKPOINT_TAMPER')
    return payload


def context_worker(root,run_dir,fingerprint,context):
    label=context['context']; directory=context_directory(run_dir,label)
    with DurableTasks(directory,fingerprint) as ledger:
        ledger.guard=_WORKER_QUOTA
        ledger.ceiling=298
        ledger.context=label
        existing=load_context(directory,fingerprint,label)
        if existing is not None: return {'context':label,'attempts':ledger.attempts,'cached':True}
        try:
            if ledger.guard: ledger.guard.check_fit()
            output=run_one_context(ledger=ledger,project_root=root,**context)
        except Exception as exc:
            if ledger.guard and ledger.guard.stop_event is not None: ledger.guard.stop_event.set()
            if not (directory/'HOLD.json').exists():
                atomic_json(directory/'HOLD.json',{'status':'PROJECT_HOLD','context':label,'reason':str(exc),
                            'fingerprint':fingerprint,'independent_review_required':True})
            raise
        atomic_json(directory/'CONTEXT.json',{'fingerprint':fingerprint,'context':label,
                    'sha256':hashlib.sha256(_encoded(output)).hexdigest(),'payload':output},_WORKER_QUOTA)
        return {'context':label,'attempts':ledger.attempts,'cached':False}


def reconcile(run_dir,fingerprint):
    from .development_accounting import read_skips,build_receipt
    states={};skips={}
    for label in context_labels():
        directory=context_directory(run_dir,label)
        if not directory.exists(): continue
        ledger=DurableTasks(directory,fingerprint)
        current,attempts=ledger._read()
        if attempts>298: raise RuntimeError('CONTEXT_FIT_CEILING_EXCEEDED')
        if any(not key.startswith(label+'|') for key in current): raise ValueError('CONTEXT_LEDGER_DRIFT')
        states.update(current);skips.update(read_skips(directory,fingerprint))
    receipt=build_receipt(states,skips)
    receipt['fingerprint']=fingerprint
    return receipt


def emergency_stop(run_dir,job,error):
    # Bounded control receipt uses the pre-existing 128 MiB reserve, independent
    # of conservative data-write reservations. Disk failure cannot bypass kill.
    try:
        atomic_json(Path(run_dir)/'EMERGENCY_HOLD.json',{'status':'HOLD','reason':str(error)[:4096]})
    except BaseException:
        pass
    finally:
        job.terminate()


def coordinate(root,run_dir,fingerprint,contexts,workers=12):
    if not 1<=workers<=12: raise ValueError('WORKER_LIMIT')
    contexts=list(contexts)
    if [c['context'] for c in contexts]!=context_labels(): raise ValueError('CONTEXT_SCHEDULE_DRIFT')
    run_dir=Path(run_dir); run_dir.mkdir(parents=True,exist_ok=True)
    binding=Path(root)/'audit/development/ACTIVE_RUN.json'
    expected={'run_directory':str(run_dir.resolve()),'fingerprint':fingerprint}
    try:
        with binding.open('xb') as handle:
            handle.write(_encoded(expected)); handle.flush(); os.fsync(handle.fileno())
    except FileExistsError:
        if json.loads(binding.read_text())!=expected: raise RuntimeError('OTHER_RUN_OR_CODE_DRIFT_HOLD')
    # Exclusive run lock plus per-context durable locks; unknown crash accounting blocks resume.
    with DurableTasks(run_dir,fingerprint):
        with ResourceGuard(run_dir,fingerprint,workers) as guard:
            counts=reconcile(run_dir,fingerprint)
            if counts['total']['interrupted']: raise RuntimeError('INTERRUPTED_ATTEMPT_REQUIRES_HOLD')
            spawn=mp.get_context('spawn')
            lock=spawn.Lock(); reserved=spawn.Value('Q',guard.disk_used(),lock=False)
            stop_event=spawn.Event()
            quota=SharedQuota(run_dir,lock,reserved,stop_event)
            progress={'status':'RUNNING','fingerprint':fingerprint,'completed':[],'workers':workers}
            atomic_json(run_dir/'PROGRESS.json',progress,quota)
            pool=futures.ProcessPoolExecutor(max_workers=workers,mp_context=spawn,
                       initializer=initialize_worker,initargs=(guard.job.name,str(run_dir),lock,reserved,stop_event))
            failed=False
            try:
                pending={pool.submit(context_worker,str(root),str(run_dir),fingerprint,c):c['context'] for c in contexts[:-1]}
                for future in futures.as_completed(pending):
                    result=future.result()
                    progress['completed'].append(result)
                    atomic_json(run_dir/'PROGRESS.json',progress,quota)
                # Full-development refits cannot run before every outer context completes.
                result=pool.submit(context_worker,str(root),str(run_dir),fingerprint,contexts[-1]).result()
                progress['completed'].append(result)
            except BaseException as exc:
                failed=True
                stop_event.set()
                emergency_stop(run_dir,guard.job,exc)
                raise
            finally:
                pool.shutdown(wait=not failed,cancel_futures=True)
            counts=reconcile(run_dir,fingerprint)
            receipt_path=run_dir/'FIT_RECONCILIATION.json'
            if receipt_path.exists() and receipt_path.read_bytes()!=_encoded(counts): raise ValueError('FIT_RECEIPT_TAMPER')
            if not receipt_path.exists(): atomic_json(receipt_path,counts,quota)
            if counts['status']!='RECONCILED': raise RuntimeError('FIT_RECONCILIATION_INCOMPLETE')
            progress.update(status='POSTFIT_RUNNING',fit_counts={k:v for k,v in counts.items() if k!='objects'})
            atomic_json(run_dir/'PROGRESS.json',progress,quota)
            from .development_postfit import evaluate
            result=evaluate(root,run_dir,fingerprint,contexts[-1],quota)
            progress.update(status=result['status'],postfit=result,fit_counts=counts)
            atomic_json(run_dir/'PROGRESS.json',progress,quota)
            return progress
