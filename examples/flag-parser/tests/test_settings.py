import unittest
from settings import parse_flag


class FlagTests(unittest.TestCase):
    def test_false_strings(self):
        for value in ['false', 'FALSE', '  off ', '0', 'no']:
            with self.subTest(value=value):
                self.assertIs(parse_flag(value), False)

    def test_true_strings(self):
        for value in ['true', 'TRUE', ' on ', '1', 'yes']:
            with self.subTest(value=value):
                self.assertIs(parse_flag(value), True)

    def test_boolean_inputs(self):
        self.assertIs(parse_flag(True), True)
        self.assertIs(parse_flag(False), False)

    def test_unknown_text_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_flag('maybe')

    def test_other_types_are_rejected(self):
        for value in [None, 0, 1, [], {}]:
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    parse_flag(value)
