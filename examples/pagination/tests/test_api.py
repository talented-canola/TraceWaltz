import unittest
from api import paginate


class PaginationTests(unittest.TestCase):
    def test_pages_use_one_based_offset(self):
        self.assertEqual(paginate([0, 1, 2, 3, 4], page=2, size=2), [2, 3])
        self.assertEqual(paginate([0, 1, 2, 3, 4], page=3, size=2), [4])

    def test_empty_and_out_of_range(self):
        self.assertEqual(paginate([], page=1, size=2), [])
        self.assertEqual(paginate([1], page=3, size=2), [])

    def test_input_is_not_modified(self):
        items = [1, 2, 3]
        self.assertEqual(paginate(items, page=1, size=2), [1, 2])
        self.assertEqual(items, [1, 2, 3])

    def test_nonpositive_values(self):
        for parameter in ['page', 'size']:
            for value in [0, -1]:
                with self.subTest(parameter=parameter, value=value):
                    with self.assertRaises(ValueError):
                        paginate([1, 2], **{parameter: value})

    def test_invalid_types_including_bool(self):
        for parameter in ['page', 'size']:
            for value in [True, False, '2', 1.5, None]:
                with self.subTest(parameter=parameter, value=value):
                    with self.assertRaises(TypeError):
                        paginate([1, 2], **{parameter: value})
