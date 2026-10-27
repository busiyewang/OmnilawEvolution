import unittest
from pager import paginate


class PagerTest(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(paginate(list(range(10)), 1, 3), [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
