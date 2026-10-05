import runpy
import unittest
from pathlib import Path


TABLE_MODULE = (
    Path(__file__).parents[1]
    / "engines/openparf/openparf/ops/sll/sll_table.py"
)
build_sll_counts_table_values = runpy.run_path(str(TABLE_MODULE))[
    "build_sll_counts_table_values"
]


class OpenPARFSllTableTest(unittest.TestCase):
    def test_one_by_two_table(self):
        self.assertEqual(
            build_sll_counts_table_values(1, 2),
            [0, 0, 0, 1],
        )

    def test_one_by_four_matches_upstream_reference(self):
        self.assertEqual(
            build_sll_counts_table_values(1, 4),
            [0, 0, 0, 1, 0, 2, 1, 2, 0, 3, 2, 3, 1, 3, 2, 3],
        )

    def test_two_by_two_matches_upstream_reference(self):
        self.assertEqual(
            build_sll_counts_table_values(2, 2),
            [0, 0, 0, 1, 0, 1, 2, 2, 0, 2, 1, 2, 1, 2, 2, 3],
        )

    def test_unbounded_exponential_table_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "at most 12"):
            build_sll_counts_table_values(4, 4)


if __name__ == "__main__":
    unittest.main()
