"""Injectable clock: real by default, fakeable with use(), nesting restores."""
import time
import unittest
from datetime import datetime, timedelta, timezone

from support import ROOT  # noqa: F401  (puts lib on sys.path)

from swarm import clock


class Fake:
    def __init__(self, t, m):
        self.t, self.m = t, m

    def now(self):
        return self.t

    def monotonic(self):
        return self.m


class ClockTests(unittest.TestCase):
    def test_real_now_is_aware_utc(self):
        n = clock.now()
        self.assertIsNotNone(n.tzinfo)
        self.assertEqual(n.utcoffset(), timedelta(0))

    def test_real_monotonic_is_the_monotonic_source(self):
        # time.time() is epoch seconds (~1.7e9); time.monotonic() is a small boot-relative value.
        # Comparing against time.monotonic() directly rejects a wall-clock implementation.
        self.assertLess(abs(clock.monotonic() - time.monotonic()), 1.0)

    def test_use_fake_and_restore(self):
        t = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with clock.use(Fake(t, 5.0)):
            self.assertEqual(clock.now(), t)
            self.assertEqual(clock.monotonic(), 5.0)
        self.assertNotEqual(clock.now(), t)

    def test_nesting_restores_outer(self):
        t1 = datetime(2030, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2031, 1, 1, tzinfo=timezone.utc)
        with clock.use(Fake(t1, 1.0)):
            with clock.use(Fake(t2, 2.0)):
                self.assertEqual(clock.now(), t2)
            self.assertEqual(clock.now(), t1)
            self.assertEqual(clock.monotonic(), 1.0)

    def test_restores_on_exception(self):
        t = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(RuntimeError):
            with clock.use(Fake(t, 1.0)):
                raise RuntimeError
        self.assertNotEqual(clock.now(), t)


if __name__ == "__main__":
    unittest.main()
