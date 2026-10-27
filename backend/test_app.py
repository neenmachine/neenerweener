import logging

import pytest
from fastapi.testclient import TestClient

import app


def test_spa_fallback(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "x.js").write_text("console.log('x')")
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    c = TestClient(app.app)
    assert c.get("/").text == "<html>app</html>"
    assert c.get("/posts/foo").text == "<html>app</html>"
    assert c.get("/assets/x.js").text == "console.log('x')"
    assert c.get("/api/nope").status_code == 404
    assert c.get("/api/nope").json() == {"detail": "not found"}
    assert c.get("/api/posts").status_code == 200


def test_frontmatter_fields(tmp_path, monkeypatch):
    (tmp_path / "iso.md").write_text(
        "---\ntitle: ISO\ndate: 2026-03-28\nexcerpt: short\nkind: demo\n---\nbody"
    )
    (tmp_path / "slash.md").write_text("---\ntitle: Slash\ndate: 03/29/2026\n---\ntext")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    c = TestClient(app.app)
    assert c.get("/api/posts").json() == [
        {"slug": "slash", "date": "2026-03-29", "title": "Slash", "excerpt": None, "draft": False},
        {"slug": "iso", "date": "2026-03-28", "title": "ISO", "excerpt": "short", "draft": False},
    ]
    assert c.get("/api/posts/iso").json() == {
        "slug": "iso", "date": "2026-03-28", "title": "ISO", "excerpt": "short", "draft": False,
        "html": "<p>body</p>\n",
    }


def test_published_requires_title(tmp_path, monkeypatch, caplog):
    (tmp_path / "untitled.md").write_text("---\ndate: 2026-01-01\n---\nbody")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        assert TestClient(app.app).get("/api/posts").json() == []
    assert "invalid post metadata in untitled.md" in caplog.messages


def test_impossible_date_skipped(tmp_path, monkeypatch, caplog):
    (tmp_path / "impossible.md").write_text("---\ntitle: Impossible\ndate: 2026-02-30\n---\nbody")
    (tmp_path / "valid.md").write_text("---\ntitle: Valid\ndate: 2026-01-01\n---\nbody")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        result = TestClient(app.app).get("/api/posts").json()
    assert [post["slug"] for post in result] == ["valid"]
    assert "invalid frontmatter in impossible.md" in caplog.messages


def test_scalar_metadata_coerced(tmp_path, monkeypatch, caplog):
    (tmp_path / "scalar.md").write_text("---\ntitle: 2026\ndate: 2026-01-01\nexcerpt: 3.5\n---\nbody")
    (tmp_path / "list.md").write_text("---\ntitle: [bad]\ndate: 2026-01-01\n---\nbody")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        result = TestClient(app.app).get("/api/posts").json()
    assert result == [{"slug": "scalar", "date": "2026-01-01", "title": "2026", "excerpt": "3.5", "draft": False}]
    assert "invalid post metadata in list.md" in caplog.messages


def test_frontmatter_bom_crlf_eof(tmp_path, monkeypatch):
    (tmp_path / "bom.md").write_text("\ufeff---\r\ntitle: Bom\r\ndate: 03/28/2026\r\n---", encoding="utf-8")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    assert TestClient(app.app).get("/api/posts").json() == [
        {"slug": "bom", "date": "2026-03-28", "title": "Bom", "excerpt": None, "draft": False}
    ]


def test_invalid_posts_skipped(tmp_path, monkeypatch, caplog):
    (tmp_path / "missing.md").write_text("body")
    (tmp_path / "bad-yaml.md").write_text("---\ntitle: [\n---\nbody")
    (tmp_path / "missing-date.md").write_text("---\ntitle: Missing\n---\nbody")
    (tmp_path / "bad-date.md").write_text("---\ntitle: Bad\ndate: never\n---\nbody")
    (tmp_path / "valid.md").write_text("---\ntitle: Valid\ndate: 2026-01-01\n---\nbody")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    with caplog.at_level(logging.WARNING):
        result = TestClient(app.app).get("/api/posts").json()
    assert [post["slug"] for post in result] == ["valid"]
    assert {record.message for record in caplog.records} == {
        "invalid frontmatter in missing.md",
        "invalid frontmatter in bad-yaml.md",
        "invalid post metadata in missing-date.md",
        "invalid post metadata in bad-date.md",
    }


def test_draft_frontmatter_optional(tmp_path, monkeypatch):
    posts = tmp_path / "posts"
    drafts = tmp_path / "drafts"
    posts.mkdir()
    drafts.mkdir()
    (drafts / "idea.md").write_text("draft body")
    (drafts / "planned.md").write_text("---\ntitle: Planned\ndate: 04/01/2026\n---\nplanned body")
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "DRAFTS_DIR", drafts)
    monkeypatch.setenv("SHOW_DRAFTS", "1")

    assert TestClient(app.app).get("/api/posts").json() == [
        {"slug": "idea", "date": None, "title": "idea", "excerpt": None, "draft": True},
        {"slug": "planned", "date": "2026-04-01", "title": "Planned", "excerpt": None, "draft": True},
    ]


def test_math():
    html = app.md.render(
        "inline $x^2$ and\n\n$$a<b$$\n\n"
        "costs $5 and $10\n\n`$x$`\n\n```text\n$x$\n```\n"
    )
    assert '<span class="math inline">x^2</span>' in html
    assert '<div class="math block">\na&lt;b\n</div>' in html
    assert "costs $5 and $10" in html
    assert '<code>$x$</code>' in html
    assert '<pre><code class="language-text">$x$\n</code></pre>' in html


@pytest.mark.parametrize("show_drafts", [None, "0", "true"])
def test_drafts_hidden_by_default(tmp_path, monkeypatch, show_drafts):
    posts = tmp_path / "posts"
    drafts = tmp_path / "drafts"
    posts.mkdir()
    drafts.mkdir()
    (drafts / "idea.md").write_text("draft")
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "DRAFTS_DIR", drafts)
    if show_drafts is None:
        monkeypatch.delenv("SHOW_DRAFTS", raising=False)
    else:
        monkeypatch.setenv("SHOW_DRAFTS", show_drafts)

    c = TestClient(app.app)
    assert c.get("/api/posts").json() == []
    assert c.get("/api/posts/idea").status_code == 404


def test_published_wins_draft_collision(tmp_path, monkeypatch):
    posts = tmp_path / "posts"
    drafts = tmp_path / "drafts"
    posts.mkdir()
    drafts.mkdir()
    (posts / "same.md").write_text("---\ntitle: Published\ndate: 2026-01-01\n---\npost")
    (drafts / "same.md").write_text("---\ntitle: Draft\n---\ndraft")
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "DRAFTS_DIR", drafts)
    monkeypatch.setenv("SHOW_DRAFTS", "1")

    assert TestClient(app.app).get("/api/posts").json() == [
        {"slug": "same", "date": "2026-01-01", "title": "Published", "excerpt": None, "draft": False}
    ]


def test_render():
    html = app.md.render(
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "![alt](/images/x.png)\n\n"
        "```python\nx = 1\n```\n\n"
        '```mermaid\nA["x<br/>y"] --> B\n```\n\n'
        "```nosuchlang\n<b>\n```\n"
    )
    assert "<table>" in html and "<td>1</td>" in html
    assert '<img src="/images/x.png" alt="alt"' in html
    assert '<code class="language-python"><span class="n">x</span>' in html
    assert '<pre class="mermaid">A[&quot;x&lt;br/&gt;y&quot;] --&gt; B\n</pre>' in html
    assert "&lt;b&gt;" in html
