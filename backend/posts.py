"""Transactional storage for posts created in the web admin."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Collection, Sequence
from datetime import date, datetime
from html.parser import HTMLParser

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from timeline import (
    CONNECT_TIMEOUT_SECONDS,
    POOL_TIMEOUT_SECONDS,
    STATEMENT_TIMEOUT_MS,
    TORONTO,
    InvalidAttachment,
    _lock_assets,
    _replace_media_ownership,
    database_engine,
    media_assets,
    metadata,
    site_profile,
)

MAX_TITLE_CHARS = 200
MAX_EXCERPT_CHARS = 2_000
MAX_BODY_CHARS = 1_000_000
MAX_TAGS = 20
MAX_TAG_CHARS = 50
SLUG = re.compile(r"[a-z0-9-]{1,100}\Z")
MANAGED_MEDIA_REFERENCE = re.compile(r"/media/([0-9a-fA-F-]{36})/(?:sm|lg)\.webp\Z")
ACTIONS = ("save", "publish", "unpublish", "rename", "delete", "restore")

web_posts = Table(
    "web_posts",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("title", Text, nullable=False),
    Column("body_markdown", Text, nullable=False),
    Column("excerpt", Text),
    Column("status", Text, nullable=False),
    Column("published_on", Date),
    Column("published_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("deleted_at", DateTime(timezone=True)),
    CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ck_web_posts_title_length"),
    CheckConstraint("excerpt IS NULL OR char_length(excerpt) <= 2000", name="ck_web_posts_excerpt_length"),
    CheckConstraint("char_length(body_markdown) <= 1000000", name="ck_web_posts_body_length"),
    CheckConstraint("status IN ('draft', 'live')", name="ck_web_posts_status"),
    CheckConstraint("version > 0", name="ck_web_posts_version_positive"),
    CheckConstraint(
        "status = 'draft' OR (published_on IS NOT NULL AND published_at IS NOT NULL)",
        name="ck_web_posts_live_publication",
    ),
)
web_post_slugs = Table(
    "web_post_slugs",
    metadata,
    Column("slug", Text, primary_key=True),
    Column("post_id", BigInteger, ForeignKey("web_posts.id"), nullable=False),
    Column("canonical", Boolean, nullable=False),
    CheckConstraint("char_length(slug) BETWEEN 1 AND 100", name="ck_web_post_slugs_length"),
    CheckConstraint("slug ~ '^[a-z0-9-]+$'", name="ck_web_post_slugs_format"),
)
Index(
    "uq_web_post_slugs_canonical_post",
    web_post_slugs.c.post_id,
    unique=True,
    postgresql_where=web_post_slugs.c.canonical,
)
web_post_tags = Table(
    "web_post_tags",
    metadata,
    Column("post_id", BigInteger, ForeignKey("web_posts.id", ondelete="CASCADE"), nullable=False),
    Column("position", SmallInteger, nullable=False),
    Column("tag", Text, nullable=False),
    PrimaryKeyConstraint("post_id", "position"),
    UniqueConstraint("post_id", "tag"),
    CheckConstraint("position BETWEEN 0 AND 19", name="ck_web_post_tags_position"),
    CheckConstraint("char_length(tag) BETWEEN 1 AND 50", name="ck_web_post_tags_length"),
)
web_post_media = Table(
    "web_post_media",
    metadata,
    Column("post_id", BigInteger, ForeignKey("web_posts.id", ondelete="CASCADE"), nullable=False),
    Column("asset_id", UUID(as_uuid=True), ForeignKey("media_assets.id"), nullable=False),
    PrimaryKeyConstraint("post_id", "asset_id"),
    UniqueConstraint("asset_id"),
)
web_post_revisions = Table(
    "web_post_revisions",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("post_id", BigInteger, ForeignKey("web_posts.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("slug", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body_markdown", Text, nullable=False),
    Column("excerpt", Text),
    Column("status", Text, nullable=False),
    Column("published_on", Date),
    Column("published_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
    Column("tags", ARRAY(Text), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("action", Text, nullable=False),
    UniqueConstraint("post_id", "version"),
    CheckConstraint("version > 0", name="ck_web_post_revisions_version_positive"),
    CheckConstraint(f"action IN {ACTIONS!r}", name="ck_web_post_revisions_action"),
)


class InvalidPost(ValueError):
    pass


class PostNotFound(LookupError):
    pass


class PostConflict(RuntimeError):
    status_code = 409

    def __init__(self, current_version: int):
        super().__init__("post version conflict")
        self.current_version = current_version


def normalize_tags(tags: Sequence[str]) -> list[str]:
    if isinstance(tags, (str, bytes)):
        raise InvalidPost("invalid tags")
    normalized: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise InvalidPost("invalid tags")
        _valid_text(tag, MAX_BODY_CHARS, "tag")
        value = tag.strip().lower()
        if value and value not in normalized:
            normalized.append(value)
    if len(normalized) > MAX_TAGS or any(len(tag) > MAX_TAG_CHARS for tag in normalized):
        raise InvalidPost("invalid tags")
    return normalized


def _valid_text(value: str, maximum: int, field: str, *, allow_empty: bool = True) -> None:
    if isinstance(value, str) and len(value) > maximum:
        raise InvalidPost(f"{field} too long")
    if not isinstance(value, str) or "\x00" in value or (not allow_empty and not value):
        raise InvalidPost(f"invalid {field}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidPost(f"invalid {field}") from exc


def check_body(body_markdown: str) -> None:
    _valid_text(body_markdown, MAX_BODY_CHARS, "body")


def _published_on(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise InvalidPost("invalid date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidPost("invalid date") from exc
    if parsed.isoformat() != value:
        raise InvalidPost("invalid date")
    return parsed


class _MediaImages(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        source = next((value for name, value in attrs if name.lower() == "src"), None)
        if source is not None:
            self.sources.append(source)


def _media_ids(rendered_html: str) -> list[uuid.UUID]:
    images = _MediaImages()
    images.feed(rendered_html)
    ids: list[uuid.UUID] = []
    for source in images.sources:
        if not source.startswith("/media/"):
            continue
        managed = MANAGED_MEDIA_REFERENCE.fullmatch(source)
        if managed is None:
            raise InvalidAttachment("invalid local media image")
        try:
            asset_id = uuid.UUID(managed.group(1))
        except ValueError as exc:
            raise InvalidAttachment("invalid local media image") from exc
        if str(asset_id) != managed.group(1).lower() or asset_id in ids:
            raise InvalidAttachment("invalid local media image")
        ids.append(asset_id)
    return ids


def _default_renderer(body_markdown: str) -> str:
    from app import render_post

    return render_post(body_markdown)


class PostStore:
    def __init__(
        self,
        database_url: str,
        *,
        engine: Engine | None = None,
        reserved_slugs: Callable[[], Collection[str]] = lambda: (),
        renderer: Callable[[str], str] = _default_renderer,
    ):
        self.engine = engine or database_engine(
            database_url,
            pool_pre_ping=True,
            pool_timeout=POOL_TIMEOUT_SECONDS,
            connect_args={
                "connect_timeout": CONNECT_TIMEOUT_SECONDS,
                "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            },
        )
        self.reserved_slugs = reserved_slugs
        self.renderer = renderer

    def create(
        self,
        *,
        title: str,
        slug: str,
        body_markdown: str = "",
        excerpt: str | None = None,
        tags: Sequence[str] = (),
        published_on: str | date | None = None,
    ) -> dict:
        values, normalized_tags, asset_ids = self._validate(
            title, slug, body_markdown, excerpt, tags, published_on, "draft"
        )
        try:
            with self.engine.begin() as connection:
                _ownership_lock(connection)
                row = dict(
                    connection.execute(insert(web_posts).values(**values).returning(web_posts)).mappings().one()
                )
                post_id = row["id"]
                connection.execute(insert(web_post_slugs).values(slug=slug, post_id=post_id, canonical=True))
                _replace_tags(connection, post_id, normalized_tags)
                _replace_media(connection, post_id, asset_ids, set())
                return _document(connection, row)
        except IntegrityError as exc:
            _raise_conflict(exc)
            raise

    def get(self, post_id: int) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(web_posts).where(web_posts.c.id == post_id)).mappings().one_or_none()
            return _document(connection, dict(row)) if row else None

    def list(self) -> list[dict]:
        with self.engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(select(web_posts).order_by(web_posts.c.updated_at.desc())).mappings()
            ]
            post_ids = [row["id"] for row in rows]
            slugs = _slugs_by_post(connection, post_ids)
            tags = _tags_by_post(connection, post_ids)
            media = _media_by_post(connection, post_ids)
            return [
                {**row, "slug": slugs[row["id"]], "tags": tags[row["id"]], "media": media[row["id"]]}
                for row in rows
            ]

    def list_public(self) -> list[dict]:
        with self.engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    select(web_posts, web_post_slugs.c.slug)
                    .join(
                        web_post_slugs,
                        (web_post_slugs.c.post_id == web_posts.c.id) & web_post_slugs.c.canonical.is_(True),
                    )
                    .where(
                        web_posts.c.status == "live",
                        web_posts.c.deleted_at.is_(None),
                    )
                ).mappings()
            ]
            tags = _tags_by_post(connection, [row["id"] for row in rows])
            return [_public_document(row, tags.get(row["id"], [])) for row in rows]

    def get_public(self, slug: str) -> tuple[dict, str] | None:
        requested_slug = web_post_slugs.alias("requested_slug")
        canonical_slug = web_post_slugs.alias("canonical_slug")
        with self.engine.connect() as connection:
            row = connection.execute(
                select(web_posts, canonical_slug.c.slug)
                .join(requested_slug, requested_slug.c.post_id == web_posts.c.id)
                .join(
                    canonical_slug,
                    (canonical_slug.c.post_id == web_posts.c.id) & canonical_slug.c.canonical.is_(True),
                )
                .where(
                    requested_slug.c.slug == slug,
                    web_posts.c.status == "live",
                    web_posts.c.deleted_at.is_(None),
                )
            ).mappings().one_or_none()
            if row is None:
                return None
            document = dict(row)
            tags = _tags_by_post(connection, [document["id"]]).get(document["id"], [])
            return _public_document(document, tags), document["slug"]

    def slugs(self) -> list[str]:
        with self.engine.connect() as connection:
            return list(connection.execute(select(web_post_slugs.c.slug)).scalars())

    def revisions(self, post_id: int) -> list[dict]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(web_post_revisions)
                    .where(web_post_revisions.c.post_id == post_id)
                    .order_by(web_post_revisions.c.id.desc())
                ).mappings()
            ]

    def update(
        self,
        post_id: int,
        version: int,
        *,
        title: str,
        slug: str,
        body_markdown: str,
        excerpt: str | None,
        tags: Sequence[str],
        published_on: str | date | None,
        status: str,
    ) -> dict:
        values, normalized_tags, asset_ids = self._validate(
            title, slug, body_markdown, excerpt, tags, published_on, status
        )
        try:
            with self.engine.begin() as connection:
                _ownership_lock(connection)
                current = _locked_document(connection, post_id, version)
                if current["deleted_at"] is not None:
                    raise PostNotFound(post_id)
                if status == "live":
                    values["published_at"] = current["published_at"] or func.now()
                    values["published_on"] = (
                        values["published_on"] or current["published_on"] or datetime.now(TORONTO).date()
                    )
                else:
                    values["published_at"] = current["published_at"]
                    if current["status"] == "live":
                        values["published_on"] = current["published_on"]
                candidate = {**current, **values, "slug": slug, "tags": normalized_tags, "media": [str(i) for i in asset_ids]}
                if _same_document(current, candidate):
                    return current
                action = _action(current, candidate)
                _record_revision(connection, current, action)
                _set_slug(connection, post_id, current["slug"], slug)
                _replace_tags(connection, post_id, normalized_tags)
                _replace_media(connection, post_id, asset_ids, {uuid.UUID(value) for value in current["media"]})
                row = dict(
                    connection.execute(
                        update(web_posts)
                        .where(web_posts.c.id == post_id, web_posts.c.version == version)
                        .values(**values, updated_at=func.now(), version=version + 1)
                        .returning(web_posts)
                    ).mappings().one()
                )
                return _document(connection, row)
        except IntegrityError as exc:
            _raise_conflict(exc)
            raise

    def delete(self, post_id: int, version: int) -> dict:
        with self.engine.begin() as connection:
            _ownership_lock(connection)
            current = _locked_document(connection, post_id, version)
            if current["deleted_at"] is not None:
                return current
            _record_revision(connection, current, "delete")
            attached = {uuid.UUID(value) for value in current["media"]}
            _lock_assets(connection, attached)
            if attached:
                connection.execute(update(media_assets).where(media_assets.c.id.in_(attached)).values(detached_at=func.now()))
            connection.execute(delete(web_post_media).where(web_post_media.c.post_id == post_id))
            row = dict(
                connection.execute(
                    update(web_posts)
                    .where(web_posts.c.id == post_id, web_posts.c.version == version)
                    .values(deleted_at=func.now(), updated_at=func.now(), version=version + 1)
                    .returning(web_posts)
                ).mappings().one()
            )
            return _document(connection, row)

    def restore(self, post_id: int, revision_id: int, version: int) -> dict:
        with self.engine.connect() as connection:
            revision = connection.execute(
                select(web_post_revisions).where(
                    web_post_revisions.c.id == revision_id,
                    web_post_revisions.c.post_id == post_id,
                )
            ).mappings().one_or_none()
        if revision is None:
            raise PostNotFound(revision_id)
        revision = dict(revision)
        values, tags, asset_ids = self._validate(
            revision["title"],
            revision["slug"],
            revision["body_markdown"],
            revision["excerpt"],
            revision["tags"],
            revision["published_on"],
            "draft",
        )
        values["published_at"] = revision["published_at"]
        try:
            with self.engine.begin() as connection:
                _ownership_lock(connection)
                current = _locked_document(connection, post_id, version)
                _record_revision(connection, current, "restore")
                _set_slug(connection, post_id, current["slug"], revision["slug"])
                _replace_tags(connection, post_id, tags)
                _replace_media(
                    connection,
                    post_id,
                    asset_ids,
                    {uuid.UUID(value) for value in current["media"]},
                    allow_recently_detached=True,
                )
                row = dict(
                    connection.execute(
                        update(web_posts)
                        .where(web_posts.c.id == post_id, web_posts.c.version == version)
                        .values(**values, deleted_at=None, updated_at=func.now(), version=version + 1)
                        .returning(web_posts)
                    ).mappings().one()
                )
                return _document(connection, row)
        except IntegrityError as exc:
            _raise_conflict(exc)
            raise

    def close(self) -> None:
        self.engine.dispose()

    def _validate(self, title, slug, body_markdown, excerpt, tags, published_on, status):
        _valid_text(title, MAX_TITLE_CHARS, "title", allow_empty=False)
        _valid_text(body_markdown, MAX_BODY_CHARS, "body")
        if excerpt is not None:
            _valid_text(excerpt, MAX_EXCERPT_CHARS, "excerpt")
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            raise InvalidPost("invalid slug")
        if slug in self.reserved_slugs():
            raise InvalidPost("slug reserved by file post")
        if status not in {"draft", "live"}:
            raise InvalidPost("invalid status")
        normalized_tags = normalize_tags(tags)
        parsed_date = _published_on(published_on)
        if status == "live" and (not title.strip() or not body_markdown.strip()):
            raise InvalidPost("live post needs title and body")
        rendered_html = self.renderer(body_markdown)
        return {
            "title": title,
            "body_markdown": body_markdown,
            "excerpt": excerpt,
            "status": status,
            "published_on": parsed_date,
        }, normalized_tags, _media_ids(rendered_html)


def _ownership_lock(connection: Connection) -> None:
    connection.execute(select(site_profile.c.id).where(site_profile.c.id == 1).with_for_update()).one()


def _locked_document(connection: Connection, post_id: int, version: int) -> dict:
    row = connection.execute(
        select(web_posts).where(web_posts.c.id == post_id).with_for_update()
    ).mappings().one_or_none()
    if row is None:
        raise PostNotFound(post_id)
    if row["version"] != version:
        raise PostConflict(row["version"])
    return _document(connection, dict(row))


def _document(connection: Connection, row: dict) -> dict:
    post_id = row["id"]
    row["slug"] = connection.execute(
        select(web_post_slugs.c.slug).where(
            web_post_slugs.c.post_id == post_id, web_post_slugs.c.canonical.is_(True)
        )
    ).scalar_one()
    row["tags"] = list(
        connection.execute(
            select(web_post_tags.c.tag)
            .where(web_post_tags.c.post_id == post_id)
            .order_by(web_post_tags.c.position)
        ).scalars()
    )
    row["media"] = [
        str(value)
        for value in connection.execute(
            select(web_post_media.c.asset_id)
            .where(web_post_media.c.post_id == post_id)
            .order_by(web_post_media.c.asset_id)
        ).scalars()
    ]
    return row


def _slugs_by_post(connection: Connection, post_ids: Sequence[int]) -> dict[int, str]:
    if not post_ids:
        return {}
    return {
        post_id: slug
        for post_id, slug in connection.execute(
            select(web_post_slugs.c.post_id, web_post_slugs.c.slug).where(
                web_post_slugs.c.post_id.in_(post_ids), web_post_slugs.c.canonical.is_(True)
            )
        )
    }


def _tags_by_post(connection: Connection, post_ids: Sequence[int]) -> dict[int, list[str]]:
    tags = {post_id: [] for post_id in post_ids}
    if not post_ids:
        return tags
    rows = connection.execute(
        select(web_post_tags.c.post_id, web_post_tags.c.tag)
        .where(web_post_tags.c.post_id.in_(post_ids))
        .order_by(web_post_tags.c.post_id, web_post_tags.c.position)
    )
    for post_id, tag in rows:
        tags[post_id].append(tag)
    return tags


def _media_by_post(connection: Connection, post_ids: Sequence[int]) -> dict[int, list[str]]:
    media = {post_id: [] for post_id in post_ids}
    if not post_ids:
        return media
    rows = connection.execute(
        select(web_post_media.c.post_id, web_post_media.c.asset_id)
        .where(web_post_media.c.post_id.in_(post_ids))
        .order_by(web_post_media.c.post_id, web_post_media.c.asset_id)
    )
    for post_id, asset_id in rows:
        media[post_id].append(str(asset_id))
    return media


def _public_document(document: dict, tags: list[str]) -> dict:
    return {
        "slug": document["slug"],
        "date": document["published_on"].isoformat(),
        "title": document["title"],
        "excerpt": document["excerpt"],
        "tags": tags,
        "body": document["body_markdown"],
        "draft": False,
        "_catalog_id": document["id"],
    }


def _same_document(current: dict, candidate: dict) -> bool:
    fields = ("title", "body_markdown", "excerpt", "status", "published_on", "published_at", "slug", "tags")
    return all(current[field] == candidate[field] for field in fields) and set(current["media"]) == set(candidate["media"])


def _action(current: dict, candidate: dict) -> str:
    if current["status"] != candidate["status"]:
        return "publish" if candidate["status"] == "live" else "unpublish"
    if current["slug"] != candidate["slug"]:
        return "rename"
    return "save"


def _record_revision(connection: Connection, document: dict, action: str) -> None:
    connection.execute(
        insert(web_post_revisions).values(
            post_id=document["id"],
            version=document["version"],
            slug=document["slug"],
            title=document["title"],
            body_markdown=document["body_markdown"],
            excerpt=document["excerpt"],
            status=document["status"],
            published_on=document["published_on"],
            published_at=document["published_at"],
            deleted_at=document["deleted_at"],
            tags=document["tags"],
            action=action,
        )
    )


def _set_slug(connection: Connection, post_id: int, old_slug: str, new_slug: str) -> None:
    if old_slug == new_slug:
        return
    existing = connection.execute(
        select(web_post_slugs.c.post_id, web_post_slugs.c.canonical)
        .where(web_post_slugs.c.slug == new_slug)
        .with_for_update()
    ).one_or_none()
    if existing is not None and existing.post_id != post_id:
        raise InvalidPost("slug already used")
    connection.execute(
        update(web_post_slugs)
        .where(web_post_slugs.c.post_id == post_id, web_post_slugs.c.canonical.is_(True))
        .values(canonical=False)
    )
    if existing is None:
        connection.execute(insert(web_post_slugs).values(slug=new_slug, post_id=post_id, canonical=True))
    else:
        connection.execute(update(web_post_slugs).where(web_post_slugs.c.slug == new_slug).values(canonical=True))


def _replace_tags(connection: Connection, post_id: int, tags: Sequence[str]) -> None:
    connection.execute(delete(web_post_tags).where(web_post_tags.c.post_id == post_id))
    if tags:
        connection.execute(
            insert(web_post_tags),
            [{"post_id": post_id, "position": position, "tag": tag} for position, tag in enumerate(tags)],
        )


def _replace_media(
    connection: Connection,
    post_id: int,
    requested: Sequence[uuid.UUID],
    current: set[uuid.UUID],
    *,
    allow_recently_detached: bool = False,
) -> None:
    def replace() -> None:
        connection.execute(delete(web_post_media).where(web_post_media.c.post_id == post_id))
        if requested:
            connection.execute(
                insert(web_post_media),
                [{"post_id": post_id, "asset_id": asset_id} for asset_id in requested],
            )

    _replace_media_ownership(
        connection,
        requested,
        current,
        replace,
        allow_recently_detached=allow_recently_detached,
    )


def _raise_conflict(exc: IntegrityError) -> None:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint in {"web_post_slugs_pkey", "uq_web_post_slugs_canonical_post"}:
        raise InvalidPost("slug already used") from exc
    if constraint in {"web_post_media_asset_id_key", "web_post_media_pkey"}:
        raise InvalidAttachment from exc
