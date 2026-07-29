from __future__ import annotations

import unittest

import app


class RouteTests(unittest.TestCase):
    def test_root_route_returns_message(self) -> None:
        status, body = app.route("/")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"message": "hello from skep-demo"})

    def test_unknown_route_returns_404(self) -> None:
        status, body = app.route("/missing")

        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not found"})


if __name__ == "__main__":
    unittest.main()
