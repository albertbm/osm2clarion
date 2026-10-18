#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Byte-for-byte reproduction against a base card.

The claim in docs/BUILD-LOG.md is that the chain reproduces a shipped database exactly:
every tile rebuilds byte-identically from its parsed form, every record rebuilds byte-exact,
make_mmain.py reproduces MMAIN001.DAT and make_drstr_card.py reproduces DRSTR001.DAT. This
checks that against a card.

The card holds licensed commercial data and cannot be distributed with the repository, so
there is nothing to check against unless you have one. Point the tests at it:

    OSM2CLARION_CARD=/Volumes/GEURX-30-105-100 python3 -m unittest discover -s tests

Without that variable every case here skips. Nothing off the card is written anywhere: the
expected values in tests/fixtures/reproduce.json are digests and byte counts, recorded once
with

    python3 tests/test_reproduce.py --record /path/to/card

The card is only read. Mount it read-only.
"""
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
EXPECTED = FIXTURES / 'reproduce.json'
sys.path.insert(0, str(ROOT / 'tools'))

import clarion_bg
import clarion_tile
import tile_retarget

CARD_ENV = 'OSM2CLARION_CARD'

# A full tile reaches 477 MB and the whole surface is 4.8 GiB, so the rebuild checks work
# through the smallest tiles until this much has been read. Raise it for a longer run.
READ_BUDGET = 64 << 20

SKIP = (
    'set %s to a mounted base card or an unpacked image to run this. The card holds a '
    'licensed commercial navigation database, which this repository cannot distribute, so '
    'there is nothing here to check against. It needs MAPAL001/ with the 52 tiles and '
    'LEV3456R.DAT, MMAIN001.DAT, DRSTR001.DAT and VER_INFO/DB_INFO.TXT, mounted read-only.'
) % CARD_ENV


def digest(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def header_strings(header):
    """The UTF-16BE fields of a tile or master header, which are 0xFF padded."""
    out = []
    for run in header.split(b'\xff'):
        if len(run) < 4 or len(run) % 2:
            continue
        try:
            text = run.decode('utf-16-be')
        except UnicodeDecodeError:
            continue
        text = text.strip('\0').strip()
        if text and all(32 <= ord(c) < 127 for c in text):
            out.append(text)
    return out


def card_identity(root):
    """What the card says it is: the data version, the volume, the database and the NAVTEQ
    quarter, read off a tile header and VER_INFO/DB_INFO.TXT."""
    tiles = sorted(Path(root, 'MAPAL001').glob('B*.DAT'))
    header = b''
    if tiles:
        with tiles[0].open('rb') as f:
            header = f.read(0x400)
    found = {}
    for text in header_strings(header):
        if text.startswith('DATA VERSION'):
            found['data_version'] = text.split(None, 2)[2]
        elif text.startswith('NAVTEQ'):
            found['navteq'] = text.split(None, 1)[1]
    info = Path(root, 'VER_INFO', 'DB_INFO.TXT')
    if info.exists():
        found['db_info'] = [l.strip() for l in info.read_text(errors='replace').splitlines()]
    return found


def load_expected():
    return json.loads(EXPECTED.read_text())


def tiles_within_budget(root, budget=READ_BUDGET):
    """The smallest tiles whose sizes together stay inside the budget."""
    paths = sorted(Path(root, 'MAPAL001').glob('B*.DAT'), key=lambda p: p.stat().st_size)
    out, total = [], 0
    for p in paths:
        size = p.stat().st_size
        if out and total + size > budget:
            break
        out.append(p)
        total += size
    return out


class CardCase(unittest.TestCase):
    """Common setup: find the card, or skip."""

    @classmethod
    def setUpClass(cls):
        where = os.environ.get(CARD_ENV)
        if not where:
            raise unittest.SkipTest(SKIP)
        cls.root = Path(where)
        if not cls.root.is_dir():
            raise unittest.SkipTest('%s is not a directory: %s' % (CARD_ENV, where))
        if not (cls.root / 'MAPAL001').is_dir():
            raise unittest.SkipTest('%s has no MAPAL001 directory: %s' % (CARD_ENV, where))
        cls.expected = load_expected()
        cls.identity = card_identity(cls.root)
        cls.label = None
        for name, rec in cls.expected['databases'].items():
            if rec['data_version'] == cls.identity.get('data_version'):
                cls.label = name
        cls.recorded = cls.expected['databases'].get(cls.label, {}).get('files', {}) if cls.label else {}


class TestCardIdentity(CardCase):

    def test_card_states_a_known_database(self):
        """FORMAT.md lists both: data version 14/11/17/01 and 17/09/01/01."""
        self.assertIn('data_version', self.identity, 'no DATA VERSION string in the tile header')
        known = {r['data_version'] for r in self.expected['databases'].values()}
        self.assertIn(self.identity['data_version'], known,
                      'unknown database %r; add it to %s' % (self.identity['data_version'], EXPECTED.name))
        self.assertIsNotNone(self.label)

    def test_navteq_quarter_agrees_with_the_data_version(self):
        if 'navteq' not in self.identity:
            self.skipTest('the tile header carries no NAVTEQ string')
        self.assertEqual(self.identity['navteq'], self.expected['databases'][self.label]['navteq'])

    def test_db_info_line_two(self):
        """The unit checks line 2 of DB_INFO.TXT and wants 0100."""
        if 'db_info' not in self.identity:
            self.skipTest('no VER_INFO/DB_INFO.TXT on the card')
        self.assertGreaterEqual(len(self.identity['db_info']), 2)
        self.assertEqual(self.identity['db_info'][1], '0100')

    def test_fifty_two_tiles_plus_the_coarse_file(self):
        mapal = self.root / 'MAPAL001'
        self.assertEqual(len(list(mapal.glob('B*.DAT'))), 52)
        self.assertTrue((mapal / 'LEV3456R.DAT').exists())


class TestRecordedDigests(CardCase):
    """Compare the card against the digests in tests/fixtures/reproduce.json."""

    def test_recorded_files_match(self):
        if not self.recorded:
            self.skipTest('no digests recorded for the %s database. docs/BUILD-LOG.md states '
                          'the reproduction in prose and quotes none, so %s ships with them '
                          'empty. Record them with: python3 tests/test_reproduce.py --record %s'
                          % (self.label, EXPECTED.name, self.root))
        missing = []
        for rel, want in sorted(self.recorded.items()):
            path = self.root / rel
            if not path.exists():
                missing.append(rel)
                continue
            with self.subTest(file=rel):
                self.assertEqual(path.stat().st_size, want['bytes'])
                self.assertEqual(digest(path), want['sha256'])
        self.assertEqual(missing, [], 'recorded files absent from the card')

    def test_index_size(self):
        want = self.expected['databases'][self.label].get('drstr_bytes')
        index = self.root / 'DRSTR001.DAT'
        if want is None or not index.exists():
            self.skipTest('no DRSTR001.DAT, or no recorded size')
        self.assertEqual(index.stat().st_size, want)


class TestTilesRebuild(CardCase):
    """read_tile then write_tile has to give the shipped bytes back."""

    def test_tiles_rebuild_byte_identically(self):
        paths = tiles_within_budget(self.root)
        self.assertGreater(len(paths), 0, 'no tiles found')
        for path in paths:
            with self.subTest(tile=path.name):
                raw = path.read_bytes()
                rebuilt = clarion_tile.write_tile(clarion_tile.read_tile(raw))
                self.assertEqual(len(rebuilt), len(raw))
                self.assertEqual(rebuilt, raw)

    def test_coarse_file_rebuilds_byte_identically(self):
        path = self.root / 'MAPAL001' / 'LEV3456R.DAT'
        if not path.exists():
            self.skipTest('no LEV3456R.DAT')
        raw = path.read_bytes()
        self.assertEqual(clarion_tile.write_tile(clarion_tile.read_tile(raw)), raw)


class TestRecordsRebuild(CardCase):
    """Every zlib background record has to rebuild byte-exact, and every parcel code has to
    verify against its directory's mesh rather than the directory-local row and column."""

    def records(self, limit=None):
        for path in tiles_within_budget(self.root, budget=8 << 20):
            t = clarion_tile.read_tile(path.read_bytes())
            for sec in t['sections']:
                for L in sec['levels']:
                    for D in L['dirs']:
                        for row, slot in D['slots'].items():
                            for r in slot['recs']:
                                if r['raw'][:2] != b'\x58\x85':
                                    continue
                                plain = zlib.decompress(r['raw'])
                                yield path.name, L['level'], D, row, r['k'], plain
                                if limit is not None:
                                    limit -= 1
                                    if limit <= 0:
                                        return

    def test_records_rebuild_byte_exact(self):
        ok = skipped = 0
        for name, level, D, row, k, plain in self.records():
            if struct.unpack_from('>H', plain, 4)[0] != 0x0019:
                skipped += 1          # shape and graph records are not background records
                continue
            rec = clarion_bg.parse_record(plain)
            self.assertEqual(clarion_bg.build_record(rec), plain,
                             '%s level %d row %d pair %d' % (name, level, row, k))
            ok += 1
        self.assertGreater(ok, 0, 'no background records found')

    def test_parcel_codes_verify_against_the_directory_mesh(self):
        ok = 0
        for name, level, D, row, k, plain in self.records():
            if struct.unpack_from('>H', plain, 4)[0] != 0x0019 or level not in clarion_bg.LEVELS:
                continue
            L = clarion_bg.LEVELS[level]
            per_row, per_col = L['rows'] // L['dir_meshes'], L['cols'] // L['dir_meshes']
            col = k // 2
            want = clarion_bg.parcel_code(level, D['y'] * per_row + row, D['x'] * per_col + col)
            self.assertEqual(struct.unpack_from('>HH', plain, 0), want,
                             '%s level %d mesh (%d, %d) row %d col %d' % (name, level, D['y'], D['x'], row, col))
            ok += 1
        self.assertGreater(ok, 0)


class TestMasterFile(CardCase):
    """make_mmain.py sums every tile's level table plus LEV3456R.DAT's and has to land on
    the shipped MMAIN001.DAT."""

    def test_reproduces_mmain(self):
        src = self.root / 'MMAIN001.DAT'
        lev = self.root / 'MAPAL001' / 'LEV3456R.DAT'
        if not src.exists() or not lev.exists():
            self.skipTest('needs MMAIN001.DAT and MAPAL001/LEV3456R.DAT')
        tiles = sorted(Path(self.root, 'MAPAL001').glob('B*.DAT'))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, 'MMAIN001.DAT')
            cmd = [sys.executable, str(ROOT / 'tools' / 'make_mmain.py'), str(src), str(out)]
            cmd += [str(p) for p in tiles] + ['--lev', str(lev)]
            run = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(out.read_bytes(), src.read_bytes(), run.stdout)


class TestDirectoryIndex(CardCase):
    """make_drstr_card.py given '-' as the tile folder takes the order from the card's own
    index and has to reproduce it byte for byte. That is the control for any index change."""

    def test_reproduces_drstr(self):
        src = self.root / 'DRSTR001.DAT'
        if not src.exists():
            self.skipTest('no DRSTR001.DAT on the card')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, 'DRSTR001.DAT')
            run = subprocess.run([sys.executable, str(ROOT / 'tools' / 'make_drstr_card.py'),
                                  str(self.root), '-', str(out)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(out.read_bytes(), src.read_bytes(), run.stdout)


class TestRoadHeaders(CardCase):
    """rd_check's header rule holds on the shipped section-2 records."""

    def test_header_rule_on_shipped_records(self):
        import rd_check
        tiles = tiles_within_budget(self.root)
        sound = checked = 0
        for p in tiles:
            tally, _, bad = rd_check.check_tile(str(p))
            for lev in (1, 2):
                ok, no = (tally[(lev, 'header where the body is sound', b)] for b in (True, False))
                self.assertEqual(no, 0, '%s level %d: %s' % (p.name, lev, bad[:3]))
                sound += ok
                checked += tally[(lev, 'body', True)] + tally[(lev, 'body', False)]
        self.assertGreater(sound, 0)
        self.assertGreater(sound / checked, 0.99, 'too many records the reader cannot parse')


class TestRetarget(CardCase):
    """The two databases differ in six header bytes: the data version and the NAVTEQ
    quarter. tile_retarget.py substitutes them."""

    def test_only_the_six_documented_bytes_change(self):
        if self.label != '2017':
            self.skipTest('the substitution runs from the 2017 template; this card is %s' % self.label)
        path = tiles_within_budget(self.root)[0]
        raw = path.read_bytes()
        out = tile_retarget.convert(raw, '2014')
        self.assertEqual(len(out), len(raw))
        changed = {i for i in range(len(raw)) if raw[i] != out[i]}
        self.assertEqual(changed, set(tile_retarget.DATABASES['2014']))
        self.assertEqual(len(changed), 6)
        other = self.expected['databases']['2014']
        strings = header_strings(out[:0x400])
        self.assertIn('DATA VERSION ' + other['data_version'], strings)

    def test_template_is_a_copy(self):
        path = tiles_within_budget(self.root)[0]
        raw = path.read_bytes()
        self.assertEqual(tile_retarget.convert(raw, tile_retarget.TEMPLATE), raw)


def record(root):
    """Write the digests for one card into tests/fixtures/reproduce.json."""
    root = Path(root)
    identity = card_identity(root)
    expected = load_expected()
    label = next((n for n, r in expected['databases'].items()
                  if r['data_version'] == identity.get('data_version')), None)
    if label is None:
        raise SystemExit('the card reports data version %r, which is not in %s'
                         % (identity.get('data_version'), EXPECTED))
    wanted = ['MMAIN001.DAT', 'DRSTR001.DAT', 'MAPAL001/LEV3456R.DAT']
    wanted += ['MAPAL001/' + p.name for p in sorted(Path(root, 'MAPAL001').glob('B*.DAT'))]
    files = {}
    for rel in wanted:
        path = root / rel
        if not path.exists():
            print('absent, skipped:', rel)
            continue
        files[rel] = dict(bytes=path.stat().st_size, sha256=digest(path))
        print('%-28s %12d  %s' % (rel, files[rel]['bytes'], files[rel]['sha256']))
    expected['databases'][label]['files'] = files
    EXPECTED.write_text(json.dumps(expected, indent=2) + '\n')
    print('recorded %d files for the %s database in %s' % (len(files), label, EXPECTED))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--record':
        record(sys.argv[2])
    else:
        unittest.main()
