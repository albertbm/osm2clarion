#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ferry ways kept by osm_roads and encoded as graph class 10. No card or OSM extract needed."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
import fa_encode
import osm_roads
import rd_encode


class FerrySelection(unittest.TestCase):
    def test_route_ferry_open_to_cars_is_kept(self):
        self.assertEqual(osm_roads.ferry_hw({'route': 'ferry', 'motor_vehicle': 'yes'}), 'ferry')
        self.assertEqual(osm_roads.ferry_hw({'route': 'ferry', 'motorcar': 'yes'}), 'ferry')
        self.assertIsNone(osm_roads.keep({'route': 'ferry', 'motor_vehicle': 'yes'}, 'ferry'))

    def test_route_ferry_without_motor_vehicle_is_dropped(self):
        # Saefari-Grimsey (w27414062): vehicle=yes but no motor_vehicle/motorcar tag
        self.assertIsNone(osm_roads.ferry_hw({'route': 'ferry', 'vehicle': 'yes'}))

    def test_route_ferry_closed_to_cars_is_dropped(self):
        self.assertIsNone(osm_roads.ferry_hw({'route': 'ferry', 'motor_vehicle': 'no'}))

    def test_seasonal_access_conditional_does_not_block_selection(self):
        # w479138550, Thorlakshofn winter route: access:conditional=no @ (Jun-Sep); the plain
        # motor_vehicle=yes tag still holds, and keep() doesn't read access:conditional
        tags = {'route': 'ferry', 'motor_vehicle': 'yes', 'access:conditional': 'no @ (Jun - Sep)'}
        self.assertEqual(osm_roads.ferry_hw(tags), 'ferry')
        self.assertIsNone(osm_roads.keep(tags, 'ferry'))

    def test_non_ferry_way_unaffected(self):
        self.assertIsNone(osm_roads.ferry_hw({'highway': 'primary'}))


class FerryEncoding(unittest.TestCase):
    def test_class_word_and_pair_slot(self):
        self.assertEqual(rd_encode.class_word(10), 0x0a8a)
        self.assertEqual(rd_encode.pair_slot(10), 10)           # 10 and up share the last pair slot, "u24"

    def test_layer_class_is_layer_zero_at_both_levels(self):
        self.assertEqual(fa_encode.layer_class('ferry', 1), (0, 10))
        self.assertEqual(fa_encode.layer_class('ferry', 2), (0, 10))

    def test_ferry_speed_reproduces_factory_ratio(self):
        self.assertEqual(rd_encode.speed_of('ferry'), 151.2)
        a, b = rd_encode.cost_words(13300, rd_encode.speed_of('ferry'))
        self.assertEqual(a & 0xfff, 3167)                      # round(13300 / 4.2), ferry/RESULT.md's worked example

    def test_l2_classes_always_includes_ferry(self):
        import clarion_profile
        self.assertIn('ferry', clarion_profile.l2_classes('narrow'))
        self.assertIn('ferry', clarion_profile.l2_classes('wide'))
        self.assertIn('ferry', clarion_profile.l2_classes('primary'))
        self.assertIn('ferry', clarion_profile.l2_classes(['primary']))


if __name__ == '__main__':
    unittest.main()
