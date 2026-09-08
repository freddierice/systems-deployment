#!/usr/bin/env python3
"""Create temporary app-image pods for PostgreSQL schema maintenance."""
import copy
import json
from pathlib import Path
import subprocess
import yaml

root = Path(__file__).resolve().parents[1]
rendered = subprocess.check_output(['helm', 'template', 'systems', str(root / 'kubernetes/charts/systems'), '--namespace', 'systems', '-f', str(root / 'kubernetes/values.production.yaml')], text=True)
for document in yaml.safe_load_all(rendered):
    if not document or document['metadata']['name'] not in ('health', 'trends', 'google-workload-identity'):
        continue
    if document['kind'] in ('ServiceAccount', 'ConfigMap'):
        document['metadata']['namespace'] = 'systems'
        document['metadata']['labels'] = {'app.kubernetes.io/managed-by': 'Helm'}
        document['metadata']['annotations'] = {'meta.helm.sh/release-name': 'systems', 'meta.helm.sh/release-namespace': 'systems'}
    elif document['kind'] == 'Deployment':
        name = document['metadata']['name']
        spec = copy.deepcopy(document['spec']['template']['spec'])
        spec['restartPolicy'] = 'Never'
        container = spec['containers'][0]
        container['command'] = ['python', '-c', 'import time; time.sleep(7200)']
        container.pop('args', None)
        for field in ('startupProbe', 'readinessProbe', 'livenessProbe', 'ports'):
            container.pop(field, None)
        document = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name + '-migration', 'namespace': 'systems'}, 'spec': spec}
    else:
        continue
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(document), text=True, check=True)
