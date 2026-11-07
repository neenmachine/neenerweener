import re

import app
from fastapi.testclient import TestClient


def _markup(response):
    return re.search(r'<div id="root">(.*)</div></body>', response.text, re.S)[1]


def test_home_server_renders_posts_and_escapes_content(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<html><body><div id="root"></div></body></html>')
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "one.md").write_text('---\ntitle: "a & <title>"\ndate: 2026-01-01\nexcerpt: "an <excerpt> & more"\ntags: [a, b]\n---\nbody')
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "draft.md").write_text('---\ntitle: Draft\ntags: [b]\n---\ndraft')
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "DRAFTS_DIR", drafts)
    monkeypatch.setenv("SHOW_DRAFTS", "1")

    with TestClient(app.app) as client:
        markup = _markup(client.get("/"))

    assert '<nav class="tag-filter" aria-label="tags">' in markup
    assert 'href="/posts/one"' in markup
    assert 'a &amp; &lt;title&gt;' in markup
    assert 'an &lt;excerpt&gt; &amp; more' in markup
    assert 'href="/?tag=a"' in markup and 'href="/?tag=b"' in markup
    assert '<span class="draft muted">draft</span>' in markup
    assert '<footer>' in markup
    assert 'a & <title>' not in markup and 'an <excerpt> & more' not in markup


def test_home_tag_filter_and_unknown_tag(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<html><body><div id="root"></div></body></html>')
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "one.md").write_text('---\ntitle: One\ndate: 2026-01-01\ntags: [a]\n---\nbody')
    (posts / "two.md").write_text('---\ntitle: Two\ndate: 2025-01-01\ntags: [b]\n---\nbody')
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.delenv("SHOW_DRAFTS", raising=False)

    with TestClient(app.app) as client:
        tagged = _markup(client.get("/?tag=a"))
        missing = _markup(client.get("/?tag=missing"))

    assert 'href="/?tag=a" aria-current="page"' in tagged
    assert 'href="/posts/one"' in tagged and 'href="/posts/two"' not in tagged
    assert "no posts with this tag" in missing
    assert '<ul class="posts"></ul>' in missing
    assert 'href="/posts/one"' not in missing and 'href="/posts/two"' not in missing
