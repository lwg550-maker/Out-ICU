"""Nested compact GBST context engine. Real execution requires an external Job.

CLI preparation/preflight are read-only. Coordinator owns reviewed manifest,
resource supervisor and launch approval. No automatic final budget or temporal.
"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from .a5_common import generate_inner_folds
from .a5_gbst import gbst_grid
from .a5_m0_cox import predict_native_risk
from .a5_calibration import validate_native_risk
from .compact_inputs import load_input_spec,development_rows,selected_manual_fields
from .compact_preprocessing import fit_preprocessor,transform_preprocessor
from .compact_selection import rank_partition,seed,BUDGETS
from .compact_storage import Store,encoded,writer_lock,CachedCompactWriteGuard


def inner_folds(ids,events,context):
    if context=='full_development':label='A5|2026102405|full_development'
    else:
        parts=context.split('|');label={'outer_repeat':int(parts[0].split('=')[1]),'outer_fold':int(parts[1].split('=')[1])}
    return generate_inner_folds(ids,events,label,{'master_seed':2026102405})


def choose_candidate(grid,squared_losses):
    if len(grid)!=len(squared_losses) or any(not len(v) or not np.isfinite(v).all() for v in squared_losses):raise ValueError('CANDIDATE_LOSS_DRIFT')
    scores=[float(np.mean(v)) for v in squared_losses];minimum=min(scores)
    eligible=[c for c,s in zip(grid,scores) if s<=minimum+1e-8]
    return min(eligible,key=lambda c:(c['max_depth'],c['n_estimators'],c['learning_rate'],c['declared_order']))


def run_context(*,context,rows,target_rows,ids,target_ids,times,events,spec,store,fit):
    """Pure explicit partition boundary; backend passed by authorized worker."""
    if not len(ids)==len(rows)==len(times)==len(events) or len(target_rows)!=len(target_ids) or len(set(map(tuple,ids)))!=len(ids) or len(set(map(tuple,target_ids)))!=len(target_ids) or set(map(tuple,ids))&set(map(tuple,target_ids)):raise ValueError('PATIENT_ORDER_OR_LEAKAGE')
    if (context=='full_development') != (not target_ids):raise ValueError('TARGET_CONTEXT_DRIFT')
    binding=hashlib.sha256(encoded(dict(context=context,rows=rows,target_rows=target_rows,ids=ids,target_ids=target_ids,times=times,events=events,spec=spec))).hexdigest()
    saved=store.execute(context+'|inputs',lambda:binding)
    if saved!=binding:raise ValueError('CONTEXT_DATA_ORDER_OUTCOME_DRIFT')
    assignment=inner_folds(ids,events,context);grid=gbst_grid()
    losses={str(b):[[] for _ in grid] for b in BUDGETS};ranks={}
    for fold in range(1,6):
        train=[i for i,a in enumerate(assignment) if a!=fold];valid=[i for i,a in enumerate(assignment) if a==fold]
        partition=f'{context}|inner={fold}'
        rank=rank_partition(rows=[rows[i] for i in train],ids=[ids[i] for i in train],times=[times[i] for i in train],events=[events[i] for i in train],spec=spec,partition=partition,store=store,fit=fit)
        ranks[str(fold)]=rank
        for budget in BUDGETS:
            groups=rank['selected'][str(budget)]
            pp=fit_preprocessor([rows[i] for i in train],groups,spec,partition_role='inner_training')
            xtrain=transform_preprocessor(pp,[rows[i] for i in train]);xvalid=transform_preprocessor(pp,[rows[i] for i in valid])
            for j,candidate in enumerate(grid):
                key=f'{partition}|budget={budget}|candidate={j}'
                model_seed=seed(context,f'budget={budget}|candidate={j}|inner={fold}')
                def operation():
                    model=fit(xtrain,[times[i] for i in train],[events[i] for i in train],candidate,model_seed)
                    risks=predict_native_risk(model,xvalid)
                    return dict(ids=[ids[i] for i in valid],risks=risks,seed=model_seed,preprocessor=pp,groups=groups)
                result=store.execute(key,operation,physical_fit=True)
                if list(map(tuple,result['ids']))!=[tuple(ids[i]) for i in valid] or result['seed']!=model_seed or result['preprocessor']!=pp or result['groups']!=groups:raise ValueError('CANDIDATE_CHECKPOINT_DRIFT')
                validate_native_risk(result['risks'])
                if len(result['risks'])!=len(valid):raise ValueError('PREDICTION_COUNT_DRIFT')
                losses[str(budget)][j].extend((float(r[1])-events[i])**2 for i,r in zip(valid,result['risks']))
    fullrank=rank_partition(rows=rows,ids=ids,times=times,events=events,spec=spec,partition=context+'|outer_training',store=store,fit=fit)
    output=dict(context=context,target_ids=target_ids,assignment=assignment,inner_ranks=ranks,full_training_rank=fullrank,budgets={})
    for budget in BUDGETS:
        selected=choose_candidate(grid,losses[str(budget)]);groups=fullrank['selected'][str(budget)]
        key=f'{context}|budget={budget}|refit';model_seed=seed(context,f'budget={budget}|candidate={selected["declared_order"]}|final')
        def refit():
            role='final_full_development_refit' if context=='full_development' else 'outer_training_refit'
            pp=fit_preprocessor(rows,groups,spec,partition_role=role)
            model=fit(transform_preprocessor(pp,rows),times,events,selected,model_seed)
            package=dict(model=model,preprocessor=pp,selected_candidate=selected,selected_groups=groups,effective_selected_groups=pp['effective_selected_group_ids'],manual_fields=selected_manual_fields(pp['effective_selected_group_ids'],spec),seed=model_seed,provisional=True)
            ref=store.save_model(key,package)
            risks=predict_native_risk(model,transform_preprocessor(pp,target_rows)) if target_rows else []
            return dict(package=ref,risks=risks,selected_candidate=selected,selected_groups=groups,effective_selected_groups=pp['effective_selected_group_ids'],manual_fields=package['manual_fields'],seed=model_seed)
        result=store.execute(key,refit,physical_fit=True);store.load_model(result['package'])
        if len(result['risks'])!=len(target_ids):raise ValueError('OUTER_PREDICTION_COUNT_DRIFT')
        if result['risks']:validate_native_risk(result['risks'])
        output['budgets'][str(budget)]=dict(result,pooled_brier=[float(np.mean(x)) for x in losses[str(budget)]])
    return store.execute(context+'|complete',lambda:output)


def context_worker(root,run_directory,fingerprint,partition,retry_failed=False):
    from .compact_authorization import real_adapter
    actual,fit=real_adapter(root)
    if actual!=fingerprint:raise PermissionError('WORKER_BINDING_MISMATCH')
    spec=load_input_spec(root)
    name=hashlib.sha256(partition['context'].encode()).hexdigest()[:20]
    directory=Path(run_directory)/name
    with writer_lock(directory):
        store=Store(directory,fingerprint,CachedCompactWriteGuard(directory,run_directory,fingerprint),retry_failed)
        return run_context(context=partition['context'],rows=development_rows(partition['core_training'],partition['extension_training'],spec),target_rows=development_rows(partition['core_target'],partition['extension_target'],spec),ids=partition['ids'],target_ids=partition['target_ids'],times=partition['times'],events=partition['events'],spec=spec,store=store,fit=fit)


def pilot(root,run,fingerprint,retry_failed=False):
    """Three extra physical fits; pilot lists are never a global selector."""
    import time
    from .development_run import iter_development_contexts
    from .compact_authorization import real_adapter
    actual,fit=real_adapter(root)
    if actual!=fingerprint:raise PermissionError('PILOT_BINDING_DRIFT')
    spec=load_input_spec(root);part=next(iter_development_contexts(root))
    assign=inner_folds(part['ids'],part['events'],part['context']);indices=[i for i,a in enumerate(assign) if a!=1]
    allrows=development_rows(part['core_training'],part['extension_training'],spec)
    rows=[allrows[i] for i in indices];ids=[part['ids'][i] for i in indices];times=[part['times'][i] for i in indices];events=[part['events'][i] for i in indices]
    path=Path(run)/'pilot'
    with writer_lock(path):
        store=Store(path,fingerprint,CachedCompactWriteGuard(path,run,fingerprint),retry_failed)
        start=time.monotonic();cpu=time.process_time()
        rank=rank_partition(rows=rows,ids=ids,times=times,events=events,spec=spec,partition='pilot|inner_training',store=store,fit=fit)
        groups=[g['id'] for g in spec['groups']]
        def worst():
            pp=fit_preprocessor(rows,groups,spec,partition_role='inner_training');x=transform_preprocessor(pp,rows)
            candidate=next(c for c in gbst_grid() if (c['max_depth'],c['learning_rate'],c['n_estimators'])==(3,.07,500))
            model=fit(x,times,events,candidate,seed('pilot','worst_grid'))
            risks=predict_native_risk(model,x);validate_native_risk(risks)
            ref=store.save_model('pilot_worst',dict(model=model,preprocessor=pp))
            restored=store.load_model(ref)
            if predict_native_risk(restored['model'],x)!=risks:raise ValueError('PILOT_SERIALIZATION_DRIFT')
            return dict(package=ref,n=len(rows),features=x.shape[1])
        result=store.execute('pilot|worst',worst,physical_fit=True)
        return store.execute('pilot|complete',lambda:dict(status='COMPLETE',rank=rank,worst=result,wall_seconds=time.monotonic()-start,cpu_seconds=time.process_time()-cpu,extra_physical_fits=3,not_global_selection=True))


def launch_contexts(root,run,fingerprint,workers=12,retry_failed=False):
    import concurrent.futures as futures
    import multiprocessing as mp
    from .development_run import iter_development_contexts
    from .compact_authorization import guard_root
    root=guard_root(root)
    if not 1<=workers<=12:raise ValueError('WORKER_LIMIT')
    with futures.ProcessPoolExecutor(max_workers=workers,mp_context=mp.get_context('spawn')) as pool:
        jobs=[pool.submit(context_worker,root,run,fingerprint,p,retry_failed) for p in iter_development_contexts(root)]
        outputs=[job.result() for job in futures.as_completed(jobs)]
    if len(outputs)!=51 or len(set(x['context'] for x in outputs))!=51:raise ValueError('CONTEXT_COVERAGE_DRIFT')
    for repeat in range(1,11):
        partitions=[p for p in outputs if p['context'].startswith(f'outer_repeat={repeat}|')]
        ids=[tuple(i) for p in partitions for i in p['target_ids']]
        if len(ids)!=2839 or len(set(ids))!=2839:raise ValueError('OUTER_COVERAGE_DRIFT')
    store=Store(run,fingerprint,CachedCompactWriteGuard(run,run,fingerprint),retry_failed)
    return store.execute('branch|complete',lambda:dict(status='COMPLETE',contexts=sorted(p['context'] for p in outputs),formal_physical_fits=9945,temporal=False,final_budget=None))


def validate_runtime(root):
    import os,sys
    from .compact_authorization import guard_root
    root=guard_root(root)
    if not sys.dont_write_bytecode or any(os.environ.get(k)!='1' for k in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS')):raise PermissionError('THREAD_OR_BYTECODE_DRIFT')
    if Path(sys.executable).resolve()!= (Path(root)/'.venv-a5/Scripts/python.exe').resolve():raise PermissionError('FROZEN_INTERPRETER_REQUIRED')
    from .a5_common import validate_installed_software_lock
    return validate_installed_software_lock(root)


def status_report(run):
    run=Path(run);events=[];complete=0
    for p in run.glob('*/ATTEMPTS.jsonl'):
        events.extend(json.loads(line) for line in p.read_text().splitlines())
    for p in run.glob('*/checkpoints/*.json'):
        key=json.loads(p.read_text()).get('key','')
        if key.endswith('|complete') and (key.startswith('outer_repeat=') or key=='full_development|complete'):complete+=1
    return dict(physical_starts=sum(e['state']=='START' and e['physical_fit'] for e in events),success=sum(e['state']=='SUCCESS' and e['physical_fit'] for e in events),failure=sum(e['state']=='FAILURE' and e['physical_fit'] for e in events),completed_contexts=complete,pilot_completed=any(json.loads(p.read_text()).get('key')=='pilot|complete' for p in (run/'pilot/checkpoints').glob('*.json')))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['prepare-binding','preflight','pilot','run','resume','status']);parser.add_argument('root');parser.add_argument('--parent-completion-receipt');parser.add_argument('--retry-failed',action='store_true');parser.add_argument('--workers',type=int,default=12);parser.add_argument('--supervision-receipt')
    args=parser.parse_args()
    from .compact_authorization import prepare_binding,validate_authorization,guard_root
    root=guard_root(args.root)
    if args.command=='prepare-binding':
        if not args.parent_completion_receipt:parser.error('--parent-completion-receipt required')
        result=prepare_binding(root,args.parent_completion_receipt)
    elif args.command=='preflight':
        validate_runtime(root);load_input_spec(root)
        fingerprint,manifest=validate_authorization(root)
        from .development_preflight import preflight
        result=dict(preflight(root),fingerprint=fingerprint,planned_fits=9945,budgets=list(BUDGETS),temporal=False)
    elif args.command=='status':
        result=status_report(root/'audit/development/compact_gbst_manual_v1')
    else:
        validate_runtime(root)
        fingerprint,_=validate_authorization(root)
        from .compact_supervision import validate_supervised_child
        validate_supervised_child(args.supervision_receipt,fingerprint=fingerprint,workers=args.workers)
        run=root/'audit/development/compact_gbst_manual_v1'
        with writer_lock(run):
            CachedCompactWriteGuard.initialize(run,fingerprint)
            result=pilot(root,run,fingerprint,args.retry_failed) if args.command=='pilot' else launch_contexts(root,run,fingerprint,args.workers,args.retry_failed)
    print(json.dumps(result,sort_keys=True,indent=2,allow_nan=False))


if __name__=='__main__':main()
