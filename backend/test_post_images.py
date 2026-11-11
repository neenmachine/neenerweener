import re

import pytest

import app
import post_images

# Synthetic, non-shipping fixtures: placeholder words only.
A = "12345678-1234-1234-1234-123456789abc"
B = "abcdef01-2345-6789-abcd-ef0123456789"
IMAGE = f"![](/media/{A}/lg.webp)"
MARKERS = re.compile(r"<!--ween-block:\d+-->\n")

MIXED = f"""# alpha

beta  with two  spaces and $x^2$ math



{IMAGE}

```mermaid
graph TD; a-->b
```

<div class="gamma">
raw html
</div>

```python
x = "![](/media/{B}/lg.webp)"
```

$$
y = 1
$$

delta ![b](/media/{B}/sm.webp "caption") epsilon
zeta
- one
- two

![cap](/images/x.png "caption")
"""


def edit(fn, body, *args):
    return fn(app.md, body, *args)


def images(body):
    return post_images.parse(app.md, body)[1].images


def blocks(body):
    return len(post_images.parse(app.md, body)[1].block_starts)


def assert_outside_unchanged(before: str, after: str, token: str) -> None:
    """after differs from before only by where token sits and by blank lines at the removal and insertion sites."""
    assert before.count(token) == after.count(token) == 1

    def lines_and_gaps(text):
        lines, gaps = [], [0]
        for line in text.replace(token, "").split("\n"):
            if line.strip():
                lines.append(line)
                gaps.append(0)
            else:
                gaps[-1] += 1
        return lines, gaps

    (a_lines, a_gaps), (b_lines, b_gaps) = lines_and_gaps(before), lines_and_gaps(after)
    assert a_lines == b_lines  # every non-blank line, exactly, in order
    assert sum(x != y for x, y in zip(a_gaps, b_gaps)) <= 2  # blank lines change only where it left and landed


def corpus():
    paths = sorted(app.POSTS_DIR.glob("*.md"))
    assert paths, "no tracked posts found"
    return [pytest.param(app.load(path)["body"], id=path.name) for path in paths]


@pytest.mark.parametrize("body", corpus())
def test_corpus_preview_is_the_real_rendering_and_edits_keep_every_other_byte(body):
    # The admin preview is the production rendering plus block markers only.
    assert MARKERS.sub("", post_images.preview(app.md, body, app.caption_figures)["html"]) == app.render_post(body)
    # No-op: an image already in place stays put, so the body comes back byte for byte.
    with_image = IMAGE + "\n\n" + body
    assert edit(post_images.move, with_image, A, 0) == with_image
    assert edit(post_images.move, with_image, A, 1) == with_image
    count = blocks(with_image)
    for target in range(count + 1):
        moved = edit(post_images.move, with_image, A, target)
        assert_outside_unchanged(with_image, moved, IMAGE)
        assert blocks(moved) == count
        assert images(moved)[A].standalone
        back = edit(post_images.move, moved, A, 0)
        assert_outside_unchanged(with_image, back, IMAGE)
        assert back.startswith(IMAGE + "\n\n")
    resized = edit(post_images.resize, with_image, A, 300)
    assert resized == f'<img src="/media/{A}/lg.webp" alt="" width="300">' + "\n\n" + body
    assert app.render_post(resized).count('width="300"') == 1


def test_preview_metadata_names_only_editable_managed_images():
    result = post_images.preview(app.md, MIXED, app.caption_figures)
    assert MARKERS.sub("", result["html"]) == app.render_post(MIXED)
    assert result["blocks"] == blocks(MIXED) == 10
    # B is referenced twice (once inside code), and its sm variant is not the managed form: not editable.
    assert result["images"] == [{"id": A, "block": 2, "standalone": True, "width": None}]
    assert result["html"].count("<!--ween-block:") == 11
    span = images(MIXED)[A]
    assert MIXED[span.start : span.end] == IMAGE


@pytest.mark.parametrize("target", range(11))
def test_move_to_every_block_keeps_every_other_byte_and_renders(target):
    moved = edit(post_images.move, MIXED, A, target)
    assert_outside_unchanged(MIXED, moved, IMAGE)
    html = app.render_post(moved)
    assert html.count(f"/media/{A}/lg.webp") == 1
    assert '<pre class="mermaid">' in html and "y = 1" in html and '<div class="gamma">' in html
    if target not in (2, 3):
        assert images(moved)[A].block == (target if target < 2 else target - 1)


def test_inline_image_moves_out_of_its_line_to_stand_alone():
    body = f"one {IMAGE} two\n\nthree\n"
    assert not images(body)[A].standalone
    assert edit(post_images.move, body, A, 0) == f"{IMAGE}\n\none  two\n\nthree\n"
    assert edit(post_images.move, body, A, 1) == f"one  two\n\n{IMAGE}\n\nthree\n"
    assert edit(post_images.move, body, A, 2) == f"one  two\n\nthree\n\n{IMAGE}\n"
    # The body keeps its own ending: no final line break is added where there was none.
    assert edit(post_images.move, body.rstrip("\n"), A, 2) == f"one  two\n\nthree\n\n{IMAGE}"


def test_move_adds_a_blank_line_where_the_block_before_has_none():
    body = f"{IMAGE}\n\npara\n# head\n"
    assert edit(post_images.move, body, A, 2) == f"para\n\n{IMAGE}\n\n# head\n"
    assert images(edit(post_images.move, body, A, 2))[A].block == 1


def test_move_keeps_crlf_bytes_outside_the_image():
    body = f"one\r\n\r\n{IMAGE}\r\n\r\ntwo\r\n"
    moved = edit(post_images.move, body, A, 0)
    assert moved.startswith(IMAGE + "\n\n") and "one\r\n\r\n" in moved and moved.endswith("two\r\n")


def test_first_resize_writes_raw_html_and_later_ones_change_only_the_width():
    body = f'para\n\n![a "q" <b> *c*](/media/{A}/lg.webp)\n\nend\n'
    first = edit(post_images.resize, body, A, 640)
    # The alt is what the renderer gave the Markdown image (markup inside alt is dropped), escaped.
    assert first == f'para\n\n<img src="/media/{A}/lg.webp" alt="a &quot;q&quot;  c" width="640">\n\nend\n'
    assert 'alt="a &quot;q&quot;  c"' in app.render_post(body)
    second = edit(post_images.resize, first, A, 320)
    assert second == first.replace('width="640"', 'width="320"')
    assert images(second)[A].width == 320
    assert f'<img src="/media/{A}/lg.webp" alt="a &quot;q&quot;  c" width="320">' in app.render_post(second)
    # A hand-written <img> without a width gets one, and nothing else in it changes.
    hand = f"x\n\n<img alt='k' src='/media/{A}/lg.webp' />\n"
    assert edit(post_images.resize, hand, A, 50) == f"x\n\n<img alt='k' src='/media/{A}/lg.webp' width=\"50\" />\n"


def test_resized_captioned_image_stays_a_figure_and_still_moves_whole():
    body = f'![a](/media/{A}/lg.webp "cap")\n\nend\n'
    resized = edit(post_images.resize, body, A, 200)
    assert resized == f'<figure><img src="/media/{A}/lg.webp" alt="a" width="200"><figcaption>cap</figcaption></figure>\n\nend\n'
    assert images(resized)[A].standalone
    token = resized.split("\n")[0]
    moved = edit(post_images.move, resized, A, 2)
    assert moved == f"end\n\n{token}\n"
    assert edit(post_images.resize, moved, A, 100) == moved.replace('width="200"', 'width="100"')


@pytest.mark.parametrize(
    "body",
    [
        f"```\n{IMAGE}\n```\n",  # inside code
        f"{IMAGE}\n\n{IMAGE}\n",  # twice
        f"![x][r]\n\n[r]: /media/{A}/lg.webp\n",  # reference style
        f"![](/media/{A.upper()}/lg.webp)\n",  # not the stored form
    ],
)
def test_ambiguous_or_unmanaged_references_are_not_editable(body):
    assert A not in images(body)
    with pytest.raises(ValueError):
        edit(post_images.move, body, A, 0)
    with pytest.raises(ValueError):
        edit(post_images.resize, body, A, 10)


@pytest.mark.parametrize("width", [0, -1, 2049])
def test_resize_rejects_widths_outside_the_large_variant(width):
    with pytest.raises(ValueError):
        edit(post_images.resize, IMAGE, A, width)


@pytest.mark.parametrize("target", [-1, 2])
def test_move_rejects_positions_outside_the_body(target):
    with pytest.raises(ValueError):
        edit(post_images.move, IMAGE + "\n", A, target)


@pytest.mark.parametrize(
    ("body", "tag"),
    [
        (f'one <img src="/media/{A}/lg.webp"> two -> three\n\npara\n', f'<img src="/media/{A}/lg.webp">'),
        (f'<div>\n<img src="/media/{A}/lg.webp">\n</div>\n\npara\n', f'<img src="/media/{A}/lg.webp">'),
        (f'<img src="/media/{A}/lg.webp">\n\npara\n', f'<img src="/media/{A}/lg.webp">'),
    ],
    ids=["inline", "in-div", "alone"],
)
def test_raw_img_span_ends_at_its_own_tag_when_src_is_the_last_attribute(body, tag):
    assert A in images(body), "the image is editable"
    span = images(body)[A]
    assert body[span.start : span.end] == tag
    for target in (0, blocks(body)):
        assert_outside_unchanged(body, edit(post_images.move, body, A, target), tag)
