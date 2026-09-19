import unittest

from web import routes_documents
from web.app import create_app
from web.context import _jinja


class WebDocumentsTest(unittest.TestCase):
    def test_document_router_is_registered(self):
        app = create_app()
        included = [getattr(route, "original_router", None) for route in app.routes]
        self.assertTrue(any(router is routes_documents.router for router in included))
        paths = {getattr(route, "path", None)
                 for route in routes_documents.router.routes}
        expected = {
            "/documents/purchase-orders/{order_id}.pdf",
            "/documents/goods-receipts/{receipt_id}.pdf",
            "/documents/delivery-challans/{fulfillment_id}.pdf",
            "/documents/invoices/{invoice_id}.pdf",
            "/shipping/{order_id}/invoice",
            "/documents/labels",
            "/documents/labels.pdf",
        }
        self.assertTrue(expected.issubset(paths))

    def test_label_template_parses(self):
        self.assertIsNotNone(_jinja.get_template("label_form.html"))


if __name__ == "__main__":
    unittest.main()
