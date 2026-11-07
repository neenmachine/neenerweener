import os
import re
from urllib.parse import quote

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


def test_code_path_is_encoded_before_html_escaping(site, tmp_path, monkeypatch):
    name = 'a b-ü&<".py'
    (tmp_path / "code" / "demo" / name).write_text("print(1)\n")
    encoded = quote(name, safe="")
    text = site.get(f"/code/demo/{encoded}").text
    expected = "https://www.neenerweener.ca/code/demo/" + encoded
    assert f'<link rel="canonical" href="{expected}" />' in text
    assert f'<meta property="og:url" content="{expected}" />' in text


def test_draft_without_date_has_no_published_time(site, monkeypatch, tmp_path):
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "undated.md").write_text("draft body\n")
    monkeypatch.setattr(__import__("app"), "DRAFTS_DIR", drafts)
    monkeypatch.setenv("SHOW_DRAFTS", "1")
    assert 'property="article:published_time"' not in site.get("/posts/undated").text


@pytest.mark.parametrize("case", CASES, ids=[case["path"] for case in CASES])
def test_each_head_tag_appears_once(site, case):
    text = site.get(case["path"], follow_redirects=False).text
    page = case.get("page")
    expected = {
        r"<title>": int(page is not None),
        r'<meta name="description"': int(page is not None and page["kind"] in {"timeline", "post"}),
        r'<link rel="canonical"': int(page is not None and not case["noindex"]),
        r'<meta property="og:title"': int(page is not None),
        r'<meta property="og:description"': int(page is not None and page["kind"] in {"timeline", "post"}),
        r'<meta property="og:url"': int(page is not None and not case["noindex"]),
        r'<meta property="og:type"': int(page is not None and not case["noindex"]),
        r'<meta property="og:site_name"': int(page is not None),
        r'<meta name="twitter:card"': int(page is not None),
        r'<meta name="robots"': int(page is not None and case["noindex"]),
    }
    for pattern, count in expected.items():
        assert len(tags(text, pattern)) == count, pattern
