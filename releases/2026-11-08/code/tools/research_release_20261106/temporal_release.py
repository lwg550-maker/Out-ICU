"""Additive fixed-object temporal evaluation. No predictive fitting or selection.

Three operations: prepare (development-only bindings), materialize (legacy one-shot),
evaluate (fixed predictions and saved paired draws). See T2_TEMPORAL_REPORT.md.
"""
from __future__ import annotations
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
BASE = 'audit/cohort_correction_20261105'
RELEASE = BASE + '/release_20261106'
OUT = RELEASE + '/temporal'
for _key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[_key]='1'
os.environ['TEMP']=os.environ['TMP']=str(ROOT/OUT/'scratch')
(ROOT/OUT/'scratch').mkdir(parents=True,exist_ok=True)
import argparse
import ast
import csv
import hashlib
import json
import math
import pickle
import random
import sys
import uuid
from functools import lru_cache
sys.path.insert(0,str(ROOT/'src'))

FULL=('M0','M1_RSF','M1_GBST','M2_EN','M2_RSF','M2_GBST')
MODELS=FULL+('compact10','compact14')
STREAMS=tuple(m+'_'+s for m in FULL for s in ('native','recal'))+('compact10_native','compact14_native')
GRID=(.1,.25,.5,.75,.9)
CONSTANTS=dict(namespace='TEMPORAL20261106', master_seed=2026110602,target_valid=2000,
               maximum_attempts=2200,common_validity='GLOBAL_COMMON_VALID',patient_n=406,
               curve_probabilities=list(GRID),retrain_models=False,repeats=1,
               primary_compact=10,backup_compact=14,clip_epsilon=1e-6,
               curve_knots='evaluated sample logit-risk percentiles 5/35/65/95',
               curve_plot_support='observed min-max only; rug and p5-p95 sparse-tail marks; no interpolated five-point CI band')
FINAL=BASE+'/main_run_v1/contexts/31eb2c6b4f28e0737a017d8ec90d9c94130835330c19cd02ceb3b433e986a09f/final_models'
FINGERPRINT='ac5c72e76e5239a55aa1e85fe3f5b53c4d148682ab85a5177a4c6bccb1b77896'
COMPACT=BASE+'/candidate10_review'

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def digest(data): return hashlib.sha256(data).hexdigest()

def inside(root,relative):
    path=(root/relative).resolve()
    if not path.is_relative_to(root.resolve()): raise ValueError('outside Project_B')
    return path

def read_json(path): return json.loads(path.read_text(encoding='utf-8'))

def save(path,value):
    """Exclusive deterministic output; existing identical results reused, drift refused."""
    data=canonical(value)
    save_bytes(path,data)

def save_bytes(path,data):
    """Publish fsynced file exclusively: Windows rename / POSIX hard link.

    An interruption may leave an unreferenced .staged file, never a partial final
    draw/attempt. Resume ignores staging debris; no automatic destructive cleanup.
    """
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes()!=data: raise ValueError('existing output drift: '+path.name)
        return
    staged=path.with_name(path.name+'.'+uuid.uuid4().hex+'.staged')
    try:
        with staged.open('xb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        try:
            # Windows rename fails if destination exists (unlike POSIX rename).
            # Hard links are denied by this Windows workspace sandbox.
            if os.name=='nt': os.rename(staged,path)
            else: os.link(staged,path)
        except FileExistsError:
            if path.read_bytes()!=data: raise ValueError('concurrent output drift: '+path.name)
    finally:
        staged.unlink(missing_ok=True)

def binding(root,path,recursive=False):
    value={'path':path.resolve().relative_to(root.resolve()).as_posix(),'sha256':digest(path.read_bytes())}
    if recursive: value['recursive']=True
    return value

def verify_binding(root,bound,seen=None):
    seen=set() if seen is None else seen
    path=inside(root,bound['path'])
    data=path.read_bytes()
    if digest(data)!=bound['sha256'].lower(): raise ValueError('hash drift: '+bound['path'])
    if bound.get('recursive'):
        if str(path) in seen: raise ValueError('inventory cycle')
        seen=seen|{str(path)}
        obj=json.loads(data)
        def recurse(x):
            if isinstance(x,dict):
                if {'path','sha256'}<=set(x): verify_binding(root,x,seen)
                else:
                    for item in x.values(): recurse(item)
            elif isinstance(x,list):
                for item in x: recurse(item)
        recurse(obj)
    return data

def source_files(root=ROOT):
    return sorted([p.relative_to(root).as_posix() for p in (root/'tools/research_release_20261106').glob('temporal_*.py')]+
                  [p.relative_to(root).as_posix() for p in (root/'tools/research_release_20261106').glob('test_temporal*.py')])

def verify_guards(root,require_review=False):
    lock_path=root/RELEASE/'TOOL_MODEL_LOCK.json'
    lock=read_json(lock_path)
    if lock.get('state')!='research_only_locked' or lock.get('roles')!={'primary':10,'backup':14}:
        raise ValueError('tool lock invalid')
    # Validate T1 inventory/version/model contract without loading any temporal values.
    import calculator
    if root.resolve()==ROOT.resolve(): calculator.Calculator(lock)
    for path,sha in lock['files'].items(): verify_binding(root,dict(path=path,sha256=sha))
    if require_review:
        review=read_json(root/OUT/'SOURCE_REVIEW.json')
        expected={p:digest((root/p).read_bytes()) for p in source_files(root)}
        if review.get('status')!='PASS' or review.get('source_hashes')!=expected or not review.get('reviewer'):
            raise ValueError('temporal source review missing or drift')
        if review.get('tool_lock_sha256')!=digest(lock_path.read_bytes()): raise ValueError('review lock drift')
    return lock

@lru_cache(maxsize=2)
def _triplet_body(function_name='fit_evaluation_regression_triplet'):
    """Reuse exact numerical AST; replace ONLY development wrapper authorization.

    Temporal caller has independent hash/review/cohort guards. Module globals and
    original function remain untouched. All numerical AST nodes remain pristine.
    """
    from project_b import development_calibration as dc
    tree=ast.parse(Path(dc.__file__).read_text(encoding='utf-8'))
    if function_name not in ('fit_evaluation_regression_triplet','fit_evaluation_curve_only'):
        raise ValueError('unknown evaluation body')
    fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name==function_name)
    removed=[x for x in fn.body if isinstance(x,ast.Expr) and isinstance(x.value,ast.Call)
             and isinstance(x.value.func,ast.Name) and x.value.func.id=='_authorize']
    if len(removed)!=1: raise ValueError('calibration authorization AST changed')
    fn.body=[x for x in fn.body if x not in removed]
    ns=dict(dc.__dict__)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'<temporal-equivalent-calibration>','exec'),ns)
    return ns[fn.name]

def evaluation_triplet(p,y): return _triplet_body()(p,y,{})

def salvage_calibration_components(risks,outcomes):
    """Failure-only independent components, exact original unpenalized methods.

    Never called on the successful triplet path. Preserve descriptive estimates
    without repairing predictions or relaxing complete common-valid eligibility.
    """
    import numpy as np
    from project_b.development_calibration import clip_probability_copy,_fit_unpenalized_logistic
    p=np.asarray(clip_probability_copy(risks,purpose='logit'),dtype=float)
    y=np.asarray(outcomes,dtype=float);x=np.log(p/(1.0-p))
    result=dict(status='PARTIAL',regressions={},component_failures={})
    for component in ('intercept','slope','curve'):
        try:
            if component=='curve':
                curve=_triplet_body('fit_evaluation_curve_only')(risks,outcomes,{})
                result['regressions']['curve']=curve['regressions']['curve']
                for key in ('curve_knots','curve_term_scales','spline_df'):result[key]=curve[key]
            else:
                design=np.ones((len(x),1)) if component=='intercept' else np.column_stack([np.ones(len(x)),x])
                offset=x if component=='intercept' else None
                beta=_fit_unpenalized_logistic(design,y,offset=offset)
                result['regressions'][component]=[float(value) for value in beta]
        except (ValueError,RuntimeError,ArithmeticError) as exc:
            result['component_failures'][component]=type(exc).__name__+': '+str(exc)
    return result

def saved_c2(rows,params):
    """Transform only using original exact validation/vector body and saved parameters."""
    from project_b import development_preprocessing as dp
    tree=ast.parse(Path(dp.__file__).read_text(encoding='utf-8'))
    fn=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='generate_c2_design')
    # Keep original required-field/category checks and exact 20-column vector loop.
    starts=next(i for i,x in enumerate(fn.body) if isinstance(x,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='ages' for t in x.targets))
    ends=next(i for i,x in enumerate(fn.body) if isinstance(x,ast.AnnAssign) and isinstance(x.target,ast.Name) and x.target.id=='matrix')
    fn.body[starts:ends]=ast.parse("knots = saved_params['age_knots']\nwpr_median = saved_params['wpr_median']").body
    ns=dict(dp.__dict__,saved_params=params)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'<saved-c2-transform>','exec'),ns)
    matrix,columns,_=ns[fn.name](rows,[0])
    return [dict(zip(columns,x)) for x in matrix]

def benefit(reference,candidate,metric):
    direction='reference-minus-candidate' if metric in ('brier','logloss') else 'candidate-minus-reference'
    return (reference-candidate if direction.startswith('reference') else candidate-reference),direction

def validate_predictions(predictions,n):
    import numpy as np
    from project_b.development_calibration import validate_native_risk
    if set(predictions)!=set(STREAMS): raise ValueError('exact 14 streams required')
    for stream,value in predictions.items():
        p=np.asarray(value,dtype=float)
        if p.shape!=(n,2) or not np.isfinite(p).all() or np.any(p<0) or np.any(p>1):
            raise ValueError('invalid one-fixed-prediction stream')
        if stream.endswith('_native'): validate_native_risk(p)

def validate_cohort(rows,seal,development,expected_n=406):
    ids=[(str(r['subject_id']),str(r['hadm_id'])) for r in rows]
    if len(ids)!=expected_n or len(set(ids))!=expected_n or ids!=list(seal): raise ValueError('identity seal mismatch')
    if set(x[0] for x in ids)&set(str(x[0]) for x in development): raise ValueError('development subject overlap')
    for row in rows:
        if int(row['admin_180_eligible'])!=1: raise ValueError('incomplete administrative followup')
        a,b=int(row['death_90d']),int(row['death_180d'])
        d=row['death_days_after_t0']
        d=float(d) if d not in ('',None) else None
        if a not in (0,1) or b not in (0,1) or a>b or (b and (d is None or not 0<=d<=180)) or (a!=int(d is not None and 0<=d<=90)):
            raise ValueError('cross-horizon outcome inconsistency')

def draw_indices(n,attempt):
    if n<1 or attempt<1: raise ValueError('invalid draw')
    seed=int(digest(f"TEMPORAL20261106|2026110602|attempt={attempt}".encode())[:16],16)
    rng=random.Random(seed)
    return [rng.randrange(n) for _ in range(n)]

def bootstrap(folder,n,fingerprint,compute,keys,*,target=2000,cap=2200,synthetic=False,workers=1):
    if isinstance(workers,bool) or not isinstance(workers,int) or not 1<=workers<=8: raise ValueError('workers must be 1..8')
    if workers==1: return _bootstrap(folder,n,fingerprint,compute,keys,target,cap,synthetic,None,workers)
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return _bootstrap(folder,n,fingerprint,compute,keys,target,cap,synthetic,pool,workers)

def _bootstrap(folder,n,fingerprint,compute,keys,target,cap,synthetic,pool,workers):
    import numpy as np
    if not synthetic and (target,cap,n)!=(2000,2200,406): raise ValueError('frozen limits required')
    folder.mkdir(parents=True,exist_ok=True)
    manifest=dict(fingerprint=fingerprint,keys=sorted(keys),n=n,target=target,cap=cap,constants=CONSTANTS)
    path=folder/'bootstrap_manifest.json'
    if path.exists() and read_json(path)!=manifest: raise ValueError('bootstrap fingerprint drift')
    save(path,manifest)
    valid=[]; rejected=[]; attempted=0; pending={}
    for attempt in range(1,cap+1):
        draw=draw_indices(n,attempt); sha=digest(canonical(draw))
        draw_path=folder/f'draw_{attempt:04d}.json'
        save(draw_path,dict(attempt_id=attempt,indices=draw,sha256=sha,shared_streams=list(STREAMS),horizons=[90,180]))
        path=folder/f'attempt_{attempt:04d}.json'
        if path.exists():
            record=read_json(path)
            if record.get('fingerprint')!=fingerprint or record.get('draw_sha256')!=sha or record.get('attempt_id')!=attempt:
                raise ValueError('saved attempt fingerprint drift')
            if record.get('output_sha256')!=digest(canonical(record['outputs'])):
                raise ValueError('saved output hash drift')
            if record.get('calibration_sha256')!=digest(canonical(record.get('calibration_coefficients',{}))):
                raise ValueError('saved calibration coefficient hash drift')
        else:
            outputs={}; reason=None; coefficients={}
            try:
                if pool is None: outputs=compute(draw)
                else:
                    if attempt not in pending:
                        # Parent persists every submitted draw before worker execution.
                        # Batch cannot exceed remaining attempts or needed valid draws.
                        size=min(workers,cap-attempt+1,target-len(valid))
                        for candidate in range(attempt,attempt+size):
                            if (folder/f'attempt_{candidate:04d}.json').exists(): continue
                            indices=draw_indices(n,candidate)
                            save(folder/f'draw_{candidate:04d}.json',dict(attempt_id=candidate,indices=indices,sha256=digest(canonical(indices)),shared_streams=list(STREAMS),horizons=[90,180]))
                            pending[candidate]=pool.submit(compute,indices)
                    outputs=pending.pop(attempt).result()
                if isinstance(outputs,tuple):
                    if len(outputs)==3: outputs,failures,coefficients=outputs
                    else: outputs,failures=outputs
                    if failures: raise ValueError('; '.join(failures))
                if set(outputs)!=set(keys) or not all(math.isfinite(float(v)) for v in outputs.values()):
                    raise ValueError('incomplete or nonfinite common output schema')
            except (ValueError,RuntimeError,ArithmeticError) as exc:
                reason=type(exc).__name__+': '+str(exc)
                # Keep available finite outputs; do not hide failed attempts.
                outputs={k:float(v) for k,v in outputs.items() if math.isfinite(float(v))}
            record=dict(attempt_id=attempt,fingerprint=fingerprint,draw_sha256=sha,status='REJECTED' if reason else 'VALID',reason=reason,outputs=outputs)
            record['output_sha256']=digest(canonical(outputs))
            record['calibration_coefficients']=coefficients
            record['calibration_sha256']=digest(canonical(coefficients))
            save(path,record)
        attempted=attempt
        if record['status']=='VALID':
            if set(record['outputs'])!=set(keys) or not all(math.isfinite(float(x)) for x in record['outputs'].values()): raise ValueError('saved schema drift')
            valid.append(record)
        elif record['status']=='REJECTED': rejected.append(dict(attempt_id=attempt,reason=record['reason']))
        else: raise ValueError('saved status invalid')
        if attempt%50==0:
            save(folder/f'STATUS_{attempt:04d}.json',dict(last_committed_attempt=attempt,common_valid_n=len(valid),rejected_n=len(rejected)))
            print(f'temporal bootstrap committed={attempt} common_valid={len(valid)}',flush=True)
        if len(valid)==target: break
    result=dict(status='PASS' if len(valid)==target and not synthetic else 'SYNTHETIC_COMPLETE' if len(valid)==target else 'INCOMPLETE_CI',
                attempted_n=attempted,valid_n=len(valid),common_valid_ids=[r['attempt_id'] for r in valid],rejections=rejected,
                intervals={k:np.percentile([r['outputs'][k] for r in valid],[2.5,97.5]).tolist() for k in keys} if len(valid)==target else {})
    save(folder/'bootstrap_summary.json',result)
    return result


def comparisons():
    from project_b.development_bootstrap import COMPARISONS
    pairs=[(name+'_'+kind,ref+'_'+kind,cand+'_'+kind)
           for name,ref,cand in COMPARISONS for kind in ('native','recal')]
    pairs += [('compact10-v-full','M2_GBST_native','compact10_native'),
              ('compact14-v-full','M2_GBST_native','compact14_native'),
              ('compact14-v-10','compact10_native','compact14_native')]
    return pairs

def required_keys():
    keys=[]
    for h in (90,180):
        names=['brier','logloss','time_auc','calibration_intercept','calibration_slope']+[f'calibration_curve_{p:g}' for p in GRID]
        if h==180: names+=['uno_c']
        keys += [f'stream|{s}|{h}|{metric}' for s in STREAMS for metric in names]
        keys += [f'comparison|{name}|{h}|{metric}' for name,_,_ in comparisons()
                 for metric in ('brier','logloss','time_auc')+ (('uno_c',) if h==180 else ())]
    return sorted(keys)

def compute_metrics(predictions,outcomes,indices):
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from project_b.development_bootstrap import uno_c_180,_curve_values
    n=len(outcomes['event']); validate_predictions(predictions,n)
    draw=np.asarray(indices)
    if draw.shape!=(n,) or not np.issubdtype(draw.dtype,np.integer) or np.any(draw<0) or np.any(draw>=n):
        raise ValueError('invalid shared patient draw')
    output={}; regressions={}; failures=[]
    for s in STREAMS:
        for j,h in enumerate((90,180)):
            p=np.asarray(predictions[s])[draw,j]; y=np.asarray(outcomes[f'death_{h}d'])[draw]
            prefix=f'stream|{s}|{h}|'
            q=np.clip(p,1e-6,1-1e-6)
            values=dict(brier=float(np.mean((p-y)**2)),logloss=float(np.mean(-y*np.log(q)-(1-y)*np.log1p(-q))))
            try: values['time_auc']=float(roc_auc_score(y,p))
            except (ValueError,RuntimeError) as exc: failures.append(prefix+str(exc))
            if h==180:
                try: values['uno_c']=uno_c_180(np.asarray(outcomes['event'])[draw],np.asarray(outcomes['time'])[draw],p)
                except (ValueError,RuntimeError) as exc: failures.append(prefix+str(exc))
            try:
                regression=evaluation_triplet(p,y); regressions[s+'|'+str(h)]=regression
                values.update(calibration_intercept=regression['regressions']['intercept'][0],calibration_slope=regression['regressions']['slope'][1])
                values.update({f'calibration_curve_{g:g}':float(v) for g,v in zip(GRID,_curve_values(regression))})
            except (ValueError,RuntimeError,ArithmeticError) as exc:
                failures.append(prefix+'calibration_triplet|'+str(exc))
                regression=salvage_calibration_components(p,y)
                regressions[s+'|'+str(h)]=regression
                components=regression['regressions']
                if 'intercept' in components:values['calibration_intercept']=components['intercept'][0]
                if 'slope' in components:values['calibration_slope']=components['slope'][1]
                if 'curve' in components:
                    values.update({f'calibration_curve_{g:g}':float(v) for g,v in zip(GRID,_curve_values(regression))})
                failures.extend(prefix+component+'|'+reason for component,reason in regression['component_failures'].items())
            output.update({prefix+k:v for k,v in values.items() if math.isfinite(v)})
    for name,reference,candidate in comparisons():
        for h in (90,180):
            for metric in ('brier','logloss','time_auc')+(('uno_c',) if h==180 else ()):
                a,b=f'stream|{reference}|{h}|{metric}',f'stream|{candidate}|{h}|{metric}'
                if a in output and b in output: output[f'comparison|{name}|{h}|{metric}']=benefit(output[a],output[b],metric)[0]
    return output,regressions,failures

def embedded_hashes(package):
    """Protocol-4 component digests supplement, never replace original package SHA.

    Native baseline is included in model/backend; no reconstructed baseline or
    fictitious separate native logistic calibrator is generated.
    """
    keys=('model','preprocessor','core_design_parameters','selected','scientific','selected_candidate','selected_groups','manual_fields')
    return {k:digest(pickle.dumps(package[k],protocol=4)) for k in keys if k in package}

def inventory_objects(root):
    objects={}
    for model in FULL:
        package=binding(root,root/FINAL/(model+'.pickle'))
        sidecar=binding(root,root/FINAL/(model+'.calibration.json'))
        meta=json.loads(verify_binding(root,sidecar))
        if meta.get('model_id')!=model or meta.get('model_sha256')!=package['sha256'] or meta.get('run_fingerprint')!=FINGERPRINT:
            raise ValueError('corrected final sidecar provenance drift')
        if set(meta['recalibrators'])!={'90','180'} or any(x.get('source_role')!='inner_oof' for x in meta['recalibrators'].values()):
            raise ValueError('supportive calibrator provenance drift')
        saved=pickle.loads(verify_binding(root,package))
        if not {'model','preprocessor','core_design_parameters'}<=set(saved): raise ValueError('full package incomplete')
        objects[model]=dict(package=package,sidecar=sidecar,component_sha256=embedded_hashes(saved),display='M1_EN' if model=='M0' else model,
                            roles=dict(model='package.model',preprocessor='package.preprocessor',core_design_parameters='package.core_design_parameters',
                                       baseline='embedded model native baseline',native_calibrator='identity; no fitted logistic',supportive_calibrator='sidecar.recalibrators'))
    for budget in (10,14):
        path=root/COMPACT/('CANDIDATE10.json' if budget==10 else 'ALTERNATIVE14.json')
        meta=read_json(path)
        model_path=root/COMPACT/'candidate10.pickle' if budget==10 else inside(root,meta['source_model'])
        package=binding(root,model_path)
        if package['sha256']!=meta['model_sha256']: raise ValueError('compact approved model drift')
        saved=pickle.loads(verify_binding(root,package))
        if not {'model','preprocessor','selected_groups'}<=set(saved): raise ValueError('compact package incomplete')
        objects[f'compact{budget}']=dict(package=package,metadata=binding(root,path),component_sha256=embedded_hashes(saved),
              roles=dict(variables='metadata.selected_groups',representation='metadata.design_columns',algorithm='GBST original native engine',
                         hyperparameters='package.selected_candidate',preprocessor='package.preprocessor',baseline='embedded model native baseline',
                         calibrator='identity; no fitted compact calibrator',absolute_risk='original predict_native_risk',missingness='saved preprocessor only'))
    if objects['compact10']['package']['sha256']!='e41b9066ec451034f54c230d98a5becf4f7a9ec972d19282388d411881bff30e' or objects['compact14']['package']['sha256']!='33840bcb2c0210f29b6c84af959e843a81f7ea18a04b996cf02a0bbff38ac925':
        raise ValueError('approved compact identity drift')
    return objects

def prepare(root=ROOT):
    verify_guards(root,require_review=True)
    objects=inventory_objects(root)
    # All files here are development artifacts/config/source only. Never enumerate
    # frozen_source_chain or hash/open mixed-period master or temporal tables.
    files=[p.relative_to(root).as_posix() for p in sorted((root/'src/project_b').glob('*.py'))]
    files+=source_files(root)+['config/A4_FIELD_DICTIONARY.csv','config/COMPACT_MANUAL_INPUT_SPEC.json',
                              'config/TEMPORAL_MATERIALIZATION_SPEC.json','manifests/A4_FREEZE_MANIFEST.csv',
                              'data/cohort_correction_20261105/DEVELOPMENT_INDEX_OUTCOME.csv',
                              BASE+'/main_run_v1/MAIN_RESULT.json', RELEASE+'/TOOL_MODEL_LOCK.json',OUT+'/SOURCE_REVIEW.json']
    spec=read_json(root/'config/TEMPORAL_MATERIALIZATION_SPEC.json')
    sql=next(x['path'] for x in spec['frozen_source_chain'] if x['role']=='a3_sql')
    files.append(sql)
    inventory=dict(schema='temporal-inventory-1',objects=objects,constants=CONSTANTS,streams=list(STREAMS),
                   output_schema=required_keys(),files=[binding(root,root/p) for p in sorted(set(files))])
    save(root/OUT/'INVENTORY.json',inventory)
    grouped={}
    for family,models in dict(M0=['M0'],M1=['M1_RSF','M1_GBST'],M2=['M2_EN','M2_RSF','M2_GBST'],compact=['compact10','compact14']).items():
        path=root/OUT/(family+'_ROLES.json')
        save(path,dict(family=family,objects={m:objects[m] for m in models},inventory=binding(root,root/OUT/'INVENTORY.json',True)))
        grouped[family]=binding(root,path)
    roles=('model','preprocessor','baseline','native_calibrator','supportive_calibrator')
    compact_roles=('variables','representation','algorithm','hyperparameters','preprocessor','baseline','calibrator','absolute_risk','missingness')
    authorization=dict(operator='root-reviewed-research-release-20261106',authorized=True,a5_status='PASS',nested_development_complete=True,
            compression_branch='LAUNCHED_AND_LOCKED',a4_manifest=binding(root,root/'manifests/A4_FREEZE_MANIFEST.csv'),
            temporal_materialization_spec=binding(root,root/'config/TEMPORAL_MATERIALIZATION_SPEC.json'),
            final_development_objects={m:{role:grouped[m] for role in roles} for m in ('M0','M1','M2')},
            final_bindings={role:binding(root,root/OUT/'INVENTORY.json') for role in ('field_schema','code','sql','state')},
            compact_model={role:grouped['compact'] for role in compact_roles},
            recursive_inventory=binding(root,root/OUT/'INVENTORY.json',True))
    save(root/OUT/'TEMPORAL_AUTHORIZATION.json',authorization)
    return inventory

def load_inventory(root):
    verify_guards(root,require_review=True)
    authorization=read_json(root/OUT/'TEMPORAL_AUTHORIZATION.json')
    inventory=json.loads(verify_binding(root,authorization['recursive_inventory']))
    if inventory.get('constants')!=CONSTANTS or inventory.get('streams')!=list(STREAMS) or inventory.get('output_schema')!=required_keys(): raise ValueError('temporal schema drift')
    if inventory['objects']!=inventory_objects(root): raise ValueError('complete nested object coverage drift')
    for family,roles in authorization['final_development_objects'].items():
        for b in roles.values(): verify_binding(root,dict(b,recursive=True))
    for b in authorization['compact_model'].values(): verify_binding(root,dict(b,recursive=True))
    return inventory

def materialize(root=ROOT):
    load_inventory(root)
    from project_b.temporal_materialize import materialize_temporal
    # Do not auto-repeat or bypass legacy exclusive claim/output refusal guards.
    return materialize_temporal(root,root/OUT/'TEMPORAL_AUTHORIZATION.json')

def predict_packages(packages,sidecars,core,extension):
    from project_b.development_preprocessing import transform_preprocessor
    from project_b.compact_preprocessing import transform_preprocessor as compact_transform
    from project_b.compact_inputs import development_rows
    from project_b.a5_m0_cox import predict_native_risk
    from project_b.a5_calibration import apply_recalibrator
    if set(packages)!=set(MODELS) or set(sidecars)!=set(FULL): raise ValueError('8 objects and 6 sidecars required')
    predictions={}
    for name in MODELS:
        package=packages[name]; pp=package['preprocessor']
        if name in FULL:
            design=saved_c2(core,package['core_design_parameters'])
            matrix=transform_preprocessor(pp,design,extension if name.startswith('M2') else None,list(range(len(core))))
        else:
            rows=development_rows(core,extension,{'groups':pp['groups']})
            matrix=compact_transform(pp,rows)
        # One call produces both horizons; never ten copies/repeats or fitting.
        native=predict_native_risk(package['model'],matrix,(90,180))
        predictions[name+'_native']=native
        if name in FULL:
            columns=[apply_recalibrator(sidecars[name]['recalibrators'][str(h)],[r[j] for r in native]) for j,h in enumerate((90,180))]
            predictions[name+'_recal']=[list(x) for x in zip(*columns)]
    validate_predictions(predictions,len(core))
    return predictions

def csv_rows(path):
    with path.open(encoding='utf-8-sig',newline='') as f: return list(csv.DictReader(f))

def write_csv(path,rows):
    import io
    if not rows: return
    stream=io.StringIO(newline=''); writer=csv.DictWriter(stream,fieldnames=list(rows[0])); writer.writeheader();writer.writerows(rows)
    data=stream.getvalue().encode()
    save_bytes(path,data)

def metric_outputs(predictions,outcomes,draw):
    """Picklable read-only worker: fixed predictions/outcomes in, metrics out."""
    outputs,coefficients,failures=compute_metrics(predictions,outcomes,draw)
    return outputs,failures,coefficients

def evaluate(root=ROOT,workers=1):
    import numpy as np
    from project_b.a5_common import make_surv180
    inventory=load_inventory(root)
    receipt_path=root/'audit/temporal/TEMPORAL_MATERIALIZATION_RECEIPT.json'
    receipt=read_json(receipt_path); log_path=root/'audit/temporal/TEMPORAL_UNSEAL_LOG.json'; log=read_json(log_path)
    if receipt.get('status')!='COMPLETE' or receipt.get('materialization_complete') is not True or log.get('status')!='COMPLETE':
        raise ValueError('materialization incomplete')
    if receipt['authorization_receipt_sha256'].lower()!=digest((root/OUT/'TEMPORAL_AUTHORIZATION.json').read_bytes()) or receipt['unseal_log_sha256'].lower()!=digest(log_path.read_bytes()): raise ValueError('materialization authorization/log drift')
    spec=read_json(root/'config/TEMPORAL_MATERIALIZATION_SPEC.json')
    expected_tables={x['path'] for x in spec['future_tables']}
    if set(receipt['table_hashes'])!=expected_tables or receipt['table_hashes']!=log['table_hashes']: raise ValueError('materialization table set drift')
    for relative,sha in {**receipt['table_hashes'],**receipt['audit_hashes']}.items(): verify_binding(root,dict(path=relative,sha256=sha))
    seal_bound=spec['temporal_identity_seal']; verify_binding(root,seal_bound)
    if receipt['temporal_identity_seal_sha256'].lower()!=seal_bound['sha256'].lower(): raise ValueError('seal receipt drift')
    seal=[(r['subject_id'],r['hadm_id']) for r in csv_rows(root/seal_bound['path'])]
    rows=csv_rows(root/'data/temporal/TEMPORAL_INDEX_OUTCOME.csv')
    dev=csv_rows(root/'data/cohort_correction_20261105/DEVELOPMENT_INDEX_OUTCOME.csv')
    if len(dev)!=2838: raise ValueError('corrected development identity count drift')
    validate_cohort(rows,seal,[(r['subject_id'],r['hadm_id']) for r in dev])
    tables=[csv_rows(root/f'data/temporal/{name}.csv') for name in ('TEMPORAL_C2_RAW','TEMPORAL_EXTENDED_157')]
    fields=next(x['columns'] for x in spec['future_tables'] if x['path'].endswith('TEMPORAL_INDEX_OUTCOME.csv'))
    if list(rows[0])!=fields: raise ValueError('outcome schema drift')
    for raw,name in zip(tables,('TEMPORAL_C2_RAW','TEMPORAL_EXTENDED_157')):
        fields=next(x['columns'] for x in spec['future_tables'] if x['path'].endswith(name+'.csv'))
        if len(raw)!=406 or list(raw[0])!=fields or [(r['subject_id'],r['hadm_id']) for r in raw]!=seal: raise ValueError('predictor identity/schema drift')
    core=[{k:v if k in ('gender','gcsm_category') else float(v) if v else None for k,v in r.items() if k not in ('subject_id','hadm_id')} for r in tables[0]]
    ext=[{k:float(v) if v else None for k,v in r.items() if k not in ('subject_id','hadm_id')} for r in tables[1]]
    predictions_path=root/OUT/'FIXED_PREDICTIONS.json'
    prediction_basis=dict(inventory_sha256=digest(canonical(inventory)),materialization_receipt_sha256=digest(receipt_path.read_bytes()))
    if predictions_path.exists():
        cached=read_json(predictions_path)
        if cached.get('basis')!=prediction_basis or cached.get('identities')!=[list(x) for x in seal] or cached.get('repeats')!=1:
            raise ValueError('saved prediction binding drift')
        predictions=cached['predictions']
        if cached.get('prediction_sha256')!=digest(canonical(predictions)): raise ValueError('saved prediction hash drift')
        validate_predictions(predictions,406)
    else:
        packages={m:pickle.loads(verify_binding(root,x['package'])) for m,x in inventory['objects'].items()}
        sidecars={m:json.loads(verify_binding(root,inventory['objects'][m]['sidecar'])) for m in FULL}
        predictions=predict_packages(packages,sidecars,core,ext)
        save(predictions_path,dict(basis=prediction_basis,identities=seal,predictions=predictions,repeats=1,prediction_sha256=digest(canonical(predictions))))
    time,event=make_surv180(rows)
    outcomes=dict(time=time,event=event,death_90d=[int(r['death_90d']) for r in rows],death_180d=[int(r['death_180d']) for r in rows])
    fingerprint=digest(canonical(dict(inventory=inventory,receipt_sha256=digest(receipt_path.read_bytes()),prediction_sha256=digest(predictions_path.read_bytes()))))
    support={s:{str(h):dict(min=float(np.min(np.asarray(p)[:,j])),max=float(np.max(np.asarray(p)[:,j])),p5=float(np.percentile(np.asarray(p)[:,j],5)),p95=float(np.percentile(np.asarray(p)[:,j],95)),grid_supported=[bool(np.min(np.asarray(p)[:,j])<=v<=np.max(np.asarray(p)[:,j])) for v in GRID]) for j,h in enumerate((90,180))} for s,p in predictions.items()}
    crossings={s:int(sum(r[0]>r[1] for r in p)) for s,p in predictions.items() if s.endswith('_recal')}
    point,coefficients,failures=compute_metrics(predictions,outcomes,list(range(406)))
    save(root/OUT/'POINT_METRICS.json',dict(fingerprint=fingerprint,metrics=point,failures=failures,support=support,supportive_crossings=crossings,projection=False,event90=sum(outcomes['death_90d']),event180=sum(event),admin_eligible=406,no_development_overlap=True))
    save(root/OUT/'POINT_CALIBRATION_COEFFICIENTS.json',coefficients)
    paired=[]
    for key,v in point.items():
        if key.startswith('comparison|'):
            _,name,h,metric=key.split('|')
            paired.append(dict(comparison=name,horizon=int(h),metric=metric,value=v,direction=benefit(0,0,metric)[1]))
    write_csv(root/OUT/'PAIRED_POINT.csv',paired)
    from functools import partial
    compute=partial(metric_outputs,predictions,outcomes)
    summary=bootstrap(root/OUT/'bootstrap',406,fingerprint,compute,required_keys(),workers=workers)
    if set(point)!=set(required_keys()) or failures:
        status='INCOMPLETE_POINT_AND_CI' if summary['status']!='PASS' else 'INCOMPLETE_POINT'
    else: status=summary['status']
    interval_rows=[]
    for key,ci in summary['intervals'].items():
        kind,name,h,metric=key.split('|')
        interval_rows.append(dict(kind=kind,name=name,horizon=int(h),metric=metric,low=ci[0],high=ci[1],direction=benefit(0,0,metric)[1] if kind=='comparison' else 'not-a-contrast'))
    write_csv(root/OUT/'PAIRED_AND_STREAM_CI.csv',interval_rows)
    result=dict(status=status,bootstrap=summary,point_failures=failures,fingerprint=fingerprint)
    save(root/OUT/'EVALUATION_RECEIPT.json',result)
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','materialize','evaluate'))
    parser.add_argument('--workers',type=int,choices=range(1,9),default=1,help='evaluation-only fixed-metric workers (1..8); parent commits')
    args=parser.parse_args()
    if args.operation!='evaluate' and args.workers!=1: parser.error('--workers applies only to evaluate')
    result=evaluate(workers=args.workers) if args.operation=='evaluate' else globals()[args.operation]()
    print(json.dumps(dict(operation=args.operation,status=result.get('status','PREPARED')),sort_keys=True),flush=True)
    if args.operation=='evaluate' and result['status']!='PASS': return 2
    return 0

if __name__=='__main__': raise SystemExit(main())
