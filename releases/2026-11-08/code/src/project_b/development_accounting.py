"""Frozen fit-object identities and explicit terminal versus incomplete accounting."""
import json
import hashlib
from pathlib import Path
from .development_run import context_labels,context_fit_plan

def frozen_plan():
    result={}
    for context in context_labels():
        for item in context_fit_plan(context):
            prefix=context+'|'+item['model']+'|'
            key=(prefix+'recalibration='+str(item['fold']) if item['kind']=='recalibration' else
                 prefix+'refit' if item['fold']=='refit' else
                 prefix+str(item['candidate'])+'|inner='+str(item['fold']))
            if key in result: raise ValueError('DUPLICATE_PLANNED_OBJECT')
            result[key]=item
    if len(result)!=15198: raise ValueError('FIT_PLAN_DRIFT')
    return result

def build_receipt(states,skips):
    plan=frozen_plan()
    if set(states)-set(plan) or set(skips)-set(plan) or set(states)&set(skips): raise ValueError('FIT_OBJECT_IDENTITY_DRIFT')
    fields=('planned','attempted','succeeded','failed','skipped','interrupted','not_executed')
    result={kind:dict.fromkeys(fields,0) for kind in ('survival','recalibration','total')}
    objects={}
    for key,item in plan.items():
        state=states.get(key)
        if state is not None and state not in ('START','SUCCESS','FAILURE'): raise ValueError('FIT_STATE_DRIFT')
        status={'START':'interrupted','SUCCESS':'succeeded','FAILURE':'failed'}.get(state,'skipped' if key in skips else 'not_executed')
        if key in skips and (not skips[key].get('reason') or not (skips[key].get('cause_task') or skips[key].get('reason_ref'))): raise ValueError('SKIP_REASON_MISSING')
        if key in skips and skips[key].get('cause_task') and skips[key]['cause_task'] not in states: raise ValueError('SKIP_CAUSE_MISSING')
        objects[key]={'kind':item['kind'],'status':status,**({'skip':skips[key]} if key in skips else {})}
        for kind in (item['kind'],'total'):
            result[kind]['planned']+=1;result[kind][status]+=1
            result[kind]['attempted']+=state is not None
    complete=True
    for kind in ('survival','recalibration','total'):
        row=result[kind]
        if row['planned']!=row['attempted']+row['skipped']+row['not_executed'] or row['attempted']!=row['succeeded']+row['failed']+row['interrupted']: raise ValueError('FIT_RECONCILIATION_FAILURE')
        complete &= row['interrupted']==0 and row['not_executed']==0
    result.update(status='RECONCILED' if complete else 'INCOMPLETE_HOLD',objects=objects)
    return result

def read_skips(directory,fingerprint):
    path=Path(directory)/'skips.jsonl';result={}
    if path.exists():
        for line in path.read_bytes().splitlines():
            row=json.loads(line)
            if row['fingerprint']!=fingerprint or row['task'] in result: raise ValueError('SKIP_CHECKPOINT_DRIFT')
            evidence=(Path(directory)/row['reason_ref']).resolve()
            if not evidence.is_relative_to(Path(directory).resolve()) or not evidence.is_file() or hashlib.sha256(evidence.read_bytes()).hexdigest()!=row.get('reason_sha256'): raise ValueError('SKIP_REASON_TAMPER')
            result[row['task']]=row
    return result

def record_skips(ledger,keys,reason,reason_ref,cause_task=None):
    from .development_run import _append,_encoded
    existing=read_skips(ledger.path,ledger.fingerprint)
    for key in keys:
        if key in ledger.state: continue
        evidence=(ledger.path/reason_ref).resolve()
        if not evidence.is_relative_to(ledger.path.resolve()) or not evidence.is_file(): raise ValueError('SKIP_REASON_MISSING')
        row={'task':key,'reason':reason,'reason_ref':reason_ref,'fingerprint':ledger.fingerprint,
             'reason_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}
        if cause_task: row['cause_task']=cause_task
        if key in existing:
            if existing[key]!=row: raise ValueError('SKIP_CHECKPOINT_DRIFT')
            continue
        if ledger.guard: ledger.guard.check_write(len(_encoded(row)))
        _append(ledger.path/'skips.jsonl',row)

def record_grid_skips(ledger,context,model,result):
    from .development_parallel import atomic_json
    path=ledger.path/(model+'_SELECTION.json')
    atomic_json(path,{'fingerprint':ledger.fingerprint,'selection':result},ledger.guard)
    plan=frozen_plan()
    missing=[k for k in plan if k.startswith(context+'|'+model+'|') and '|inner=' in k and k not in ledger.state]
    failures=result.get('ledger',{}).get('failures',[])
    for key in missing:
        candidate=key.split('|')[-2]
        matching=[f for f in failures if str(f['candidate_order'])==candidate]
        cause=(context+'|'+model+'|'+candidate+'|inner='+str(matching[0]['fold'])) if matching else None
        record_skips(ledger,[key],str(matching[0]['reason']) if matching else 'GRID_STOPPED:'+result['status'],path.name,cause)


if __name__=='__main__':
    import argparse
    from .development_parallel import reconcile
    parser=argparse.ArgumentParser(description='Read-only fit accounting, including interrupted and unexecuted objects')
    parser.add_argument('run_directory');parser.add_argument('fingerprint')
    args=parser.parse_args()
    report=reconcile(Path(args.run_directory),args.fingerprint)
    print(json.dumps(report,sort_keys=True))
