# SPDX-License-Identifier: GPL-3.0-or-later
"""The level-1 node a level-2 link triple names: the next one along the link, not the one ahead."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
import rd_build

PC = (96, 898, 7, 9)
L1 = (96, 899, 28, 4)       # the level-1 parcel in cell (0, 0) of PC


class L1Next(unittest.TestCase):
    def setUp(self):
        rd_build.l1_next_node.by_heading = 0
        # the twin is node 0; the level-2 link (class 7, id 5) bends north through node 2,
        # and a dead end runs on east through node 1, straight at the link's far node
        self.adj = {L1: {0: [(1, (25.001, 65.0), None), (2, (25.0, 65.001), (7, 5))]}}
        self.frm, self.to = (25.0, 65.0), (25.01, 65.0005)

    def next(self, link):
        return rd_build.l1_next_node((0, 0), PC, self.adj, self.frm, self.to, link)

    def test_the_link_is_followed_not_the_heading(self):
        self.assertEqual(self.next((7, 5)), 2)
        self.assertEqual(rd_build.l1_next_node.by_heading, 0)

    def test_class_and_id_both_count(self):
        self.assertEqual(self.next((5, 5)), 1)
        self.assertEqual(self.next((7, 6)), 1)
        self.assertEqual(rd_build.l1_next_node.by_heading, 2)

    def test_a_link_passing_the_twin_twice_is_told_apart_by_heading(self):
        self.adj[L1][0].append((3, (25.001, 65.0001), (7, 5)))
        self.assertEqual(self.next((7, 5)), 3)
        self.assertEqual(rd_build.l1_next_node.by_heading, 0)

    def test_no_twin_or_no_neighbour_names_nothing(self):
        self.assertIsNone(rd_build.l1_next_node(None, PC, self.adj, self.frm, self.to, (7, 5)))
        self.assertIsNone(rd_build.l1_next_node((0, 9), PC, self.adj, self.frm, self.to, (7, 5)))


class PairingFault(unittest.TestCase):
    def tearDown(self):
        rd_build.encode_parcels.l1_missing = rd_build.encode_parcels.l1_mismatch = 0
        rd_build.encode_parcels.unpaired = []

    def test_every_node_paired_is_no_fault(self):
        rd_build.encode_parcels.l1_missing = rd_build.encode_parcels.l1_mismatch = 0
        rd_build.encode_parcels.unpaired = []
        self.assertIsNone(rd_build.pairing_fault())

    def test_a_node_with_no_level_1_node_near_stops_the_build(self):
        rd_build.encode_parcels.l1_missing, rd_build.encode_parcels.l1_mismatch = 1, 0
        rd_build.encode_parcels.unpaired = [(PC, (5, 5), None, (25.0, 65.0))]
        self.assertIn('1 nodes have no level-1 node at their place', rd_build.pairing_fault())
        self.assertIn('65.000000,25.000000', rd_build.pairing_fault())

    def test_one_whose_twin_is_outside_the_parcel_does_too(self):
        rd_build.encode_parcels.l1_missing, rd_build.encode_parcels.l1_mismatch = 0, 2
        rd_build.encode_parcels.unpaired = []
        self.assertIn('2 with theirs outside the parcel', rd_build.pairing_fault())


if __name__ == '__main__':
    unittest.main()
