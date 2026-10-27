#!/usr/bin/env python3
"""Inventory a retail map's named files and inspect a downloaded WDT (no conversion)."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import struct


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def chunks(data):
    result = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError('truncated chunk header')
        tag, size = struct.unpack_from('<4sI', data, offset)
        end = offset + 8 + size
        if end > len(data):
            raise ValueError('truncated chunk payload')
        result.append({'tag': tag[::-1].decode('ascii'), 'bytes': size})
        offset = end
    if not result or result[0] != {'tag': 'MVER', 'bytes': 4}:
        raise ValueError('not a versioned WDT chunk stream')
    return result


def inventory(map_csv, listfile, map_id, wdt=None):
    with map_csv.open(encoding='utf-8-sig', newline='') as stream:
        rows = [row for row in csv.DictReader(stream) if row['ID'] == str(map_id)]
    if len(rows) != 1:
        raise ValueError('map ID must match exactly one Map.csv row')
    row = rows[0]
    directory = row['Directory'].replace('\\', '/').lower()
    if not directory or '/' in directory or directory in ('.', '..'):
        raise ValueError('invalid map directory')
    prefix = 'world/maps/' + directory + '/'
    files = {}
    with listfile.open(encoding='utf-8-sig', newline='') as stream:
        for number, fields in enumerate(csv.reader(stream, delimiter=';'), 1):
            if not fields:
                continue
            if len(fields) != 2:
                raise ValueError('invalid listfile row %d' % number)
            file_id, name = fields
            name = name.replace('\\', '/').lower()
            if not name.startswith(prefix):
                continue
            file_id = int(file_id)
            if file_id <= 0:
                raise ValueError('file IDs must be positive')
            if file_id in files and files[file_id] != name:
                raise ValueError('conflicting paths for file ID %d' % file_id)
            files[file_id] = name
    wdt_id = int(row['WdtFileDataID'])
    if files.get(wdt_id) != prefix + directory + '.wdt':
        raise ValueError('Map.csv WDT ID does not match the listfile')
    result = {
        'schema': 1, 'map': row,
        'inputs': {'map_csv_sha256': digest(map_csv), 'listfile_sha256': digest(listfile)},
        'files': [{'id': key, 'path': files[key]} for key in sorted(files)],
        'limits': ['Named map-directory files only; not a recursive dependency closure.',
                   'Metadata and listfile do not prove availability in one retail build.',
                   'No geometry conversion or 3.3.5 compatibility is established.'],
    }
    if wdt:
        data = wdt.read_bytes()
        result['wdt'] = {'sha256': digest(wdt), 'bytes': len(data), 'chunks': chunks(data),
                         'identity': 'Caller-supplied bytes; build and file ID not authenticated.'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map-csv', type=Path, required=True)
    parser.add_argument('--listfile', type=Path, required=True)
    parser.add_argument('--map-id', type=int, required=True)
    parser.add_argument('--wdt', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = inventory(args.map_csv, args.listfile, args.map_id, args.wdt)
    except (ValueError, KeyError, OSError, UnicodeError) as error:
        parser.exit(1, 'map audit: %s\n' % error)
    args.out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
