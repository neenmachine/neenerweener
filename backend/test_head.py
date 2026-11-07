import os
import re

import pytest
from test_routes import CASES, site


def tags(text, pattern):
    return re.findall(pattern, text)


def test_canonical_uses_configured_origin_not_host_header(site, monkeypatch):
    monkeypatch.setenv("CANONICAL_ORIGIN", "https://configured.example")
    text = site.get("/", headers={"Host": "evil.example"}).text
    assert 'rel="canonical" href="https://configured.example/"' in text
    assert 'property="og:url" content="https://configured.example/"' in text


def test_tag_query_canonicalizes_to_home(site):
    assert 'rel="canonical" href="https://www.neenerweener.ca/"' in site.get("/?tag=engineering").text


def test_query_params_dropped(site):
    assert 'rel="canonical" href="https://www.neenerweener.ca/timeline"' in site.get("/timeline?before=2").text


def test_post_is_article_with_published_time(site):
    text = site.get("/posts/hello").text
    assert 'property="og:type" content="article"' in text
    assert 'property="article:published_time" content="2026-01-01"' in text


def test_home_timeline_code_are_website(site):
    for path in ("/", "/timeline", "/code/demo", "/code/demo/hello.py"):
        assert 'property="og:type" content="website"' in site.get(path).text


def test_not_found_and_admin_noindex_without_canonical(site):
    for path in ("/nope", "/admin"):
        text = site.get(path).text
        assert 'name="robots" content="noindex"' in text
        assert 'rel="canonical"' not in text
        assert 'property="og:url"' not in text
    assert site.get("/nope").headers["X-Robots-Tag"] == "noindex"


def test_code_pages_have_title_from_names(site):
    assert "<title>demo</title>" in site.get("/code/demo").text
    assert "<title>hello.py · demo</title>" in site.get("/code/demo/hello.py").text


@pytest.mark.parametrize("case", CASES, ids=[case["path"] for case in CASES])
def test_each_head_tag_appears_once(site, case):
    text = site.get(case["path"]).text
    for pattern in (
        r"<title>",
        r'<meta name="description"',
        r'<link rel="canonical"',
        r'<meta property="og:title"',
        r'<meta property="og:description"',
        r'<meta property="og:url"',
        r'<meta property="og:type"',
        r'<meta property="og:site_name"',
        r'<meta name="robots"',
    ):
        assert len(tags(text, pattern)) <= 1
