"""Authorized development task ledger and independently gated production entrypoint."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

CEILING=15198


class RecordedFailure(RuntimeError):
    pass


class DevelopmentHold(RuntimeError):
    pass


def pause_for_failure(ledger,context,model,stage,reason,force_project=False):
    import re
    from .a5_common import classify_failure
    from .development_parallel import atomic_json
    from .development_accounting import frozen_plan,record_skips,read_skips
    status='PROJECT_HOLD' if force_project or model in ('M0','M2_EN') else 'ALGORITHM_HOLD'
    for token in re.findall(r'(?<![A-Z0-9_])[A-Z][A-Z0-9_]*(?![A-Z0-9_])',str(reason)):
        try:
            if classify_failure(token) in ('PROJECT_HOLD','EXECUTION_ERROR'): status='PROJECT_HOLD'
        except ValueError: pass
    if ledger.guard and getattr(ledger.guard,'stop_event',None) is not None: ledger.guard.stop_event.set()
    record={'status':status,'context':context,'model':model,'family':model.split('_')[-1],
            'stage':stage,'reason':str(reason),'fingerprint':ledger.fingerprint,
            'independent_review_required':True}
    atomic_json(ledger.path/'HOLD.json',record,ledger.guard)
    prior_skips=read_skips(ledger.path,ledger.fingerprint)
    record_skips(ledger,[k for k in frozen_plan() if k.startswith(context+'|') and k not in ledger.state and k not in prior_skips],
                 status+':'+str(reason),'HOLD.json')
    raise DevelopmentHold(status+':'+model+':'+str(reason))


def _sha256_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def context_labels():
    return [f'outer_repeat={r}|outer_fold={k}' for r in range(1,11) for k in range(1,6)] + ['full_development']


def _encoded(obj):
    return (json.dumps(obj,sort_keys=True,separators=(',', ':'),allow_nan=False)+'\n').encode()


def _append(path, obj):
    with path.open('ab') as f:
        f.write(_encoded(obj)); f.flush(); os.fsync(f.fileno())


class DurableTasks:
    """One exclusive coordinator; unfinished attempt is never silently replayed."""
    def __init__(self, directory, fingerprint):
        self.path=Path(directory)
        self.path.mkdir(parents=True,exist_ok=True)
        self.fingerprint=fingerprint
        if not isinstance(fingerprint,str) or len(fingerprint)!=64:
            raise ValueError('full run fingerprint required')
        self.lock=self.path/'coordinator.lock'
        self.fd=None
        self.ledger=self.path/'attempts.jsonl'
        self.state={}
        self.hashes={}
        self.failures={}
        self.attempts=0
        self.guard=None
        self.ceiling=CEILING
        self.context=None

    def __enter__(self):
        self.fd=os.open(self.lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.write(self.fd,_encoded({'pid':os.getpid(),'fingerprint':self.fingerprint}))
        os.fsync(self.fd)
        try:
            self.state, self.attempts = self._read()
            if 'START' in self.state.values():
                raise RuntimeError('INTERRUPTED_ATTEMPT_REQUIRES_HOLD')
        except BaseException:
            os.close(self.fd); self.fd=None
            self.lock.unlink()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        os.close(self.fd); self.fd=None
        self.lock.unlink()

    def _read(self):
        entries=[]
        if self.ledger.exists():
            for line in self.ledger.read_bytes().splitlines():
                entries.append(json.loads(line))
        state={}
        self.hashes={}
        self.failures={}
        attempts=0
        for e in entries:
            if e.get('fingerprint')!=self.fingerprint: raise ValueError('CHECKPOINT_FINGERPRINT_DRIFT')
            key=e['task']; action=e['action']
            if action=='START':
                if key in state: raise ValueError('DUPLICATE_ATTEMPT')
                state[key]='START'; attempts+=1
            elif action in ('SUCCESS','FAILURE') and state.get(key)=='START':
                state[key]=action
                if action=='FAILURE': self.failures[key]=e.get('message','RECORDED_FAILURE')
                if action=='SUCCESS':
                    path=self.path/'results'/f'{hashlib.sha256(key.encode()).hexdigest()}.json'
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=e['sha256']:
                        raise ValueError('CHECKPOINT_TAMPER')
                    self.hashes[key]=e['sha256']
            else: raise ValueError('CHECKPOINT_LEDGER_SEQUENCE')
        if attempts>CEILING: raise ValueError('FIT_CEILING_EXCEEDED')
        return state,attempts

    def _record(self, obj):
        if self.guard: self.guard.check_write(len(_encoded(obj)))
        _append(self.ledger,obj)

    def execute(self, task, operation):
        if self.fd is None: raise RuntimeError('coordinator lock required')
        if not isinstance(task,str) or not task: raise ValueError('task identity required')
        state,attempts=self.state,self.attempts
        target=self.path/'results'/f'{hashlib.sha256(task.encode()).hexdigest()}.json'
        if state.get(task)=='SUCCESS':
            payload=target.read_bytes()
            if hashlib.sha256(payload).hexdigest()!=self.hashes[task]: raise ValueError('CHECKPOINT_TAMPER')
            return json.loads(payload)
        if state.get(task)=='FAILURE': raise RecordedFailure(self.failures[task])
        if task in state: raise RuntimeError('ATTEMPT_NOT_REPLAYABLE')
        from .development_accounting import read_skips
        if task in read_skips(self.path,self.fingerprint): raise RuntimeError('SKIPPED_TASK_NOT_REPLAYABLE')
        if self.guard and hasattr(self.guard,'check_fit'): self.guard.check_fit()
        if self.context and not task.startswith(self.context+'|'): raise ValueError('CONTEXT_LEDGER_DRIFT')
        if attempts>=self.ceiling: raise RuntimeError('FIT_CEILING_EXCEEDED')
        self._record({'action':'START','task':task,'fingerprint':self.fingerprint})
        self.state[task]='START'; self.attempts+=1
        try:
            result=operation()
            payload=_encoded(result)
            if self.guard: self.guard.check_write(len(payload))
            target.parent.mkdir(exist_ok=True)
            tmp=target.with_suffix('.tmp')
            with tmp.open('xb') as f:
                f.write(payload); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,target)
            self._record({'action':'SUCCESS','task':task,'fingerprint':self.fingerprint,
                                 'sha256':hashlib.sha256(payload).hexdigest()})
            self.state[task]='SUCCESS'; self.hashes[task]=hashlib.sha256(payload).hexdigest()
            return result
        except Exception as exc:
            self._record({'action':'FAILURE','task':task,'fingerprint':self.fingerprint,
                                 'error_type':type(exc).__name__,'message':str(exc)})
            self.state[task]='FAILURE'; self.failures[task]=str(exc)
            raise

def assemble_selected_oof(target_ids, selected, fold_predictions):
    """Reconstruct selected OOF predictions from already-accounted candidate fits."""
    from .a5_calibration import validate_native_risk
    ids=[tuple(identity) for identity in target_ids]
    if len(ids)!=len(set(ids)) or not ids:
        raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
    if len(fold_predictions)!=5:
        raise ValueError('INNER_FOLD_COUNT_DRIFT')
    result={}
    seen_folds=set()
    for record in fold_predictions:
        fold=record['fold']
        if fold not in range(1,6) or fold in seen_folds or record['candidate_id']!=selected:
            raise ValueError('OOF_CANDIDATE_OR_FOLD_DRIFT')
        seen_folds.add(fold)
        fold_ids=[tuple(identity) for identity in record['ids']]
        risks=record['risks']
        if len(fold_ids)!=len(risks) or len(set(fold_ids))!=len(fold_ids):
            raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
        validate_native_risk(risks)
        for identity,risk in zip(fold_ids,risks):
            if identity in result:
                raise ValueError('INNER_OOF_OVERLAP')
            result[identity]=risk
    if set(result)!=set(ids):
        raise ValueError('INNER_OOF_COVERAGE_FAILURE')
    return [result[identity] for identity in ids]

def context_fit_plan(context):
    """Enumerate the frozen charged fits; selected refits depend on selection."""
    if context not in context_labels():
        raise ValueError('CONTEXT_DRIFT')
    items=[]
    def add(kind,model,candidate,fold):
        items.append({'context':context,'kind':kind,'model':model,'candidate':candidate,'fold':fold})
    for fold in range(1,6): add('survival','M0','fixed',fold)
    for model,count in [('M2_EN',15),('M1_RSF',8),('M2_RSF',8),('M1_GBST',12),('M2_GBST',12)]:
        for candidate in range(count):
            for fold in range(1,6): add('survival',model,candidate,fold)
    for model in ('M0','M2_EN','M1_RSF','M2_RSF','M1_GBST','M2_GBST'):
        for horizon in (90,180): add('recalibration',model,'selected',horizon)
        add('survival',model,'selected' if model!='M0' else 'fixed','refit')
    if sum(x['kind']=='survival' for x in items)!=286 or len(items)!=298:
        raise AssertionError('FIT_PLAN_DRIFT')
    return items


def run_m0_inner_context(*, ledger, context, ids, core_raw, times, events, project_root):
    """Fit five M0 inner-training partitions, preserving their OOF risks and coefficients."""
    from .a5_common import generate_inner_folds
    from .a5_m0_cox import predict_native_risk
    from .development_m0_cox import fit_m0
    from .development_preprocessing import generate_c2_design, fit_preprocessor, transform_preprocessor, load_frozen_preprocessing_spec
    import numpy as np
    n=len(ids)
    if not n or not (len(core_raw)==len(times)==len(events)==n):
        raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
    if context=='full_development':
        fold_context='A5|2026102405|full_development'
    else:
        label=context.split('|')
        if context not in context_labels() or len(label)!=2:
            raise ValueError('CONTEXT_DRIFT')
        fold_context={'outer_repeat':int(label[0].split('=')[1]),'outer_fold':int(label[1].split('=')[1])}
    assignment=generate_inner_folds(ids,events,fold_context,{'master_seed':2026102405})
    metadata=load_frozen_preprocessing_spec(project_root)
    output=[]
    for fold in range(1,6):
        train=[i for i in range(n) if assignment[i]!=fold]
        valid=[i for i in range(n) if assignment[i]==fold]
        if not train or not valid: raise ValueError('EMPTY_INNER_FOLD')
        def fit_partition():
            core, names, _ = generate_c2_design(core_raw,train)
            rows=[dict(zip(names,row)) for row in core]
            prep=fit_preprocessor(rows,None,train,{**metadata,'partition_role':'inner_training','model_level':'M1'},'EN')
            xtrain=transform_preprocessor(prep,rows,None,train)
            xvalid=transform_preprocessor(prep,rows,None,valid)
            model=fit_m0(xtrain,[times[i] for i in train],[events[i] for i in train],{'project_root':str(project_root)})
            risk=predict_native_risk(model,xvalid)
            beta=np.asarray(model['backend'].params_,dtype=float).tolist()
            if len(beta)!=20 or not np.isfinite(beta).all(): raise ValueError('M0_NUMERICAL_HOLD')
            return {'fold':fold,'candidate_id':'fixed','ids':[list(ids[i]) for i in valid],
                    'risks':risk,'core_beta':beta,'acceptance_diagnostics':model['acceptance_diagnostics']}
        output.append(ledger.execute(f'{context}|M0|fixed|inner={fold}',fit_partition))
    assemble_selected_oof(ids,'fixed',output)
    return output


def run_tree_inner_context(*, ledger, context, model_id, ids, core_raw, extension, times, events, project_root, partition_cache=None):
    """Tune a frozen RSF or GBST candidate grid on the five inner folds."""
    from .a5_common import generate_inner_folds, derive_model_seed
    from .a5_selection import evaluate_candidate_grid, candidate_for_fold
    from .a5_m0_cox import predict_native_risk
    from .development_preprocessing import generate_c2_design, fit_preprocessor, transform_preprocessor, load_frozen_preprocessing_spec
    from .development_rsf import fit_rsf, rsf_grid
    from .development_gbst import fit_gbst, gbst_grid
    if model_id not in ('M1_RSF','M2_RSF','M1_GBST','M2_GBST'):
        raise ValueError('MODEL_ID_DRIFT')
    n=len(ids)
    if not n or not (len(core_raw)==len(times)==len(events)==n):
        raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
    level,family=model_id.split('_')
    if (level=='M1' and extension is not None) or (level=='M2' and (extension is None or len(extension)!=n)):
        raise ValueError('EXTENSION_SCOPE_DRIFT')
    if context=='full_development':
        fold_context='A5|2026102405|full_development'
    else:
        if context not in context_labels(): raise ValueError('CONTEXT_DRIFT')
        parts=context.split('|')
        fold_context={'outer_repeat':int(parts[0].split('=')[1]),'outer_fold':int(parts[1].split('=')[1])}
    assignment=generate_inner_folds(ids,events,fold_context,{'master_seed':2026102405})
    metadata=load_frozen_preprocessing_spec(project_root)
    cache_key=(context,level,'TREE')
    partitions=None if partition_cache is None else partition_cache.get(cache_key)
    if partitions is None:
        partitions=[]
        for fold in range(1,6):
            train=[i for i in range(n) if assignment[i]!=fold]
            valid=[i for i in range(n) if assignment[i]==fold]
            if not train or not valid: raise ValueError('EMPTY_INNER_FOLD')
            core,names,_=generate_c2_design(core_raw,train)
            rows=[dict(zip(names,row)) for row in core]
            prep=fit_preprocessor(rows,extension,train,{**metadata,'partition_role':'inner_training','model_level':level},family)
            partitions.append({'ids':[tuple(ids[i]) for i in valid], 'outcomes':[events[i] for i in valid],
                               'train':train,'valid':valid,'xtrain':transform_preprocessor(prep,rows,extension,train),
                               'xvalid':transform_preprocessor(prep,rows,extension,valid)})
        if partition_cache is not None: partition_cache[cache_key]=partitions
    width=len(partitions[0]['xtrain'][0])
    candidates=rsf_grid(width) if family=='RSF' else gbst_grid()
    def fit_fold(candidate,fold,part):
        order=candidate['declared_order']
        def operation():
            fitted_candidate=candidate_for_fold(family,candidate,n_features=len(part['xtrain'][0]))
            seed=derive_model_seed(context,model_id,str(order),str(fold),{'master_seed':2026102405})
            spec={'project_root':str(project_root),'random_state':seed}
            fit=fit_rsf if family=='RSF' else fit_gbst
            model=fit(part['xtrain'],[times[i] for i in part['train']],[events[i] for i in part['train']],fitted_candidate,spec)
            risks=predict_native_risk(model,part['xvalid'])
            return {'fold':fold,'candidate_id':str(order),'ids':[list(x) for x in part['ids']],
                    'risks':risks,'seed':seed}
        result=ledger.execute(f'{context}|{model_id}|{order}|inner={fold}',operation)
        return {'ids':[tuple(x) for x in result['ids']], 'risk180':[x[1] for x in result['risks']], 'nonzero_count':0}
    result=evaluate_candidate_grid(family,candidates,partitions,fit_fold,n_features=width)
    from .development_accounting import record_grid_skips
    record_grid_skips(ledger,context,model_id,result)
    if result['status']!='PASS': return result
    selected=str(result['selected']['declared_order'])
    checkpoints=[]
    for fold in range(1,6):
        # Selected candidate already succeeded in all five folds; this is retrieval, never a fit.
        key=f'{context}|{model_id}|{selected}|inner={fold}'
        checkpoints.append(ledger.execute(key,lambda: (_ for _ in ()).throw(RuntimeError('MISSING_SELECTED_CHECKPOINT'))))
    result['selected_oof']=assemble_selected_oof(ids,selected,checkpoints)
    return result


def run_en_inner_context(*, ledger, context, ids, core_raw, extension, times, events, project_root, m0_inner):
    """Tune the frozen M2 EN grid conditional on the matched M0 inner fits."""
    from .a5_common import generate_inner_folds
    from .a5_en_cox import en_grid
    from .a5_selection import evaluate_candidate_grid, conditional_en_lambdas
    from .a5_m0_cox import predict_native_risk
    from .development_preprocessing import generate_c2_design, fit_preprocessor, transform_preprocessor, load_frozen_preprocessing_spec
    from .development_en_cox import fit_m2_en
    import numpy as np
    n=len(ids)
    if not n or not (len(core_raw)==len(extension)==len(times)==len(events)==n):
        raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
    if context=='full_development':
        fold_context='A5|2026102405|full_development'
    else:
        if context not in context_labels(): raise ValueError('CONTEXT_DRIFT')
        parts=context.split('|')
        fold_context={'outer_repeat':int(parts[0].split('=')[1]),'outer_fold':int(parts[1].split('=')[1])}
    assignment=generate_inner_folds(ids,events,fold_context,{'master_seed':2026102405})
    assemble_selected_oof(ids,'fixed',m0_inner)
    by_fold={row['fold']:row for row in m0_inner}
    metadata=load_frozen_preprocessing_spec(project_root)
    partitions=[]
    for fold in range(1,6):
        train=[i for i in range(n) if assignment[i]!=fold]
        valid=[i for i in range(n) if assignment[i]==fold]
        if not train or not valid or [tuple(x) for x in by_fold[fold]['ids']] != [tuple(ids[i]) for i in valid]:
            raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
        core,names,_=generate_c2_design(core_raw,train)
        rows=[dict(zip(names,row)) for row in core]
        prep=fit_preprocessor(rows,extension,train,{**metadata,'partition_role':'inner_training','model_level':'M2'},'EN')
        xtrain=transform_preprocessor(prep,rows,extension,train)
        xvalid=transform_preprocessor(prep,rows,extension,valid)
        lambdas=conditional_en_lambdas(xtrain,[times[i] for i in train],[events[i] for i in train],by_fold[fold]['core_beta'])
        partitions.append({'ids':[tuple(ids[i]) for i in valid], 'outcomes':[events[i] for i in valid],
                           'train':train,'xtrain':xtrain,'xvalid':xvalid,'lambdas':lambdas})
    def fit_fold(candidate,fold,part):
        order=candidate['declared_order']
        def operation():
            scientific=part['lambdas'][order]
            model=fit_m2_en(part['xtrain'],[times[i] for i in part['train']],[events[i] for i in part['train']],
                            [0]*20+[1]*(len(part['xtrain'][0])-20),scientific,{'project_root':str(project_root)})
            risks=predict_native_risk(model,part['xvalid'])
            coeff=np.asarray(model['backend'].params_,dtype=float)
            return {'fold':fold,'candidate_id':str(order),'ids':[list(x) for x in part['ids']],
                    'risks':risks,'nonzero_count':int(np.sum(np.abs(coeff[20:])>1e-8)),
                    'scientific_lambda':scientific['lambda'],'lambda_max':scientific['lambda_max']}
        result=ledger.execute(f'{context}|M2_EN|{order}|inner={fold}',operation)
        return {'ids':[tuple(x) for x in result['ids']], 'risk180':[x[1] for x in result['risks']],
                'nonzero_count':result['nonzero_count']}
    result=evaluate_candidate_grid('EN',en_grid(),partitions,fit_fold)
    from .development_accounting import record_grid_skips
    record_grid_skips(ledger,context,'M2_EN',result)
    if result['status']!='PASS': return result
    selected=str(result['selected']['declared_order'])
    checkpoints=[]
    for fold in range(1,6):
        key=f'{context}|M2_EN|{selected}|inner={fold}'
        checkpoints.append(ledger.execute(key,lambda: (_ for _ in ()).throw(RuntimeError('MISSING_SELECTED_CHECKPOINT'))))
    result['selected_oof']=assemble_selected_oof(ids,selected,checkpoints)
    return result


def fit_context_recalibrators(*, ledger, context, model_id, oof, outcome90, outcome180):
    """Fit exactly two support recalibrators using only reconstructed inner OOF risks."""
    from .a5_calibration import fit_logistic_recalibrator, validate_native_risk
    if model_id not in ('M0','M2_EN','M1_RSF','M2_RSF','M1_GBST','M2_GBST'):
        raise ValueError('MODEL_ID_DRIFT')
    if context not in context_labels(): raise ValueError('CONTEXT_DRIFT')
    if len(oof)!=len(outcome90) or len(oof)!=len(outcome180): raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
    validate_native_risk(oof)
    result={}
    for col,(horizon,outcomes) in enumerate(((90,outcome90),(180,outcome180))):
        def fit_one():
            return fit_logistic_recalibrator([r[col] for r in oof],outcomes,horizon,{'source_role':'inner_oof'})
        try:
            result[str(horizon)]=ledger.execute(f'{context}|{model_id}|recalibration={horizon}',fit_one)
        except (ValueError, RecordedFailure) as exc:
            if 'RECALIBRATION_STREAM_HOLD' not in str(exc): raise
            result[str(horizon)]={'status':'STREAM_HOLD_NATIVE_RETAINED','reason':str(exc)}
    return result



class _QuotaWriter:
    def __init__(self, handle, guard): self.handle=handle; self.guard=guard
    def write(self, data):
        if self.guard: self.guard.check_write(len(data))
        return self.handle.write(data)


def fit_context_refit(*, ledger, context, model_id, core_training, core_target, extension_training,
                      extension_target, times, events, project_root, selected=None, m0_core_beta=None, partition_cache=None):
    """Fit one frozen selected model on the entire context training partition."""
    import pickle
    import numpy as np
    from .a5_common import derive_model_seed
    from .a5_selection import candidate_for_fold, conditional_en_lambdas
    from .a5_m0_cox import predict_native_risk
    from .development_preprocessing import generate_c2_design, fit_preprocessor, transform_preprocessor, load_frozen_preprocessing_spec
    from .development_m0_cox import fit_m0
    from .development_en_cox import fit_m2_en
    from .development_rsf import fit_rsf
    from .development_gbst import fit_gbst
    if context not in context_labels() or model_id not in ('M0','M2_EN','M1_RSF','M2_RSF','M1_GBST','M2_GBST'):
        raise ValueError('MODEL_OR_CONTEXT_DRIFT')
    n=len(core_training); target_n=len(core_target)
    if not n or len(times)!=n or len(events)!=n: raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
    if context=='full_development' and target_n: raise ValueError('FINAL_CONTEXT_TARGET_FORBIDDEN')
    level='M2' if model_id.startswith('M2_') else 'M1'
    family='EN' if model_id in ('M0','M2_EN') else model_id.split('_')[1]
    if level=='M2':
        if extension_training is None or extension_target is None or len(extension_training)!=n or len(extension_target)!=target_n:
            raise ValueError('EXTENSION_SCOPE_DRIFT')
        ext=list(extension_training)+list(extension_target)
    else:
        if extension_training is not None or extension_target is not None: raise ValueError('EXTENSION_SCOPE_DRIFT')
        ext=None
    train=list(range(n)); target=list(range(n,n+target_n))
    task=f'{context}|{model_id}|refit'
    def operation():
        cache_key=(context,level,'FINAL', 'EN' if family=='EN' else 'TREE')
        cached=None if partition_cache is None else partition_cache.get(cache_key)
        if cached is None:
            core,names,c2_parameters=generate_c2_design(list(core_training)+list(core_target),train)
            rows=[dict(zip(names,row)) for row in core]
            metadata=load_frozen_preprocessing_spec(project_root)
            role='final_full_development_refit' if context=='full_development' else 'outer_training_refit'
            prep=fit_preprocessor(rows,ext,train,{**metadata,'partition_role':role,'model_level':level},family)
            xtrain=transform_preprocessor(prep,rows,ext,train)
            xtarget=transform_preprocessor(prep,rows,ext,target)
            if partition_cache is not None: partition_cache[cache_key]=(prep,xtrain,xtarget,c2_parameters)
        else:
            prep,xtrain,xtarget,c2_parameters=cached
            prep=dict(prep,model_family=family)
        seed=derive_model_seed(context,model_id,'fixed' if model_id=='M0' else str(selected['declared_order']),'final',{'master_seed':2026102405})
        spec={'project_root':str(project_root),'random_state':seed}
        scientific=None
        if model_id=='M0': model=fit_m0(xtrain,times,events,spec)
        elif model_id=='M2_EN':
            if m0_core_beta is None: raise ValueError('MISSING_PROVENANCE')
            scientific=conditional_en_lambdas(xtrain,times,events,m0_core_beta)[selected['declared_order']]
            model=fit_m2_en(xtrain,times,events,[0]*20+[1]*(len(xtrain[0])-20),scientific,spec)
        else:
            candidate=candidate_for_fold(family,selected,n_features=len(xtrain[0]))
            model=(fit_rsf if family=='RSF' else fit_gbst)(xtrain,times,events,candidate,spec)
        native=predict_native_risk(model,xtarget) if target_n else []
        result={'model_id':model_id,'native_risks':native,'model_seed_64':seed,
                'selected':selected,'scientific':scientific, 'core_design_parameters':c2_parameters,
                'preprocessor':prep}
        if model_id=='M0': result['core_beta']=np.asarray(model['backend'].params_,dtype=float).tolist()
        if context=='full_development':
            artifact_dir=ledger.path/'final_models'; artifact_dir.mkdir(exist_ok=True)
            target_path=artifact_dir/(model_id+'.pickle'); tmp=target_path.with_suffix('.tmp')
            with tmp.open('xb') as f:
                writer=_QuotaWriter(f,ledger.guard)
                pickle.dump({'model':model,'preprocessor':prep,'core_design_parameters':c2_parameters,
                             'selected':selected,'scientific':scientific,'model_seed_64':seed},writer,pickle.HIGHEST_PROTOCOL)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp,target_path)
            result['model_file']=str(target_path.relative_to(ledger.path))
            result['model_sha256']=_sha256_file(target_path)
        return result
    result=ledger.execute(task,operation)
    if context=='full_development':
        p=ledger.path/result['model_file']
        if not p.is_file() or _sha256_file(p)!=result['model_sha256']:
            raise ValueError('CHECKPOINT_TAMPER')
    return result



def save_final_calibration_sidecar(ledger, model, calibrators):
    """Bind trained final calibration and model without performing another fit."""
    if 'model_file' not in model: raise ValueError('MISSING_PROVENANCE')
    parent=ledger.path/'final_models'
    path=parent/(model['model_id']+'.calibration.json')
    data=_encoded({'model_id':model['model_id'], 'model_sha256':model['model_sha256'],
                   'run_fingerprint':ledger.fingerprint, 'recalibrators':calibrators})
    if path.exists():
        if path.read_bytes()!=data: raise ValueError('CHECKPOINT_TAMPER')
    else:
        tmp=path.with_suffix('.tmp')
        if ledger.guard: ledger.guard.check_write(len(data))
        with tmp.open('xb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    return {'path':str(path.relative_to(ledger.path)),'sha256':hashlib.sha256(data).hexdigest()}


def run_one_context(*, ledger, context, ids, target_ids, core_training, core_target,
                    extension_training, extension_target, times, events, outcome90, project_root):
    """Execute one full nested context; caller must enforce resources and run authorization."""
    from .a5_calibration import apply_recalibrator, report_recalibrated_monotonicity
    if context not in context_labels(): raise ValueError('CONTEXT_DRIFT')
    if len(set(map(tuple,ids)))!=len(ids) or len(set(map(tuple,target_ids)))!=len(target_ids):
        raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
    if set(map(tuple,ids)) & set(map(tuple,target_ids)):
        raise ValueError('LEAKAGE')
    if len(core_training)!=len(ids) or len(core_target)!=len(target_ids) or len(outcome90)!=len(ids):
        raise ValueError('PATIENT_FOLD_COLUMN_CHANGE')
    if (context=='full_development') != (len(target_ids)==0):
        raise ValueError('CONTEXT_TARGET_DRIFT')
    partition_cache={}
    m0=run_m0_inner_context(ledger=ledger,context=context,ids=ids,core_raw=core_training,
                            times=times,events=events,project_root=project_root)
    m0_oof=assemble_selected_oof(ids,'fixed',m0)
    calibration=fit_context_recalibrators(ledger=ledger,context=context,model_id='M0',oof=m0_oof,
                                           outcome90=outcome90,outcome180=events)
    m0_final=fit_context_refit(ledger=ledger,context=context,model_id='M0',core_training=core_training,
                               core_target=core_target,extension_training=None,extension_target=None,
                               times=times,events=events,project_root=project_root,partition_cache=partition_cache)
    def prediction_result(model, calibrators, inner=None):
        if context=='full_development':
            model['calibration_sidecar']=save_final_calibration_sidecar(ledger,model,calibrators)
        native=model['native_risks']
        if native:
            columns=[]
            for col,horizon in enumerate(('90','180')):
                calibration=calibrators[horizon]
                columns.append([None]*len(native) if calibration.get('status')=='STREAM_HOLD_NATIVE_RETAINED'
                               else apply_recalibrator(calibration,[p[col] for p in native]))
            recal=[list(p) for p in zip(*columns)]
            monotonic=({'status':'NOT_ESTIMABLE_STREAM_HOLD'} if any(None in row for row in recal)
                       else report_recalibrated_monotonicity(recal))
        else:
            recal=[]; monotonic={'status':'NO_TARGET_ROWS'}
        return {'refit':model,'recalibrators':calibrators,'recalibrated_risks':recal,
                'recalibrated_monotonicity':monotonic,'inner':inner}
    output={'context':context,'target_ids':[list(x) for x in target_ids],
            'models':{'M0':prediction_result(m0_final,calibration)}}
    output['models']['M1_EN']={'alias_of':'M0','additional_fit_count':0}
    for model_id in ('M2_EN','M1_RSF','M2_RSF','M1_GBST','M2_GBST'):
        if model_id=='M2_EN':
            tuned=run_en_inner_context(ledger=ledger,context=context,ids=ids,core_raw=core_training,
                                        extension=extension_training,times=times,events=events,
                                        project_root=project_root,m0_inner=m0)
        else:
            tuned=run_tree_inner_context(ledger=ledger,context=context,model_id=model_id,ids=ids,
                                          core_raw=core_training,extension=extension_training if model_id.startswith('M2') else None,
                                          times=times,events=events,project_root=project_root,partition_cache=partition_cache)
        if tuned['status']!='PASS':
            pause_for_failure(ledger,context,model_id,'tuning',tuned.get('failure_code') or tuned['status'],
                              force_project=tuned['status']=='PROJECT_HOLD')
        calibrators=fit_context_recalibrators(ledger=ledger,context=context,model_id=model_id,oof=tuned['selected_oof'],
                                               outcome90=outcome90,outcome180=events)
        try:
            final=fit_context_refit(ledger=ledger,context=context,model_id=model_id,core_training=core_training,
                                    core_target=core_target,extension_training=extension_training if model_id.startswith('M2') else None,
                                    extension_target=extension_target if model_id.startswith('M2') else None,
                                    times=times,events=events,project_root=project_root,selected=tuned['selected'],
                                    m0_core_beta=m0_final.get('core_beta'),partition_cache=partition_cache)
        except Exception as exc:
            pause_for_failure(ledger,context,model_id,'selected_refit',str(exc),
                              force_project=isinstance(exc,(OSError,PermissionError,MemoryError)))
        output['models'][model_id]=prediction_result(final,calibrators,tuned)
    return output


def compute_run_fingerprint(project_root):
    """Bind current code, signed authority, data, interpreter and thread configuration."""
    from .development_preflight import INPUTS, preflight
    from .a5_common import validate_installed_software_lock
    root=Path(project_root).resolve(strict=True)
    preflight(root)
    software=validate_installed_software_lock(root)
    required=['audit/ML_FIT_AUTHORIZATION.json','audit/a5/ML_FIT_RESEARCHER_SIGNATURE.json',
              'audit/a5/ML_FIT_STATE_TRANSITION.json','manifests/PROJECT_STATE.json',
              'audit/a5/A5_HASH_MANIFEST.csv','docs/sdd/task-06-brief.md',*INPUTS]
    required.extend(p.relative_to(root).as_posix() for p in sorted((root/'src/project_b').glob('development_*.py')))
    hashes={name:_sha256_file(root/name) for name in required}
    return hashlib.sha256(_encoded({'files':hashes,'software':software})).hexdigest()


def iter_development_contexts(project_root):
    """Read only the allowed frozen development tables and yield authorized partitions."""
    import csv
    from .development_preflight import preflight
    from .a5_common import make_surv180
    root=Path(project_root).resolve(strict=True)
    preflight(root)
    def read(name):
        with (root/'data/a4'/name).open(encoding='utf-8',newline='') as f:
            return list(csv.DictReader(f))
    index=read('DEVELOPMENT_INDEX_OUTCOME.csv')
    core=read('DEVELOPMENT_C2_RAW.csv')
    ext=read('DEVELOPMENT_EXTENDED_157.csv')
    folds=read('OUTER_FOLD_ASSIGNMENTS.csv')
    ids=[(int(row['subject_id']),int(row['hadm_id'])) for row in index]
    outcomes=[]
    for row in index:
        converted=dict(row)
        for key in ('death_90d','death_180d','admin_180_eligible'):
            converted[key]=int(row[key])
        converted['death_days_after_t0']=float(row['death_days_after_t0']) if row['death_days_after_t0'] else None
        outcomes.append(converted)
    times, events=make_surv180(outcomes)
    outcome90=[row['death_90d'] for row in outcomes]
    def core_convert(row):
        return {key:(value if key in ('gender','gcsm_category') else float(value) if value else None)
                for key,value in row.items() if key not in ('subject_id','hadm_id')}
    def ext_convert(row):
        return {key:float(value) if value else None for key,value in row.items() if key not in ('subject_id','hadm_id')}
    core=[core_convert(row) for row in core]
    ext=[ext_convert(row) for row in ext]
    for repeat in range(1,11):
        for fold in range(1,6):
            target=[i for i,row in enumerate(folds) if int(row[f'repeat_{repeat}_fold'])==fold]
            training=[i for i,row in enumerate(folds) if int(row[f'repeat_{repeat}_fold'])!=fold]
            if len(training) not in (2271,2272) or sum(events[i] for i in training)!=348:
                raise ValueError('OUTER_PARTITION_DRIFT')
            yield {'context':f'outer_repeat={repeat}|outer_fold={fold}',
                   'ids':[ids[i] for i in training], 'target_ids':[ids[i] for i in target],
                   'core_training':[core[i] for i in training], 'core_target':[core[i] for i in target],
                   'extension_training':[ext[i] for i in training], 'extension_target':[ext[i] for i in target],
                   'times':[times[i] for i in training], 'events':[events[i] for i in training],
                   'outcome90':[outcome90[i] for i in training]}
    yield {'context':'full_development','ids':ids,'target_ids':[], 'core_training':core,'core_target':[],
           'extension_training':ext,'extension_target':[], 'times':times,'events':events,'outcome90':outcome90}


def run_development(project_root, run_id):
    """Run all contexts and frozen postfit evaluation after bound independent review."""
    import re
    from .development_resources import ResourceGuard
    root=Path(project_root).resolve(strict=True)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',run_id): raise ValueError('INVALID_RUN_ID')
    fingerprint=compute_run_fingerprint(root)
    review_path=root/'audit/development/TASK06_INDEPENDENT_REVIEW.json'
    if not review_path.is_file(): raise PermissionError('INDEPENDENT_REVIEW_REQUIRED')
    review=json.loads(review_path.read_text(encoding='utf-8'))
    if review.get('status')!='PASS' or review.get('run_fingerprint')!=fingerprint or review.get('critical_findings')!=0 or review.get('important_findings')!=0:
        raise PermissionError('INDEPENDENT_REVIEW_NOT_BOUND_OR_APPROVED')
    from .development_parallel import coordinate
    run_dir=root/'audit/development/runs'/run_id
    return coordinate(root,run_dir,fingerprint,iter_development_contexts(root))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('project_root'); parser.add_argument('run_id')
    args=parser.parse_args()
    print(json.dumps(run_development(args.project_root,args.run_id),sort_keys=True))
