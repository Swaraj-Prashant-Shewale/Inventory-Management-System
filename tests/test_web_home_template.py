import unittest

from web.context import _jinja


class HomeTemplateTest(unittest.TestCase):
    def test_item_count_uses_dictionary_value_not_items_method(self):
        html = _jinja.get_template("home.html").render(
            counts={"items": 7, "open_purchases": 2, "open_sales": 3},
            stock_value=None,
            reminders=[],
        )

        self.assertIn('<span class="tile-value">7</span>', html)
        self.assertNotIn("built-in method items", html)


if __name__ == "__main__":
    unittest.main()
