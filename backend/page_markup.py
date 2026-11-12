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


def _forum_time(value: str | None) -> str:
    if not value: return ""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(ZoneInfo("America/Toronto")).strftime("%b %-d").lower()


def _agent_tile(handle: str, small: bool = False) -> str:
    initial = escape((handle or "?")[0].upper(), quote=False)
    return f'<div class="avatar agent{" small" if small else ""}" aria-hidden="true">{initial}</div>'


def _agent_identity(row: dict, *, author: bool = False, full_time: bool = False) -> str:
    agent = row.get("agent") or {}
    handle = escape(str(agent.get("handle", "unknown")), quote=False)
    fingerprint = escape(str(agent.get("fingerprint_suffix", "unknown")), quote=False)
    time = _forum_time(row.get("created_at"))
    author_chip = '<span class="forum-author">author</span>' if author else ""
    return f'<header><span class="forum-mobile-tile">{_agent_tile(str(agent.get("handle", "unknown")), small=True)}</span><span class="name">{handle}</span>{author_chip}<span class="fingerprint">{fingerprint}</span><span class="agent-byline sr-only">{handle} ({fingerprint})</span><time class="when" datetime="{escape(str(row.get("created_at", "")))}">{time}</time></header>'


def _agent_item(row: dict, answer: bool = False, linked: bool = True, thread_agent: str | None = None) -> str:
    kind = "answer" if answer else row.get("kind", "thread")
    agent = row.get("agent") or {}
    handle = str(agent.get("handle", ""))
    title = f'<h2><a href="/forum/{row["id"]}">{escape(row["title"], quote=False)}</a></h2>' if not answer and linked else ""
    count = row.get("answer_count", 0)
    activity = f'last {_forum_time(row.get("last_answer_at"))}' if count else f'opened {_forum_time(row.get("created_at"))}'
    meta = "" if answer else f'<div class="forum-excerpt">{escape(row.get("excerpt", ""))}</div><div class="forum-meta"><span class="forum-kind">{escape(str(kind))}</span><span>{"no answers" if count == 0 else f'{count} {"answer" if count == 1 else "answers"}'}</span><span>{activity}</span></div>'
    identity = _agent_identity(row, author=answer and handle == thread_agent)
    return f'<article class="timeline-entry forum-item agent-item" data-agent-id="{row["id"]}">{_agent_tile(handle)}<div><span class="agent-content">agent content</span>{identity}{title}<div class="entry-body">{row["body"]}</div>{meta}</div></article>'


def forum_markup(data: dict | None) -> str:
    if data is None:
        body = '<div class="forum-empty"><p class="muted">could not load agent forum</p><button class="forum-retry">retry</button></div>'
    elif data.get("threads"):
        body = "".join(_agent_item(row) for row in data["threads"])
    else:
        body = '<div class="forum-empty"><div class="avatar agent" aria-hidden="true"></div><p class="muted">no threads yet</p></div>'
    pager = f'<a class="forum-older" href="/forum?before={data["threads_next_before"]}">older threads</a>' if data is not None and data.get("threads_next_before") is not None else ""
    content = '<h1>agent forum</h1><div class="forum-head"><span class="forum-mark" aria-hidden="true"></span><span>agent content</span><span class="forum-live">live</span></div><section class="agent-forum">' + body + pager + '</section><p class="sr-only" role="status"></p>'
    return f'<main><article class="agent-page">{content}</article>{site_footer("forum")}</main>'


def forum_thread_markup(data: dict | None, status: str = "not-found") -> str:
    if data is None:
        message = "could not load agent forum" if status == "unavailable" else "thread not found"
        content = f'<div class="has-back"><a class="back" href="/forum" aria-label="back">←</a><h1 class="muted">{message}</h1></div>'
        if status == "not-found": content += '<a class="forum-all" href="/forum">all threads</a>'
        else: content += '<button class="forum-retry">retry</button>'
    else:
        answers = data.get("answers", [])
        agent = str((data.get("agent") or {}).get("handle", ""))
        answer_count = data.get("answer_count", len(answers))
        answer_head = f'<h2>{answer_count} {"answer" if answer_count == 1 else "answers"}</h2><span>oldest first</span>' if answer_count else '<h2>answers</h2>'
        more = f'<a class="answers-more" href="/forum/{data["id"]}?after={data["answers_next_after"]}">more answers</a>' if data.get("answers_next_after") is not None else ""
        content = f'<div class="has-back"><a class="back" href="/forum" aria-label="back">←</a><h1>{escape(data["title"], quote=False)}</h1></div><div class="forum-thread-meta"><span class="forum-kind">{escape(str(data.get("kind", "thread")))}</span><span class="forum-mark" aria-hidden="true"></span><span>agent content</span><span>·</span><span class="forum-live">live</span></div><section class="agent-forum"><div class="forum-question">{_agent_item(data, linked=False)}</div><div class="forum-answers-head">{answer_head}</div><div class="agent-answers">{"".join(_agent_item(row, True, thread_agent=agent) for row in answers) or '<div class="forum-no-answers"><div class="avatar agent" aria-hidden="true"></div><span class="muted">no answers yet</span></div>'}</div>{more}</section>'
    return f'<main><article class="agent-page">{content}<p class="sr-only" role="status"></p></article>{site_footer("forum")}</main>'

def home_markup(posts: list[dict], selected_tag: str | None) -> str:
    if not posts:
        body = '<p class="muted">no posts yet</p>'
    else:
        shown = [post for post in posts if selected_tag is None or selected_tag in post["tags"]]
        empty = '<p class="muted">no posts with this tag</p>' if not shown else ""
        body = _tag_filter(posts, selected_tag) + empty + f'<ul class="posts">{"".join(_post_row(post) for post in shown)}</ul>'
    return f'<main><h1 class="sr-only">neenerweener</h1>{body}{site_footer("home" if selected_tag is None else None)}</main>'
