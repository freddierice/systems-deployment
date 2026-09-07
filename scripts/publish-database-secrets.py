#!/usr/bin/env python3
"""Publish Terraform database credentials to Google; sync pods separately."""
import json
import subprocess
from urllib.parse import quote, urlencode

PROJECT = '186933910776'


def publish(name, payload):
    args = ['gcloud', 'secrets', '--project=' + PROJECT]
    if subprocess.run(args + ['describe', name], capture_output=True).returncode:
        subprocess.run(args + ['create', name, '--replication-policy=automatic'], capture_output=True, check=True)
    current = subprocess.run(args + ['versions', 'access', 'latest', '--secret=' + name], capture_output=True, text=True)
    if current.returncode or current.stdout != payload:
        subprocess.run(args + ['versions', 'add', name, '--data-file=-'], input=payload, capture_output=True, text=True, check=True)
    print('Stored ' + name)


def main():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['terraform', '-chdir=' + str(root / 'infra'), 'output', '-json', 'database_bootstrap'], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    publish('systems-postgres-bootstrap', result.stdout)
    for name, app in data['apps'].items():
        query = urlencode({'sslmode': 'verify-full', 'sslrootcert': '/etc/postgresql/ca.crt'})
        url = f"postgresql://{quote(app['user'], safe='')}:{quote(app['password'], safe='')}@{data['host']}:{data['port']}/{quote(app['database'], safe='')}?{query}"
        publish('systems-' + name + '-database', json.dumps({'DATABASE_URL': url}, sort_keys=True))


if __name__ == '__main__':
    main()
