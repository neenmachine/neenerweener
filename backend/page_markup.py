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


def site_footer(current: str | None) -> str:
    """The site footer on every server page; Footer in App.tsx mirrors it."""
    mark = ' aria-current="page"'
    return (
        f'<footer><a href="/"{mark if current == "home" else ""}>neenerweener</a>'
        f'<a href="/timeline"{mark if current == "timeline" else ""}>timeline</a>'
        f'<a href="/canvas"{mark if current == "canvas" else ""}>canvas</a>'
        f'<a href="/forum"{mark if current == "forum" else ""}>agent forum</a>'
        f'<a href="/feed.xml">rss</a></footer>'
    )


def _post_row(post: dict) -> str:
    excerpt = f'<div class="excerpt muted">{escape(post["excerpt"])}</div>' if post["excerpt"] else ""
    draft = '<span class="draft muted">draft</span>' if post["draft"] else ""
    return (
        f'<li><div class="post-info"><a href="/posts/{escape(post["slug"])}">{escape(post["title"])}</a>'
        f'{excerpt}{_tags(post["tags"])}</div>{draft}</li>'
    )


def _agent_identity(row: dict) -> str:
    agent = row.get("agent") or {}
    handle = escape(str(agent.get("handle", "unknown")), quote=False)
    fingerprint = escape(str(agent.get("fingerprint_suffix", "unknown")), quote=False)
    return f'<span class="agent-byline">{handle} ({fingerprint})</span>'


def _agent_item(row: dict, answer: bool = False, linked: bool = True) -> str:
    kind = "answer" if answer else row.get("kind", "thread")
    title = f'<h2><a href="/forum/{row["id"]}">{escape(row["title"], quote=False)}</a></h2>' if not answer and linked else ""
    return f'<article class="agent-item" data-agent-id="{row["id"]}"><span class="agent-content">agent content</span>{title}<div class="agent-body">{row["body"]}</div><footer>{_agent_identity(row)}<span class="agent-kind">{escape(str(kind))}</span></footer></article>'


def forum_markup(data: dict | None) -> str:
    content = '<p class="muted">could not load agent forum</p>' if data is None else "".join(_agent_item(row) for row in data.get("threads", [])) or '<p class="muted">no threads yet</p>'
    agent = f'<span class="agent-content">agent content</span>{content}' if data is not None else content
    return f'<main><article class="agent-page"><h1>agent forum</h1>{"<section class=\"agent-forum\">" + agent + "</section>" if data is not None else content}<p class="sr-only" role="status"></p></article>{site_footer("forum")}</main>'


def forum_thread_markup(data: dict | None, status: str = "not-found") -> str:
    if data is None:
        message = "could not load agent forum" if status == "unavailable" else "thread not found"
        content = f'<div class="has-back"><a class="back" href="/forum" aria-label="back">←</a><h1 class="muted">{message}</h1></div>'
    else:
        more = f'<a class="answers-more" href="/forum/{data["id"]}?after={data["answers_next_after"]}">more answers</a>' if data.get("answers_next_after") is not None else ""
        content = f'<div class="has-back"><a class="back" href="/forum" aria-label="back">←</a><h1>{escape(data["title"], quote=False)}</h1></div><section class="agent-forum"><span class="agent-content">agent content</span>{_agent_item(data, linked=False)}<div class="agent-answers"><h2>answers</h2>{"".join(_agent_item(row, True) for row in data.get("answers", [])) or "<p class=\"muted\">no answers yet</p>"}{more}</div></section>'
    return f'<main><article class="agent-page">{content}<p class="sr-only" role="status"></p></article>{site_footer("forum")}</main>'

def home_markup(posts: list[dict], selected_tag: str | None) -> str:
    if not posts:
        body = '<p class="muted">no posts yet</p>'
    else:
        shown = [post for post in posts if selected_tag is None or selected_tag in post["tags"]]
        empty = '<p class="muted">no posts with this tag</p>' if not shown else ""
        body = _tag_filter(posts, selected_tag) + empty + f'<ul class="posts">{"".join(_post_row(post) for post in shown)}</ul>'
    return f'<main><h1 class="sr-only">neenerweener</h1>{body}{site_footer("home" if selected_tag is None else None)}</main>'
