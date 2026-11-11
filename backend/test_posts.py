import concurrent.futures
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from markdown_it import MarkdownIt
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.exc import IntegrityError

import timeline as timeline_module
from posts import (
    InvalidPost,
    PostConflict,
    PostStore,
    web_post_media,
    web_post_revisions,
    web_post_slugs,
    web_post_tags,
    web_posts,
)
from timeline import (
    InvalidAttachment,
    TimelineStore,
    media_assets,
    media_sweep_claims,
    site_profile,
    timeline_entries,
    timeline_entry_media,
)

UTC = timezone.utc


@pytest.fixture
def post_store():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured")
    store = PostStore(database_url, renderer=MarkdownIt("commonmark").render)
    with store.engine.begin() as connection:
        _clear(connection)
    yield store
    with store.engine.begin() as connection:
        _clear(connection)
    store.close()


def _clear(connection):
    connection.execute(delete(media_sweep_claims))
    connection.execute(delete(timeline_entry_media))
    connection.execute(update(site_profile).where(site_profile.c.id == 1).values(avatar_asset_id=None))
    connection.execute(delete(timeline_entries))
    connection.execute(delete(web_post_media))
    connection.execute(delete(web_post_tags))
    connection.execute(delete(web_post_revisions))
    connection.execute(delete(web_post_slugs))
    connection.execute(delete(web_posts))
    connection.execute(delete(media_assets))


def _update(store, post, **changes):
    values = {
        "title": post["title"],
        "slug": post["slug"],
        "body_markdown": post["body_markdown"],
        "excerpt": post["excerpt"],
        "tags": post["tags"],
        "published_on": post["published_on"],
        "status": post["status"],
    }
    values.update(changes)
    return store.update(post["id"], post["version"], **values)


def _asset(store, *, age=timedelta()):
    asset_id = uuid.uuid4()
    with store.engine.begin() as connection:
        connection.execute(
            insert(media_assets).values(
                id=asset_id,
                width=1,
                height=1,
                byte_size=1,
                sha256=b"x",
                created_at=datetime.now(UTC) - age,
            )
        )
    return asset_id


def _image(asset_id):
    return f"![](/media/{asset_id}/lg.webp)"


def test_create_normalizes_tags_and_rejects_reserved_file_slug(post_store):
    post_store.reserved_slugs = lambda: {"file-post"}
    post = post_store.create(
        title="fixture",
        slug="web-post",
        body_markdown="",
        tags=[" Notes ", "notes", "ML", f"  {'x' * 50}  "],
    )

    assert (post["status"], post["version"], post["tags"], post["media"]) == (
        "draft",
        1,
        ["notes", "ml", "x" * 50],
        [],
    )
    with pytest.raises(InvalidPost, match="slug reserved by file post"):
        post_store.create(title="fixture", slug="file-post")


def test_publish_unpublish_and_rename_keep_publication_and_redirect_history(post_store):
    draft = post_store.create(title="fixture", slug="first")
    published = _update(
        post_store,
        draft,
        slug="second",
        body_markdown="body",
        status="live",
    )
    unpublished = _update(post_store, published, status="draft", published_on=None)

    assert published["published_on"] is not None
    assert published["published_at"] is not None
    assert unpublished["published_on"] == published["published_on"]
    assert unpublished["published_at"] == published["published_at"]
    with post_store.engine.connect() as connection:
        slugs = connection.execute(
            select(web_post_slugs.c.slug, web_post_slugs.c.canonical).order_by(web_post_slugs.c.slug)
        ).all()
    assert slugs == [("first", False), ("second", True)]
    assert [revision["action"] for revision in reversed(post_store.revisions(draft["id"]))] == [
        "publish",
        "unpublish",
    ]


def test_soft_delete_and_revision_restore_make_post_a_draft(post_store):
    original = post_store.create(title="original", slug="original", body_markdown="old", tags=["old"])
    changed = _update(
        post_store,
        original,
        title="changed",
        slug="changed",
        body_markdown="new",
        tags=["new"],
    )
    first_revision = post_store.revisions(original["id"])[0]
    deleted = post_store.delete(changed["id"], changed["version"])
    restored = post_store.restore(deleted["id"], first_revision["id"], deleted["version"])

    assert deleted["deleted_at"] is not None
    assert (restored["title"], restored["slug"], restored["body_markdown"], restored["tags"]) == (
        "original",
        "original",
        "old",
        ["old"],
    )
    assert (restored["status"], restored["deleted_at"], restored["version"]) == ("draft", None, 4)
    revisions = list(reversed(post_store.revisions(original["id"])))
    assert [(item["version"], item["action"], item["title"]) for item in revisions] == [
        (1, "rename", "original"),
        (2, "delete", "changed"),
        (3, "restore", "changed"),
    ]


def test_stale_version_reports_409_and_current_version(post_store):
    post = post_store.create(title="fixture", slug="fixture")
    current = _update(post_store, post, body_markdown="changed")

    with pytest.raises(PostConflict) as conflict:
        _update(post_store, post, body_markdown="stale")

    assert conflict.value.status_code == 409
    assert conflict.value.current_version == current["version"]
    assert post_store.get(post["id"])["body_markdown"] == "changed"


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"title": "x" * 201}, InvalidPost),
        ({"title": "bad\x00title"}, InvalidPost),
        ({"body_markdown": "bad\ud800body"}, InvalidPost),
        ({"body_markdown": "x" * 1_000_001}, InvalidPost),
        ({"excerpt": "x" * 2_001}, InvalidPost),
        ({"slug": "not trimmed "}, InvalidPost),
        ({"published_on": "01/02/2026"}, InvalidPost),
        ({"tags": ["x" * 51]}, InvalidPost),
        ({"tags": [str(i) for i in range(21)]}, InvalidPost),
    ],
)
def test_validation_rejects_invalid_unicode_sizes_and_shapes(post_store, changes, error):
    values = {"title": "fixture", "slug": "fixture", "body_markdown": "", "excerpt": None, "tags": []}
    values.update(changes)
    with pytest.raises(error):
        post_store.create(**values)


def test_unicode_limits_count_code_points(post_store):
    post = post_store.create(
        title="😀" * 200,
        slug="unicode",
        excerpt="é" * 2_000,
        tags=["界" * 50],
    )
    assert (len(post["title"]), len(post["excerpt"]), len(post["tags"][0])) == (200, 2_000, 50)


def test_renderer_failure_rolls_back_before_a_transaction(post_store):
    post_store.renderer = lambda _body: (_ for _ in ()).throw(ValueError("render failed"))
    with pytest.raises(ValueError, match="render failed"):
        post_store.create(title="fixture", slug="fixture", body_markdown="bad")
    assert post_store.list() == []


def test_media_attach_replace_delete_restore_and_liveness(post_store):
    first = _asset(post_store)
    second = _asset(post_store)
    post = post_store.create(title="fixture", slug="fixture", body_markdown=_image(first))
    changed = _update(post_store, post, body_markdown=_image(second))
    timeline = TimelineStore("unused", engine=post_store.engine)

    with post_store.engine.connect() as connection:
        detached = connection.execute(
            select(media_assets.c.id, media_assets.c.detached_at).where(media_assets.c.id.in_([first, second]))
        ).all()
    assert dict(detached)[first] is not None
    assert dict(detached)[second] is None
    assert timeline.is_in_use(second) is True

    deleted = post_store.delete(changed["id"], changed["version"])
    delete_revision = post_store.revisions(post["id"])[0]
    assert timeline.is_in_use(second) is False
    restored = post_store.restore(post["id"], delete_revision["id"], deleted["version"])
    assert restored["media"] == [str(second)]
    assert timeline.is_in_use(second) is True

    with post_store.engine.begin() as connection:
        connection.execute(
            update(media_assets).where(media_assets.c.id == first).values(detached_at=datetime.now(UTC) - timedelta(days=8))
        )
    assert first in timeline.expired_media_assets(20)
    assert second not in timeline.expired_media_assets(20)


def test_media_ownership_uses_rendered_local_images_only(post_store):
    markdown_asset = _asset(post_store)
    html_asset = _asset(post_store)
    inline_code_asset = _asset(post_store)
    fenced_code_asset = _asset(post_store)
    external_asset = _asset(post_store)
    body = f"""
`{_image(inline_code_asset)}`

```
{_image(fenced_code_asset)}
```

![external](https://example.com/media/{external_asset}/lg.webp)

{_image(markdown_asset)}

<img src="/media/{html_asset}/sm.webp">
"""

    post = post_store.create(title="fixture", slug="media-references", body_markdown=body)

    assert set(post["media"]) == {str(markdown_asset), str(html_asset)}


def test_external_media_path_does_not_reject_a_post(post_store):
    try:
        post = post_store.create(
            title="fixture",
            slug="external-media",
            body_markdown="![external](https://example.com/media/photo.jpg)",
        )
    except InvalidAttachment:
        post = None

    assert post is not None, "an external /media/ path must not be validated as a local asset"
    assert post["media"] == []


def test_duplicate_or_foreign_media_references_are_rejected(post_store):
    asset_id = _asset(post_store)
    duplicate = f"{_image(asset_id)}\n{_image(asset_id)}"
    with pytest.raises(InvalidAttachment):
        post_store.create(title="fixture", slug="duplicate", body_markdown=duplicate)
    with pytest.raises(InvalidAttachment):
        post_store.create(title="fixture", slug="malformed", body_markdown="![](/media/not-an-id/lg.webp)")

    post_store.create(title="fixture", slug="owner", body_markdown=_image(asset_id))
    with pytest.raises(InvalidAttachment):
        post_store.create(title="fixture", slug="other", body_markdown=_image(asset_id))


def test_identical_save_with_reordered_media_is_a_no_op(post_store):
    first, second = sorted((_asset(post_store), _asset(post_store)), reverse=True)
    body = f"{_image(first)}\n{_image(second)}"
    post = post_store.create(title="fixture", slug="no-op", body_markdown=body)

    unchanged = _update(post_store, post)

    assert unchanged["version"] == 1
    assert post_store.revisions(post["id"]) == []


def test_restore_and_sweep_serialize_on_the_asset(post_store, monkeypatch):
    asset_id = _asset(post_store)
    post = post_store.create(title="fixture", slug="race", body_markdown=_image(asset_id))
    deleted = post_store.delete(post["id"], post["version"])
    revision = post_store.revisions(post["id"])[0]
    lock_attempted = threading.Event()
    original_lock_assets = timeline_module._lock_assets

    def signaled_lock_assets(connection, asset_ids):
        lock_attempted.set()
        return original_lock_assets(connection, asset_ids)

    monkeypatch.setattr(timeline_module, "_lock_assets", signaled_lock_assets)
    timeline = TimelineStore("unused", engine=post_store.engine)
    with post_store.engine.begin() as connection:
        connection.execute(
            update(media_assets)
            .where(media_assets.c.id == asset_id)
            .values(detached_at=datetime.now(UTC) - timedelta(days=7) + timedelta(seconds=0.5))
        )
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        with post_store.engine.begin() as connection:
            connection.execute(select(media_assets.c.id).where(media_assets.c.id == asset_id).with_for_update()).one()
            restoring = pool.submit(post_store.restore, post["id"], revision["id"], deleted["version"])
            assert lock_attempted.wait(2), "restore did not reach the asset lock"
            time.sleep(0.6)
            selected = timeline.expired_media_assets(20)
        restored = restoring.result(timeout=2)
    finally:
        pool.shutdown(wait=True)

    assert not (
        asset_id in selected and str(asset_id) in restored["media"]
    ), "the sweep must not select an asset while restore owns its row lock"


def test_restore_refuses_an_asset_claimed_by_the_sweep(post_store):
    asset_id = _asset(post_store)
    post = post_store.create(title="fixture", slug="claimed", body_markdown=_image(asset_id))
    deleted = post_store.delete(post["id"], post["version"])
    revision = post_store.revisions(post["id"])[0]
    timeline = TimelineStore("unused", engine=post_store.engine)
    with post_store.engine.begin() as connection:
        connection.execute(
            update(media_assets)
            .where(media_assets.c.id == asset_id)
            .values(detached_at=datetime.now(UTC) - timedelta(days=8))
        )
    assert asset_id in timeline.expired_media_assets(20)
    with post_store.engine.begin() as connection:
        connection.execute(
            update(media_assets).where(media_assets.c.id == asset_id).values(detached_at=datetime.now(UTC))
        )

    with pytest.raises(InvalidAttachment, match="being swept"):
        post_store.restore(post["id"], revision["id"], deleted["version"])


def test_concurrent_slug_and_media_ownership_have_one_winner(post_store):
    asset_id = _asset(post_store)

    def create(index):
        try:
            return post_store.create(
                title="fixture",
                slug="same" if index < 2 else f"media-{index}",
                body_markdown="" if index < 2 else _image(asset_id),
            )["id"]
        except (InvalidPost, InvalidAttachment):
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        slug_results = list(executor.map(create, range(2)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        media_results = list(executor.map(create, range(2, 4)))

    assert sum(result is not None for result in slug_results) == 1
    assert sum(result is not None for result in media_results) == 1


@pytest.mark.parametrize(
    "path",
    ["timeline-create", "timeline-patch", "post-save", "revision-restore", "post-undelete"],
)
def test_all_media_attach_paths_share_the_ownership_gate(post_store, path):
    timeline = TimelineStore("unused", engine=post_store.engine)
    sequence = iter(range(100))

    def prepare(asset_id, *, duplicate=False):
        suffix = next(sequence)
        assets = [asset_id, asset_id] if duplicate else [asset_id]
        if path == "timeline-create":
            return lambda: timeline.create("fixture", [(item, "") for item in assets])
        if path == "timeline-patch":
            entry = timeline.create("before")
            return lambda: timeline.update(entry["id"], "after", [(item, "") for item in assets])

        post = post_store.create(title="fixture", slug=f"gate-{path}-{suffix}", body_markdown="")
        if path == "post-save":
            return lambda: _update(post_store, post, body_markdown="\n".join(_image(item) for item in assets))

        with_asset = _update(post_store, post, body_markdown=_image(asset_id))
        if path == "revision-restore":
            current = _update(post_store, with_asset, body_markdown="")
        else:
            current = post_store.delete(with_asset["id"], with_asset["version"])
        revision = post_store.revisions(post["id"])[0]
        if duplicate:
            with post_store.engine.begin() as connection:
                connection.execute(
                    update(web_post_revisions)
                    .where(web_post_revisions.c.id == revision["id"])
                    .values(body_markdown=f"{_image(asset_id)}\n{_image(asset_id)}")
                )
        return lambda: post_store.restore(post["id"], revision["id"], current["version"])

    success = _asset(post_store)
    assert prepare(success)() is not None
    assert timeline.is_in_use(success)

    claimed = _asset(post_store)
    claimed_call = prepare(claimed)
    with post_store.engine.begin() as connection:
        connection.execute(insert(media_sweep_claims).values(asset_id=claimed))
    with pytest.raises(InvalidAttachment):
        claimed_call()
    assert not timeline.is_in_use(claimed)

    concurrent_asset = _asset(post_store)
    concurrent_call = prepare(concurrent_asset)
    checked_out = threading.Event()
    backend_pid = []

    def capture_backend(dbapi_connection, _record, _proxy):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend_pid.append(cursor.fetchone()[0])
        checked_out.set()

    with post_store.engine.connect() as holder:
        transaction = holder.begin()
        holder.execute(select(media_assets.c.id).where(media_assets.c.id == concurrent_asset).with_for_update()).one()
        event.listen(post_store.engine, "checkout", capture_backend)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(concurrent_call)
            try:
                assert checked_out.wait(2), "attachment did not check out a database connection"
                event.remove(post_store.engine, "checkout", capture_backend)
                deadline = time.monotonic() + 2
                while holder.execute(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": backend_pid[0]},
                ).scalar_one() is not True:
                    assert time.monotonic() < deadline, "attachment did not wait for the asset row lock"
                    time.sleep(0.01)
                holder.execute(insert(media_sweep_claims).values(asset_id=concurrent_asset))
                transaction.commit()
            finally:
                if event.contains(post_store.engine, "checkout", capture_backend):
                    event.remove(post_store.engine, "checkout", capture_backend)
                if transaction.is_active:
                    transaction.rollback()
            try:
                result = future.result(timeout=5)
            except Exception as exc:
                result = exc
    assert isinstance(result, InvalidAttachment)
    with post_store.engine.connect() as connection:
        is_claimed = connection.execute(
            select(media_sweep_claims.c.asset_id).where(media_sweep_claims.c.asset_id == concurrent_asset)
        ).scalar_one_or_none()
    assert (timeline.is_in_use(concurrent_asset), is_claimed) == (False, concurrent_asset)

    foreign = _asset(post_store)
    foreign_call = prepare(foreign)
    with post_store.engine.begin() as connection:
        connection.execute(update(media_assets).where(media_assets.c.id == foreign).values(detached_at=None))
    timeline.create("other owner", [(foreign, "")])
    with pytest.raises(InvalidAttachment):
        foreign_call()

    duplicate = _asset(post_store)
    with pytest.raises(InvalidAttachment):
        prepare(duplicate, duplicate=True)()


def test_database_constraints_reject_invalid_direct_rows(post_store):
    with pytest.raises(IntegrityError):
        with post_store.engine.begin() as connection:
            connection.execute(
                insert(web_posts).values(
                    title="",
                    body_markdown="",
                    status="live",
                    published_on=None,
                    published_at=None,
                )
            )
