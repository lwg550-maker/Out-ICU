"""Race-safe disk accounting and validation of interrupted tree checkpoints."""
import os
from pathlib import Path
import stat

from .recovery_production import RecoveryWriteGuard


def _regular_size(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise RuntimeError('SYMLINK_DISK_HOLD')
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError('RENAMED_TARGET_NOT_REGULAR')
    return info.st_size


def renamed_temp_size(path):
    """Count the final rename target, never suppress unrelated missing files.

    If the directory snapshot also contains that final target, double counting
    is conservative. No persistent file, permission error, or unknown deletion
    is silently ignored. Existing JSON/pickle writers use these two suffixes.
    """
    path = Path(path)
    if path.suffix == '.tmp':
        for suffix in ('.json', '.pickle'):
            try:
                return _regular_size(path.with_suffix(suffix).stat(follow_symlinks=False))
            except FileNotFoundError:
                pass
    raise FileNotFoundError('DISK_ENTRY_DISAPPEARED_WITHOUT_RENAME_TARGET: '+str(path))


def disk_used(path):
    """One non-following stat per entry; tolerate only a resolved atomic rename."""
    root = Path(path)
    root_info = root.stat(follow_symlinks=False)
    if stat.S_ISLNK(root_info.st_mode) or getattr(root_info, 'st_file_attributes', 0) & 0x400:
        raise RuntimeError('SYMLINK_DISK_HOLD')
    pending, total = [str(root)], 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    total += renamed_temp_size(entry.path)
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise RuntimeError('SYMLINK_DISK_HOLD')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
    return total


class SafeRecoveryWriteGuard(RecoveryWriteGuard):
    def check_write(self, size=0):
        import shutil
        if size < 0:
            raise ValueError('negative size')
        if disk_used(self.accounting_root)+size+128*1024**2 > 50*1024**3:
            raise RuntimeError('ARTIFACT_DISK_HOLD')
        if shutil.disk_usage(self.path).free-size-128*1024**2 < 100*1024**3:
            raise RuntimeError('FREE_DISK_HOLD')


def validate_tree_checkpoint(row, *, candidate, fold, ids, seed):
    from .a5_calibration import validate_native_risk
    if (set(row) != {'candidate_id','fold','ids','risks','seed'}
            or row['candidate_id'] != str(candidate) or row['fold'] != fold
            or row['seed'] != seed or [tuple(v) for v in row['ids']] != list(map(tuple, ids))
            or len(row['risks']) != len(ids)):
        raise ValueError('INTERRUPTED_CHECKPOINT_IDENTITY_DRIFT')
    validate_native_risk(row['risks'])
