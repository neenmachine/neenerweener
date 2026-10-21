from fastapi.testclient import TestClient

import app


def test_posts(tmp_path, monkeypatch):
    (tmp_path / "2024-01-01-old.md").write_text("# Old\nfirst")
    (tmp_path / "2024-02-01-new.md").write_text("no title line")
    (tmp_path / "notes.md").write_text("ignored: bad file name")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)
    c = TestClient(app.app)

    assert c.get("/api/posts").json() == [
        {"slug": "new", "date": "2024-02-01", "title": "new"},
        {"slug": "old", "date": "2024-01-01", "title": "Old"},
    ]
    assert c.get("/api/posts/old").json() == {
        "slug": "old", "date": "2024-01-01", "title": "Old", "html": "<p>first</p>\n"
    }
    assert c.get("/api/posts/missing").status_code == 404


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
