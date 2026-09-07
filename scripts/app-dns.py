#!/usr/bin/env python3
"""Inspect or switch only the two private application A records."""
import argparse
import json
import subprocess
import urllib.request


def secret(name):
    return subprocess.check_output(['gcloud', 'secrets', 'versions', 'access', 'latest', '--project=186933910776', '--secret=' + name], text=True).strip()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--switch-to', choices=['100.91.90.6', '100.66.233.125'])
    a = p.parse_args()
    account = secret('systems-cluster-cloudflare-account')
    token = secret('systems-cluster-cloudflare-token')
    headers = {'Content-Type': 'application/json'}
    if '@' in account:
        headers.update({'X-Auth-Email': account, 'X-Auth-Key': token})
    else:
        headers['Authorization'] = 'Bearer ' + token
    def api(path, body=None):
        request = urllib.request.Request('https://api.cloudflare.com/client/v4/' + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method='PATCH' if body is not None else 'GET')
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        if not result.get('success'):
            raise RuntimeError('Cloudflare request failed')
        return result['result']
    zones = api('zones?name=freddie.xyz')
    if len(zones) != 1:
        raise RuntimeError('Expected exactly one freddie.xyz zone')
    zone = zones[0]
    if len(account) == 32 and '@' not in account and zone['account']['id'] != account:
        raise RuntimeError('Cloudflare account does not match')
    records = []
    for app in ('health', 'trends'):
        records += api('zones/' + zone['id'] + '/dns_records?name=' + app + '.freddie.xyz')
    if len(records) != 2 or any(r['type'] != 'A' or r['content'] not in ('100.66.233.125', '100.91.90.6') or r['proxied'] for r in records):
        raise RuntimeError('Unexpected app DNS records; inspect before switching')
    for record in records:
        if a.switch_to and record['content'] != a.switch_to:
            record = api('zones/' + zone['id'] + '/dns_records/' + record['id'], {'content': a.switch_to, 'proxied': False})
        print(json.dumps({key: record[key] for key in ('name', 'type', 'content', 'ttl', 'proxied')}))


if __name__ == '__main__':
    main()
