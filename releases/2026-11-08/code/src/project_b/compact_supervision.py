"""Compact-only Windows child Job supervision; no numerical fitting code.

The controller stays outside its child Job, survives child termination and
records final accounting. CPU/wall polling is discrete, not a zero-overshoot
guarantee. A fresh nonce handshake prevents an accidentally unsupervised CLI.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
CPU_CEILING = 800 * 3600
WALL_CEILING = 96 * 3600
DISK_CEILING = 50 * 1024 ** 3
MINIMUM_FREE = 100 * 1024 ** 3
RESERVE = 128 * 1024 ** 2


def _finite_nonnegative(value):
    if isinstance(value, bool):
        raise ValueError('invalid resource number')
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('invalid resource number')
    return value


def validate_workers(workers):
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 12:
        raise ValueError('compact workers must be integer1..12')
    return workers


def checked_project_path(path):
    """Reject lexical escapes before any stat; never follow a junction/symlink."""
    path = Path(os.path.abspath(path))
    if any(p.lower() == 'project_a' for p in path.parts):
        raise PermissionError('PROJECT_A_PROHIBITED')
    try:
        parts = path.relative_to(ROOT).parts
    except ValueError:
        raise PermissionError('PROJECT_B_ONLY') from None
    current = ROOT
    for part in ('', *parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if current.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PermissionError('PROJECT_REPARSE_POINT_PROHIBITED')
    return path


def remaining_limits(base_cpu, base_wall, completed_receipts,
                     *, preparation_cpu=3600., preparation_wall=7200.):
    """Conservative preparation allocation is explicit, not measured CPU/time."""
    cpu = _finite_nonnegative(base_cpu) + _finite_nonnegative(preparation_cpu)
    wall = _finite_nonnegative(base_wall) + _finite_nonnegative(preparation_wall)
    for row in completed_receipts:
        if (row.get('status') != 'FINAL_STOPPED'
                or type(row.get('active_child_n')) is not int or row['active_child_n'] != 0):
            raise RuntimeError('UNRESOLVED_COMPACT_RESOURCE_TAIL')
        cpu += _finite_nonnegative(row['accounting_cpu_seconds'])
        wall += _finite_nonnegative(row['accounting_wall_seconds'])
    if cpu >= CPU_CEILING or wall >= WALL_CEILING:
        raise RuntimeError('COMPACT_RESOURCE_BUDGET_EXHAUSTED')
    return {'cpu_limit': CPU_CEILING - cpu, 'wall_limit': WALL_CEILING - wall,
            'charged_cpu_seconds': cpu, 'charged_wall_seconds': wall,
            'preparation_charge_basis': 'Conservative allocation, not measured or proven strict bound',
            'preparation_cpu_seconds': preparation_cpu, 'preparation_wall_seconds': preparation_wall}


def stop_reason(*, wall, cpu, wall_limit, cpu_limit, disk_bytes, free_bytes):
    values = [_finite_nonnegative(x) for x in (wall, cpu, wall_limit, cpu_limit, disk_bytes, free_bytes)]
    wall, cpu, wall_limit, cpu_limit, disk_bytes, free_bytes = values
    if not 0 < wall_limit <= WALL_CEILING or not 0 < cpu_limit <= CPU_CEILING:
        raise ValueError('resource allowance outside global ceiling')
    if wall >= wall_limit:
        return 'WALL_BUDGET'
    if cpu >= cpu_limit:
        return 'CPU_BUDGET'
    if disk_bytes + RESERVE >= DISK_CEILING:
        return 'DISK_BUDGET'
    if free_bytes < MINIMUM_FREE + RESERVE:
        return 'FREE_DISK_BUDGET'
    return None


def terminal_status(active_children):
    if type(active_children) is not int or active_children < 0:
        raise ValueError('invalid active child count')
    return 'FINAL_STOPPED' if active_children == 0 else 'UNRESOLVED_CHILD_TAIL'


def validate_receipt(receipt, *, fingerprint, workers, pid, ppid, go_line):
    validate_workers(workers)
    expected = {'schema_version', 'status', 'fingerprint', 'workers', 'nonce',
                'launcher_pid', 'supervisor_pid'}
    valid = (set(receipt) == expected
             and receipt['schema_version'] == 'compact-supervision-1.0'
             and receipt['status'] == 'CHILD_GATED'
             and re.fullmatch('[0-9a-f]{64}', fingerprint) is not None
             and receipt['fingerprint'] == fingerprint and receipt['workers'] == workers
             and re.fullmatch('[0-9a-f]{32}', str(receipt['nonce'])) is not None
             and isinstance(receipt['launcher_pid'], int) and receipt['launcher_pid'] > 0
             and isinstance(receipt['supervisor_pid'], int) and receipt['supervisor_pid'] > 0
             and receipt['supervisor_pid'] != pid
             and (pid == receipt['launcher_pid'] or ppid == receipt['launcher_pid'])
             and go_line.strip() == 'GO ' + receipt['nonce'])
    if not valid:
        raise PermissionError('COMPACT_SUPERVISION_RECEIPT_OR_GO_MISMATCH')
    return receipt


def validate_supervised_child(receipt_path, *, fingerprint, workers):
    path = checked_project_path(receipt_path)
    if path.name != 'CHILD_RECEIPT.json' or path.parent.parent.name != 'supervision':
        raise PermissionError('COMPACT_SUPERVISION_RECEIPT_PATH')
    receipt = json.loads(path.read_text(encoding='utf-8'))
    return validate_receipt(receipt, fingerprint=fingerprint, workers=workers,
                            pid=os.getpid(), ppid=os.getppid(), go_line=sys.stdin.readline())


def make_child_command(mode, root, workers, *, retry_failed=False):
    validate_workers(workers)
    root = checked_project_path(root)
    if (root != ROOT or mode not in ('pilot', 'run', 'resume') or (mode == 'pilot' and workers != 1)
            or not isinstance(retry_failed, bool)):
        raise ValueError('invalid compact child mode, root or pilot worker count')
    command = [str(root / '.venv-a5/Scripts/python.exe'), '-B', '-m', 'project_b.compact_run',
               mode, str(root), '--workers', str(workers), '--supervision-receipt', '{supervision_receipt}']
    return command + (['--retry-failed'] if retry_failed else [])


def supervise(command, run_dir, *, fingerprint, workers, cpu_limit, wall_limit,
              stage, poll_seconds=1., budget_resolver=None):
    """One durable, exclusive, hidden child session. Caller supplies remaining limits.

    command must contain one literal '{supervision_receipt}' argument, replaced
    before launch. The child calls validate_supervised_child before real fits.
    New compact physical-fit accounting belongs to its own ledger, not an old
    hard-coded parent-run attempt ceiling.
    """
    from contextlib import ExitStack

    validate_workers(workers)
    run = checked_project_path(run_dir)
    if (not re.fullmatch('[a-z0-9_-]{1,48}', stage)
            or re.fullmatch('[0-9a-f]{64}', fingerprint) is None
            or not isinstance(command, (list, tuple))
            or list(command).count('{supervision_receipt}') != 1
            or not .05 <= poll_seconds <= 5.):
        raise ValueError('invalid compact supervisor arguments')
    stop_reason(wall=0, cpu=0, wall_limit=wall_limit, cpu_limit=cpu_limit,
                disk_bytes=0, free_bytes=MINIMUM_FREE + RESERVE)
    run.mkdir(parents=True, exist_ok=True)
    with ExitStack() as cleanup:
        return _supervise_session(command, run, fingerprint=fingerprint, workers=workers,
                                  cpu_limit=cpu_limit, wall_limit=wall_limit, stage=stage,
                                  poll_seconds=poll_seconds, cleanup=cleanup, budget_resolver=budget_resolver)


def _unlock_file(lock):
    import msvcrt
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _supervise_session(command, run, *, fingerprint, workers, cpu_limit, wall_limit,
                       stage, poll_seconds, cleanup, budget_resolver):
    import ctypes
    from ctypes import wintypes
    import msvcrt
    import subprocess
    from .development_resources import _ExtendedLimit
    from .io_recovery_v5 import disk_used
    from .m0_recovery_execute import ChildJob, durable_json

    artifact_root = next((p for p in (run, *run.parents) if p.name == 'development'), run)
    lock = (run / 'SUPERVISOR.lock').open('a+b')
    cleanup.callback(lock.close)
    acquired = False
    process = job = None
    directory = None
    wall_start = time.monotonic()
    cpu_start = time.process_time()
    reason = error = None
    try:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('COMPACT_SUPERVISOR_ALREADY_RUNNING') from None
        acquired = True
        cleanup.callback(_unlock_file, lock)
        if budget_resolver is not None:
            budget = budget_resolver()
            cpu_limit, wall_limit = budget['cpu_limit'], budget['wall_limit']
            stop_reason(wall=0, cpu=0, wall_limit=wall_limit, cpu_limit=cpu_limit,
                        disk_bytes=0, free_bytes=MINIMUM_FREE + RESERVE)
        supervision = checked_project_path(run / 'supervision')
        supervision.mkdir(exist_ok=True)
        for number in range(1, 10001):
            directory = checked_project_path(supervision / f'{stage}_{number:04d}')
            try:
                directory.mkdir()
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError('COMPACT_STAGE_DIRECTORY_EXHAUSTED')
        disk_bytes = disk_used(artifact_root)
        free_bytes = shutil.disk_usage(run).free
        reason = stop_reason(wall=0, cpu=0, wall_limit=wall_limit, cpu_limit=cpu_limit,
                             disk_bytes=disk_bytes, free_bytes=free_bytes)
        if reason:
            raise RuntimeError(reason)
        job = ChildJob()
        cleanup.callback(job.close)
        limits = _ExtendedLimit()
        limits.basic.flags = 0x2000 | 0x8 | 0x100 | 0x200
        limits.basic.active_process_limit = 2 + 2 * workers
        limits.process_memory_limit = 32 * 1024 ** 3
        limits.job_memory_limit = 32 * 1024 ** 3
        if not job.k.SetInformationJobObject(job.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        receipt_path = directory / 'CHILD_RECEIPT.json'
        child_command = [str(receipt_path) if item == '{supervision_receipt}' else str(item)
                         for item in command]
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1')
        for key in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            env[key] = '1'
        with (directory / 'stdout.log').open('xb') as stdout, (directory / 'stderr.log').open('xb') as stderr:
            process = subprocess.Popen(child_command, stdin=subprocess.PIPE, stdout=stdout,
                                       stderr=stderr, cwd=run, env=env,
                                       creationflags=subprocess.CREATE_NO_WINDOW | 0x4)
            job.assign(process)
            nonce = uuid.uuid4().hex
            durable_json(receipt_path, {'schema_version': 'compact-supervision-1.0', 'status': 'CHILD_GATED',
                                       'fingerprint': fingerprint, 'workers': workers, 'nonce': nonce,
                                       'launcher_pid': process.pid, 'supervisor_pid': os.getpid()}, exclusive=True)
            resume = ctypes.WinDLL('ntdll').NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]; resume.restype = ctypes.c_long
            if resume(int(process._handle)) != 0:
                raise RuntimeError('COMPACT_CHILD_RESUME_FAILED')
            process.stdin.write(('GO ' + nonce + '\n').encode())
            process.stdin.flush(); process.stdin.close()
            scan_at = report_at = 0.
            while process.poll() is None:
                now = time.monotonic()
                accounting = job.accounting()
                wall = now - wall_start
                cpu = accounting['child_cpu_seconds'] + time.process_time() - cpu_start
                if now - scan_at >= 15.:
                    disk_bytes = disk_used(artifact_root)
                    free_bytes = shutil.disk_usage(run).free
                    scan_at = now
                reason = stop_reason(wall=wall, cpu=cpu, wall_limit=wall_limit, cpu_limit=cpu_limit,
                                     disk_bytes=disk_bytes, free_bytes=free_bytes)
                if (run / 'STOP_REQUESTED.json').exists():
                    reason = 'USER_STOP_REQUESTED'
                if now - report_at >= 5. or reason:
                    durable_json(directory / 'RESOURCE_STATUS.json', {
                        'status': 'STOPPING' if reason else 'RUNNING', 'fingerprint': fingerprint,
                        'reason': reason, 'wall_seconds': wall, 'controller_cpu_seconds': time.process_time() - cpu_start,
                        'workers': workers, 'pid': process.pid, 'supervisor_pid': os.getpid(), **accounting})
                    report_at = now
                if reason:
                    job.kill()
                    break
                time.sleep(poll_seconds)
            process.wait(timeout=15)
            process._handle.Close()
    except BaseException as exc:
        error = exc
    finally:
        if job is not None and process is not None:
            # Wrapper exit can precede the Job's final process/accounting
            # release. Five direct Windows diagnostics showed active1 at wait
            # return and active0 after50ms. Observe completion before treating
            # a still-running descendant as a genuine unjoined-child failure.
            settle_deadline = time.monotonic() + 2.
            if error is None and reason is None and process.poll() is not None:
                while job.accounting()['active_child_n'] and time.monotonic() < settle_deadline:
                    a = job.accounting()
                    if time.monotonic() - wall_start >= wall_limit:
                        reason = 'WALL_BUDGET'; break
                    if a['child_cpu_seconds'] + time.process_time() - cpu_start >= cpu_limit:
                        reason = 'CPU_BUDGET'; break
                    time.sleep(.01)
            if process.poll() is None or job.accounting()['active_child_n']:
                if error is None and reason is None and process.poll() is not None:
                    reason = 'UNJOINED_CHILDREN'
                try:
                    job.kill()
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=15)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            for _ in range(40):
                if not job.accounting()['active_child_n']:
                    break
                time.sleep(.05)
        if directory is not None:
            accounting = job.accounting() if job is not None else {
                'child_cpu_seconds': 0., 'peak_child_commit_bytes': 0, 'active_child_n': 0}
            wall = time.monotonic() - wall_start
            cpu = accounting['child_cpu_seconds'] + time.process_time() - cpu_start
            result = {'status': terminal_status(accounting['active_child_n']), 'fingerprint': fingerprint, 'stage': stage,
                      'stage_directory': str(directory), 'outcome': 'SUCCESS' if error is None and reason is None
                      and process is not None and process.returncode == 0 and accounting['active_child_n'] == 0 else 'FAILURE',
                      'reason': str(error) if error is not None else reason,
                      'exit_code': None if process is None else process.returncode,
                      'wall_seconds': wall, 'controller_cpu_seconds': time.process_time() - cpu_start,
                      'accounting_cpu_seconds': cpu + 1., 'accounting_wall_seconds': wall + 1.,
                      'cpu_limit_seconds': cpu_limit, 'wall_limit_seconds': wall_limit,
                      'terminal_reserve': 'One conservative second, not measured CPU',
                      'parent_outside_child_job': True, 'memory_job_limit_gib': 32,
                      'model_workers': workers, 'os_process_ceiling': 2 + 2 * workers,
                      'poll_seconds': poll_seconds, 'disk_scan_seconds': 15.,
                      'possible_polling_overshoot': True, **accounting}
            try:
                durable_json(directory / 'RESOURCE_STATUS.json', result)
                durable_json(directory / 'FINAL_STOPPED.json', result, exclusive=True)
            finally:
                if job is not None:
                    job.close()
        elif job is not None:
            job.close()
    if error is not None:
        raise error
    return result


if __name__ == '__main__':
    import argparse
    from .compact_authorization import validate_authorization
    from .m0_recovery_execute import durable_json

    parser = argparse.ArgumentParser(description='Supervised compact-only development pilot/run/resume')
    parser.add_argument('mode', choices=('pilot', 'run', 'resume'))
    parser.add_argument('--workers', type=int)
    parser.add_argument('--retry-failed', action='store_true', help='Explicitly permit linked retries of unfinished/failed operations')
    args = parser.parse_args()
    workers = args.workers if args.workers is not None else (1 if args.mode == 'pilot' else 8)
    command = make_child_command(args.mode, ROOT, workers, retry_failed=args.retry_failed)
    fingerprint, manifest = validate_authorization(ROOT)
    readiness_path = ROOT / 'audit/compact_gbst/COMPACT_RELEASE_READINESS.json'
    readiness = json.loads(readiness_path.read_text(encoding='utf-8'))
    allowed_status = ('READY_FOR_PILOT', 'READY_INTERNAL_VALIDATION') if args.mode == 'pilot' else ('READY_INTERNAL_VALIDATION',)
    if (readiness.get('fingerprint') != fingerprint or readiness.get('status') not in allowed_status
            or readiness.get('temporal_unseal_authorized') is not False):
        raise PermissionError('COMPACT_REVIEW_PILOT_OR_RESOURCE_READINESS_REQUIRED')
    parent_manifest = json.loads((ROOT / 'audit/development/serialization_resume_v6/RESUME_MANIFEST.json').read_text(encoding='utf-8'))
    parent_final = json.loads((ROOT / 'audit/development/recovery_runs/serialization_resume_v6/resume_final_FINAL_STOPPED.json').read_text(encoding='utf-8'))
    charges = parent_manifest['resource_charges']
    base_cpu = charges['old_cpu_charge'] + charges['diagnostic_cpu_charge'] + parent_final['accounting_cpu_seconds']
    base_wall = charges['old_wall_charge'] + charges['diagnostic_wall_charge'] + parent_final['accounting_wall_seconds']
    run = ROOT / 'audit/development/compact_gbst_manual_v1'
    def reconcile_limits():
        # Called only after exclusive launch ownership, before stage registration.
        prior = []
        if (run / 'supervision').exists():
            for stage_path in sorted((run / 'supervision').iterdir()):
                path = checked_project_path(stage_path / 'FINAL_STOPPED.json')
                if not path.is_file():
                    raise RuntimeError('UNRESOLVED_COMPACT_RESOURCE_TAIL: ' + str(stage_path))
                prior.append(json.loads(path.read_text(encoding='utf-8')))
        limits = remaining_limits(base_cpu, base_wall, prior)
        durable_json(run / 'LATEST_LAUNCH_BUDGET.json', {'fingerprint': fingerprint, **limits})
        return limits
    result = supervise(command, run, fingerprint=fingerprint, workers=workers,
                       cpu_limit=CPU_CEILING, wall_limit=WALL_CEILING, stage=args.mode,
                       budget_resolver=reconcile_limits)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['outcome'] == 'SUCCESS' else 1)
