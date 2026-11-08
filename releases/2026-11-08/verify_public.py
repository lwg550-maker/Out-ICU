"""Verify only public release files; no data access or model execution."""
from pathlib import Path
import hashlib,json
root=Path(__file__).resolve().parent
manifest=json.loads((root/'MANIFEST_SHA256.json').read_text())
actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name!='MANIFEST_SHA256.json'}
assert actual==set(manifest),'Release inventory mismatch'
for path,digest in manifest.items():
 assert hashlib.sha256((root/path).read_bytes()).hexdigest()==digest,path
print('PASS:',len(manifest),'public files verified; no restricted objects required.')
