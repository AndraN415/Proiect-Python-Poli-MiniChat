import re

from tests import AppTestCase

STATIC_URL = re.compile(r'(?:src|href)="(/static/[^"]+)"')


class IndexTests(AppTestCase):
    def page(self) -> str:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_logged_out_gets_login_page(self) -> None:
        html = self.page()
        self.assertIn('id="register-form"', html)
        self.assertIn('id="login-form"', html)
        self.assertNotIn('id="messages"', html)

    def test_logged_in_gets_chat_page_with_own_login_id(self) -> None:
        user = self.register("alice")
        html = self.page()
        self.assertIn('id="messages"', html)
        self.assertIn("alice", html)
        self.assertIn(user["id"], html)
        self.assertNotIn('id="login-form"', html)

    def test_display_name_is_escaped(self) -> None:
        self.register("<b>alice</b>")
        html = self.page()
        self.assertIn("&lt;b&gt;alice&lt;/b&gt;", html)
        self.assertNotIn("<b>alice</b>", html)

    def test_session_for_unknown_user_gets_login_page(self) -> None:
        with self.client.session_transaction() as session:
            session["user_id"] = "000000000000"
        self.assertIn('id="login-form"', self.page())

    def test_only_get_is_allowed(self) -> None:
        self.assertEqual(self.client.post("/").status_code, 405)


class StaticAssetTests(AppTestCase):
    def assert_assets_served(self, html: str) -> None:
        urls = STATIC_URL.findall(html)
        self.assertTrue(urls)
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_login_page_assets_exist(self) -> None:
        self.assert_assets_served(self.client.get("/").get_data(as_text=True))

    def test_chat_page_assets_exist(self) -> None:
        self.register()
        self.assert_assets_served(self.client.get("/").get_data(as_text=True))

    def test_missing_asset_is_404(self) -> None:
        self.assertEqual(self.client.get("/static/js/nope.js").status_code, 404)
