"""Forward-only, provider-free requalification of a retained professional app.

Only factory-owned trusted runtime bytes may change. Generated station files and
the frozen ProductSpec are copied verbatim from the verified source package.
The complete frozen gates, package smoke and browser checks run again before
Medaurus may review the new package.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ...qualification_executor import materialize
from ..normal_app_gates import evaluate_normal_app
from ..product_packaging import verify_package
from ..runtime_identity import normal_app_runtime_status
from .browser import audit
from .quality import http_smoke, prepare_project
from .spec import MODEL, sha
from . import runtime


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rebuild(source, segment, spec, frozen_contract):
    """Re-run the original gate contract on original producers plus current runtime."""
    source, segment = Path(source).resolve(), Path(segment).resolve()
    original_sha = verify_package(source)
    manifest = json.loads((source / 'package.json').read_text(encoding='utf-8'))
    producer_paths = sorted(name for name in manifest['files'] if name.startswith('product_app/') and name.endswith('.py'))
    if 'product_app/runtime.py' not in producer_paths or 'product_app/definition.py' not in producer_paths:
        raise ValueError('Retained professional package has no trusted runtime or frozen definition')
    sources = {Path(name).name: (source / name).read_text(encoding='utf-8') for name in producer_paths}
    if len(sources) != len(producer_paths): raise ValueError('Duplicate producer module name')
    new_runtime = Path(runtime.__file__).read_text(encoding='utf-8')
    old_runtime_sha = _hash(source / 'product_app/runtime.py')
    current_runtime_sha = hashlib.sha256(new_runtime.encode()).hexdigest()
    if old_runtime_sha == current_runtime_sha:
        raise ValueError('No shared runtime change to requalify')
    sources['runtime.py'] = new_runtime
    expected_files = set(frozen_contract['required_files'])
    if set(sources) != expected_files or set(frozen_contract['product']) != {'package','public_checks','private_checks','integration_probe'}:
        raise ValueError('Retained frozen gate contract does not match producer modules')
    retained = materialize(product_key='professional_product', arm='shared_renderer_requalification_offline',
                           model=MODEL, patches=sources)
    gates = evaluate_normal_app(retained, frozen_contract['product'],
                                {'required_files': list(sources)}, ws_root=segment / 'gates')
    (segment / 'frozen_gate_result.json').write_text(json.dumps(gates, indent=2), encoding='utf-8')
    if not gates['all_mandatory_gates_pass']:
        raise ValueError('Requalified frozen gates failed: ' + json.dumps(gates['reasons']))
    verified = {name: (segment / 'gates' / 'product_app' / name).read_text(encoding='utf-8') for name in sources}
    if verified != sources: raise ValueError('Requalification gate bytes changed before packaging')
    app = prepare_project(segment / 'app', verified, spec)
    smoke = http_smoke(app, spec, segment / 'delivery-smoke')
    browser = audit(app, spec, smoke['database'], segment / 'delivery-browser')
    result = {'schema_version': 'shared_renderer_requalification_v1',
              'original_package_sha256': original_sha, 'original_runtime_sha256': old_runtime_sha,
              'product_spec_sha256': sha(spec), 'frozen_gate_contract_sha256': sha(frozen_contract),
              'new_runtime_sha256': current_runtime_sha, 'new_package_sha256': verify_package(app),
              'source_identity': normal_app_runtime_status(), 'generated_modules_preserved':
              {name: _hash(source / ('product_app/' + name)) for name in sources if name not in {'runtime.py','definition.py'}},
              'frozen_gate_result_sha256': _hash(segment / 'frozen_gate_result.json'),
              'smoke': smoke, 'browser': browser}
    with (segment / 'shared_renderer_requalification.json').open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
    return result
