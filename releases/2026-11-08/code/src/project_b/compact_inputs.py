"""Outcome-blind manual registry. Canonical rows are flat raw-column mappings.

Plain numeric payloads use the spec unit. A {value, unit} wrapper is also
accepted, with exact unit matching. Only selected keys are dereferenced.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from pathlib import Path


def ordered_groups(group_ids, spec):
    ids = list(group_ids)
    known = {g['id'] for g in spec['groups']}
    if len(ids) != len(set(ids)) or not set(ids) <= known:
        raise ValueError('unknown or duplicate compact group')
    return [g for g in spec['groups'] if g['id'] in ids]


def selected_manual_fields(group_ids, spec):
    """Ordered unique actual page fields; len(result) is budget cost."""
    return list(dict.fromkeys(f for g in ordered_groups(group_ids, spec) for f in g['manual_fields']))


def group_columns(group_ids, spec, preprocessor=None):
    """Dictionary-order mapping ID -> model column names (not indices).

    With preprocessor, filter to stored active columns; inactive groups map [].
    """
    active = None if preprocessor is None else set(preprocessor['final_columns'])
    return {g['id']:[c for c in g['model_columns'] if active is None or c in active]
            for g in ordered_groups(group_ids, spec)}


def load_input_spec(root):
    root = Path(root)
    spec = json.loads((root/'config/COMPACT_MANUAL_INPUT_SPEC.json').read_text(encoding='utf-8'))
    if spec['schema_version'] != 'compact-manual-1.0' or spec['budgets'] != [6,10,14]:
        raise ValueError('compact schema drift')
    for path, key in [('config/A4_FIELD_DICTIONARY.csv','dictionary_sha256'),('config/PREPROCESSING_SPEC.json','preprocessing_sha256'),('config/A2_EXPECTED_SOURCE_CONTRACTS.csv','source_contracts_sha256'),('inputs/frozen/07_P002_DESIGN_DICTIONARY.csv','core_dictionary_sha256'),('inputs/frozen/08_P004_MATRIX_SPEC.csv','core_matrix_sha256')]:
        if hashlib.sha256((root/path).read_bytes()).hexdigest().upper() != spec[key]:
            raise ValueError('frozen metadata drift')
    with (root/'config/A4_FIELD_DICTIONARY.csv').open(encoding='utf-8',newline='') as f:
        dictionary = list(csv.DictReader(f))
    with (root/'inputs/frozen/07_P002_DESIGN_DICTIONARY.csv').open(encoding='utf-8',newline='') as f:
        core_dictionary = {r['field']:r for r in csv.DictReader(f)}
    with (root/'inputs/frozen/08_P004_MATRIX_SPEC.csv').open(encoding='utf-8',newline='') as f:
        core_matrix = list(csv.DictReader(f))
    groups=spec['groups']; ids=[g['id'] for g in groups]
    from .development_preprocessing import C2_COLUMNS
    if [c for g in groups if g['source']=='core' for c in g['model_columns']] != C2_COLUMNS:
        raise ValueError('compact core design schema drift')
    for g in groups:
        if g['source'] not in ('core','extension') or g['manual_field_count']!=len(g['manual_fields']) or len(g['manual_fields'])!=len(set(g['manual_fields'])):
            raise ValueError('compact source/manual cost schema drift')
        if g['source']=='extension' and g['model_columns']!=g['raw_columns']:
            raise ValueError('compact extension design schema drift')
        if g['source']=='core':
            for m in g['metadata']:
                if m.get('core_dictionary_path')!='inputs/frozen/07_P002_DESIGN_DICTIONARY.csv' or m.get('core_matrix_path')!='inputs/frozen/08_P004_MATRIX_SPEC.csv' or m.get('core_dictionary_row')!=core_dictionary.get(m.get('core_dictionary_field')) or m.get('core_matrix_rows')!=[r for r in core_matrix if r['source_field']==m['column']]:
                    raise ValueError('compact core source metadata drift')
    if len(set(ids)) != len(ids) or [g['order'] for g in groups] != list(range(len(groups))):
        raise ValueError('compact group order drift')
    columns=[c for g in groups for c in g['model_columns']]
    if len(columns)!=len(set(columns)): raise ValueError('duplicate design column')
    for source,table in [('core','DEVELOPMENT_C2_RAW'),('extension','DEVELOPMENT_EXTENDED_157')]:
        expected=[r['column_name'] for r in dictionary if r['table_name']==table and r['role']!='identity']
        included=[c for g in groups if g['source']==source for c in g['raw_columns']]
        if len(included)!=len(set(included)) or included!=[c for c in expected if c in included]:
            raise ValueError('compact raw dictionary order/schema drift')
        excluded=[] if source=='core' else [r['column'] for r in spec['excluded_columns']]
        if set(included)|set(excluded)!=set(expected) or set(included)&set(excluded):
            raise ValueError('incomplete compact excluded ledger')
    return spec


def _required(mapping,key):
    try: return mapping[key]
    except KeyError: raise ValueError(f'missing required input key: {key}') from None


def _number(value, name, unit=None, nullable=False):
    if isinstance(value,dict):
        if unit is None or value.get('unit')!=unit: raise ValueError(f'{name}: invalid unit')
        value=_required(value,'value')
    if value is None and nullable:return None
    if value is None or isinstance(value,(str,bool)):
        raise ValueError(f'{name}: expected finite numeric value')
    try: result=float(value)
    except (TypeError,ValueError,OverflowError):raise ValueError(f'{name}: invalid numeric value') from None
    if not math.isfinite(result):raise ValueError(f'{name}: nonfinite numeric value')
    return result


def _validate_group(row,g):
    """Validate selected canonical values without opening any other group."""
    raw=g['raw_columns']; kind=g['kind']
    values=[_required(row,c) for c in raw]
    if kind=='sex':
        if values[0] not in ('M','F'):raise ValueError('invalid gender')
        return
    if kind=='gcsm':
        if values[0] not in ('6','4-5','1-3','unassessable'):raise ValueError('invalid GCS-M category')
        return
    for i,(c,v) in enumerate(zip(raw,values)):
        nullable=(kind=='wpr' and i==0) or (g['source']=='extension' and c not in g['binary_columns'])
        n=_number(v,c,nullable=nullable)
        if n is None:continue
        binary=kind in ('binary','rrt') or c in g['binary_columns'] or c in ('pupil_unassessable','wpr_unavailable')
        if binary and n not in (0,1):raise ValueError(f'{c}: invalid binary')
        if g['source']=='core' and kind not in ('wpr','binary','rrt') and n<0:raise ValueError(f'{c}: negative value')
        if kind=='age' and n<18:raise ValueError('adult age required')
        if c in ('nonreactive_pupil_count','initial_pupil_nonreactive_count_value') and n not in (0,1,2):raise ValueError('invalid pupil count')
        if c in ('charlson_prior_no_age','modified_non_neuro_sofa2_total','abnormal_non_neuro_organ_count','initial_gcs_total_value') and not n.is_integer():raise ValueError('integer score required')
        # The original registry's frozen legal ranges, never empirical ranges.
        metadata=next((m for m in g['metadata'] if m['column']==c),{})
        legal=metadata.get('legal_range')
        if 'canonical_range' in metadata and not binary:
            low,high=metadata['canonical_range']
            if not low<=n<=high:raise ValueError(f'{c}: outside frozen source range')
        elif legal and not binary:
            match=re.fullmatch(r'(-?[\d.]+)(<=|<)value(<=|<)(-?[\d.]+)',legal.replace('value_c','value'))
            if match:
                lo,lop,hop,hi=match.groups(); low=float(lo); high=float(hi)
                if not (n>=low if lop=='<=' else n>low) or not (n<=high if hop=='<=' else n<high):raise ValueError(f'{c}: outside frozen legal range')
    if kind=='wpr' and ((values[0] is None)!=(values[1]==1)):raise ValueError('WPR availability mismatch')
    if kind=='initial' and ((values[0] is None)!=(values[1]==1)):raise ValueError('initial assessment availability mismatch')
    if kind=='rrt' and values[1]>values[0]:raise ValueError('RRT active implies ever')
    if kind=='sofa':
        total,count=values
        if not 0<=count<=5 or not count<=total<=4*count:raise ValueError('incoherent modified SOFA scores')


def manual_row(payload, selected_group_ids, spec):
    result={}
    for g in ordered_groups(selected_group_ids,spec):
        kind=g['kind']; raw=g['raw_columns']; fields=g['manual_fields']
        # Do not enumerate/copy/hash the payload, including unselected values.
        values=[_required(payload,f) for f in fields]
        if kind=='wpr':
            w,p=[_number(v,f,g['unit'],nullable=True) for f,v in zip(fields,values)]
            if (w is None)!=(p is None):raise ValueError('partial WPR pair')
            if w is not None and (w<=0 or p<=0):raise ValueError('WPR pair must be positive')
            result.update(log2_wpr=None if w is None else math.log2(w)-math.log2(p),wpr_unavailable=int(w is None))
        elif kind=='pupil':
            v=values[0]
            if isinstance(v,str):
                category=g['manual_aliases'].get(v,v)
                state=next((c['canonical'] for c in g['manual_categories'] if c['value']==category),None)
                if state is None:raise ValueError('invalid joint terminal pupil category')
                result.update(state)
            else:result.update(nonreactive_pupil_count=_number(v,fields[0],'count'),pupil_unassessable=0)
        elif kind=='initial':
            unit=g['metadata'][0]['unit'];v=values[0]
            if v=='unassessable':v=None
            result[raw[0]]=_number(v,fields[0],unit,nullable=True)
            result[raw[1]]=int(result[raw[0]] is None)
        elif kind in ('sex','gcsm'):
            result[raw[0]]=values[0]
        else:
            for c,v in zip(raw,values):
                unit=g['manual_field_units'][c]
                if kind=='age':unit='years'
                if kind=='sofa':unit='points' if c==raw[0] else 'count'
                result[c]=_number(v,c,unit,nullable=g['source']=='extension' and c not in g['binary_columns'])
        _validate_group(result,g)
    return result


def development_rows(core_rows, extension_rows, spec):
    """No joins or sorting: caller supplies aligned development rows only.

    Copies only registered raw predictor fields; never derives WPR from last CBC.
    """
    if len(core_rows)!=len(extension_rows):raise ValueError('development row count mismatch')
    result=[]
    for core,ext in zip(core_rows,extension_rows):
        row={}
        for g in spec['groups']:
            source=core if g['source']=='core' else ext
            for c in g['raw_columns']:row[c]=_required(source,c)
            _validate_group(row,g)
        result.append(row)
    return result
