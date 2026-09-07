#!/usr/bin/env python3
"""Create a consistent, owner-only SQLite backup and validate its integrity."""
import argparse
from pathlib import Path
import sqlite3

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('source', type=Path)
p.add_argument('destination', type=Path)
a = p.parse_args()
a.destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
# Refuse replacement: each final migration must retain its original snapshot.
fd = a.destination.open('xb')
fd.close()
a.destination.chmod(0o600)
with sqlite3.connect(a.source.resolve().as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(a.destination) as target:
    source.backup(target)
    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or target.execute('PRAGMA foreign_key_check').fetchall():
        raise SystemExit('Snapshot integrity check failed')
print('Verified snapshot: ' + str(a.destination))
