import asyncio
import logging
import re
import threading
import time
from datetime import date

import app
import pytest
from fastapi.testclient import TestClient


def test_index_html_redirects_to_directory_and_keeps_query(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>")
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    with TestClient(app.app) as client:
        assert client.get("/index.html", follow_redirects=False).status_code == 301
        assert client.get("/index.html", follow_redirects=False).headers["location"] == "/"
        assert client.head("/index.html", follow_redirects=False).status_code == 301
        assert client.get("/index.html?tag=x", follow_redirects=False).headers["location"] == "/?tag=x"


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


def test_admin_spa_is_noindex_and_public_pages_do_not_link_to_it(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        '<html><head><title>app</title></head><body><div id="root"></div></body></html>'
    )
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    with TestClient(app.app) as client:
        for path in ("/admin", "/admin/"):
            admin = client.get(path)
            assert admin.status_code == 200
            assert admin.headers["X-Robots-Tag"] == "noindex"
            assert '"not-found"' not in admin.text
            head = client.head(path)
            assert head.status_code == 200
            assert head.headers["X-Robots-Tag"] == "noindex"
        for path in ("/admin/settings", "/admin/a/b"):
            missing = client.get(path)
            assert missing.status_code == 404
            assert missing.headers["X-Robots-Tag"] == "noindex"
            assert h1s(missing.text) == ["not found"]
            assert client.head(path).status_code == 404
            assert client.head(path).headers["X-Robots-Tag"] == "noindex"
        missing = client.get("/missing")
        assert missing.status_code == 404
        assert missing.headers["X-Content-Type-Options"] == "nosniff"
        assert missing.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert missing.headers["X-Frame-Options"] == "DENY"
        assert client.head("/admin").headers["X-Robots-Tag"] == "noindex"
        for path in ("/", "/timeline"):
            public = client.get(path)
            assert "X-Robots-Tag" not in public.headers
            assert 'href="/admin"' not in public.text


def test_only_admin_page_has_no_javascript_notice(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<html><head></head><body><div id="root"></div></body></html>')
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "hello.md").write_text("---\ntitle: Hi\ndate: 2026-01-01\n---\nbody\n")
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    with TestClient(app.app) as client:
        for path in ("/admin", "/admin/"):
            response = client.get(path)
            assert response.status_code == 200
            page = response.text
            assert "<title>admin</title>" in page
            noscript = re.search(r"<noscript>(.*?)</noscript>", page, re.S)[1]
            assert '<p class="muted">the admin panel needs JavaScript</p>' in noscript
            assert "<h1>admin</h1>" in noscript
            assert '<footer><a href="/">neenerweener</a><a href="/timeline">timeline</a></footer>' in noscript
        for path in ("/", "/timeline", "/posts/hello"):
            page = client.get(path)
            assert page.status_code in (200, 503)  # 503: /timeline has no database in tests
            assert "<noscript" not in page.text
            assert "the admin panel" not in page.text
        missing = client.get("/admin/x")
        assert missing.status_code == 404
        assert "<noscript" not in missing.text
        assert "the admin panel" not in missing.text


def test_lifespan_closes_stores_sequentially_off_event_loop(monkeypatch):
    close_calls = []

    class BlockingStore:
        def __init__(self, name):
            self.name = name

        def close(self):
            time.sleep(0.01)
            close_calls.append((self.name, threading.get_ident()))

    monkeypatch.setattr(app, "timeline_store", BlockingStore("timeline"))
    monkeypatch.setattr(app, "auth_store", BlockingStore("auth"))

    async def run_lifespan():
        loop_thread = threading.get_ident()
        async with app.lifespan(app.app):
            pass
        return loop_thread

    loop_thread = asyncio.run(run_lifespan())

    assert [name for name, _ in close_calls] == ["timeline", "auth"]
    assert all(thread_id != loop_thread for _, thread_id in close_calls)


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
    assert '<h2 class="readme-h1">demo</h2>' in readme  # the page crumbs are the one h1
    assert "<h1" not in readme
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


def test_readme_markdown_headings_are_demoted_with_source_classes(code_dir):
    readme = code_dir / "demo" / "README.md"
    readme.write_text("# one\n\n## two\n\n### three\n\n#### four\n\n##### five\n\n###### six\n")
    html = TestClient(app.app).get("/api/code/demo").json()["readme_html"]
    assert html == (
        '<h2 class="readme-h1">one</h2>\n<h3 class="readme-h2">two</h3>\n<h4 class="readme-h3">three</h4>\n'
        '<h5 class="readme-h4">four</h5>\n<h6 class="readme-h5">five</h6>\n<h6 class="readme-h6">six</h6>\n'
    )


@pytest.mark.parametrize(
    "source, expected",
    [
        # shortcut: raw HTML headings are trusted README content and keep their level; demoting them needs an HTML parser.
        (
            '<h1 title="a > b">raw</h1><pre><code><h1>literal</h1></code></pre><script>"<h1>literal</h1>"</script><div><h10>ten</h10></div>\n',
            '<h1 title="a > b">raw</h1><pre><code><h1>literal</h1></code></pre><script>"<h1>literal</h1>"</script><div><h10>ten</h10></div>\n',
        ),
        (
            "```\n# x\n<h1>literal</h1>\n```\n",
            "<pre><code># x\n&lt;h1&gt;literal&lt;/h1&gt;\n</code></pre>\n",
        ),
    ],
)
def test_readme_raw_html_and_code_keep_their_headings(code_dir, source, expected):
    (code_dir / "demo" / "README.md").write_text(source)
    assert TestClient(app.app).get("/api/code/demo").json()["readme_html"] == expected


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


def test_old_now_redirects_and_api_is_gone():
    c = TestClient(app.app)
    for path in ("/now", "/now/"):
        for method in ("get", "head"):
            response = getattr(c, method)(path, follow_redirects=False)
            assert response.status_code == 301
            assert response.headers["location"] == "/timeline"
    assert c.get("/api/now").status_code == 404


INDEX = '<html><head><title>neenerweener</title></head><body><div id="root"> </div></body></html>'


def test_ssr_timeline_unavailable_is_503(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    class BrokenStore:
        def list(self, before, limit):
            raise RuntimeError("database password and post body must not leak")

    monkeypatch.setattr(app, "timeline_store", BrokenStore())
    response = TestClient(app.app).get("/timeline")
    assert response.status_code == 503
    assert "could not load timeline" in response.text
    assert "database password" not in response.text


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
    class Store:
        def list(self, before, limit):
            from datetime import datetime, timezone
            return [{"id": 2, "body_markdown": "first & <safe>", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc), "edited_at": datetime(2026, 1, 3, tzinfo=timezone.utc)}]

    monkeypatch.setattr(app, "timeline_store", Store())
    c = TestClient(app.app)

    page = c.get("/posts/hello").text
    assert "<title>Hi &amp; &lt;bye&gt;</title>" in page
    assert page.count("<title>") == 1
    assert '<meta property="og:description" content="First para here." />' in page
    assert '<div id="root"><main><article><div class="has-back post-head">' in page
    assert '<footer><a href="/">neenerweener</a><a href="/timeline">timeline</a></footer>' in page
    assert '<a href="/?tag=a">#a</a>' in page
    preload = page.split('<script id="preload" type="application/json">')[1].split("</script>")[0]
    assert "<" not in preload  # post text cannot close the script tag
    data = __import__("json").loads(preload)
    assert data["/api/posts/hello"]["title"] == "Hi & <bye>"
    assert "/api/now" not in data

    timeline = c.get("/timeline").text
    assert "<title>timeline</title>" in timeline
    assert '<meta name="description" content="timeline" />' in timeline
    assert 'id="entry-2"' in timeline
    assert 'title="January 2, 2026 at 12:00 AM"' in timeline
    assert 'datetime="2026-01-02T00:00:00.000Z"' in timeline
    assert 'aria-label="January 2, 2026 at 12:00 AM"' in timeline
    assert '<span class="edited" title="January 3, 2026 at 12:00 AM">edited<span class="sr-only"> January 3, 2026 at 12:00 AM</span></span>' in timeline
    assert '>jan 2<' in timeline
    assert '<footer><a href="/">neenerweener</a><a href="/timeline" aria-current="page">timeline</a></footer>' in timeline
    assert "first &amp; &lt;safe&gt;" in timeline
    assert '"/api/timeline"' in timeline
    assert "<" not in timeline.split('<script id="preload" type="application/json">')[1].split("</script>")[0]

    assert '<meta name="description" content="short one" />' in c.get("/posts/brief").text
    missing = c.get("/posts/nope")
    assert missing.status_code == 404
    assert '{"not-found": "post not found"}' in missing.text and '<meta name="robots" content="noindex">' in missing.text
    home = c.get("/?tag=a").text
    assert '"/api/posts": [' in home and '<div id="root"><main><h1 class="sr-only">neenerweener</h1></main></div>' in home


@pytest.mark.parametrize(
    ("query", "expected_id", "next_cursor", "status_code"),
    [
        ("", 1, 11, 200),
        ("?before=11", 11, 22, 200),
        ("?before=nope", None, None, 400),
        ("?before=11&before=22", None, None, 400),
        ("?before=11&before=nope", None, None, 400),
        ("?before=", None, None, 400),
        ("?before=0", None, None, 400),
        ("?before=-1", None, None, 400),
        ("?before=22", 22, None, 200),
        ("?before=33", 33, 11, 200),
    ],
)
def test_timeline_ssr_and_preload_share_one_initial_page(tmp_path, monkeypatch, query, expected_id, next_cursor, status_code):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    class Store:
        def list(self, before, limit):
            from datetime import datetime, timezone
            row_id = before or 1
            count = 21 if next_cursor is not None else 1
            rows = [{"id": row_id, "body_markdown": f"entry {row_id}", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "edited_at": None} for _ in range(count)]
            if next_cursor is not None:
                rows[19]["id"] = next_cursor
            return rows

    monkeypatch.setattr(app, "timeline_store", Store())
    response = TestClient(app.app).get("/timeline" + query)
    assert response.status_code == status_code
    if status_code == 400:
        assert response.json() == {"detail": "invalid timeline cursor"}
        assert 'id="preload"' not in response.text
        return
    html = response.text
    assert len(h1s(html)) == 1
    preload = html.split('<script id="preload" type="application/json">')[1].split("</script>")[0]
    data = __import__("json").loads(preload)
    assert set(data) == {"/api/timeline"}
    assert data["/api/timeline"]["entries"][0]["id"] == expected_id
    assert f'id="entry-{expected_id}"' in html
    control = f'<div class="load-more"><a href="/timeline?before={next_cursor}">load more</a></div>'
    assert (control in html) is (next_cursor is not None)


def test_timeline_empty_and_unavailable_are_distinct(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)

    class EmptyStore:
        def list(self, before, limit):
            return []

    monkeypatch.setattr(app, "timeline_store", EmptyStore())
    empty = TestClient(app.app).get("/timeline").text
    assert '"/api/timeline": {"entries": [], "next_cursor": null}' in empty
    assert "nothing here yet" in empty
    assert "could not load timeline" not in empty

    monkeypatch.setattr(app, "timeline_store", None)
    unavailable = TestClient(app.app).get("/timeline").text
    assert '"/api/timeline": null' in unavailable
    assert "could not load timeline" in unavailable
    assert "nothing here yet" not in unavailable

    class MalformedPageStore:
        def list(self, before, limit):
            return [{"id": 30 - i} for i in range(limit + 1)]

    monkeypatch.setattr(app, "timeline_store", MalformedPageStore())
    malformed = TestClient(app.app).get("/timeline").text
    assert '"/api/timeline": {"entries": [], "next_cursor": 11}' in malformed
    assert "nothing here yet" not in malformed
    assert '<div class="load-more"><a href="/timeline?before=11">load more</a></div>' in malformed

    class BrokenStore:
        def list(self, before, limit):
            raise RuntimeError("database details must not reach the page")

    monkeypatch.setattr(app, "timeline_store", BrokenStore())
    broken = TestClient(app.app).get("/timeline")
    assert broken.status_code == 503
    assert "could not load timeline" in broken.text
    assert "database details" not in broken.text


@pytest.mark.parametrize(
    ("page", "has_control", "href"),
    [
        ({"entries": [], "next_cursor": 11}, True, "/timeline?before=11"),
        ({"entries": [], "next_cursor": None}, False, None),
        ({"entries": [{"id": 1, "html": "<p>x</p>", "created_at": "2026-01-01T00:00:00+00:00"}], "next_cursor": 11}, True, "/timeline?before=11"),
        ({"entries": [{"id": 1, "html": "<p>x</p>", "created_at": "2026-01-01T00:00:00+00:00"}], "next_cursor": None}, False, None),
    ],
)
def test_timeline_markup_pagination_control_matches_cursor(page, has_control, href):
    markup = app.timeline_markup(page)
    control = '<div class="load-more"><a href="' + href + '">load more</a></div>' if href else ""
    assert (control != "" and control in markup) is has_control
    assert ('class="load-more"' in markup) is has_control


def test_timeline_timestamp_format_is_utc_and_year_aware():
    assert app.compact_timestamp("2025-03-01T23:30:00-05:00", date(2026, 11, 7)) == "mar 2, 2025"
    assert app.compact_timestamp("2026-11-07T00:30:00-05:00", date(2026, 11, 7)) == "nov 7"
    assert app.full_timestamp("2026-11-07T00:30:00-05:00") == "November 7, 2026 at 5:30 AM"


def test_image_caption():
    html = app.render_post('![a plot](/images/p.png "the caption")\n\n![no title](/images/q.png)')
    assert '<figure><img src="/images/p.png" alt="a plot" title="the caption" /><figcaption>the caption</figcaption></figure>' in html
    assert '<p><img src="/images/q.png" alt="no title" /></p>' in html


def h1s(page: str) -> list[str]:
    import re
    return re.findall(r"<h1[^>]*>(.*?)</h1>", page.split('<div id="root">')[1], re.S)


def test_post_ssr_h1_contains_only_title(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "hello.md").write_text("---\ntitle: Hi & <bye>\ndate: 2026-01-01\n---\nbody")
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    page = TestClient(app.app).get("/posts/hello").text
    assert h1s(page) == ["Hi &amp; &lt;bye&gt;"]
    # The back link stays first in the article, just outside the heading.
    assert '<article><div class="has-back post-head"><a class="back" href="/" aria-label="back">←</a><h1>' in page


def test_home_ssr_has_one_h1(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    posts = tmp_path / "posts"
    posts.mkdir()
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    c = TestClient(app.app)
    for url in ("/", "/?tag=a"):
        page = c.get(url).text
        assert h1s(page) == ["neenerweener"]
        assert '<h1 class="sr-only">neenerweener</h1>' in page


@pytest.mark.parametrize(
    ("url", "expected_h1s"),
    [
        ("/", 1),
        ("/?tag=a", 1),
        ("/posts/hello", 1),
        ("/timeline", 1),
        ("/nope", 1),
        ("/posts/missing", 1),
        ("/code/missing", 1),
        ("/code/demo/missing.py", 1),
        ("/code/demo", 0),
        ("/code/demo/hello.py", 0),
        ("/admin", 1),
        ("/admin/settings", 1),
    ],
)
def test_ssr_route_heading_table(tmp_path, monkeypatch, url, expected_h1s):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(INDEX)
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "hello.md").write_text("---\ntitle: Hi\ndate: 2026-01-01\ntags: [a]\n---\nbody")
    code = tmp_path / "code" / "demo"
    code.mkdir(parents=True)
    (code / "hello.py").write_text("print('hello')")
    monkeypatch.setattr(app, "FRONTEND_DIST", dist)
    monkeypatch.setattr(app, "POSTS_DIR", posts)
    monkeypatch.setattr(app, "CODE_DIR", tmp_path / "code")

    class Store:
        def list(self, before, limit):
            return []

    monkeypatch.setattr(app, "timeline_store", Store())
    page = TestClient(app.app).get(url).text
    assert len(h1s(page)) == expected_h1s
