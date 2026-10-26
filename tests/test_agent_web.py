import importlib.util
import unittest

from omnilaw_evolution.agent_web import extract_page


@unittest.skipUnless(importlib.util.find_spec("trafilatura"), "install .[agent]")
class LocalExtractionTests(unittest.TestCase):
    def client(self, handler):
        import httpx
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        self.addCleanup(client.close)
        return client

    def test_reads_actual_html_and_preserves_source(self):
        import httpx
        client = self.client(lambda req: httpx.Response(200, headers={"content-type": "text/html"},
            text="<html><title>Source title</title><main><p>Useful source text for extraction.</p></main></html>"))
        page = extract_page("https://source.test/page", client=client, safe_url=lambda _: True)
        self.assertEqual(page["title"], "Source title")
        self.assertIn("Useful source text", page["content"])
        self.assertEqual(page["metadata"]["sourceURL"], "https://source.test/page")

    def test_redirect_cannot_bypass_private_address_check(self):
        import httpx
        calls = []
        def handle(req):
            calls.append(str(req.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        client = self.client(handle)
        with self.assertRaisesRegex(ValueError, "private"):
            extract_page("https://source.test", client=client, safe_url=lambda u: "127.0.0.1" not in u)
        self.assertEqual(calls, ["https://source.test"])

    def test_download_is_bounded(self):
        import httpx
        client = self.client(lambda req: httpx.Response(200, content=b"x" * 200))
        with self.assertRaisesRegex(ValueError, "download limit"):
            extract_page("https://source.test", client=client, safe_url=lambda _: True, max_bytes=100)


if __name__ == "__main__":
    unittest.main()
