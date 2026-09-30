"""Seed smoke checks only; deliberately incomplete, not acceptance evidence."""

import unittest

from total import total_cents


class SeedSmokeTests(unittest.TestCase):
    def test_positive_quantity(self):
        self.assertEqual(total_cents([{"unit_cents": 250, "quantity": 2}]), 500)

    def test_empty_receipt(self):
        self.assertEqual(total_cents([]), 0)


if __name__ == "__main__":
    unittest.main()
