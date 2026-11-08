"""Exercise original selection function bodies on artificial, nonpatient inputs."""
from pathlib import Path
import ast
root=Path(__file__).resolve().parent
space={}
for file,names in [('compact_inputs.py',('ordered_groups','selected_manual_fields')),('compact_selection.py',('aggregate_importance',))]:
 tree=ast.parse((root/'code/src/project_b'/file).read_text())
 selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
 assert len(selected)==len(names)
 exec(compile(ast.Module(body=selected,type_ignores=[]),file,'exec'),{'BUDGETS':(6,10,14),**space},space)
 # Bind referenced original functions without importing model-training dependencies.
 for fn in space.values():
  if callable(fn):fn.__globals__.update(space)
spec={'groups':[{'id':'a','manual_fields':['x','y']},{'id':'b','manual_fields':['z']},{'id':'c','manual_fields':['x']},{'id':'d','manual_fields':['w']},{'id':'inactive','manual_fields':['q']}]}
e=[{'group':g,'heldout_n':10,'delta':v} for g,v in [('a',1),('b',1),('c',0),('d',-1)]]
r=space['aggregate_importance'](e,spec,budgets=(1,2,3,4))
assert r['ordered_groups']==['a','b','c','d'] # exact ties: dictionary order
assert r['selected']['1']==['b'] # skip oversized first group, continue
assert r['selected']['2']==['a','c'] # shared x counts once
assert r['selected']['3']==['a','b','c']
assert r['selected']['4']==['a','b','c','d'] # negative importance retained
assert r['inactive_groups']==['inactive']
print('PASS: original function bodies; ties, skip-and-continue, field union, negative importance, inactive groups. No predictive fitting.')
