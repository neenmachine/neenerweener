from html import escape
from urllib.parse import quote


def _url_tag(tag: str) -> str:
    return quote(tag, safe="-_.!~*'()")


def _tags(tags: list[str]) -> str:
    return '<span class="tags">' + "".join(
        f'<a href="/?tag={_url_tag(tag)}">#{escape(tag, quote=False)}</a>' for tag in tags
    ) + "</span>"


def _tag_filter(posts: list[dict], selected_tag: str | None) -> str:
    counts: dict[str, int] = {}
    for post in posts:
        for tag in post["tags"]:
            counts[tag] = counts.get(tag, 0) + 1
    if not counts:
        return ""
    all_link = '<a href="/" aria-current="page">all</a>' if selected_tag is None else '<a href="/">all</a>'
    links = [all_link]
    # match JavaScript string comparison, which compares UTF-16 code units
    links.extend(
        f'<a href="/?tag={_url_tag(tag)}"'
        f'{" aria-current=\"page\"" if tag == selected_tag else ""}>#{escape(tag, quote=False)}<span class="muted"> {count}</span></a>'
        # The post loader guarantees that tags contain only Unicode scalar values.
        for tag, count in sorted(counts.items(), key=lambda item: item[0].encode("utf-16-be"))
    )
    return f'<nav class="tag-filter" aria-label="tags">{"".join(links)}</nav>'


def _footer(selected_tag: str | None) -> str:
    current = ' aria-current="page"' if selected_tag is None else ""
    return f'<footer><a href="/"{current}>neenerweener</a><a href="/timeline">timeline</a></footer>'


def _post_row(post: dict) -> str:
    excerpt = f'<div class="excerpt muted">{escape(post["excerpt"])}</div>' if post["excerpt"] else ""
    draft = '<span class="draft muted">draft</span>' if post["draft"] else ""
    return (
        f'<li><div class="post-info"><a href="/posts/{escape(post["slug"])}">{escape(post["title"])}</a>'
        f'{excerpt}{_tags(post["tags"])}</div>{draft}</li>'
    )


def home_markup(posts: list[dict], selected_tag: str | None) -> str:
    if not posts:
        body = '<p class="muted">no posts yet</p>'
    else:
        shown = [post for post in posts if selected_tag is None or selected_tag in post["tags"]]
        empty = '<p class="muted">no posts with this tag</p>' if not shown else ""
        body = _tag_filter(posts, selected_tag) + empty + f'<ul class="posts">{"".join(_post_row(post) for post in shown)}</ul>'
    return f'<main><h1 class="sr-only">neenerweener</h1>{body}{_footer(selected_tag)}</main>'
