"""Failure-boundary tests for the offline map audit."""
import tempfile
import unittest
from pathlib import Path
import struct
from map_audit import chunks, inventory


class MapAuditTests(unittest.TestCase):
    def test_truncated_or_non_wdt_input(self):
        for data in (b'', b'<html>error</html>', b'REVM',
                     b'REVM' + struct.pack('<I', 4) + b'\x12'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                chunks(data)

    def test_chunk_bounds(self):
        data = b'REVM' + struct.pack('<II', 4, 18)
        self.assertEqual(chunks(data), [{'tag': 'MVER', 'bytes': 4}])
        with self.assertRaises(ValueError):
            chunks(data + b'x')

    def test_explicit_map_and_wdt_crosscheck(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            table, names = root / 'Map.csv', root / 'list.csv'
            table.write_text('ID,Directory,WdtFileDataID\n2680,2680,10\n2836,2836,20\n')
            names.write_text('10;world/maps/2680/2680.wdt\n11;world/maps/2680/a.adt\n20;world/maps/2836/2836.wdt\n')
            result = inventory(table, names, 2680)
            self.assertEqual([item['id'] for item in result['files']], [10, 11])
            with self.assertRaises(ValueError):
                inventory(table, names, 999)
            names.write_text('12;world/maps/2680/2680.wdt\n')
            with self.assertRaises(ValueError):
                inventory(table, names, 2680)


if __name__ == '__main__':
    unittest.main()
