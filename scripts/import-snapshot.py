#!/usr/bin/env python3
"""Stream one frozen snapshot into its exact app image and run the importer."""
import argparse
import json
from pathlib import Path
import subprocess
import yaml

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('application', choices=['health', 'trends'])
p.add_argument('snapshot', type=Path)
p.add_argument('--report', required=True, type=Path)
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
values = yaml.safe_load((root / 'kubernetes/values.production.yaml').read_text())
pod = a.application + '-migration'
info = json.loads(subprocess.check_output(['kubectl', '-n', 'systems', 'get', 'pod', pod, '-o', 'json']))
if info['spec']['containers'][0]['image'] != values['apps'][a.application]['image']:
    raise SystemExit('Migration image differs from the intended deployment')
status = info.get('status', {}).get('containerStatuses', [{}])[0]
expected = values['apps'][a.application]['image']
if not status.get('ready') or not status.get('imageID', '').endswith(expected.split('@')[1]):
    raise SystemExit('Wait until the running migration container matches the intended digest')
remote = '/migration/' + a.application + '.sqlite3'
copy = 'import os,sys; p=sys.argv[1]; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); f=os.fdopen(fd,"wb"); f.write(sys.stdin.buffer.read()); f.close()'
with a.snapshot.open('rb') as stream:
    subprocess.run(['kubectl', '-n', 'systems', 'exec', '-i', pod, '--', 'python', '-c', copy, remote], stdin=stream, check=True)
result = subprocess.run(['kubectl', '-n', 'systems', 'exec', pod, '--', 'python', '-m', a.application + '.migrate', '--import-sqlite', remote], text=True, capture_output=True)
if result.returncode:
    raise SystemExit('Import did not commit. Review the migration pod without disclosing private rows.')
report = json.loads(result.stdout.splitlines()[0])
a.report.write_text(json.dumps(report, indent=2) + '\n')
a.report.chmod(0o600)
print(a.application + ': committed ' + str(len(report['tables'])) + ' verified tables, ' + str(sum(t['rows'] for t in report['tables'].values())) + ' rows')
