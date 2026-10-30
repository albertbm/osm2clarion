#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Naming of unnamed *_link ways (--ramp-names). Synthetic ways, no PBF needed."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))

import osm_roads


def way(id, hw, nodes, name=None, dir=0):
    return dict(id=id, hw=hw, name=name, ref=None, dir=dir, nodes=nodes)


class TestRampNames(unittest.TestCase):
    def test_own_name_is_kept(self):
        ramp = way(1, 'motorway_link', [(0, 0), (1, 0)], name='Slip Road', dir=1)
        road = way(2, 'motorway', [(1, 0), (2, 0)], name='M1')
        ways = [ramp, road]
        osm_roads.name_ramps(ways)
        self.assertEqual(ways[0]['name'], 'Slip Road')

    def test_leads_to_the_named_road_ahead(self):
        ramp = way(1, 'primary_link', [(0, 0), (1, 0)], dir=1)
        road = way(2, 'primary', [(1, 0), (2, 0)], name='Vesturlandsvegur')
        ways = [ramp, road]
        osm_roads.name_ramps(ways)
        self.assertEqual(ways[0]['name'], 'Vesturlandsvegur')

    def test_chained_links_before_the_named_road(self):
        a = way(1, 'secondary_link', [(0, 0), (1, 0)], dir=1)
        b = way(2, 'secondary_link', [(1, 0), (2, 0)], dir=1)
        road = way(3, 'secondary', [(2, 0), (3, 0)], name='Víkurvegur')
        ways = [a, b, road]
        osm_roads.name_ramps(ways)
        self.assertEqual(ways[0]['name'], 'Víkurvegur')
        self.assertEqual(ways[1]['name'], 'Víkurvegur')

    def test_trails_from_when_nothing_lies_ahead(self):
        # dir=1: the ramp leads nowhere named, so it falls back to the road behind it
        trail = way(1, 'residential', [(-1, 0), (0, 0)], name='Þúsöld')
        ramp = way(2, 'tertiary_link', [(0, 0), (1, 0)], dir=1)
        ways = [trail, ramp]
        osm_roads.name_ramps(ways)
        self.assertEqual(ways[1]['name'], 'Þúsöld')

    def test_oneway_minus_one_reverses_the_leads_to_end(self):
        ramp = way(1, 'trunk_link', [(0, 0), (1, 0)], dir=-1)
        road = way(2, 'trunk', [(0, 0), (-1, 0)], name='Vesturlandsvegur')
        ways = [ramp, road]
        osm_roads.name_ramps(ways)
        self.assertEqual(ways[0]['name'], 'Vesturlandsvegur')

    def test_two_way_link_tries_both_ends(self):
        ramp = way(1, 'tertiary_link', [(0, 0), (1, 0)], dir=0)
        behind = way(2, 'residential', [(-1, 0), (0, 0)], name='Back Street')
        ahead = way(3, 'primary', [(1, 0), (2, 0)], name='Front Road')
        ways = [behind, ramp, ahead]
        osm_roads.name_ramps(ways)
        self.assertEqual(ways[1]['name'], 'Front Road', 'primary outranks residential')

    def test_junction_tie_breaks_by_class_then_frequency(self):
        ramp = way(1, 'tertiary_link', [(0, 0), (1, 0)], dir=1)
        minor_a = way(2, 'residential', [(1, 0), (2, 0)], name='Þúsöld')
        minor_b = way(3, 'residential', [(1, 0), (2, 1)], name='Þúsöld')
        major = way(4, 'secondary', [(1, 0), (2, -1)], name='Víkurvegur')
        ways = [ramp, minor_a, minor_b, major]
        osm_roads.name_ramps(ways)
        self.assertEqual(ways[0]['name'], 'Víkurvegur', 'higher class wins over a repeated name')

    def test_destination_tag_is_never_used(self):
        ramp = way(1, 'motorway_link', [(0, 0), (1, 0)], dir=1)
        ramp['destination'] = 'Akureyri;Akranes'
        ways = [ramp]
        osm_roads.name_ramps(ways)
        self.assertNotIn(ways[0].get('name'), ('Akureyri;Akranes', 'Akureyri', 'Akranes'))
        self.assertIsNone(ways[0]['name'])

    def test_unreachable_link_stays_unnamed(self):
        ramp = way(1, 'motorway_link', [(0, 0), (1, 0)], dir=1)
        ways = [ramp]
        osm_roads.name_ramps(ways)
        self.assertIsNone(ways[0]['name'])

    def test_link_entered_against_its_flow_is_not_followed(self):
        # S flows foo -> P (dir=1); R also ends at P. Driving R to P and on into S would
        # mean driving S backward, which its oneway forbids, so R must stay unnamed.
        foo = way(1, 'secondary', [(-2, 0), (-1, 0)], name='Foo')
        s = way(2, 'secondary_link', [(-1, 0), (0, 0)], dir=1)
        ramp = way(3, 'primary_link', [(1, 0), (0, 0)], dir=1)
        ways = [foo, s, ramp]
        osm_roads.name_ramps(ways)
        self.assertIsNone(ways[2]['name'])

    def test_named_road_arriving_is_not_taken_as_leads_to(self):
        # Bar is oneway and terminates at P; R cannot drive away from P onto Bar.
        bar = way(1, 'primary', [(-1, 0), (0, 0)], name='Bar', dir=1)
        ramp = way(2, 'secondary_link', [(1, 0), (0, 0)], dir=1)
        ways = [bar, ramp]
        osm_roads.name_ramps(ways)
        self.assertIsNone(ways[1]['name'])

    def test_trails_from_skips_a_oneway_leaving_the_entry(self):
        # trail's oneway leaves (0, 0), so nothing could have driven from it into the ramp.
        trail = way(1, 'residential', [(0, 0), (-1, 0)], name='Leaving', dir=1)
        ramp = way(2, 'tertiary_link', [(0, 0), (1, 0)], dir=1)
        ways = [trail, ramp]
        osm_roads.name_ramps(ways)
        self.assertIsNone(ways[1]['name'])

    def test_trails_from_takes_a_oneway_arriving_at_the_entry(self):
        # trail's oneway arrives at (0, 0), so it could have driven straight into the ramp.
        trail = way(1, 'residential', [(-1, 0), (0, 0)], name='Arriving', dir=1)
        ramp = way(2, 'tertiary_link', [(0, 0), (1, 0)], dir=1)
        ways = [trail, ramp]
        osm_roads.name_ramps(ways)
        self.assertEqual(ways[1]['name'], 'Arriving')

    def test_a_loop_does_not_hang(self):
        a = way(1, 'motorway_link', [(0, 0), (1, 0)], dir=0)
        b = way(2, 'motorway_link', [(1, 0), (0, 0)], dir=0)
        ways = [a, b]
        osm_roads.name_ramps(ways)  # should return, not recurse forever
        self.assertIsNone(ways[0]['name'])
        self.assertIsNone(ways[1]['name'])


if __name__ == '__main__':
    unittest.main()
