from fastapi.testclient import TestClient

import app


def test_posts(tmp_path, monkeypatch):
    (tmp_path / "2024-01-01-old.md").write_text("# Old\nfirst")
    (tmp_path / "2024-02-01-new.md").write_text("no title line")
    (tmp_path / "notes.md").write_text("ignored: bad file name")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)
    c = TestClient(app.app)

    assert c.get("/api/posts").json() == [
        {"slug": "new", "date": "2024-02-01", "title": "new", "draft": False},
        {"slug": "old", "date": "2024-01-01", "title": "Old", "draft": False},
    ]
    assert c.get("/api/posts/old").json() == {
        "slug": "old", "date": "2024-01-01", "title": "Old", "draft": False, "html": "<p>first</p>\n"
    }
    assert c.get("/api/posts/missing").status_code == 404


def test_drafts_hidden_by_default(tmp_path, monkeypatch):
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "idea.md").write_text("# Idea\nnotes")
    monkeypatch.setattr(app, "DRAFTS_DIR", drafts)
    monkeypatch.delenv("SHOW_DRAFTS", raising=False)

    c = TestClient(app.app)
    assert c.get("/api/posts").json() == []
    assert c.get("/api/posts/idea").status_code == 404


def test_drafts_shown_with_flag(tmp_path, monkeypatch):
    posts = tmp_path / "posts"
    drafts = tmp_path / "drafts"
    posts.mkdir()
    drafts.mkdir()
    (posts / "2024-01-01-published.md").write_text("# Published\npost")
    (posts / "2024-01-02-same.md").write_text("# Published same\npost")
    (drafts / "idea.md").write_text("# Idea\ndraft")
    (drafts / "2024-02-01-planned.md").write_text("# Planned\ndraft")
    (drafts / "2024-03-01-same.md").write_text("# Draft same\ndraft")
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "DRAFTS_DIR", drafts)
    monkeypatch.setenv("SHOW_DRAFTS", "1")

    c = TestClient(app.app)
    assert c.get("/api/posts").json() == [
        {"slug": "idea", "date": None, "title": "Idea", "draft": True},
        {"slug": "planned", "date": "2024-02-01", "title": "Planned", "draft": True},
        {"slug": "same", "date": "2024-01-02", "title": "Published same", "draft": False},
        {"slug": "published", "date": "2024-01-01", "title": "Published", "draft": False},
    ]
    assert c.get("/api/posts/idea").json() == {
        "slug": "idea", "date": None, "title": "Idea", "draft": True, "html": "<p>draft</p>\n"
    }


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
    assert "&lt;b&gt;" in html  # unknown language still escaped
