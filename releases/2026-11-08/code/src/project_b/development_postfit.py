"""Saved OOF evaluation; no survival or recalibration training occurs here."""
import hashlib
import json
from pathlib import Path
import numpy as np
from .development_bootstrap import (MODELS, REQUIRED_STREAMS, compute_development_attempt,
    attempt_indices, select_global_common_valid, required_output_keys)
from .development_parallel import atomic_json, load_context, context_directory
from .development_run import context_labels, _encoded


def assemble(run_dir,fingerprint,full):
    ids=[tuple(x) for x in full['ids']]; lookup={v:i for i,v in enumerate(ids)}
    if len(lookup)!=len(ids): raise ValueError('PATIENT_ID_DUPLICATION')
    arrays={s:np.full((10,len(ids),2),np.nan) for s in REQUIRED_STREAMS}
    coverage=np.zeros((10,len(ids)),dtype=int); failures=[]
    for label in context_labels()[:-1]:
        record=load_context(context_directory(run_dir,label),fingerprint,label)
        if record is None: raise ValueError('MISSING_OUTER_CONTEXT')
        repeat=int(label.split('|')[0].split('=')[1])-1
        positions=[lookup[tuple(x)] for x in record['target_ids']]
        if len(set(positions))!=len(positions): raise ValueError('OOF_DUPLICATION')
        coverage[repeat,positions]+=1
        for model in MODELS:
            result=record['models'][model]
            for suffix,key in [('native','native_risks'),('recal','recalibrated_risks')]:
                values=result.get('refit',{}).get(key) if suffix=='native' else result.get(key)
                stream=model+'_'+suffix
                if values is None: failures.append({'context':label,'stream':stream,'reason':result.get('status','MISSING')}); continue
                values=np.asarray(values,dtype=float)
                if values.shape!=(len(positions),2): raise ValueError('PREDICTION_SHAPE_DRIFT')
                arrays[stream][repeat,positions]=values
                if not np.isfinite(values).all(): failures.append({'context':label,'stream':stream,'reason':'STREAM_HOLD'})
    if not np.all(coverage==1): raise ValueError('OOF_COVERAGE_FAILURE')
    return arrays,failures


def point_estimates(predictions,outcome,root):
    """Evaluate each complete patient vector separately; never fill missing rows."""
    from sklearn.metrics import roc_auc_score
    from .development_bootstrap import COMPARISONS,CURVE_PROBABILITIES,_curve_values,uno_c_180
    from .development_calibration import _authorize,fit_evaluation_curve_only,_fit_unpenalized_logistic
    _authorize(str(root))
    repeats=[{} for _ in range(10)]
    def value(operation):
        try:
            answer=float(operation())
            if not np.isfinite(answer): raise ValueError('NONFINITE_METRIC')
            return {'value':answer,'reason':None}
        except (ValueError,RuntimeError,FloatingPointError,np.linalg.LinAlgError) as exc:
            return {'value':None,'reason':str(exc)}
    for stream in REQUIRED_STREAMS:
        a=np.asarray(predictions[stream],dtype=float)
        if a.shape!=(10,len(outcome['event']),2): raise ValueError('PREDICTION_SHAPE_DRIFT')
        for r in range(10):
            for h,horizon in enumerate((90,180)):
                prefix=f'stream|{stream}|{horizon}|'
                names=['brier','logloss','time_auc','calibration_intercept','calibration_slope']+[f'calibration_curve_{p:g}' for p in CURVE_PROBABILITIES]
                if horizon==180: names.append('uno_c')
                p=a[r,:,h];y=np.asarray(outcome[f'death_{horizon}d'])
                if not np.isfinite(p).all() or np.any(p<0) or np.any(p>1):
                    repeats[r].update({prefix+k:{'value':None,'reason':'INCOMPLETE_OR_INVALID_PATIENT_VECTOR'} for k in names})
                    continue
                q=np.clip(p,1e-6,1-1e-6);x=np.log(q/(1-q))
                metrics={'brier':value(lambda:np.mean((p-y)**2)),
                         'logloss':value(lambda:np.mean(-y*np.log(q)-(1-y)*np.log1p(-q))),
                         'time_auc':value(lambda:roc_auc_score(y,p)),
                         'calibration_intercept':value(lambda:_fit_unpenalized_logistic(np.ones((len(p),1)),y,offset=x)[0]),
                         'calibration_slope':value(lambda:_fit_unpenalized_logistic(np.column_stack([np.ones(len(p)),x]),y)[1])}
                if horizon==180: metrics['uno_c']=value(lambda:uno_c_180(outcome['event'],outcome['time'],p))
                try:
                    curves=_curve_values(fit_evaluation_curve_only(p,y,{'project_root':str(root)}))
                    for point,curve in zip(CURVE_PROBABILITIES,curves): metrics[f'calibration_curve_{point:g}']=value(lambda:curve)
                except (ValueError,RuntimeError,FloatingPointError,np.linalg.LinAlgError) as exc:
                    for point in CURVE_PROBABILITIES: metrics[f'calibration_curve_{point:g}']={'value':None,'reason':str(exc)}
                repeats[r].update({prefix+k:v for k,v in metrics.items()})
    for rows in repeats:
        for name,reference,candidate in COMPARISONS:
            for suffix in ('native','recal'):
                for horizon in (90,180):
                    prefix=f'stream|{reference}_{suffix}|{horizon}|'
                    for key in [k for k in rows if k.startswith(prefix)]:
                        metric=key[len(prefix):];left=rows[key];right=rows[f'stream|{candidate}_{suffix}|{horizon}|{metric}']
                        out=f'comparison|{name}_{suffix}|{horizon}|{metric}'
                        if left['value'] is None or right['value'] is None:
                            rows[out]={'value':None,'reason':'PAIRED_COMPONENT_UNAVAILABLE','components':[key,f'stream|{candidate}_{suffix}|{horizon}|{metric}']}
                        else:
                            delta=left['value']-right['value'] if metric in ('brier','logloss') else right['value']-left['value']
                            rows[out]={'value':float(delta),'reason':None}
    means={}
    for key in required_output_keys():
        values=[row[key]['value'] for row in repeats]
        means[key]=({'value':float(np.mean(values)),'reason':None} if all(v is not None for v in values) else
                    {'value':None,'reason':'INCOMPLETE_TEN_REPEAT_ESTIMATE','unavailable_repeats':[r+1 for r,v in enumerate(values) if v is None]})
    return {'status':'PASS' if all(x['value'] is not None for x in means.values()) else 'POINT_ESTIMATES_PARTIAL',
            'all_metrics_by_repeat':repeats,'mean_metrics_and_paired_differences':means}


def evaluate(root,run_dir,fingerprint,full,quota):
    directory=Path(run_dir)/'postfit'; directory.mkdir(exist_ok=True)
    predictions,failures=assemble(run_dir,fingerprint,full)
    outcome={'time':full['times'],'event':full['events'],'death_90d':full['outcome90'],'death_180d':full['events']}
    # Preserve all streams, full patient identities, and failed slots as JSON null.
    archive={'fingerprint':fingerprint,'ids':full['ids'],'outcomes':outcome,'failures':failures,
             'predictions':{s:[[[None if not np.isfinite(v) else float(v) for v in row] for row in rep] for rep in a] for s,a in predictions.items()}}
    atomic_json(directory/'OOF_PREDICTIONS.json',archive,quota)
    point=point_estimates(predictions,outcome,root)
    point['failures']=failures
    atomic_json(directory/'POINT_ESTIMATES.json',point,quota)
    if failures:
        result={'status':'BOOTSTRAP_INTERVALS_HOLD','reason':'MISSING_REQUIRED_STREAM','intervals':[],
                'attempted_n':0,'common_valid_ids':[],'rejections':{}}
        atomic_json(directory/'BOOTSTRAP.json',result,quota)
        return {'status':'HOLD','bootstrap_status':result['status'],'point_status':point['status'],'intervals':[]}
    spec=json.loads((Path(root)/'config/BOOTSTRAP_SPEC.json').read_text()); spec['project_root']=str(root)
    archive_hash=hashlib.sha256(_encoded(archive)).hexdigest()
    def attempts():
        for attempt in range(1,2201):
            path=directory/'bootstrap_attempts'/f'{attempt:04d}.json'
            if path.exists():
                saved=json.loads(path.read_text())
                if saved['fingerprint']!=fingerprint or saved['archive_sha256']!=archive_hash or saved['sha256']!=hashlib.sha256(_encoded(saved['payload'])).hexdigest():
                    raise ValueError('CHECKPOINT_TAMPER')
                row=saved['payload']
            else:
                indices=attempt_indices(len(full['ids']),attempt_id=attempt,spec=spec)
                try: row={'attempt_id':attempt,'outputs':compute_development_attempt(predictions,outcome,indices,str(root)),'failures':[]}
                except (ValueError,RuntimeError,FloatingPointError) as exc: row={'attempt_id':attempt,'outputs':{},'failures':[str(exc)]}
                row['draw_sha256']=hashlib.sha256(_encoded(indices)).hexdigest()
                atomic_json(path,{'fingerprint':fingerprint,'archive_sha256':archive_hash,'payload':row,'sha256':hashlib.sha256(_encoded(row)).hexdigest()},quota)
            yield row
    result=select_global_common_valid(attempts(),target_valid=2000,maximum_attempts=2200,required_outputs=required_output_keys())
    atomic_json(directory/'BOOTSTRAP.json',result,quota)
    return {'status':'COMPLETE' if result['status']=='PASS' and point['status']=='PASS' else 'HOLD',
            'bootstrap_status':result['status'],'point_status':point['status'],'attempts':result['attempted_n']}
