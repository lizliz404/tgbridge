import unittest

import tgbridge


class RegressionGateTests(unittest.TestCase):
    def test_full_selftest(self):
        tgbridge.selftest()


if __name__ == "__main__":
    unittest.main()
