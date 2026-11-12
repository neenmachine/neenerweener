"""Persistent timeline reads and rendering for untrusted submitted Markdown."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Collection, Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from untrusted_markdown import render_untrusted_markdown
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    and_,
    column,
    create_engine,
    delete,
    desc,
    exists,
    func,
    insert,
    or_,
    select,
    table,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
timeline_entries = Table(
    "timeline_entries",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("body_markdown", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("edited_at", DateTime(timezone=True), nullable=True),
)
media_assets = Table(
    "media_assets",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("width", Integer, nullable=False),
    Column("height", Integer, nullable=False),
    Column("byte_size", Integer, nullable=False),
    Column("sha256", LargeBinary, nullable=False),
    Column("alt_text", Text, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("detached_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("char_length(alt_text) <= 1000", name="ck_media_assets_alt_text_length"),
)
site_profile = Table(
    "site_profile", metadata,
    Column("id", SmallInteger, primary_key=True, server_default="1"),
    Column("display_name", Text, nullable=False, server_default="neenerweener"),
    Column("handle", Text),
    Column("avatar_asset_id", UUID(as_uuid=True), ForeignKey("media_assets.id")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("id = 1", name="ck_site_profile_singleton"),
    CheckConstraint("char_length(btrim(display_name)) BETWEEN 1 AND 80", name="ck_site_profile_display_name"),
    CheckConstraint("handle IS NULL OR handle ~ '^[A-Za-z0-9_]{1,30}$'", name="ck_site_profile_handle"),
    UniqueConstraint("avatar_asset_id"),
)
media_sweep_claims = Table(
    "media_sweep_claims",
    metadata,
    Column("asset_id", UUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="CASCADE"), primary_key=True),
    Column("claimed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
timeline_entry_media = Table(
    "timeline_entry_media",
    metadata,
    Column("entry_id", BigInteger, ForeignKey("timeline_entries.id", ondelete="CASCADE"), nullable=False),
    Column("asset_id", UUID(as_uuid=True), ForeignKey("media_assets.id"), nullable=False),
    Column("position", SmallInteger, nullable=False),
    PrimaryKeyConstraint("entry_id", "position"),
    UniqueConstraint("asset_id"),
    CheckConstraint("position BETWEEN 0 AND 3", name="ck_timeline_entry_media_position"),
)
# The sweep never deletes the row of an attached asset, but it deletes objects before the row. Attach therefore
# refuses never-attached assets older than ATTACH_WINDOW, an hour before the sweep may touch them, and delete
# sets detached_at so the sweep waits DETACHED_GRACE.
_web_post_media = table("web_post_media", column("asset_id"))
_unowned = (
    ~exists().where(timeline_entry_media.c.asset_id == media_assets.c.id)
    & ~exists().where(site_profile.c.avatar_asset_id == media_assets.c.id)
    & ~exists().where(_web_post_media.c.asset_id == media_assets.c.id)
)
_unused = _unowned & ~exists().where(media_sweep_claims.c.asset_id == media_assets.c.id)
NEVER_ATTACHED_GRACE = timedelta(hours=24)
ATTACH_WINDOW = timedelta(hours=23)
DETACHED_GRACE = timedelta(days=7)
MAX_ATTACHMENTS = 4
MAX_ALT_CHARS = 1000


class InvalidAttachment(ValueError):
    """An attachment that is unknown, already attached, detached, expired, or malformed."""


CONNECT_TIMEOUT_SECONDS = 2
POOL_TIMEOUT_SECONDS = 1
STATEMENT_TIMEOUT_MS = 2000


def database_engine(database_url: str, **kwargs) -> Engine:
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return create_engine(database_url, **kwargs)


class TimelineStoreProtocol(Protocol):
    def list(self, before: int | None, limit: int) -> list[dict]: ...
    def get(self, entry_id: int) -> dict | None: ...
    def count(self) -> int: ...
    def create(self, body_markdown: str, attachments: Sequence[tuple[uuid.UUID, str]] = ()) -> dict: ...
    def update(
        self, entry_id: int, body_markdown: str, attachments: Sequence[tuple[uuid.UUID, str]] | None = None
    ) -> dict | None: ...
    def delete(self, entry_id: int) -> bool: ...
    def create_media_asset(self, asset_id: uuid.UUID, width: int, height: int, byte_size: int, sha256: bytes) -> None: ...
    def expired_media_assets(self, limit: int) -> list[uuid.UUID]: ...
    def delete_media_asset(self, asset_id: uuid.UUID) -> None: ...
    def is_in_use(self, asset_id: uuid.UUID) -> bool: ...
    def get_profile(self) -> dict: ...
    def update_profile(self, display_name: str, handle: str | None, avatar_asset_id=...) -> dict: ...
    def schema_revisions(self) -> frozenset[str]: ...
    def ping(self) -> None: ...
    def close(self) -> None: ...



def validate_timeline_body(body: str) -> None:
    if "\x00" in body or not body.strip() or len(body) > 10_000:
        raise ValueError("invalid body")
    try:
        body.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid body") from exc
    render_timeline(body)


def validate_timeline_create(body: str, alts: Sequence[str]) -> None:
    validate_timeline_body(body)
    if any("\x00" in alt or len(alt) > MAX_ALT_CHARS for alt in alts):
        raise InvalidAttachment
    try:
        for alt in alts:
            alt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidAttachment from exc


def render_timeline(body: str) -> str:
    """Render submitted Markdown without HTML, images, or unsafe links."""
    return render_untrusted_markdown(body, maximum=10_000, absolute_links=False)


class TimelineStore:
    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(
            database_url,
            pool_pre_ping=True,
            pool_timeout=POOL_TIMEOUT_SECONDS,
            connect_args={
                "connect_timeout": CONNECT_TIMEOUT_SECONDS,
                "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            },
        )

    def list(self, before: int | None, limit: int) -> list[dict]:
        query = select(timeline_entries).order_by(desc(timeline_entries.c.id)).limit(limit + 1)
        if before is not None:
            query = query.where(timeline_entries.c.id < before)
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(query).mappings()]
            media = media_for(connection, [row["id"] for row in rows])
        return [{**row, "media": media.get(row["id"], [])} for row in rows]

    def count(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(timeline_entries)).scalar_one()

    def get(self, entry_id: int) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(timeline_entries).where(timeline_entries.c.id == entry_id)
            ).mappings().one_or_none()
            return _with_media(connection, row) if row else None

    def create(self, body_markdown: str, attachments: Sequence[tuple[uuid.UUID, str]] = ()) -> dict:
        """Insert the entry and attach assets in order, all or nothing."""
        ids = [asset_id for asset_id, _alt in attachments]
        if len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids):
            raise InvalidAttachment
        validate_timeline_create(body_markdown, [alt for _id, alt in attachments])
        try:
            with self.engine.begin() as connection:
                connection.execute(select(site_profile.c.id).where(site_profile.c.id == 1).with_for_update()).one()
                row = connection.execute(
                    insert(timeline_entries).values(body_markdown=body_markdown).returning(timeline_entries)
                ).mappings().one()
                _replace_media_ownership(
                    connection,
                    ids,
                    (),
                    lambda: _write_entry_media(connection, row["id"], attachments),
                )
                return _with_media(connection, row)
        except IntegrityError as exc:
            _raise_attachment_conflict(exc)
            raise

    def update(
        self, entry_id: int, body_markdown: str, attachments: Sequence[tuple[uuid.UUID, str]] | None = None
    ) -> dict | None:
        """Replace the body and, unless attachments is None, the full ordered image list, all or nothing.

        Locks the entry, then its current and requested assets in id order, the same order as delete, so two
        edits or an edit and a delete never deadlock. Kept images stay valid at any age; new ones follow create's
        rules; removed ones get detached_at so the sweep keeps their objects for DETACHED_GRACE.
        """
        ids = [asset_id for asset_id, _alt in attachments or ()]
        if len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids):
            raise InvalidAttachment
        validate_timeline_create(body_markdown, [alt for _id, alt in attachments or ()])
        try:
            with self.engine.begin() as connection:
                connection.execute(select(site_profile.c.id).where(site_profile.c.id == 1).with_for_update()).one()
                current = _lock_entry(connection, entry_id)
                if current is None:
                    return None
                media = media_for(connection, [entry_id]).get(entry_id, [])
                same_media = attachments is None or [(uuid.UUID(m["id"]), m["alt"]) for m in media] == list(attachments)
                if current["body_markdown"] == body_markdown and same_media:
                    return {**current, "media": media}
                if attachments is not None:
                    _replace_entry_media(connection, entry_id, attachments, media, ids)
                row = _update_entry_row(connection, entry_id, body_markdown)
                return _with_media(connection, row)
        except IntegrityError as exc:
            _raise_attachment_conflict(exc)
            raise

    def delete(self, entry_id: int) -> bool:
        with self.engine.begin() as connection:
            if _lock_entry(connection, entry_id) is None:
                return False
            attached = connection.execute(
                select(timeline_entry_media.c.asset_id).where(timeline_entry_media.c.entry_id == entry_id)
            ).scalars().all()
            _lock_assets(connection, attached)
            # Mark the assets first so the sweep keeps their objects for DETACHED_GRACE.
            if attached:
                connection.execute(update(media_assets).where(media_assets.c.id.in_(attached)).values(detached_at=func.now()))
            connection.execute(delete(timeline_entries).where(timeline_entries.c.id == entry_id))
        return True

    def create_media_asset(self, asset_id: uuid.UUID, width: int, height: int, byte_size: int, sha256: bytes) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(media_assets).values(
                    id=asset_id, width=width, height=height, byte_size=byte_size, sha256=sha256
                )
            )

    def expired_media_assets(self, limit: int) -> list[uuid.UUID]:
        """Claim expired assets so they cannot be attached while their objects are deleted."""
        now = func.now()
        with self.engine.begin() as connection:
            claimed = list(
                connection.execute(
                    select(media_sweep_claims.c.asset_id)
                    .join(media_assets, media_assets.c.id == media_sweep_claims.c.asset_id)
                    .order_by(media_sweep_claims.c.claimed_at)
                    .limit(limit)
                ).scalars()
            )
            candidate_condition = or_(
                media_assets.c.detached_at < now - DETACHED_GRACE,
                and_(
                    media_assets.c.detached_at.is_(None),
                    media_assets.c.created_at < now - NEVER_ATTACHED_GRACE,
                ),
            )
            locked = list(
                connection.execute(
                    select(media_assets.c.id)
                    .where(_unused, candidate_condition)
                    .order_by(media_assets.c.id)
                    .limit(limit - len(claimed))
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            candidates = list(
                connection.execute(
                    select(media_assets.c.id)
                    .where(media_assets.c.id.in_(locked), _unused, candidate_condition)
                    .order_by(media_assets.c.id)
                ).scalars()
            )
            if candidates:
                connection.execute(insert(media_sweep_claims), [{"asset_id": asset_id} for asset_id in candidates])
            return claimed + candidates

    def delete_media_asset(self, asset_id: uuid.UUID) -> None:
        with self.engine.begin() as connection:
            asset = connection.execute(
                select(media_assets.c.id).where(media_assets.c.id == asset_id).with_for_update()
            ).scalar_one_or_none()
            if asset is None:
                return
            deletable = connection.execute(
                select(
                    exists().where(
                        media_assets.c.id == asset_id,
                        _unowned,
                        exists().where(media_sweep_claims.c.asset_id == asset_id),
                    )
                )
            ).scalar_one()
            if deletable:
                connection.execute(delete(media_assets).where(media_assets.c.id == asset_id))

    def is_in_use(self, asset_id: uuid.UUID) -> bool:
        in_use = (
            exists().where(timeline_entry_media.c.asset_id == asset_id)
            | exists().where(site_profile.c.avatar_asset_id == asset_id)
            | exists().where(_web_post_media.c.asset_id == asset_id)
        )
        with self.engine.connect() as connection:
            return connection.execute(select(in_use)).scalar_one()

    def get_profile(self) -> dict:
        with self.engine.connect() as connection:
            return dict(connection.execute(select(site_profile).where(site_profile.c.id == 1)).mappings().one())

    def update_profile(self, display_name: str, handle: str | None, avatar_asset_id=...) -> dict:
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 80:
            raise ValueError("invalid display name")
        if handle is not None and (not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,30}", handle)):
            raise ValueError("invalid handle")
        with self.engine.begin() as connection:
            profile = connection.execute(select(site_profile).where(site_profile.c.id == 1).with_for_update()).mappings().one()
            old = profile["avatar_asset_id"]
            new = old if avatar_asset_id is ... else avatar_asset_id
            if new != old:
                _replace_media_ownership(
                    connection,
                    () if new is None else (new,),
                    () if old is None else (old,),
                    lambda: connection.execute(
                        update(site_profile).where(site_profile.c.id == 1).values(avatar_asset_id=new)
                    ),
                )
            values = {"display_name": display_name.strip(), "handle": handle, "updated_at": func.now()}
            return dict(connection.execute(update(site_profile).where(site_profile.c.id == 1).values(**values).returning(site_profile)).mappings().one())

    def schema_revisions(self) -> frozenset[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version"))
            return frozenset(rows.scalars())

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def close(self) -> None:
        self.engine.dispose()


def _replace_media_ownership(
    connection: Connection,
    requested: Sequence[uuid.UUID],
    current: Collection[uuid.UUID],
    replace: Callable[[], object],
    *,
    allow_recently_detached: bool = False,
) -> None:
    """Lock assets in one order, recheck eligibility, then replace ownership."""
    requested_set = set(requested)
    current_set = set(current)
    if len(requested_set) != len(requested):
        raise InvalidAttachment
    _lock_assets(connection, requested_set | current_set)
    new = requested_set - current_set
    if new:
        condition = media_assets.c.detached_at.is_(None)
        age = media_assets.c.created_at > func.now() - ATTACH_WINDOW
        if allow_recently_detached:
            recently_detached = media_assets.c.detached_at > func.now() - DETACHED_GRACE
            condition = condition | recently_detached
            age = age | recently_detached
        attachable = connection.execute(
            select(func.count())
            .select_from(media_assets)
            .where(media_assets.c.id.in_(new), condition, age, _unused)
        ).scalar_one()
        if attachable != len(new):
            raise InvalidAttachment("media asset is unavailable or being swept")
    replace()
    removed = current_set - requested_set
    if removed:
        connection.execute(update(media_assets).where(media_assets.c.id.in_(removed)).values(detached_at=func.now()))
    if requested_set:
        connection.execute(update(media_assets).where(media_assets.c.id.in_(requested_set)).values(detached_at=None))


def _write_entry_media(connection: Connection, entry_id: int, attachments) -> None:
    connection.execute(delete(timeline_entry_media).where(timeline_entry_media.c.entry_id == entry_id))
    for position, (asset_id, alt) in enumerate(attachments):
        connection.execute(update(media_assets).where(media_assets.c.id == asset_id).values(alt_text=alt))
        connection.execute(insert(timeline_entry_media).values(entry_id=entry_id, asset_id=asset_id, position=position))


def _replace_entry_media(connection: Connection, entry_id: int, attachments, media, ids) -> None:
    _replace_media_ownership(
        connection,
        ids,
        {uuid.UUID(item["id"]) for item in media},
        lambda: _write_entry_media(connection, entry_id, attachments),
    )


def _update_entry_row(connection: Connection, entry_id: int, body_markdown: str):
    return connection.execute(
        update(timeline_entries)
        .where(timeline_entries.c.id == entry_id)
        .values(body_markdown=body_markdown, edited_at=func.now())
        .returning(timeline_entries)
    ).mappings().one()


def _lock_entry(connection: Connection, entry_id: int):
    return connection.execute(
        select(timeline_entries).where(timeline_entries.c.id == entry_id).with_for_update()
    ).mappings().one_or_none()


def _lock_assets(connection: Connection, ids) -> None:
    """Lock asset rows in the one id order used by attachment, deletion, and sweeping."""
    if ids:
        connection.execute(
            select(media_assets.c.id).where(media_assets.c.id.in_(ids)).order_by(media_assets.c.id).with_for_update()
        ).all()


def _raise_attachment_conflict(exc: IntegrityError) -> None:
    constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if constraint_name in {"timeline_entry_media_asset_id_key", "ck_timeline_entry_media_position"}:
        raise InvalidAttachment from exc


def media_for(connection: Connection, entry_ids: Sequence[int]) -> dict[int, list[dict]]:
    """Attached media per entry in position order, in one query for the whole page."""
    media: dict[int, list[dict]] = {}
    if not entry_ids:
        return media
    rows = connection.execute(
        select(
            timeline_entry_media.c.entry_id,
            media_assets.c.id,
            media_assets.c.width,
            media_assets.c.height,
            media_assets.c.alt_text,
        )
        .join(media_assets, media_assets.c.id == timeline_entry_media.c.asset_id)
        .where(timeline_entry_media.c.entry_id.in_(entry_ids))
        .order_by(timeline_entry_media.c.entry_id, timeline_entry_media.c.position)
    )
    for entry_id, asset_id, width, height, alt in rows:
        media.setdefault(entry_id, []).append({"id": str(asset_id), "width": width, "height": height, "alt": alt})
    return media


def _with_media(connection: Connection, row) -> dict:
    return {**row, "media": media_for(connection, [row["id"]]).get(row["id"], [])}


def serialize_entry(entry: dict, *, admin: bool = False) -> dict:
    result = {
        "id": entry["id"],
        "html": render_timeline(entry["body_markdown"]),
        "created_at": _timestamp(entry["created_at"]),
        "created_zone": _zone(entry["created_at"]),
        "edited_at": _timestamp(entry.get("edited_at")),
        "edited_zone": _zone(entry.get("edited_at")),
        "media": entry["media"],
    }
    if admin:
        result["body_markdown"] = entry["body_markdown"]
    return result


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


# The site shows every time in Toronto. Python alone names the zone (EST or EDT) for each timestamp, so both
# renderers print the same abbreviation; browsers' Intl can say "GMT-5" instead.
TORONTO = ZoneInfo("America/Toronto")


def _zone(value: datetime | None) -> str | None:
    return value.astimezone(TORONTO).tzname() if value is not None else None
