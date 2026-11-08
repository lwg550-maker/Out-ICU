"""Compact-only training-local tree preprocessing; no all-core entrypoint.

Ranker partitions use partition_role='inner_training'. Canonical rows contain
only raw inputs; selected_group_ids may be any ordered subset of the registry.
Returned state is JSON serializable; transform returns numpy float64 (n,p).
"""
from __future__ import annotations

import copy
import math
import statistics
from collections import defaultdict

import numpy as np

from .compact_inputs import ordered_groups, group_columns, _validate_group
from .development_preprocessing import (_quantile, _rcs_one, _near_zero_variance,
                                        _equivalent, validate_fit_partition,
                                        C2_BINARY_COLUMNS)


def _core_values(row,g,state):
    kind=g['kind'];raw=g['raw_columns'];v=[row[c] for c in raw]
    if kind=='age':return [float(v[0]),_rcs_one(float(v[0]),state['age_knots'])]
    if kind=='sex':return [float(v[0]=='F')]
    if kind=='log1p':return [math.log1p(float(v[0]))]
    if kind=='gcsm':return [float(v[0]==x) for x in ('4-5','1-3','unassessable')]
    if kind=='wpr':return [state['wpr_median'] if v[0] is None else float(v[0]),float(v[1])]
    return [float(x) for x in v]


def fit_preprocessor(rows, selected_group_ids, spec, *, partition_role):
    validate_fit_partition(partition_role)
    if not len(rows):raise ValueError('training rows required')
    groups=ordered_groups(selected_group_ids,spec)
    if not groups:raise ValueError('at least one selected group required')
    for row in rows:
        for g in groups:_validate_group(row,g)
    state={'schema_version':'compact-preprocessing-1.0','partition_role':partition_role,
           'selected_group_ids':[g['id'] for g in groups],
           'groups':copy.deepcopy(groups),'training_n':len(rows),
           'target_rows_used_for_fit':0,'age_knots':None,'wpr_median':None,
           'coverage':{},'inactive_columns':{},'winsor_limits':{},'medians':{},
           'representatives':{},'correlation_components':[],'scaling':False}
    if any(g['kind']=='age' for g in groups):
        state['age_knots']=[_quantile([r['age_at_admit'] for r in rows],p) for p in (.1,.5,.9)]
    if any(g['kind']=='wpr' for g in groups):
        observed=[float(r['log2_wpr']) for r in rows if r['log2_wpr'] is not None]
        if not observed:raise ValueError('WPR training median is not estimable')
        state['wpr_median']=float(statistics.median(observed))
    core={}
    for g in groups:
        if g['source']=='core':
            vectors=[_core_values(r,g,state) for r in rows]
            core.update({c:[v[i] for v in vectors] for i,c in enumerate(g['model_columns'])})
    extension=[c for g in groups if g['source']=='extension' for c in g['model_columns']]
    binary={c for g in groups if g['source']=='extension' for c in g['binary_columns']}
    observed={c:[r[c] for r in rows] for c in extension}
    threshold=max(50,math.ceil(.05*len(rows)))
    survivors=[]
    for c in extension:
        values=[v for v in observed[c] if v is not None]
        state['coverage'][c]=len(values)
        if c not in binary and len(values)<threshold:state['inactive_columns'][c]='LOW_COVERAGE'
        elif _near_zero_variance(values):state['inactive_columns'][c]='ZERO_OR_NEAR_ZERO_VARIANCE'
        else:
            survivors.append(c)
            if c not in binary:state['winsor_limits'][c]=[_quantile(values,p) for p in (.01,.99)]
    graph=list(core)+survivors if survivors else []
    parent={c:c for c in graph}
    def find(c):
        while parent[c]!=c:
            parent[c]=parent[parent[c]];c=parent[c]
        return c
    all_values={**core,**observed}
    for i,left in enumerate(graph):
        for right in graph[i+1:]:
            a,b=all_values[left],all_values[right]
            pairs=[(float(x),float(y)) for x,y in zip(a,b) if x is not None and y is not None]
            if len(pairs)<threshold:continue
            equivalent=_equivalent(a,b)
            if not equivalent:
                ac=[x for x,y in pairs];bc=[y for x,y in pairs]
                if left in state['winsor_limits']:
                    lo,hi=state['winsor_limits'][left];ac=[min(max(v,lo),hi) for v in ac]
                if right in state['winsor_limits']:
                    lo,hi=state['winsor_limits'][right];bc=[min(max(v,lo),hi) for v in bc]
                # Constant protected core columns are legal for this tree branch.
                if len(set(ac))<2 or len(set(bc))<2:continue
                from scipy.stats import pearsonr, spearmanr
                coefficient=float((pearsonr if left in binary|C2_BINARY_COLUMNS and right in binary|C2_BINARY_COLUMNS else spearmanr)(ac,bc).statistic)
                equivalent=math.isfinite(coefficient) and abs(coefficient)>=.95
            if equivalent:parent[find(right)]=find(left)
    components=defaultdict(list)
    for c in graph:components[find(c)].append(c)
    retained=[]
    for members in components.values():
        candidates=[c for c in members if c in extension]
        if not candidates:continue
        representative=min(candidates,key=lambda c:(-state['coverage'][c],extension.index(c),c))
        retained.append(representative)
        if len(members)>1:
            state['correlation_components'].append(members)
            state['representatives']['|'.join(candidates)]=representative
            for c in candidates:
                if c!=representative:state['inactive_columns'][c]=f'REDUNDANT_WITH:{representative}'
    final_ext=[]
    for c in extension:
        if c not in retained:continue
        median=float(statistics.median(v for v in observed[c] if v is not None))
        state['medians'][c]=median
        values=[median if v is None else float(v) for v in observed[c]]
        if c in state['winsor_limits']:
            lo,hi=state['winsor_limits'][c];values=[min(max(v,lo),hi) for v in values]
        if len(set(values))<2:state['inactive_columns'][c]='POST_IMPUTATION_SD0'
        else:final_ext.append(c)
    state['core_columns']=list(core);state['extension_columns']=final_ext
    state['final_columns']=list(core)+final_ext
    state['group_columns']=group_columns(state['selected_group_ids'],spec,state)
    state['inactive_groups']={g['id']:{c:state['inactive_columns'][c] for c in g['model_columns']}
                              for g in groups if not state['group_columns'][g['id']]}
    state['effective_selected_group_ids']=[g['id'] for g in groups if state['group_columns'][g['id']]]
    return state


def transform_preprocessor(preprocessor, rows):
    state=preprocessor;groups=state['groups'];result=[]
    for row in rows:
        values={}
        for g in groups:
            if not state['group_columns'][g['id']]:continue
            _validate_group(row,g)
            if g['source']=='core':values.update(zip(g['model_columns'],_core_values(row,g,state)))
        for c in state['extension_columns']:
            v=row[c];n=state['medians'][c] if v is None else float(v)
            if c in state['winsor_limits']:
                lo,hi=state['winsor_limits'][c];n=min(max(n,lo),hi)
            values[c]=n
        result.append([values[c] for c in state['final_columns']])
    matrix=np.asarray(result,dtype=float).reshape(len(rows),len(state['final_columns']))
    if not np.isfinite(matrix).all():raise ValueError('nonfinite compact design')
    return matrix
