#!/usr/bin/env python3
"""Inspect/switch private application DNS, or provision Time on the same gateway."""
import argparse
import json
import subprocess
import urllib.request


def secret(name):
    return subprocess.check_output(['gcloud', 'secrets', 'versions', 'access', 'latest', '--project=186933910776', '--secret=' + name], text=True).strip()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--switch-to', choices=['100.91.90.6', '100.66.233.125'])
    p.add_argument('--ensure-time', action='store_true', help='Create Time DNS on the current shared app gateway.')
    a = p.parse_args()
    if a.ensure_time and a.switch_to:
        p.error('Provision Time separately from switching the gateway.')
    account = secret('systems-cluster-cloudflare-account')
    token = secret('systems-cluster-cloudflare-token')
    headers = {'Content-Type': 'application/json'}
    if '@' in account:
        headers.update({'X-Auth-Email': account, 'X-Auth-Key': token})
    else:
        headers['Authorization'] = 'Bearer ' + token
    def api(path, body=None, method=None):
        request = urllib.request.Request('https://api.cloudflare.com/client/v4/' + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method or ('PATCH' if body is not None else 'GET'))
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
    path = 'zones/' + zone['id'] + '/dns_records'
    existing = api(path + '?name=time.freddie.xyz')
    if existing and (len(existing) != 1 or existing[0]['type'] != 'A' or existing[0]['content'] not in ('100.66.233.125', '100.91.90.6') or existing[0]['proxied']):
        raise RuntimeError('Unexpected Time DNS record; inspect before changing')
    if a.ensure_time:
        if len({r['content'] for r in records}) != 1:
            raise RuntimeError('Health and Trends must use the same gateway before adding Time')
        if existing and existing[0]['content'] != records[0]['content']:
            raise RuntimeError('Time uses a different gateway; inspect before changing')
        if not existing:
            existing = [api(path, {'type': 'A', 'name': 'time.freddie.xyz', 'content': records[0]['content'], 'ttl': 1, 'proxied': False}, method='POST')]
    records += existing
    for record in records:
        if a.switch_to and record['content'] != a.switch_to:
            record = api('zones/' + zone['id'] + '/dns_records/' + record['id'], {'content': a.switch_to, 'proxied': False})
        print(json.dumps({key: record[key] for key in ('name', 'type', 'content', 'ttl', 'proxied')}))


if __name__ == '__main__':
    main()
