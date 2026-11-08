"""Read-only development preflight. This module does not fit models."""
import csv
import hashlib
import shutil
from pathlib import Path
from .development_authorization import validate_development_authorization
from .a5_common import make_surv180

INPUTS = ('data/a4/DEVELOPMENT_INDEX_OUTCOME.csv', 'data/a4/DEVELOPMENT_C2_RAW.csv',
          'data/a4/DEVELOPMENT_EXTENDED_157.csv', 'data/a4/OUTER_FOLD_ASSIGNMENTS.csv',
          'config/A4_FIELD_DICTIONARY.csv')


def preflight(root):
    root=Path(root).resolve(strict=True)
    validate_development_authorization(root)
    if shutil.disk_usage(root).free < 100 * 1024 ** 3:
        raise ValueError('MINIMUM_FREE_DISK_HOLD')
    with (root/'manifests/A4_FREEZE_MANIFEST.csv').open(encoding='utf-8', newline='') as f:
        manifest={r['relative_path']:r for r in csv.DictReader(f)}
    for path in INPUTS:
        data=(root/path).read_bytes()
        if hashlib.sha256(data).hexdigest().upper() != manifest[path]['sha256']:
            raise ValueError('A4_INPUT_HASH_DRIFT: '+path)
    with (root/'config/A4_FIELD_DICTIONARY.csv').open(encoding='utf-8', newline='') as f:
        dictionary=list(csv.DictReader(f))
    tables=[]
    for path in INPUTS[:4]:
        with (root/path).open(encoding='utf-8', newline='') as f:
            reader=csv.DictReader(f); rows=list(reader); names=reader.fieldnames
        if len(rows)!=2839: raise ValueError('ROW_COUNT_FAILURE: '+path)
        if path != INPUTS[3]:
            table=Path(path).stem
            expected=[r['column_name'] for r in dictionary if r['table_name']==table]
        else:
            expected=['subject_id','hadm_id']+[f'repeat_{i}_fold' for i in range(1,11)]
        if names!=expected: raise ValueError('COLUMN_ORDER_FAILURE: '+path)
        ids=[(r['subject_id'], r['hadm_id']) for r in rows]
        if len(set(ids))!=2839 or len({i[0] for i in ids})!=2839: raise ValueError('ID_DUPLICATION: '+path)
        if tables and ids!=tables[0][0]: raise ValueError('ORDERED_ID_FAILURE: '+path)
        tables.append((ids,rows))
    outcomes=tables[0][1]
    for r in outcomes:
        for k in ('admin_90_eligible','admin_180_eligible','death_90d','death_180d'):
            r[k]=int(r[k])
        r['death_days_after_t0']=float(r['death_days_after_t0']) if r['death_days_after_t0'] else None
    for r in outcomes:
        if r['admin_90_eligible'] != 1 or r['death_90d'] not in (0, 1):
            raise ValueError('HORIZON_90_OUTCOME_FAILURE')
        expected90 = int(r['death_180d'] == 1 and r['death_days_after_t0'] is not None and r['death_days_after_t0'] <= 90)
        if r['death_90d'] != expected90:
            raise ValueError('HORIZON_OUTCOME_INCONSISTENCY')
    _, events=make_surv180(outcomes)
    if sum(events)!=435: raise ValueError('EVENT_COUNT_FAILURE')
    for repeat in range(1,11):
        folds=[int(r[f'repeat_{repeat}_fold']) for r in tables[3][1]]
        if set(folds)!=set(range(1,6)): raise ValueError('OUTER_FOLD_FAILURE')
        if any(sum(e for e,k in zip(events,folds) if k==fold)!=87 for fold in range(1,6)):
            raise ValueError('OUTER_EVENT_COUNT_FAILURE')
    return {'status':'PASS','patients':2839,'events_180':435,'outer_contexts':50}


if __name__=='__main__':
    import json, sys
    print(json.dumps(preflight(sys.argv[1]), indent=2))
