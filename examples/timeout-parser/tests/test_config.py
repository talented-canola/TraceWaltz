import unittest
from config import parse_timeout


class TimeoutTests(unittest.TestCase):
    def test_none_uses_default(self):
        self.assertEqual(parse_timeout(None), 30)
        self.assertEqual(parse_timeout(None, default=5), 5)

    def test_zero_is_preserved(self):
        self.assertEqual(parse_timeout(0), 0)
        self.assertEqual(parse_timeout(' 0 '), 0)

    def test_nonnegative_values(self):
        self.assertEqual(parse_timeout(12), 12)
        self.assertEqual(parse_timeout(' 12 '), 12)

    def test_invalid_values(self):
        for value in [-1, '-1', '', ' ', '1.5', 'abc', '+2']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_timeout(value)

    def test_invalid_types(self):
        for value in [True, False, 1.5, [], {}]:
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    parse_timeout(value)
