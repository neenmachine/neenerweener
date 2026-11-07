from fastapi.testclient import TestClient

import app


def test_post_heading_ids_and_anchor_links():
    html = app.render_post(
        "# untouched\n\n## Hello, world!\n\n## Hello, world!\n\n"
        "## Café & 日本\n\n## `inline` *emphasis*\n\n###### !!!\n"
    )

    assert '<h1>untouched</h1>' in html
    assert '<h2 id="hello-world"><a class="anchor" href="#hello-world" aria-label="link to section">#</a>' in html
    assert '<h2 id="hello-world-1"><a class="anchor" href="#hello-world-1" aria-label="link to section">#</a>' in html
    assert '<h2 id="café-日本"><a class="anchor" href="#café-日本" aria-label="link to section">#</a>' in html
    assert '<h2 id="inline-emphasis"><a class="anchor" href="#inline-emphasis" aria-label="link to section">#</a>' in html
    assert '<h6 id="section"><a class="anchor" href="#section" aria-label="link to section">#</a>' in html
    assert '<h1 id=' not in html


def test_post_request_renders_stable_heading_ids(tmp_path, monkeypatch):
    (tmp_path / "anchors.md").write_text(
        "---\ntitle: Anchors\ndate: 2026-01-01\n---\n\n## A section\n"
    )
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    response = TestClient(app.app).get("/api/posts/anchors")

    assert response.status_code == 200
    assert '<h2 id="a-section"><a class="anchor" href="#a-section" aria-label="link to section">#</a>A section</h2>' in response.json()["html"]
