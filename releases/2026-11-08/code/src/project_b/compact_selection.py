"""Training-local coherent grouped permutation ranking for all manual budgets."""
import hashlib
import numpy as np
from .a5_common import derive_model_seed
from .a5_m0_cox import predict_native_risk
from .compact_inputs import selected_manual_fields
from .compact_preprocessing import fit_preprocessor,transform_preprocessor

MASTER=2026102405
BUDGETS=(6,10,14)
RANKER=dict(max_depth=2,learning_rate=.03,n_estimators=250,min_samples_leaf=64,subsample=.8,max_features=1.0,loss='coxph',early_stopping=False)


def seed(context,label):
    return derive_model_seed(context,'COMPACT_GBST',label,'compact',{'master_seed':MASTER})


def rank_folds(ids,events,partition):
    if len(ids)!=len(events) or len(set(map(tuple,ids)))!=len(ids) or any(e not in (0,1) for e in events):raise ValueError('RANK_ID_OUTCOME_DRIFT')
    assignments={}
    for event in (0,1):
        members=[(hashlib.sha256(f'COMPACT_RANK|{MASTER}|{partition}|{event}|{int(i[0])}|{int(i[1])}'.encode()).hexdigest(),tuple(i)) for i,e in zip(ids,events) if e==event]
        for j,(_,identity) in enumerate(sorted(members)):assignments[identity]=j%2
    return [assignments[tuple(i)] for i in ids]


def permute_group(matrix,columns,permutation_seed):
    out=matrix.copy();order=np.random.default_rng(permutation_seed).permutation(len(matrix))
    out[:,columns]=matrix[order][:,columns]
    return out


def aggregate_importance(evidence,spec,budgets=BUDGETS):
    order=[g['id'] for g in spec['groups']];importance={}
    for group in order:
        rows=[r for r in evidence if r['group']==group]
        if rows:importance[group]=sum(r['heldout_n']*r['delta'] for r in rows)/sum(r['heldout_n'] for r in rows)
    ranked=sorted(importance,key=lambda g:(-importance[g],order.index(g)))
    selected={}
    # Union manual dependencies, not transformed-column counts. No sign filter.
    for budget in budgets:
        picked=[]
        for group in ranked:
            if len(selected_manual_fields(picked+[group],spec))<=budget:picked.append(group)
        selected[str(budget)]=picked
    return dict(importance=importance,ordered_groups=ranked,inactive_groups=[g for g in order if g not in importance],selected=selected,evidence=evidence)


def rank_partition(*,rows,ids,times,events,spec,partition,store,fit):
    """fit(matrix,times,events,candidate,seed) is explicit real/synthetic adapter.

    Save each fitted ranker BEFORE permutations. Restarting a permutation stage
    loads that exact model and training-only preprocessor, never refits it.
    """
    from .compact_storage import encoded
    binding=hashlib.sha256(encoded(dict(rows=rows,ids=ids,times=times,events=events,spec=spec))).hexdigest()
    prior=store.execute(partition+'|rank_inputs',lambda:binding)
    if prior!=binding:raise ValueError('RANK_DATA_ORDER_OUTCOME_DRIFT')
    assignment=rank_folds(ids,events,partition);groups=[g['id'] for g in spec['groups']]
    evidence=[];active={};inactive={}
    for fold in (0,1):
        train=[i for i,a in enumerate(assignment) if a!=fold];valid=[i for i,a in enumerate(assignment) if a==fold]
        if not train or not valid:raise ValueError('EMPTY_RANK_FOLD')
        key=f'{partition}|rank={fold}|{binding}'
        def train_ranker():
            pp=fit_preprocessor([rows[i] for i in train],groups,spec,partition_role='inner_training')
            x=transform_preprocessor(pp,[rows[i] for i in train])
            model=fit(x,[times[i] for i in train],[events[i] for i in train],RANKER,seed(partition,f'rank={fold}'))
            return store.save_model(key,dict(model=model,preprocessor=pp,train_ids=[ids[i] for i in train]))
        ref=store.execute(key,train_ranker,physical_fit=True)
        package=store.load_model(ref);pp=package['preprocessor'];model=package['model']
        xvalid=transform_preprocessor(pp,[rows[i] for i in valid]);truth=np.array([events[i] for i in valid])
        baseline=np.mean((np.array(predict_native_risk(model,xvalid))[:,1]-truth)**2)
        active[str(fold)]={g:pp['group_columns'][g] for g in groups};inactive[str(fold)]=pp['inactive_groups']
        for group in groups:
            cols=[pp['final_columns'].index(c) for c in pp['group_columns'][group]]
            if not cols:continue
            for repeat in range(5):
                pseed=seed(partition,f'rank={fold}|group={group}|repeat={repeat}')
                def perm():
                    risk=np.array(predict_native_risk(model,permute_group(xvalid,cols,pseed)))[:,1]
                    return dict(group=group,fold=fold,repeat=repeat,heldout_n=len(valid),baseline=float(baseline),permuted_loss=float(np.mean((risk-truth)**2)),delta=float(np.mean((risk-truth)**2)-baseline),seed=pseed)
                evidence.append(store.execute(f'{key}|group={group}|repeat={repeat}',perm))
    return dict(aggregate_importance(evidence,spec),assignment=assignment,active_by_fold=active,inactive_by_fold=inactive,training_binding=binding)
