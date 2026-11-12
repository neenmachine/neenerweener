"""storage and public interfaces for the agent pixel canvas."""
from __future__ import annotations

import base64
import io
from datetime import datetime, timezone

from PIL import Image
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, SmallInteger, Table, Text, delete, select, text
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert
from sqlalchemy.engine import Engine

from agent_commons import AgentPrincipal, AgentCommonsStore, public_agent
from agent_commons import agent_keys, agents
from timeline import database_engine, metadata

WIDTH = HEIGHT = 64
EMPTY = 255
PALETTE = ("#000000", "#ffffff", "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff", "#9a6324", "#808000")
canvas_events = Table("canvas_events", metadata, Column("revision", BigInteger), Column("x", SmallInteger), Column("y", SmallInteger), Column("color", SmallInteger), Column("agent_id", UUID(as_uuid=True)), Column("event_kind", Text), Column("created_at", DateTime(timezone=True)), Column("clear_bounds", JSONB))
canvas_cells = Table("canvas_cells", metadata, Column("x", SmallInteger), Column("y", SmallInteger), Column("color", SmallInteger), Column("revision", BigInteger), Column("agent_id", UUID(as_uuid=True)), Column("updated_at", DateTime(timezone=True)))


async def authorize_canvas_write(store: AgentCommonsStore, token: str, client_ip: str | None) -> AgentPrincipal:
    """The sole canvas caller of the shared agent mutation policy."""
    principal = await store.authorize_mutation(token, "canvas:pixel", client_ip)
    if principal is None:
        raise PermissionError
    return principal


class CanvasStore:
    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(database_url, pool_pre_ping=True, pool_timeout=1, connect_args={"connect_timeout": 2, "options": "-c statement_timeout=2000"})

    @staticmethod
    def _lock(c) -> None:
        c.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('agent-canvas', 901))"))

    def place(self, principal: AgentPrincipal, x: int, y: int, color: int) -> dict:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as c:
            self._lock(c)
            revision = c.execute(insert(canvas_events).values(x=x, y=y, color=color, agent_id=principal.agent_id, event_kind="place", created_at=now).returning(canvas_events.c.revision)).scalar_one()
            c.execute(insert(canvas_cells).values(x=x, y=y, color=color, revision=revision, agent_id=principal.agent_id, updated_at=now).on_conflict_do_update(index_elements=[canvas_cells.c.x, canvas_cells.c.y], set_=dict(color=color, revision=revision, agent_id=principal.agent_id, updated_at=now)))
        return {"revision": revision, "x": x, "y": y, "color": color, **public_agent(principal.handle, principal.fingerprint)}

    def snapshot(self) -> tuple[int, bytes]:
        with self.engine.begin() as c:
            self._lock(c)
            revision = c.execute(select(canvas_events.c.revision).order_by(canvas_events.c.revision.desc()).limit(1)).scalar_one_or_none() or 0
            cells = c.execute(select(canvas_cells.c.x, canvas_cells.c.y, canvas_cells.c.color)).all()
        data = bytearray([EMPTY] * (WIDTH * HEIGHT))
        for x, y, color in cells:
            data[y * WIDTH + x] = color
        return revision, bytes(data)

    def events(self, after: int, limit: int = 500) -> dict:
        with self.engine.begin() as c:
            self._lock(c)
            latest = c.execute(select(canvas_events.c.revision).order_by(canvas_events.c.revision.desc()).limit(1)).scalar_one_or_none() or 0
            if latest - after > 10000:
                return {"reset": True, "revision": latest, "events": []}
            rows = c.execute(
                select(canvas_events.c.revision, canvas_events.c.x, canvas_events.c.y, canvas_events.c.color, canvas_events.c.event_kind, canvas_events.c.created_at, canvas_events.c.clear_bounds, agents.c.handle, agent_keys.c.fingerprint)
                .select_from(canvas_events.outerjoin(agents, canvas_events.c.agent_id == agents.c.id).outerjoin(agent_keys, agent_keys.c.agent_id == agents.c.id))
                .where(canvas_events.c.revision > after).order_by(canvas_events.c.revision).limit(limit)
            ).mappings().all()
        events = []
        for row in rows:
            event = {"revision": row["revision"], "x": row["x"], "y": row["y"], "color": row["color"], "event_kind": row["event_kind"], "created_at": row["created_at"], "clear_bounds": row["clear_bounds"], **(public_agent(row["handle"], row["fingerprint"]) if row["handle"] and row["fingerprint"] else {"handle": row["handle"], "fingerprint_suffix": None})}
            events.append(event)
        return {"reset": False, "revision": latest, "events": events}

    def clear(self, x: int, y: int, width: int, height: int) -> int:
        now = datetime.now(timezone.utc)
        with self.engine.begin() as c:
            self._lock(c)
            revision = c.execute(insert(canvas_events).values(event_kind="clear", clear_bounds={"x": x, "y": y, "width": width, "height": height}, created_at=now).returning(canvas_events.c.revision)).scalar_one()
            c.execute(delete(canvas_cells).where(canvas_cells.c.x.between(x, x + width - 1), canvas_cells.c.y.between(y, y + height - 1)))
        return revision

    def close(self): self.engine.dispose()


def snapshot_payload(revision: int, pixels: bytes) -> dict:
    return {"width": WIDTH, "height": HEIGHT, "palette": list(PALETTE), "revision": revision, "pixels": base64.b64encode(pixels).decode("ascii")}


def png_bytes(pixels: bytes) -> bytes:
    image = Image.new("P", (WIDTH, HEIGHT), EMPTY)
    image.putdata(pixels)
    palette = []
    for value in PALETTE:
        value = value.lstrip("#"); palette.extend(bytes.fromhex(value))
    palette.extend([0, 0, 0] * (256 - len(PALETTE)))
    image.putpalette(palette)
    image.info["transparency"] = EMPTY
    image = image.resize((512, 512), Image.Resampling.NEAREST)
    output = io.BytesIO(); image.save(output, format="PNG", optimize=False)
    return output.getvalue()
