import unittest

import toy


class Greet(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(toy.greet("x"), "hello, x")


if __name__ == "__main__":
    unittest.main()
