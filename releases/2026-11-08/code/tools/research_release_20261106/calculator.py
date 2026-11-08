"""Local research calculator: frozen original native-risk engine, no fitting."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import pickle
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

for _name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[_name] = '1'

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'tools/research_release_20261106'
BASE = 'audit/cohort_correction_20261105'
RELEASE = BASE + '/release_20261106'
CANDIDATES = BASE + '/candidate10_review'
MODEL = CANDIDATES + '/candidate10.pickle'
DICTIONARY = CANDIDATES + '/MANUAL_INPUT_DICTIONARY.json'
LOCK = ROOT / RELEASE / 'TOOL_MODEL_LOCK.json'
EXPECTED = {
    'candidate10.pickle': 'e41b9066ec451034f54c230d98a5becf4f7a9ec972d19282388d411881bff30e',
    'CANDIDATE10.json': '403bee96aeef5b140dfa53fe4f10e7f47b185e36099506f18ded89c6c6f98f5d',
    'ALTERNATIVE14.json': 'e877228fb03333b0a8ad8e0840f469a6bdf910c6cd3e8e42342bdb714032c64f',
    'MANUAL_INPUT_DICTIONARY.json': '3b18905ce0e2ee560bb41e9d39e8d1b231a22d8dad77e49490d03b0b41306718',
    'REVIEW_BUNDLE.json': 'bc54286488dce1af9ac548b58545b284546320d9585e9741656e81f080160dd5',
}
AUTHORIZATION = '启动时间迁移评价和计算器开发。全部完成后更新更正队列后的SHAP、论文图表及Word。'
MANUAL_FIELDS = ('age_at_admit', 'charlson_prior_no_age', 'gcsm_category', 'wpr_wbc',
                 'wpr_platelet', 'initial_gcs_total', 'spo2_last', 'hemoglobin_last',
                 'wbc_absolute_last', 'bun_last')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _runtime_versions():
    return {'python': sys.version, **{name: importlib.metadata.version(name) for name in (
        'numpy', 'scipy', 'pandas', 'scikit-learn', 'scikit-survival')}}


def _source_paths():
    # Bind only source/configuration/evidence, never traverse patient/temporal inputs.
    paths = [CANDIDATES + '/' + name for name in EXPECTED]
    paths += [BASE + '/postfit_finalize_20261106/' + name for name in (
        'EXECUTION_COMPLETION_RECEIPT.json', 'CANDIDATE_SUMMARY.json',
        'FINALIZATION_RECEIPT.json')]
    paths += ['config/COMPACT_MANUAL_INPUT_SPEC.json']
    paths += [str(p.relative_to(ROOT)).replace('\\', '/') for p in sorted((ROOT / 'src/project_b').glob('*.py'))]
    paths += [SOURCE + '/' + name for name in ('calculator.py', 'calculator.html')]
    return sorted(paths)


def build_lock():
    """Construct additive lock metadata without writing or fitting anything."""
    files = {p: _digest((ROOT / p).read_bytes()) for p in _source_paths()}
    for name, expected in EXPECTED.items():
        if files[CANDIDATES + '/' + name] != expected:
            raise ValueError('approved candidate hash drift: ' + name)
    candidate = json.loads((ROOT / CANDIDATES / 'CANDIDATE10.json').read_text(encoding='utf-8'))
    dictionary = json.loads((ROOT / DICTIONARY).read_text(encoding='utf-8'))
    return dict(schema_version='tool-model-lock-1', state='research_only_locked',
                clinical_validated=False, created_utc=datetime.now(timezone.utc).isoformat(),
                authorization=AUTHORIZATION, authorization_basis='current user instruction; primary10/backup14 approved',
                model_version='corrected-cohort-candidate10-e41b9066ec45',
                roles={'primary': 10, 'backup': 14},
                native_risk_interface={'horizons_days': [90, 180], 'scale': 'native_probability',
                                       'engine': 'project_b.a5_m0_cox.predict_native_risk'},
                files=files, preprocessor_sha256=_digest(_canonical(candidate['preprocessor'])),
                schema_sha256=_digest(_canonical(dictionary)), manual_fields=list(MANUAL_FIELDS),
                runtime_versions=_runtime_versions(),
                candidate_provisional_flag_preserved=True)


def write_lock(path=LOCK):
    """Exclusive creation: never silently replace a previous research lock."""
    path = Path(path).resolve()
    if not path.is_relative_to((ROOT / RELEASE).resolve()):
        raise ValueError('lock output outside authorized release directory')
    content = build_lock()
    with path.open('x', encoding='utf-8') as stream:
        json.dump(content, stream, indent=2, ensure_ascii=False, allow_nan=False)
    return content


class Calculator:
    def __init__(self, lock):
        if (lock.get('schema_version') != 'tool-model-lock-1'
                or lock.get('state') != 'research_only_locked'
                or lock.get('clinical_validated') is not False
                or lock.get('roles') != {'primary': 10, 'backup': 14}
                or lock.get('manual_fields') != list(MANUAL_FIELDS)
                or lock.get('native_risk_interface') != {'horizons_days': [90, 180], 'scale': 'native_probability', 'engine': 'project_b.a5_m0_cox.predict_native_risk'}):
            raise ValueError('invalid research lock metadata')
        if set(lock['files']) != set(_source_paths()):
            raise ValueError('lock source inventory drift')
        if lock.get('runtime_versions') != _runtime_versions():
            raise ValueError('locked runtime version drift')
        # Read verified bytes once; load those exact bytes (not a second unverified read).
        verified = {}
        for relative, expected in lock['files'].items():
            data = (ROOT / relative).read_bytes()
            if _digest(data) != expected:
                raise ValueError('locked source hash drift: ' + relative)
            verified[relative] = data
        for name, expected in EXPECTED.items():
            if lock['files'][CANDIDATES + '/' + name] != expected:
                raise ValueError('approved candidate hash drift')
        dictionary = json.loads(verified[DICTIONARY])
        if _digest(_canonical(dictionary)) != lock['schema_sha256']:
            raise ValueError('manual schema hash drift')
        sys.path.insert(0, str(ROOT / 'src'))
        self.package = pickle.loads(verified[MODEL])
        if _digest(_canonical(self.package['preprocessor'])) != lock['preprocessor_sha256']:
            raise ValueError('preprocessor hash drift')
        self.spec = {'groups': dictionary['selected_groups']}
        self.manual_fields = tuple(self.package['manual_fields'])
        if self.manual_fields != MANUAL_FIELDS or self.package['preprocessor']['final_columns'] != dictionary['design_columns']:
            raise ValueError('model input schema drift')
        self.html = verified[SOURCE + '/calculator.html']

    @classmethod
    def from_lock(cls, path=LOCK):
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    def predict_manual(self, payload, *, event_available_before_t0: bool):
        """Only selected keys are dereferenced; caller must explicitly confirm t0."""
        if event_available_before_t0 is not True:
            raise ValueError('explicit event and availability before t0 confirmation required')
        from project_b.compact_inputs import manual_row
        from project_b.compact_preprocessing import transform_preprocessor
        from project_b.a5_m0_cox import predict_native_risk
        row = manual_row(payload, self.package['selected_groups'], self.spec)
        matrix = transform_preprocessor(self.package['preprocessor'], [row])
        a, b = predict_native_risk(self.package['model'], matrix, (90, 180))[0]
        return {'risk_90_days': a, 'risk_180_days': b, 'scale': 'native_probability',
                'clinical_validated': False}

    def predict_api(self, request):
        if not isinstance(request, dict) or set(request) != {'inputs', 'event_available_before_t0'}:
            raise ValueError('API requires inputs and explicit t0 confirmation only')
        if not isinstance(request['inputs'], dict) or set(request['inputs']) != set(MANUAL_FIELDS):
            raise ValueError('API requires exactly the ten manual inputs; no identifiers or extra predictors')
        for value in request['inputs'].values():
            if isinstance(value, dict) and set(value) != {'value', 'unit'}:
                raise ValueError('unit wrappers require exactly value and unit; no extra fields')
        return self.predict_manual(request['inputs'], event_available_before_t0=request['event_available_before_t0'])


def predict_manual(payload, *, event_available_before_t0: bool):
    """Public locked-only interface; refusal before external lock exists is intentional."""
    return Calculator.from_lock().predict_manual(payload, event_available_before_t0=event_available_before_t0)


def create_server(engine, *, host='127.0.0.1', port=8765):
    if host != '127.0.0.1':
        raise ValueError('only 127.0.0.1 loopback binding is permitted')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No patient/access logging.

        def _reply(self, status, body, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            authority = '127.0.0.1:' + str(self.server.server_address[1])
            return (self.headers.get('Host') == authority
                    and self.headers.get('Origin') in (None, 'http://' + authority))

        def do_GET(self):
            if not self._local():
                return self._reply(403, b'{"error":"local origin required"}')
            if self.path != '/':
                return self._reply(404, b'{"error":"not found"}')
            self._reply(200, engine.html, 'text/html')

        def do_POST(self):
            if not self._local():
                return self._reply(403, b'{"error":"local origin required"}')
            if self.path != '/predict':
                return self._reply(404, b'{"error":"not found"}')
            try:
                if self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('application/json required')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise ValueError('invalid request size')
                request = json.loads(self.rfile.read(length))
                response = engine.predict_api(request)
                self._reply(200, _canonical(response))
            except (ValueError, TypeError, KeyError, OverflowError):
                # Never echo patient values into response, logs, or disk.
                self._reply(400, b'{"error":"Invalid inputs, units, categories, WPR pair, or t0 confirmation. Check the input dictionary."}')

    return HTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('lock', 'serve'))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.command == 'lock':
        write_lock()
        print('Research-only external model lock created; no clinical release.')
    else:
        engine = Calculator.from_lock()
        server = create_server(engine, port=args.port)
        print(f'Research-only calculator: http://127.0.0.1:{server.server_address[1]}/ (Ctrl+C to stop)')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
