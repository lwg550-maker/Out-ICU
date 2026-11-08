"""Presentation v1.1 only: clarify frozen initial-GCS first-six-hour window."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'tools/research_release_20261106/'
RELEASE = ROOT / 'audit/cohort_correction_20261105/release_20261106'
RECEIPT = RELEASE / 'UI_CLARIFICATION_V1_1.json'
LOCK = RELEASE / 'TOOL_MODEL_LOCK.json'
LOCK_SHA = 'f6fd5cf85e20290211d4f07eaac4e4c26004d2a3c8a0372966164d249f33d75c'
MODEL_SHA = 'e41b9066ec451034f54c230d98a5becf4f7a9ec972d19282388d411881bff30e'
OLD_CODE_SHA = 'a3271ca96df8fb5f28ad77ba743aa6f7125f6c5aee4fca979b4a0fd000cdabeb'
PURPOSE = 'presentation-only clarification, no predictor definition change'
FILES = (
    SOURCE + 'calculator_ui_v1_1.py', SOURCE + 'calculator_ui_v1_1.html',
    SOURCE + 'test_calculator_ui_v1_1.py', SOURCE + 'README_UI_V1_1.md',
    'config/EXTENDED_VARIABLE_REGISTRY_A1_FROZEN.csv',
    'config/A2_EXPECTED_SOURCE_CONTRACTS.csv', 'sql/a3_development_value_extract.sql',
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def original_module():
    """Import only exact historical code after checking the original lock bytes."""
    if _sha(LOCK.read_bytes()) != LOCK_SHA:
        raise ValueError('original research lock hash drift')
    path = ROOT / SOURCE / 'calculator.py'
    if _sha(path.read_bytes()) != OLD_CODE_SHA:
        raise ValueError('original calculator code hash drift')
    name = '_project_b_locked_calculator_ui_delegate'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def load_engine(*, receipt=None):
    """Validate dated UI bindings, then replace only original engine.html bytes."""
    if receipt is None:
        receipt = json.loads(RECEIPT.read_bytes())
    if (receipt.get('schema_version') != 'presentation-clarification-1.1'
            or receipt.get('purpose') != PURPOSE
            or receipt.get('original_model_sha256') != MODEL_SHA
            or receipt.get('original_lock_sha256') != LOCK_SHA
            or receipt.get('predictor_definition_changed') is not False
            or receipt.get('clinical_validated') is not False):
        raise ValueError('invalid presentation clarification receipt')
    if set(receipt.get('files', {})) != set(FILES):
        raise ValueError('UI receipt file inventory drift')
    verified = {}
    for relative in FILES:
        data = (ROOT / relative).read_bytes()
        if _sha(data) != receipt['files'][relative]:
            raise ValueError('UI source hash drift: ' + relative)
        verified[relative] = data
    original = original_module()
    engine = original.Calculator.from_lock()  # All original 83 hashes must validate.
    engine.html = verified[SOURCE + 'calculator_ui_v1_1.html']
    return engine


def main():
    engine = load_engine()
    server = original_module().create_server(engine, host='127.0.0.1', port=8765)
    print('Presentation UI v1.1: http://127.0.0.1:8765/ ; unchanged research model (Ctrl+C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
