"""Aggregate Windows Job memory/CPU guard with one-second wall/storage polling.
Polling is discrete and does not claim mathematical zero overshoot.
"""
import ctypes
import os
from ctypes import wintypes


class _BasicLimit(ctypes.Structure):
    _fields_=[('per_process_user_time',ctypes.c_longlong),('per_job_user_time',ctypes.c_longlong),
              ('flags',wintypes.DWORD),('minimum_working_set',ctypes.c_size_t),
              ('maximum_working_set',ctypes.c_size_t),('active_process_limit',wintypes.DWORD),
              ('affinity',ctypes.c_size_t),('priority_class',wintypes.DWORD),('scheduling_class',wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_=[(name,ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]


class _ExtendedLimit(ctypes.Structure):
    _fields_=[('basic',_BasicLimit),('io',_IOCounters),('process_memory_limit',ctypes.c_size_t),
              ('job_memory_limit',ctypes.c_size_t),('peak_process_memory_used',ctypes.c_size_t),
              ('peak_job_memory_used',ctypes.c_size_t)]


class MemoryJob:
    """Parent plus bounded children share a 32 GiB Windows Job commit limit."""
    def __init__(self, workers=12):
        if os.name!='nt': raise RuntimeError('WINDOWS_JOB_OBJECT_REQUIRED')
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=(ctypes.c_void_p,wintypes.LPCWSTR)
        self.kernel.CreateJobObjectW.restype=wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes=(wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD)
        self.kernel.SetInformationJobObject.restype=wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes=(wintypes.HANDLE,wintypes.HANDLE)
        self.kernel.AssignProcessToJobObject.restype=wintypes.BOOL
        self.kernel.GetCurrentProcess.restype=wintypes.HANDLE
        self.kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
        self.name='ProjectB_'+str(os.getpid())+'_'+__import__('uuid').uuid4().hex
        self.handle=self.kernel.CreateJobObjectW(None,self.name)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
        try:
            info=_ExtendedLimit()
            info.basic.flags=0x00000008 | 0x00000100 | 0x00000200 | 0x00002000
            info.basic.active_process_limit=workers+1
            info.process_memory_limit=32*1024**3
            info.job_memory_limit=32*1024**3
            self.limit_info=info
            if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle,self.kernel.GetCurrentProcess()):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close(); raise

    def cpu_seconds(self):
        class Accounting(ctypes.Structure):
            _fields_=[('user',ctypes.c_longlong),('kernel',ctypes.c_longlong),
                      ('period_user',ctypes.c_longlong),('period_kernel',ctypes.c_longlong),
                      ('faults',wintypes.DWORD),('total',wintypes.DWORD),
                      ('active',wintypes.DWORD),('terminated',wintypes.DWORD)]
        self.kernel.QueryInformationJobObject.argtypes=(wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p)
        info=Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle,1,ctypes.byref(info),ctypes.sizeof(info),None):
            raise ctypes.WinError(ctypes.get_last_error())
        return (info.user+info.kernel)/1e7

    def terminate(self):
        self.kernel.TerminateJobObject.argtypes=(wintypes.HANDLE,wintypes.UINT)
        if not self.kernel.TerminateJobObject(self.handle,86):
            error=ctypes.get_last_error()
            # Last-handle closure retains KILL_ON_JOB_CLOSE as fallback.
            self.kernel.CloseHandle(self.handle);self.handle=None
            raise ctypes.WinError(error)

    def close(self):
        if self.handle:
            # All owned workers have been joined on normal exit. Clear only the
            # kill-on-close flag so closing our last handle does not kill parent.
            if hasattr(self,'limit_info'):
                info=self.limit_info
                info.basic.flags &= ~0x00002000
                if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
                    self.terminate()
                    raise ctypes.WinError(ctypes.get_last_error())
            self.kernel.CloseHandle(self.handle); self.handle=None

    def __enter__(self): return self
    def __exit__(self,*_): self.close()

import json
import shutil
import threading
import time
from pathlib import Path

class ResourceGuard:
    """Fail-stop aggregate development resource guard; polling interval is 1 s."""
    WALL_LIMIT=96*3600
    CPU_LIMIT=800*3600
    DISK_LIMIT=50*1024**3
    MINIMUM_FREE=100*1024**3
    RESERVE=128*1024**2

    def __init__(self, run_dir, fingerprint, workers=12):
        self.workers=workers
        self.path=Path(run_dir); self.path.mkdir(parents=True,exist_ok=True)
        self.fingerprint=fingerprint; self.receipt=self.path/'RESOURCE_STATUS.json'
        self.stop=threading.Event(); self.reason=None
        self.previous_wall=0.; self.previous_cpu=0.; self.job=None
        self.disk_lock=threading.Lock(); self.cached_disk=None; self.disk_scan_at=0.; self.reserved_bytes=0
        if self.receipt.exists():
            prior=json.loads(self.receipt.read_text(encoding='utf-8'))
            if prior.get('fingerprint')!=fingerprint: raise ValueError('RESOURCE_FINGERPRINT_DRIFT')
            if prior.get('status')!='STOPPED': raise RuntimeError('RESOURCE_INTERRUPTION_HOLD')
            self.previous_wall=float(prior['wall_seconds']); self.previous_cpu=float(prior['cpu_seconds'])

    def disk_used(self):
        total=0
        for root, dirs, files in os.walk(self.path,followlinks=False):
            for d in dirs:
                if (Path(root)/d).is_symlink(): raise RuntimeError('RESOURCE_SYMLINK_HOLD')
            for name in files:
                p=Path(root)/name
                if p.is_symlink(): raise RuntimeError('RESOURCE_SYMLINK_HOLD')
                try: total+=p.stat().st_size
                except FileNotFoundError: continue
        return total

    def check_write(self, additional_bytes=0):
        if additional_bytes<0: raise ValueError('negative write size')
        with self.disk_lock:
            now=time.monotonic()
            if self.cached_disk is None or now-self.disk_scan_at>=1:
                self.cached_disk=self.disk_used(); self.disk_scan_at=now; self.reserved_bytes=0
            self.reserved_bytes+=additional_bytes
            if self.cached_disk+self.reserved_bytes+self.RESERVE > self.DISK_LIMIT: raise RuntimeError('ARTIFACT_DISK_HOLD')
            if shutil.disk_usage(self.path).free-self.reserved_bytes-self.RESERVE < self.MINIMUM_FREE: raise RuntimeError('FREE_DISK_HOLD')
            if self.reason: raise RuntimeError(self.reason)

    def _snapshot(self,status,reason=None):
        wall=self.previous_wall+time.monotonic()-self.wall_start
        cpu=self.previous_cpu+self.job.cpu_seconds()-self.cpu_start
        doc={'status':status,'reason':reason,'fingerprint':self.fingerprint,'wall_seconds':wall,
             'cpu_seconds':cpu,'poll_interval_seconds':1,'process_limit':self.workers+1,'job_commit_limit_gib':32}
        temp=self.receipt.with_suffix('.tmp')
        with temp.open('w',encoding='utf-8') as f:
            json.dump(doc,f,sort_keys=True,allow_nan=False); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temp,self.receipt)
        return wall,cpu

    def _watch(self):
        while not self.stop.wait(1):
            try:
                wall,cpu=self._snapshot('RUNNING')
                if wall>=self.WALL_LIMIT: raise RuntimeError('WALL_LIMIT_HOLD')
                if cpu>=self.CPU_LIMIT: raise RuntimeError('CPU_LIMIT_HOLD')
                self.check_write()
            except BaseException as exc:
                self.reason=str(exc)
                try: self._snapshot('HOLD',self.reason)
                finally: self.job.terminate()

    def __enter__(self):
        self.check_write()
        self.job=MemoryJob(self.workers)
        self.wall_start=time.monotonic(); self.cpu_start=self.job.cpu_seconds()
        if self.previous_wall>=self.WALL_LIMIT or self.previous_cpu>=self.CPU_LIMIT:
            self.job.close(); raise RuntimeError('RESOURCE_BUDGET_EXHAUSTED')
        self._snapshot('RUNNING')
        self.thread=threading.Thread(target=self._watch,name='development-resource-guard',daemon=True)
        self.thread.start()
        return self

    def __exit__(self,kind,value,tb):
        self.stop.set(); self.thread.join()
        self._snapshot('HOLD' if kind else 'STOPPED',str(value) if kind else None)
        self.job.close()
