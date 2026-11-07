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
    assert c.get("/code/demo").text == "<html>app</html>"
    assert c.get("/nope").text == "<html>app</html>"
    assert "<html>app" in c.get("/").text
    assert c.get("/assets/x.js").text == "console.log('x')"
    assert c.get("/api/nope").status_code == 404
    assert c.get("/api/nope").json() == {"detail": "not found"}
    assert c.get("/api/posts").status_code == 200


def test_spa_index_symlink_blocked(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (dist / "index.html").symlink_to(outside)
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    c = TestClient(app.app)
    assert c.get("/").status_code == 404
    assert c.get("/posts/foo").status_code == 404


def test_spa_head(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "x.js").write_text("console.log('x')")
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    c = TestClient(app.app)
    assert c.head("/").status_code == 200
    assert c.head("/assets/x.js").status_code == 200
    assert c.head("/api/nope").status_code == 404
    assert c.post("/").status_code == 405


def test_frontmatter_fields(tmp_path, monkeypatch):
    (tmp_path / "iso.md").write_text(
        "---\ntitle: ISO\ndate: 2026-03-28\nexcerpt: short\nkind: demo\n---\nbody"
    )
    (tmp_path / "slash.md").write_text("---\ntitle: Slash\ndate: 03/29/2026\n---\ntext")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    c = TestClient(app.app)
    assert c.get("/api/posts").json() == [
        {"slug": "slash", "date": "2026-03-29", "title": "Slash", "excerpt": None, "tags": [], "draft": False},
        {"slug": "iso", "date": "2026-03-28", "title": "ISO", "excerpt": "short", "tags": [], "draft": False},
    ]
    assert c.get("/api/posts/iso").json() == {
        "slug": "iso", "date": "2026-03-28", "title": "ISO", "excerpt": "short", "tags": [], "draft": False,
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
    assert result == [{"slug": "scalar", "date": "2026-01-01", "title": "2026", "excerpt": "3.5", "tags": [], "draft": False}]
    assert "invalid post metadata in list.md" in caplog.messages


def test_frontmatter_bom_crlf_eof(tmp_path, monkeypatch):
    (tmp_path / "bom.md").write_text("\ufeff---\r\ntitle: Bom\r\ndate: 03/28/2026\r\n---", encoding="utf-8")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    assert TestClient(app.app).get("/api/posts").json() == [
        {"slug": "bom", "date": "2026-03-28", "title": "Bom", "excerpt": None, "tags": [], "draft": False}
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
        {"slug": "idea", "date": None, "title": "idea", "excerpt": None, "tags": [], "draft": True},
        {"slug": "planned", "date": "2026-04-01", "title": "Planned", "excerpt": None, "tags": [], "draft": True},
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
        {"slug": "same", "date": "2026-01-01", "title": "Published", "excerpt": None, "tags": [], "draft": False}
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


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00"


@pytest.fixture
def code_dir(tmp_path, monkeypatch):
    code = tmp_path / "code"
    project = code / "demo"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text(
        "# demo\n\n![plot](src/plot.png) [main](./main.py) [web](https://example.com) [top](/x)\n"
    )
    (project / "main.py").write_text("def f():\n    return '''a\nb'''\n")
    (project / "notes.md").write_text("# not rendered\n")
    (project / "src" / "plot.png").write_bytes(PNG)
    (project / "data.bin").write_bytes(b"\xff\xfe\x00")
    (project / "labels.idx").write_bytes(b"\x00\x00\x08\x01")
    (code / ".sync-tmp").mkdir()
    monkeypatch.setattr(app, "CODE_DIR", code)
    return code


def test_code_api(code_dir):
    c = TestClient(app.app)
    project = c.get("/api/code/demo").json()
    assert project["name"] == "demo"
    assert project["files"] == ["README.md", "data.bin", "labels.idx", "main.py", "notes.md", "src/plot.png"]
    readme = project["readme_html"]
    assert "<h1>demo</h1>" in readme
    assert '<img src="/code-raw/demo/src/plot.png" alt="plot"' in readme
    assert 'href="/code/demo/main.py"' in readme
    assert 'href="https://example.com"' in readme and 'href="/x"' in readme

    file = c.get("/api/code/demo/file/main.py").json()
    assert file["path"] == "main.py"
    assert '<span class="line"><span class="k">def</span><span class="w"> </span><span class="nf">f</span>' in file["html"]
    assert file["html"].count('<span class="line">') == 3  # the multi-line string is split per line
    assert "<h1>" not in c.get("/api/code/demo/file/notes.md").json()["html"]
    assert c.get("/api/code/demo/file/data.bin").json() == {"path": "data.bin", "binary": True}
    assert c.get("/api/code/demo/file/labels.idx").json() == {"path": "labels.idx", "binary": True}

    raw = c.get("/code-raw/demo/src/plot.png")
    assert raw.content == PNG and raw.headers["content-type"] == "image/png"
    assert c.get("/api/code/nope").status_code == 404
    assert c.get("/api/code/nope").json() == {"detail": "project not found"}
    assert c.get("/api/code/demo/file/missing.py").status_code == 404


def test_code_raw_head(code_dir):
    c = TestClient(app.app)
    for path in ("src/plot.png", "main.py"):
        get = c.get(f"/code-raw/demo/{path}")
        head = c.head(f"/code-raw/demo/{path}")
        assert head.status_code == 200
        assert head.headers["content-type"] == get.headers["content-type"]
        assert head.headers["content-security-policy"] == "sandbox"


def test_readme_parent_links_blocked(code_dir):
    (code_dir / "demo" / "README.md").write_text(
        "[parent](../other/x.py) [far](../../etc) [main](main.py)"
    )
    readme = TestClient(app.app).get("/api/code/demo").json()["readme_html"]
    assert 'href="/code/demo/../other/x.py"' not in readme
    assert 'href="/code/demo/../../etc"' not in readme
    assert 'href="/code/demo/main.py"' in readme


def test_code_no_readme(code_dir):
    (code_dir / "demo" / "README.md").unlink()
    assert TestClient(app.app).get("/api/code/demo").json()["readme_html"] is None


def test_code_containment(code_dir):
    (code_dir / "secret.txt").write_text("outside secret")
    (code_dir / "demo" / "link.txt").symlink_to(code_dir / "secret.txt")
    (code_dir / "demo" / "linkdir").symlink_to(code_dir)
    (code_dir / "Bad").mkdir()
    (code_dir / "Bad" / "x.py").write_text("x")
    c = TestClient(app.app)

    assert "link.txt" not in c.get("/api/code/demo").json()["files"]
    assert c.get("/api/code/Bad").status_code == 404
    for path in [
        "/api/code/demo/file/../secret.txt",
        "/api/code/demo/file/%2e%2e/secret.txt",
        "/api/code/demo/file/%2e%2e%2fsecret.txt",
        f"/api/code/demo/file/{code_dir / 'secret.txt'}",
        "/api/code/demo/file/link.txt",
        "/api/code/demo/file/linkdir/secret.txt",
        "/code-raw/demo/%2e%2e/secret.txt",
        f"/code-raw/demo/{code_dir / 'secret.txt'}",
        "/code-raw/demo/link.txt",
        "/api/code/Bad",
        "/api/code/Bad/file/x.py",
        "/api/code/%2e%2e",
        "/code-raw/%2e%2e/secret.txt",
    ]:
        r = c.get(path)
        assert r.status_code == 404, path
        assert "outside secret" not in r.text, path


def run_sync(tmp_path, repo, *args):
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "code_sync.py"
    env = {**__import__("os").environ, "CODE_DIR": str(tmp_path / "code")}
    return subprocess.run([sys.executable, script, repo, *args], env=env, capture_output=True, text=True)


def make_repo(path, files):
    import subprocess

    path.mkdir()
    for name, content in files.items():
        (path / name).write_bytes(content)
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", path, *cmd], check=True)
    return path


def test_code_sync(tmp_path):
    repo = make_repo(tmp_path / "tiny", {
        "a.py": b"x = 1\n",
        "big.bin": b"0" * (1024 * 1024 + 1),
        "who.txt": b"by someone@example.com\n",
    })
    (repo / "a.py").write_text("x = 2\n")
    (repo / "new.py").write_text("new\n")

    r = run_sync(tmp_path, repo)
    assert r.returncode == 0, r.stderr
    out = tmp_path / "code" / "tiny"
    assert (out / "a.py").read_text() == "x = 1\n"
    assert not (out / "new.py").exists() and not (out / "big.bin").exists() and not (out / ".git").exists()
    assert "uncommitted changes" in r.stdout and "new.py" in r.stdout
    assert "skipped: big.bin" in r.stdout
    assert "warning: personal info: who.txt:1" in r.stdout
    assert [p.name for p in (tmp_path / "code").iterdir()] == ["tiny"]  # no temp dir left

    leaky = make_repo(tmp_path / "leaky", {"k.py": b'api_key = "abcdefghijklmnop"\n'})
    r = run_sync(tmp_path, leaky)
    assert r.returncode == 1 and "k.py:1" in r.stderr
    assert not (tmp_path / "code" / "leaky").exists()
    assert run_sync(tmp_path, leaky, "--name", "Bad_Name").returncode == 2


def test_tags(tmp_path, monkeypatch, caplog):
    (tmp_path / "list.md").write_text("---\ntitle: List\ndate: 2026-01-02\ntags: [ML, cuda, ml, 3]\n---\nbody")
    (tmp_path / "one.md").write_text("---\ntitle: One\ndate: 2026-01-01\ntags: notes\n---\nbody")
    (tmp_path / "bad.md").write_text("---\ntitle: Bad\ndate: 2026-01-01\ntags: [[x]]\n---\nbody")
    monkeypatch.setattr(app, "POSTS_DIR", tmp_path)

    c = TestClient(app.app)
    with caplog.at_level(logging.WARNING):
        result = c.get("/api/posts").json()
    assert [(p["slug"], p["tags"]) for p in result] == [("list", ["ml", "cuda", "3"]), ("one", ["notes"])]
    assert "invalid post metadata in bad.md" in caplog.messages
    assert c.get("/api/posts/one").json()["tags"] == ["notes"]


def test_now(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "PAGES_DIR", tmp_path)
    c = TestClient(app.app)
    assert c.get("/api/now").status_code == 404

    (tmp_path / "now.md").write_text("plain body")
    assert c.get("/api/now").json() == {"html": "<p>plain body</p>\n"}


INDEX = '<html><head><title>neenerweener</title></head><body><div id="root"> </div></body></html>'


def test_server_rendered_pages(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "hello.md").write_text(
        "---\ntitle: Hi & <bye>\ndate: 2026-01-01\ntags: [a]\n---\nFirst *para* </script> here.\n\nsecond"
    )
    (posts / "brief.md").write_text("---\ntitle: Brief\ndate: 2026-01-02\nexcerpt: short one\n---\nbody")
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "PAGES_DIR", tmp_path / "pages")
    c = TestClient(app.app)

    page = c.get("/posts/hello").text
    assert "<title>Hi &amp; &lt;bye&gt;</title>" in page
    assert page.count("<title>") == 1
    assert '<meta property="og:description" content="First para here." />' in page
    assert '<div id="root"><main><article><h1 class="has-back">' in page
    assert '<a href="/?tag=a">#a</a>' in page
    preload = page.split('<script id="preload" type="application/json">')[1].split("</script>")[0]
    assert "<" not in preload  # post text cannot close the script tag
    data = __import__("json").loads(preload)
    assert data["/api/posts/hello"]["title"] == "Hi & <bye>"
    assert data["/api/now"] is None

    assert '<meta name="description" content="short one" />' in c.get("/posts/brief").text
    missing = c.get("/posts/nope").text
    assert '"/api/posts/nope": null' in missing and "<title>neenerweener</title>" in missing
    home = c.get("/?tag=a").text
    assert '"/api/posts": [' in home and '<div id="root"></div>' in home


def test_image_caption():
    html = app.render_post('![a plot](/images/p.png "the caption")\n\n![no title](/images/q.png)')
    assert '<figure><img src="/images/p.png" alt="a plot" title="the caption" decoding="async" /><figcaption>the caption</figcaption></figure>' in html
    assert '<p><img src="/images/q.png" alt="no title" loading="lazy" decoding="async" /></p>' in html
