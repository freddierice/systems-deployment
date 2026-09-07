#!/usr/bin/env python3
"""One-time import from the legacy droplet; subsequent changes use Google."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--health', required=True, type=Path)
p.add_argument('--trends', required=True, type=Path)
a = p.parse_args()
spec = importlib.util.spec_from_file_location('publisher', Path(__file__).with_name('publish-database-secrets.py'))
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
result = subprocess.run(['uv', 'run', '--project', str(a.health), '--no-sync', 'python', '-c', 'import json,sys; from dotenv import dotenv_values; print(json.dumps(dict(dotenv_values(sys.argv[1]))))', str(a.health / '.env')], text=True, capture_output=True, check=True)
values = {k: v for k, v in json.loads(result.stdout).items() if v and k.startswith(('WITHINGS_', 'FITBIT_', 'GOOGLE_HEALTH_'))}
publisher.publish('systems-health-runtime', json.dumps(values, sort_keys=True))
for key in ('thetadata', 'fmp'):
    path = a.trends / ('data/.' + key + '-key')
    publisher.publish('systems-trends-' + key, path.read_text().strip() if path.exists() else '')
