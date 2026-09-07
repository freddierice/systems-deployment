#!/usr/bin/env python3
"""Build a clean app checkout and push to DOCR with a one-hour credential."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('application', choices=['health', 'trends'])
p.add_argument('checkout', type=Path)
p.add_argument('--buildctl', default='buildctl')
p.add_argument('--address', default='tcp://127.0.0.1:1234')
p.add_argument('--metadata', type=Path, required=True)
a = p.parse_args()
checkout = a.checkout.resolve()
if subprocess.check_output(['git', '-C', str(checkout), 'status', '--porcelain']).strip():
    raise SystemExit('Commit reviewed application changes before building.')
revision = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
image = f'registry.digitalocean.com/freddierice-systems/{a.application}:{revision}'
with tempfile.TemporaryDirectory(prefix='systems-docr-') as folder:
    config = Path(folder) / 'config.json'
    result = subprocess.run(['doctl', '--context', 'freddie-pki', 'registry', 'docker-config', 'freddierice-systems', '--read-write', '--expiry-seconds=3600'], capture_output=True, check=True)
    config.write_bytes(result.stdout)
    config.chmod(0o600)
    env = os.environ.copy()
    env['DOCKER_CONFIG'] = folder
    subprocess.run([a.buildctl, '--addr', a.address, 'build', '--frontend=dockerfile.v0', '--local', 'context=' + str(checkout), '--local', 'dockerfile=' + str(checkout), '--opt', 'platform=linux/amd64', '--opt', 'label:org.opencontainers.image.revision=' + revision, '--output', 'type=image,name=' + image + ',push=true', '--metadata-file', str(a.metadata)], env=env, check=True)
metadata = json.loads(a.metadata.read_text())
print(image.split(':')[0] + '@' + metadata['containerimage.digest'])
