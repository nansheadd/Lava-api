import sys
import types
import unittest


def _ensure_playwright_stub() -> None:
    if "playwright" in sys.modules and "playwright.sync_api" in sys.modules:
        return

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Error = Exception
    sync_api.Locator = object
    sync_api.Page = object
    sync_api.TimeoutError = TimeoutError
    sync_api.sync_playwright = lambda: (_ for _ in ()).throw(RuntimeError("stub"))

    playwright = types.ModuleType("playwright")
    sys.modules["playwright"] = playwright
    sys.modules["playwright.sync_api"] = sync_api


class NormaliseBaseURLTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_playwright_stub()
        from app.main import _normalise_base_url  # type: ignore

        self._normalise = _normalise_base_url

    def test_basic_domain(self) -> None:
        self.assertEqual(self._normalise("https://example.com"), "https://example.com/")

    def test_trailing_slash_preserved(self) -> None:
        self.assertEqual(self._normalise("https://example.com/"), "https://example.com/")

    def test_removes_wp_admin_suffix(self) -> None:
        self.assertEqual(self._normalise("https://example.com/wp-admin"), "https://example.com/")
        self.assertEqual(self._normalise("https://example.com/wp-admin/"), "https://example.com/")

    def test_removes_login_suffix(self) -> None:
        self.assertEqual(
            self._normalise("https://example.com/wp-login.php"), "https://example.com/"
        )

    def test_preserves_subdirectory(self) -> None:
        self.assertEqual(
            self._normalise("https://example.com/blog"),
            "https://example.com/blog/",
        )
        self.assertEqual(
            self._normalise("https://example.com/blog/wp-admin"),
            "https://example.com/blog/",
        )

    def test_strips_query_and_fragment(self) -> None:
        self.assertEqual(
            self._normalise("https://example.com/wp-admin/?foo=bar#baz"),
            "https://example.com/",
        )

    def test_infers_scheme(self) -> None:
        self.assertEqual(self._normalise("example.com/wp-admin"), "https://example.com/")


if __name__ == "__main__":
    unittest.main()
