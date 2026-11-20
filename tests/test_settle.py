#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""settle: the kunta and settlement of a register address, rule by rule, on a made-up tree."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tools' / 'addr'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import addr_links
import addre_io
import clarion_format
import frames
import settle
from test_addr_country import icode

V5 = clarion_format.X08_02
BOUNDS = (4800, 1280, -1200, 4080)
TREE = {
    'LAHTI': ((6, 1, 1), {'LAHTI': 0, 'JALKARANTA': 1}),
    'NASTOLA': ((6, 1, 2), {'NASTOLA': 0, 'UUSIKYLÄ': 1}),          # merged into Lahti in 2016
    'TAMMERFORS': ((6, 1, 3), {'TAMMERFORS': 0, 'HERVANTA': 1}),   # the tree spells it in Swedish
}
CITY, SUBURB, MARKER = (60.98, 25.66), (61.00, 25.60), (60.99, 25.70)
RECORDS = [
    ('LAHTI', 'KAUPPAKATU', 0, *CITY),
    ('LAHTI', 'SALPAUSSELÄNTIE', 0, *CITY),
    ('LAHTI', 'SALPAUSSELÄNTIE', 1, *SUBURB),
    ('LAHTI', 'JALKARANNANTIE', 1, *SUBURB),
    ('LAHTI', 'METSÄTIE', 2, *MARKER),            # only under the no-settlement marker
    ('LAHTI', 'VANHATIE', 1, None, None),         # no parcel position
    ('NASTOLA', 'ASEMATIE', 0, 60.95, 25.93),
    ('NASTOLA', 'KYLÄTIE', 1, 60.97, 26.05),
    ('TAMMERFORS', 'HÄMEENKATU', 0, 61.50, 23.76),
    ('TAMMERFORS', 'HERVANNAN VALTAVÄYLÄ', 1, 61.45, 23.85),
]
MUNICIPALITIES = {398: ['Lahti', 'Lahti', 'Lahtis'], 837: ['Tampere', 'Tampere', 'Tammerfors']}


def row(street, svf, lat, lon, swe=''):
    return dict(street=street, street_swe=swe, svf=svf, lat=lat, lon=lon)


class Rules(unittest.TestCase):
    def setUp(self):
        self.fold = settle.folder(V5)
        self.S = settle.Settler(TREE, RECORDS, MUNICIPALITIES, self.fold)

    def assign(self, *rows):
        out, home = self.S.assign(list(rows))
        return out, home

    def test_kunta_by_number_on_any_of_its_names(self):
        self.assertEqual(self.S.kunta_of, {398: 'LAHTI', 837: 'TAMMERFORS'})
        self.assertEqual(self.S.merged, ['NASTOLA'])

    def test_one_factory_settlement_wins_over_position(self):
        out, _ = self.assign(row('Hämeenkatu', '837', 61.45, 23.85))
        self.assertEqual(out, [('TAMMERFORS', 0, 'a: municipality number', 'b: one factory settlement')])

    def test_several_settlements_take_the_nearest_record_of_the_name(self):
        out, _ = self.assign(row('Salpausseläntie', '398', 61.001, 25.601), row('Salpausseläntie', '398', 60.979, 25.66))
        self.assertEqual([(k, s, h) for k, s, _, h in out],
                         [('LAHTI', 1, 'c: nearest same-name record'), ('LAHTI', 0, 'c: nearest same-name record')])

    def test_a_new_street_takes_the_nearest_record(self):
        out, _ = self.assign(row('Uusitie', '398', 61.0, 25.61), row('Uusitie', '398', 60.99, 25.69))
        # the marker record is nearer to the second, but d only picks a real settlement
        self.assertEqual([(s, h) for _, s, _, h in out], [(1, 'd: nearest factory record'), (0, 'd: nearest factory record')])

    def test_merged_kunta_by_position(self):
        out, home = self.assign(row('Uusitie', '398', 60.97, 26.04), row('Kauppakatu', '398', 60.981, 25.661))
        self.assertEqual(home, {'NASTOLA': 398})
        self.assertEqual(out, [('NASTOLA', 1, 'a: merged kunta by position', 'd: nearest factory record'),
                               ('LAHTI', 0, 'a: municipality number', 'b: one factory settlement')])

    def test_a_merged_kunta_far_from_every_row_has_no_home(self):
        # rows of Tampere alone, 130 km from Nastola
        out, home = self.assign(row('Hämeenkatu', '837', 61.5, 23.76))
        self.assertEqual((home, out[0][:3]), ({}, ('TAMMERFORS', 0, 'a: municipality number')))

    def test_marker_and_unlocated_records_count_where_the_name_is(self):
        out, _ = self.assign(row('Metsätie', '398', 61.0, 25.6), row('Vanhatie', '398', 61.0, 25.6))
        self.assertEqual([s for _, s, _, _ in out], [2, 1])

    def test_swedish_name_finds_the_street(self):
        out, _ = self.assign(row('Hervantavägen', '837', 61.5, 23.76, swe='Hervannan valtaväylä'))
        self.assertEqual(out[0][1], 1)

    def test_no_number_goes_by_position_alone(self):
        out, _ = self.assign(row('Asematie', '', 60.951, 25.931))
        self.assertEqual(out, [('NASTOLA', 0, 'a: kunta by position only', 'b: one factory settlement')])

    def test_counts(self):
        rows = [row('Hämeenkatu', '837', 61.5, 23.76), row('Hämeenkatu', '837', 61.5, 23.77), row('Uusitie', '398', 61.0, 25.6)]
        out, _ = self.S.assign(rows)
        self.assertEqual(settle.counts(rows, out, self.fold),
                         {'a: municipality number': (3, 2), 'b: one factory settlement': (2, 1),
                          'd: nearest factory record': (1, 1)})


class Nested(unittest.TestCase):
    """The factory lists a village's links under the town around it as well."""
    RECORDS = [
        ('LAHTI', 'KYLÄTIE', 1, *SUBURB, 'k1'), ('LAHTI', 'KYLÄTIE', 0, *SUBURB, 'k1'),     # one link, under both
        ('LAHTI', 'KYLÄTIE', 0, *CITY, 'k2'),
        ('LAHTI', 'RANTATIE', 1, *SUBURB, 'r1'), ('LAHTI', 'RANTATIE', 0, *SUBURB, 'r1'),
        ('LAHTI', 'KAUPPAKATU', 0, *CITY, 'c1'),
    ]

    def extra(self, *rows):
        S = settle.Settler(TREE, self.RECORDS, MUNICIPALITIES, settle.folder(V5))
        out, _ = S.assign(list(rows))
        return [(s, more) for (_, s, _, _), more in zip(out, S.extra)]

    def test_a_street_the_factory_lists_under_both(self):
        self.assertEqual(self.extra(row('Kylätie', '398', 61.001, 25.601), row('Kylätie', '398', 60.981, 25.661)),
                         [(0, (1,)), (0, ())])

    def test_a_new_street_follows_the_links_around_it(self):
        self.assertEqual(self.extra(row('Uusitie', '398', 61.001, 25.601), row('Uusitie', '398', 60.981, 25.661)),
                         [(0, (1,)), (0, ())])

    def test_a_street_in_one_settlement_stays_there(self):
        self.assertEqual(self.extra(row('Kauppakatu', '398', 61.001, 25.601)), [(0, ())])

    def test_records_without_links_add_nothing(self):
        S = settle.Settler(TREE, RECORDS, MUNICIPALITIES, settle.folder(V5))
        S.assign([row('Salpausseläntie', '398', 61.001, 25.601)])
        self.assertEqual(S.extra, [()])


class FromTheCard(unittest.TestCase):
    def test_area_tree(self):
        tree, labels = settle.area_tree(icode(), 246, 'latin-1')
        self.assertEqual(tree, {'JÄMSÄ': ((1, 1, 0), {}), 'JYVÄSKYLÄ': ((1, 1, 1), {'HAAPANIEMI': 0, 'JYVÄSKYLÄ': 1, 'PALOKKA': 2})})
        self.assertEqual(len(labels), 4)

    def test_factory_records_read_back(self):
        lat, lon = 62.24, 25.753
        la, lo = addre_io.parcel_words(lat, lon)
        ents = [dict(name='KAUPPAKATU', nums=[], ladder=(6, 1, 1, 2), link=dict(la=la, lo=lo, refs=addre_io.street_refs(10, 20))),
                dict(name='KOULUTIE', nums=[], ladder=(6, 1, 1, 3), link=dict(la=la, lo=lo, refs=addre_io.street_refs(10, 20))),
                dict(name='MUU', nums=[], ladder=(6, 1, 9, 0), link=dict(la=la, lo=lo, refs=addre_io.street_refs(10, 20)))]
        out, _, _ = addr_links.addre_mixed(ents, lambda e: e['ladder'], addr_links.word_keys,
                                           frames.addre_prefix(BOUNDS, '14/04/12/01', V5), frames.addre_df(V5),
                                           6, 3, fmt=V5)
        tree = {'JYVÄSKYLÄ': ((6, 1, 1), {'A': 0, 'B': 1, 'C': 2})}
        got = settle.factory_records(addre_io.Addre(out), V5, tree)
        self.assertEqual([g[:3] for g in got], [('JYVÄSKYLÄ', 'KAUPPAKATU', 2), ('JYVÄSKYLÄ', 'KOULUTIE', 3)])
        self.assertEqual({g[5] for g in got}, {(la, lo, ((2, 0x40, 10), (3, 0x40, 20)))})
        for g in got:     # the parcel centre, within half a parcel of the point
            self.assertLess(abs(g[3] - lat), 1 / 96)
            self.assertLess(abs(g[4] - lon), 1 / 64)


if __name__ == '__main__':
    unittest.main()
