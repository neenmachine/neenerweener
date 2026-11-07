from fastapi.testclient import TestClient

import app


def test_post_heading_ids_and_anchor_links():
    html = app.render_post(
        "# untouched\n\n## Hello, world!\n\n## Hello, world!\n\n"
        "## Café & 日本\n\n## `inline` *emphasis*\n\n###### !!!\n"
    )

    assert '<h1>untouched</h1>' in html
    assert '<h2 id="hello-world">Hello, world!<a class="anchor" href="#hello-world" aria-label="link to section">#</a></h2>' in html
    assert '<h2 id="hello-world-1">Hello, world!<a class="anchor" href="#hello-world-1" aria-label="link to section">#</a></h2>' in html
    assert '<h2 id="café-日本">Café &amp; 日本<a class="anchor" href="#café-日本" aria-label="link to section">#</a></h2>' in html
    assert '<h2 id="inline-emphasis"><code>inline</code> <em>emphasis</em><a class="anchor" href="#inline-emphasis" aria-label="link to section">#</a></h2>' in html
    assert '<h6 id="section">!!!<a class="anchor" href="#section" aria-label="link to section">#</a></h6>' in html
    assert '<h1 id=' not in html


def test_heading_ids_are_unique_and_avoid_shell_ids():
    html = app.render_post(
        "## Foo\n\n## Foo\n\n## Foo-1\n\n## Root\n\n## preload"
    )

    assert '<h2 id="foo">Foo<a' in html
    assert '<h2 id="foo-1">Foo<a' in html
    assert '<h2 id="foo-1-1">Foo-1<a' in html
    assert '<h2 id="root-1">Root<a' in html
    assert '<h2 id="preload-1">preload<a' in html
    assert html.count('<h2 id="foo-1">') == 1


def test_raw_html_heading_is_untouched():
    html = app.render_post("<h2>Raw</h2>")

    assert "<h2>Raw</h2>" in html
    assert '<h2 id="raw">' not in html


def test_post_request_renders_stable_heading_ids(tmp_path, monkeypatch):
    (tmp_path / "anchors.md").write_text(
        "---\ntitle: Anchors\ndate: 2026-01-01\n---\n\n## A section\n"
    )
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    response = TestClient(app.app).get("/api/posts/anchors")

    assert response.status_code == 200
    assert '<h2 id="a-section">A section<a class="anchor" href="#a-section" aria-label="link to section">#</a></h2>' in response.json()["html"]
