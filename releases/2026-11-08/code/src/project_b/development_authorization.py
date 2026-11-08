"""Read-only authorization for development; never applies a transition."""
import hashlib
import json
from pathlib import Path
from .a5_dry_run import _validate_authorization_bundle


def validate_development_authorization(project_root):
    root = Path(project_root).resolve(strict=True)
    snapshot, binding, before = _validate_authorization_bundle(root, root / 'audit/ML_FIT_AUTHORIZATION.json', None)
    state_path = root / 'manifests/PROJECT_STATE.json'
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    transition = json.loads((root / 'audit/a5/ML_FIT_STATE_TRANSITION.json').read_text(encoding='utf-8'))
    expected = dict(before, phase='MODEL_DEVELOPMENT', fit_authorized=True, compression_branch='PENDING')
    if state != expected or state.get('temporal_unsealed') is not False:
        raise PermissionError('current development state does not equal authorized transition')
    if (transition.get('status') != 'APPLIED' or
        transition.get('previous_state_sha256') != snapshot.get('state_sha256_before') or
        transition.get('state_sha256') != hashlib.sha256(state_bytes).hexdigest().upper() or
        any(transition.get(k) != binding[k] for k in ('authorization_draft_sha256', 'signature_sha256'))):
        raise PermissionError('transition receipt does not bind current state and signature')
    return {'status': 'PASS', 'state_sha256': transition['state_sha256'], **binding}
