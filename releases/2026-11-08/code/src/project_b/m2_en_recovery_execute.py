"""Exactly two fixed EN step-size verification calls; no production entrypoint."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_b.m0_recovery_execute import (sha256, durable_json, append_event,
    read_events, bound_path, check_budget, disk_used, supervise)

REL = 'audit/development/m2_en_recovery_v1'
OLD = 'audit/development/m2_en_diagnosis_v1'
CASES = {'F1': (5, 1), 'S1': (1, 10)}
FIT_OPTIONS = {'step_size': .25, 'precision': 1e-10, 'max_steps': 1000}
HISTORICAL_CALLS = 56
ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = ['src/project_b/m2_en_recovery_execute.py', 'src/project_b/m2_en_checked.py',
                'tests/test_m2_en_recovery_execute.py', 'tests/test_m2_en_checked.py']


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def verify_hashes(root, hashes):
    for relative, digest in hashes.items():
        if sha256(bound_path(root, relative)) != digest.lower():
            raise RuntimeError('HASH_DRIFT: ' + relative)


def validate_case(case, events):
    if case not in CASES:
        raise RuntimeError('FIXED_CASES_ONLY')
    starts = [r for r in events if r['action'] == 'START']
    ends = {r['case'] for r in events if r['action'] == 'FINAL_STOPPED'}
    if any(r['case'] == case for r in starts):
        raise RuntimeError('DUPLICATE_CASE')
    if len(starts) >= 2:
        raise RuntimeError('TWO_CALL_CEILING')
    if any(r['case'] not in ends for r in starts):
        raise RuntimeError('UNRESOLVED_CALL')
    if case == 'S1' and 'F1' not in ends:
        raise RuntimeError('F1_FIRST')


def claim(out, case, binding_hash):
    validate_case(case, read_events(Path(out) / 'RECOVERY_CALLS.jsonl'))
    directory = Path(out) / case
    directory.mkdir()
    count = 1 + sum(r['action'] == 'START' for r in read_events(Path(out) / 'RECOVERY_CALLS.jsonl'))
    append_event(Path(out) / 'RECOVERY_CALLS.jsonl', dict(action='START', case=case,
        binding_sha256=binding_hash, new_calls=count, total_actual_calls=HISTORICAL_CALLS + count,
        step_size=.25, zero_initialization=True))
    return directory


def validate_gate(root):
    root = Path(root).resolve()
    if root != ROOT:
        raise RuntimeError('PROJECT_B_ONLY')
    out = root / REL
    binding = read(out / 'PREFIT_BINDING.json')
    if binding.get('status') != 'CONTROLLED_VERIFICATION_READY':
        raise RuntimeError('PREFIT_NOT_READY')
    verify_hashes(root, binding['protected_sha256'])
    verify_hashes(root, binding['new_source_and_scope_sha256'])
    scope = read(out / 'RECOVERY_SCOPE.json')
    if (scope['historical_actual_calls_at_entry'] != 56 or scope['new_patient_optimizations_maximum'] != 2
            or scope['fit_options'] != FIT_OPTIONS or scope['production_restart'] is not False
            or scope['temporal_access'] is not False or scope['scientific_definition_changes'] is not False):
        raise RuntimeError('SCOPE_DRIFT')
    if read(out / 'SYNTHETIC_TESTS.json')['status'] != 'PASS':
        raise RuntimeError('SYNTHETIC_TESTS_REQUIRED')
    if read(out / 'OLD_RETURN_REJECTION.json')['old_return_rejected'] is not True:
        raise RuntimeError('REGRESSION_CHECK_REQUIRED')
    from project_b.recovery_production import validate_recovery_authorization
    validate_recovery_authorization(root)  # Existing authorities checked read-only.
    return binding


def prepare(root):
    root = Path(root).resolve()
    if root != ROOT:
        raise RuntimeError('PROJECT_B_ONLY')
    out = root / REL
    if (out / 'PREFIT_BINDING.json').exists():
        raise RuntimeError('PREFIT_ALREADY_BOUND')
    protected = read(root / OLD / 'D0_PROTECTION_MANIFEST.json')['hashes'].copy()
    protected.update(read(root / OLD / 'D0_REPORT.json')['source_hashes'])
    for path in (root / OLD).rglob('*'):
        if path.is_symlink():
            raise RuntimeError('SYMLINK_FORBIDDEN')
        if path.is_file():
            protected[str(path.relative_to(root)).replace('\\', '/')] = sha256(path)
    for relative in ('docs/sdd/m2-en-failure-diagnosis.md', 'docs/sdd/m2-en-failure-d0.md'):
        protected[relative] = sha256(root / relative)
    verify_hashes(root, protected)
    if read(root / OLD / 'FINAL_DIAGNOSIS_RECEIPT.json')['total_actual_calls'] != 56:
        raise RuntimeError('HISTORY_DRIFT')
    result = subprocess.run([str(root / '.venv-a5/Scripts/python.exe'), '-B', '-m', 'unittest',
                             'discover', '-s', 'tests', '-p', 'test_m2_en*.py', '-v'],
                            cwd=root, text=True, capture_output=True, timeout=60)
    durable_json(out / 'SYNTHETIC_TESTS.json', dict(status='PASS' if result.returncode == 0 else 'FAIL',
        returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
        patient_fits=0, historical_red_tests_observed=True), exclusive=True)
    if result.returncode:
        raise RuntimeError('SYNTHETIC_TEST_FAILURE')
    import numpy as np
    from project_b.m2_en_checked import evaluate_solution
    meta = read(root / OLD / 'inner_1_metadata.json')
    candidate = next(c for c in meta['candidates'] if c['declared_order'] == 10)
    with np.load(root / OLD / 'inner_1.npz', allow_pickle=False) as raw:
        data = {k: raw[k].copy() for k in ('X', 't', 'e', 'beta0')}
    old_return = read(root / OLD / 'S1/RESULT.json')
    acceptance = evaluate_solution(data['X'], data['t'], data['e'], old_return['beta'],
        candidate['mapping']['a'], candidate['mapping']['lambda'], core_n=20,
        backend_success=False, warning_records=old_return['warnings'], trace_complete=True,
        reference_beta=data['beta0'])
    if acceptance['passed'] or acceptance['core_score_inf_per_event'] < .6:
        raise RuntimeError('OLD_RETURN_WRONGLY_ACCEPTED')
    durable_json(out / 'OLD_RETURN_REJECTION.json', dict(old_return_rejected=True,
        case='previous S1, no refit', acceptance=acceptance, patient_fits=0), exclusive=True)
    old_review = read(root / OLD / 'INDEPENDENT_PREFIT_DIAGNOSTIC_REVIEW.json')
    spent = dict(old_review['stage_budget_accounting'])
    for case in CASES:
        r = read(root / OLD / case / 'FINAL_STOPPED.json')['resources']
        spent['cpu_seconds'] += r['accounting_cpu_seconds']
        spent['wall_seconds'] += r['accounting_wall_seconds']
    # Explicit accounting allocations, not measured use or established bounds.
    spent['cpu_seconds'] += 120
    spent['wall_seconds'] += 600
    spent['new_prefit_allocation'] = {'cpu_seconds': 120, 'wall_seconds': 600,
                                    'basis': 'conservative accounting allocation; not measured or proved upper bound'}
    sources = SOURCE_FILES + [REL + '/' + name for name in
                             ('RECOVERY_SCOPE.json', 'SYNTHETIC_TESTS.json', 'OLD_RETURN_REJECTION.json')]
    binding = dict(status='CONTROLLED_VERIFICATION_READY', protected_sha256=protected,
        new_source_and_scope_sha256={name: sha256(root / name) for name in sources},
        budget_spent_at_entry=spent, historical_actual_calls=56, allowed_new_calls=2,
        review_method='Main-agent source, synthetic, saved-state and binding review; not an independent external review',
        production_resume_authorized=False)
    durable_json(out / 'PREFIT_BINDING.json', binding, exclusive=True)
    validate_gate(root)
    return {'status': binding['status'], 'protected_files': len(protected), 'patient_fits': 0}


def execute_case(root, case):
    root = Path(root).resolve()
    binding = validate_gate(root)
    out = root / REL
    lock = out / 'CONTROLLER.lock'
    with lock.open('x') as stream:
        stream.write(str(os.getpid()))
        stream.flush()
        os.fsync(stream.fileno())
    claimed = finished = False
    start, cpu_start = time.monotonic(), time.process_time()
    try:
        events = read_events(out / 'RECOVERY_CALLS.jsonl')
        validate_case(case, events)
        spent = dict(binding['budget_spent_at_entry'])
        for event in events:
            if event['action'] == 'FINAL_STOPPED':
                r = event['resources']
                if not r or r.get('accounting_cpu_seconds') is None:
                    raise RuntimeError('RESOURCE_TAIL_UNKNOWN')
                spent['wall_seconds'] += r['accounting_wall_seconds']
                spent['cpu_seconds'] += r['accounting_cpu_seconds']
                verify_hashes(out / event['case'], event['artifact_hashes'])
        wall_limit, cpu_limit = check_budget(spent, disk_used(out))
        directory = claim(out, case, sha256(out / 'PREFIT_BINDING.json'))
        claimed = True
        request = dict(root=str(root), case=case, fit_options=FIT_OPTIONS,
                       binding_sha256=sha256(out / 'PREFIT_BINDING.json'))
        durable_json(directory / 'REQUEST.json', request, exclusive=True)
        resources = None
        error = None
        try:
            resources = supervise([str(root / '.venv-a5/Scripts/python.exe'), '-B', str(Path(__file__).resolve()),
                                   '--child', str(directory / 'REQUEST.json')], directory,
                                  wall_limit=wall_limit, cpu_limit=cpu_limit, disk_root=out)
            validate_gate(root)
        except BaseException:
            error = traceback.format_exc()
        if resources:
            resources['accounting_wall_seconds'] = time.monotonic() - start + 1
            resources['accounting_cpu_seconds'] = resources['child_cpu_seconds'] + time.process_time() - cpu_start + 1
            resources['serialization_reserve_seconds'] = 1
        if not (directory / 'RESULT.json').exists():
            durable_json(directory / 'RESULT.json', dict(status='NO_ACCEPTED_RETURN', acceptance_passed=False,
                         exception_chain=error), exclusive=True)
        result = read(directory / 'RESULT.json')
        accepted = bool(resources and resources['exit_code'] == 0 and error is None
                        and result.get('acceptance_passed') is True)
        final = dict(action='FINAL_STOPPED', case=case, resources=resources, exception_chain=error,
            outcome='NUMERICALLY_ACCEPTED' if accepted else 'NUMERICAL_FAILURE', acceptance_passed=accepted,
            production_restart=False,
            total_actual_calls=HISTORICAL_CALLS + sum(r['action'] == 'START' for r in read_events(out / 'RECOVERY_CALLS.jsonl')),
            artifact_hashes={path.name: sha256(path) for path in directory.iterdir() if path.is_file()})
        durable_json(directory / 'FINAL_STOPPED.json', final, exclusive=True)
        append_event(out / 'RECOVERY_CALLS.jsonl', final)
        finished = True
        return {k: final[k] for k in ('case', 'outcome', 'acceptance_passed', 'total_actual_calls')}
    finally:
        if not claimed or finished:
            lock.unlink()


def child_main(request_path):
    if sys.stdin.readline().strip() != 'GO':
        raise RuntimeError('SUPERVISOR_GO_REQUIRED')
    request_path = Path(request_path).resolve()
    cfg = read(request_path)
    root, case = Path(cfg['root']).resolve(), cfg['case']
    if root != ROOT or case not in CASES or request_path != root / REL / case / 'REQUEST.json':
        raise RuntimeError('FIXED_REQUEST_PATH_REQUIRED')
    directory = request_path.parent
    try:
        validate_gate(root)
        if cfg['binding_sha256'] != sha256(root / REL / 'PREFIT_BINDING.json') or cfg['fit_options'] != FIT_OPTIONS:
            raise RuntimeError('REQUEST_BINDING_DRIFT')
        events = read_events(root / REL / 'RECOVERY_CALLS.jsonl')
        if (sum(r['action'] == 'START' and r['case'] == case for r in events) != 1
                or any(r['action'] == 'FINAL_STOPPED' and r['case'] == case for r in events)):
            raise RuntimeError('UNCLAIMED_OR_FINISHED')
        fold, order = CASES[case]
        meta = read(root / OLD / f'inner_{fold}_metadata.json')
        candidate = next(c for c in meta['candidates'] if c['declared_order'] == order)
        snapshot = root / OLD / f'inner_{fold}.npz'
        if sha256(snapshot) != meta['snapshot']['sha256']:
            raise RuntimeError('SNAPSHOT_DRIFT')
        import numpy as np
        from project_b.m2_en_checked import fit_checked, NumericalAcceptanceError
        from project_b.m2_en_trace import _json
        from project_b.a5_m0_cox import predict_native_risk
        with np.load(snapshot, allow_pickle=False) as raw:
            data = {k: raw[k].copy() for k in ('X', 't', 'e', 'beta0')}
        model, acceptance = fit_checked(data['X'], data['t'], data['e'], candidate['mapping'],
            directory / 'TRACE.jsonl', fit_options=FIT_OPTIONS, candidate=candidate,
            provenance=cfg, reference_beta=data['beta0'])
        # Training-array numerical risk audit only; no validation predictions or metrics.
        risks = np.asarray(predict_native_risk({'model_id': 'M2_EN', 'backend': model,
                                               'columns': list(model.params_.index)}, data['X']))
        if not (np.isfinite(risks).all() and ((risks >= 0) & (risks <= 1)).all()
                and (risks[:, 0] <= risks[:, 1]).all()):
            raise RuntimeError('NATIVE_RISK_INVALID')
        result = dict(status='NUMERICALLY_ACCEPTED', acceptance_passed=True,
            acceptance=acceptance, mapping=candidate['mapping'], candidate_order=order, inner_fold=fold,
            beta=model.params_.to_numpy(), internal_center=model._norm_mean.to_numpy(),
            internal_scale=model._norm_std.to_numpy(),
            baseline_event_times=model.baseline_cumulative_hazard_.index.to_numpy(),
            baseline_cumulative_hazard=model.baseline_cumulative_hazard_.iloc[:, 0].to_numpy(),
            training_risks_90_180=risks, risk_definition='frozen right-continuous Breslow step baseline',
            diagnostic_only=True, production_model=False)
        durable_json(directory / 'RESULT.json', _json(result), exclusive=True)
        return 0
    except BaseException as error:
        result = dict(status='NUMERICAL_FAILURE', acceptance_passed=False, exception_chain=traceback.format_exc(),
                      diagnostic_only=True, production_model=False)
        if hasattr(error, 'acceptance'):
            result['acceptance'] = error.acceptance
        from project_b.m2_en_trace import _json
        durable_json(directory / 'RESULT.json', _json(result), exclusive=True)
        return 1


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prepare', action='store_true')
    group.add_argument('--case', choices=tuple(CASES))
    group.add_argument('--child')
    args = parser.parse_args()
    if args.child:
        sys.exit(child_main(args.child))
    print(json.dumps(prepare(ROOT) if args.prepare else execute_case(ROOT, args.case)))
