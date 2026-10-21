import logging

import pytest
from fastapi.testclient import TestClient

import app


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
