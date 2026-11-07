import pytest
from fastapi.testclient import TestClient

import app


@pytest.fixture
def code_dir(tmp_path, monkeypatch):
    code = tmp_path / "code"
    project = code / "demo"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("![plot](src/plot.png)")
    (project / "src" / "plot.png").write_bytes(b"png")
    monkeypatch.setattr(app, "CODE_DIR", code)
    return code


def test_post_images_keep_first_eager_and_lazy_load_later_images():
    html = app.render_post(
        '![first alt](/images/first.png "first caption")\n\n'
        '![second alt](/images/second.png "second caption")\n\n'
        '![third alt](/images/third.png)\n\n'
        '<img src="/images/raw.png" alt="raw">'
    )

    assert '<img src="/images/first.png" alt="first alt" title="first caption" decoding="async" />' in html
    assert '<img src="/images/second.png" alt="second alt" title="second caption" loading="lazy" decoding="async" />' in html
    assert '<img src="/images/third.png" alt="third alt" loading="lazy" decoding="async" />' in html
    assert '<img src="/images/raw.png" alt="raw">' in html
    assert '<figcaption>first caption</figcaption>' in html
    assert '<figcaption>second caption</figcaption>' in html


def test_post_api_and_server_render_include_image_attributes(tmp_path, monkeypatch):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "first.png").write_bytes(b"png")
    (tmp_path / "images" / "second.png").write_bytes(b"png")
    (tmp_path / "image-post.md").write_text(
        '---\ntitle: Images\ndate: 2026-01-01\n---\n\n'
        '![first](/images/first.png)\n\n![second](/images/second.png)'
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<html><head><title>app</title></head><body><div id="root"></div></body></html>')
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    client = TestClient(app.app)
    html = client.get("/api/posts/image-post").json()["html"]
    page = client.get("/posts/image-post").text

    assert 'src="/images/first.png" alt="first" decoding="async"' in html
    assert 'src="/images/second.png" alt="second" loading="lazy" decoding="async"' in html
    assert 'src="/images/second.png" alt="second" loading="lazy" decoding="async"' in page


def test_readme_images_are_lazy_loaded(code_dir):
    readme = TestClient(app.app).get("/api/code/demo").json()["readme_html"]

    assert '<img src="/code-raw/demo/src/plot.png" alt="plot" loading="lazy" decoding="async" />' in readme
