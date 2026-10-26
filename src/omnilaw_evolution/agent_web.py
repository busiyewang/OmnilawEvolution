"""A keyless local extractor registered through Hermes' provider extension API."""
from __future__ import annotations

from urllib.parse import urljoin


def extract_page(url, *, client, safe_url, max_bytes=2_000_000):
    """Bound downloads and validate every redirect before following it."""
    import trafilatura
    from bs4 import BeautifulSoup

    original = url
    for _ in range(6):
        if not safe_url(url):
            raise ValueError("URL targets a private/internal address or unsupported scheme")
        with client.stream("GET", url) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect has no Location")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Page exceeds 2 MB download limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
            content_type = response.headers.get("content-type", "").lower()
            if "html" in content_type or b"<html" in raw[:1000].lower():
                soup = BeautifulSoup(raw, "html.parser")
                title = soup.title.get_text(" ", strip=True) if soup.title else url
                content = trafilatura.extract(raw, include_tables=True, include_links=True)
                if not content:
                    for element in soup(["script", "style", "nav"]):
                        element.decompose()
                    content = soup.get_text("\n", strip=True)
            elif content_type.startswith("text/") or "json" in content_type:
                title, content = url, raw.decode(response.encoding or "utf-8", errors="replace")
            else:
                raise ValueError("This extractor reads HTML/text; use browser or terminal for this content type")
            return {"url": original, "title": title, "content": content, "raw_content": content,
                    "metadata": {"sourceURL": url, "bytes": size, "extractor": "omnilaw-local"}}
    raise ValueError("Too many redirects")


def register_extractor():
    # Only imported in the isolated worker, after adding the vendored source.
    from agent.web_search_provider import WebSearchProvider
    from agent.web_search_registry import register_provider
    from tools.url_safety import create_ssrf_safe_client, is_safe_url

    class LocalExtractor(WebSearchProvider):
        name = "omnilaw-local"

        def is_available(self):
            return True

        def supports_search(self):
            return False

        def supports_extract(self):
            return True

        def extract(self, urls, **kwargs):
            results = []
            with create_ssrf_safe_client(timeout=20, follow_redirects=False,
                    headers={"User-Agent": "Omnilaw/0.3 (Hermes web extraction)"}) as client:
                for url in urls:
                    try:
                        results.append(extract_page(url, client=client, safe_url=is_safe_url))
                    except Exception as exc:
                        results.append({"url": url, "title": "", "content": "", "error": str(exc)})
            return results

    register_provider(LocalExtractor())
