"""Append-only attempts and hash-bound, atomic compact checkpoints.

One Store per context; its caller holds the context lock. No failed physical
attempt is silently retried. Pickled models are trusted local bound artifacts.
"""
import hashlib
import json
import os
import pickle
import time
import uuid
import shutil
import math
from pathlib import Path
from contextlib import contextmanager
from .serialization_io_v6 import QuotaWriter


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()


def preserved_temp(path,guard=None,*,metadata_reserve=False):
    """Return a scanner-compatible temp handle, preserving stale bytes first.

    Caller owns the context/quota lock. Copy (never move/hardlink) interrupted
    bytes to immutable .bin evidence, fsync and verify before any truncation.
    Tiny quota metadata copies use the explicit128MiB metadata reserve.
    """
    tmp=Path(path).with_suffix('.tmp')
    if not tmp.exists():return tmp,tmp.open('xb')
    if tmp.is_symlink() or getattr(tmp.lstat(),'st_file_attributes',0)&0x400 or not tmp.is_file():raise ValueError('UNSAFE_INTERRUPTED_TEMP')
    if metadata_reserve and tmp.stat().st_size>1024**2:raise RuntimeError('QUOTA_TEMP_METADATA_TOO_LARGE')
    expected=sha(tmp);archive=tmp.with_name(tmp.name+'.interrupted.'+uuid.uuid4().hex+'.bin')
    with tmp.open('rb') as source,archive.open('xb') as destination:
        shutil.copyfileobj(source,QuotaWriter(destination,guard),length=1024**2)
        destination.flush();os.fsync(destination.fileno())
    if sha(archive)!=expected or sha(tmp)!=expected:raise ValueError('INTERRUPTED_TEMP_COPY_VERIFICATION_FAILED')
    return tmp,tmp.open('wb')


@contextmanager
def writer_lock(directory,blocking=False):
    """OS lock is released on process death; the persistent file is not a lease."""
    path=Path(directory);path.mkdir(parents=True,exist_ok=True)
    with (path/'WRITER.lock').open('a+b') as f:
        f.seek(0);f.write(b'0');f.flush();f.seek(0)
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(f.fileno(),msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,1)
            except OSError as exc:raise RuntimeError('CONCURRENT_COMPACT_WRITER') from exc
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB))
        try:yield
        finally:
            f.seek(0)
            if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f,fcntl.LOCK_UN)


class Store:
    def __init__(self,directory,fingerprint,guard=None,retry_failed=False):
        self.path=Path(directory);self.path.mkdir(parents=True,exist_ok=True)
        self.fingerprint=fingerprint;self.guard=guard;self.retry_failed=retry_failed
        self._events=None
        self.cp=self.path/'checkpoints';self.cp.mkdir(exist_ok=True)
        bind=self.path/'BINDING.json'
        if bind.exists():
            if json.loads(bind.read_text())!={'fingerprint':fingerprint}:raise ValueError('BINDING_MISMATCH')
        else:self.atomic(bind,{'fingerprint':fingerprint})

    def atomic(self,path,value):
        data=encoded(value)
        if self.guard:self.guard.check_write(len(data))
        tmp,handle=preserved_temp(path,self.guard)
        with handle as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)

    def events(self):
        if self._events is not None:return self._events
        p=self.path/'ATTEMPTS.jsonl'
        if not p.exists():self._events=[];return self._events
        try:
            self._events=[json.loads(line) for line in p.read_text().splitlines()]
            if any(e.get('fingerprint')!=self.fingerprint for e in self._events):raise ValueError('ATTEMPT_BINDING_DRIFT')
            return self._events
        except Exception as exc:raise ValueError('ATTEMPT_LEDGER_CORRUPTION') from exc

    def record(self,row):
        record=dict(row,fingerprint=self.fingerprint,time=time.time())
        cached=self.events();data=encoded(record)+b'\n'
        if self.guard:self.guard.check_write(len(data))
        with (self.path/'ATTEMPTS.jsonl').open('ab') as f:f.write(data);f.flush();os.fsync(f.fileno())
        cached.append(record)

    def execute(self,key,operation,*,physical_fit=False):
        name=hashlib.sha256(key.encode()).hexdigest();path=self.cp/(name+'.json')
        if path.exists():
            row=json.loads(path.read_text())
            if row.get('fingerprint')!=self.fingerprint or row.get('key')!=key or row.get('sha256')!=hashlib.sha256(encoded(row.get('result'))).hexdigest():raise ValueError('CHECKPOINT_TAMPER')
            prior=[e for e in self.events() if e['key']==key]
            if not prior or prior[-1]['attempt']!=row['attempt']:raise ValueError('CHECKPOINT_ATTEMPT_DRIFT')
            if prior[-1]['state']=='START':self.record(dict(key=key,state='SUCCESS',attempt=row['attempt'],physical_fit=physical_fit,recovered_commit=True))
            elif prior[-1]['state']!='SUCCESS':raise ValueError('CHECKPOINT_FAILURE_DRIFT')
            return row['result']
        prior=[e for e in self.events() if e['key']==key]
        if prior and prior[-1]['state']=='SUCCESS':raise ValueError('MISSING_SUCCESSFUL_CHECKPOINT')
        if prior and not self.retry_failed:raise RuntimeError('EXPLICIT_RETRY_REQUIRED:'+key)
        attempt=uuid.uuid4().hex
        self.record(dict(key=key,state='START',attempt=attempt,physical_fit=physical_fit,retry_of=prior[-1]['attempt'] if prior else None))
        cpu_start=time.process_time();wall_start=time.monotonic()
        try:
            if physical_fit and self.guard:self.guard.check_fit()
            result=operation()
            self.atomic(path,dict(key=key,fingerprint=self.fingerprint,result=result,attempt=attempt,sha256=hashlib.sha256(encoded(result)).hexdigest()))
        except BaseException as exc:
            self.record(dict(key=key,state='FAILURE',attempt=attempt,physical_fit=physical_fit,error=repr(exc),cpu_seconds=time.process_time()-cpu_start,wall_seconds=time.monotonic()-wall_start));raise
        self.record(dict(key=key,state='SUCCESS',attempt=attempt,physical_fit=physical_fit,cpu_seconds=time.process_time()-cpu_start,wall_seconds=time.monotonic()-wall_start))
        return result

    def save_model(self,key,payload):
        path=self.path/(hashlib.sha256(key.encode()).hexdigest()+'.pickle')
        # Uncommitted previous files are retained, not mistaken for a success.
        if path.exists():path=self.path/(path.stem+'.'+uuid.uuid4().hex+'.pickle')
        tmp,handle=preserved_temp(path,self.guard)
        with handle as f:
            pickle.dump(payload,QuotaWriter(f,self.guard),protocol=5);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        return {'file':path.name,'sha256':sha(path)}

    def load_model(self,ref):
        path=(self.path/ref['file']).resolve()
        if path.parent!=self.path.resolve() or not path.is_file() or path.is_symlink() or sha(path)!=ref['sha256']:raise ValueError('MODEL_HASH_OR_PATH_DRIFT')
        with path.open('rb') as f:return pickle.load(f)


from .io_recovery_v5 import SafeRecoveryWriteGuard,disk_used


class CachedCompactWriteGuard(SafeRecoveryWriteGuard):
    """Shared conservative reservations; no per-chunk directory-tree scan.

    Initialize once under the global writer lock before spawning. Never refund
    reservations (including interrupted writes). Reconcile upward on resume.
    Disk use by unrelated jobs is additionally checked by the supervisor.
    """
    BLOCK=8*1024**2

    @classmethod
    def initialize(cls,run,fingerprint):
        run=Path(run);run.mkdir(parents=True,exist_ok=True)
        accounting=run.parent
        with writer_lock(run/'quota_lock',blocking=True):
            p=run/'WRITE_RESERVATIONS.json';used=disk_used(accounting)
            if p.exists():
                row=json.loads(p.read_text())
                if row['fingerprint']!=fingerprint:raise ValueError('QUOTA_BINDING_MISMATCH')
                used=max(used,row['reserved_total'])
            cls._write(p,dict(fingerprint=fingerprint,reserved_total=used))

    @staticmethod
    def _write(p,row):
        tmp,handle=preserved_temp(p,metadata_reserve=True)
        with handle as f:f.write(encoded(row));f.flush();os.fsync(f.fileno())
        os.replace(tmp,p)

    def __init__(self,path,run,fingerprint):
        super().__init__(path);self.run=Path(run);self.fingerprint=fingerprint;self.credit=0

    def check_write(self,size=0):
        if size<0:raise ValueError('NEGATIVE_WRITE')
        if size<=self.credit:self.credit-=size;return
        reserve=max(self.BLOCK,math.ceil((size-self.credit)/self.BLOCK)*self.BLOCK)
        with writer_lock(self.run/'quota_lock',blocking=True):
            p=self.run/'WRITE_RESERVATIONS.json';row=json.loads(p.read_text())
            if row['fingerprint']!=self.fingerprint:raise ValueError('QUOTA_BINDING_MISMATCH')
            total=row['reserved_total']+reserve
            if total+128*1024**2>=50*1024**3:raise RuntimeError('ARTIFACT_DISK_HOLD')
            if shutil.disk_usage(self.run).free-reserve-128*1024**2<100*1024**3:raise RuntimeError('FREE_DISK_HOLD')
            self._write(p,dict(fingerprint=self.fingerprint,reserved_total=total))
        self.credit+=reserve-size
