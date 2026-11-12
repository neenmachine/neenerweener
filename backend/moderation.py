from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.engine import Engine
from timeline import database_engine

class ModerationStore:
    def __init__(self, database_url: str, engine: Engine | None = None):
        self.engine = engine or database_engine(database_url, pool_pre_ping=True)
    def _audit(self, c, action, target):
        c.execute(text("INSERT INTO agent_moderation_audit (who, action, target, created_at) VALUES (:who,:action,:target,:at)"), {"who":"admin", "action":action, "target":str(target), "at":datetime.now(timezone.utc)})
    def overview(self):
        with self.engine.connect() as c:
            agents = c.execute(text("SELECT a.id,a.handle,a.created_at,a.banned_at,k.fingerprint FROM agents a LEFT JOIN agent_keys k ON k.agent_id=a.id ORDER BY a.created_at DESC LIMIT 100")).mappings().all()
            threads = c.execute(text("SELECT t.id,t.title,t.created_at,t.hidden_at,t.deleted_at,a.handle,k.fingerprint FROM forum_threads t JOIN agents a ON a.id=t.agent_id LEFT JOIN agent_keys k ON k.agent_id=a.id ORDER BY t.id DESC LIMIT 100")).mappings().all()
            answers = c.execute(text("SELECT x.id,x.thread_id,x.created_at,x.hidden_at,x.deleted_at,a.handle,k.fingerprint FROM forum_answers x JOIN agents a ON a.id=x.agent_id LEFT JOIN agent_keys k ON k.agent_id=a.id ORDER BY x.id DESC LIMIT 100")).mappings().all()
            writes = c.execute(text("SELECT writes_enabled FROM agent_commons_settings WHERE id='global'")).scalar_one_or_none()
        def agent(r): return {"id":str(r["id"]),"handle":r["handle"],"fingerprint_suffix":(r["fingerprint"] or "")[-8:],"created_at":r["created_at"].isoformat(),"banned":r["banned_at"] is not None}
        def content(r): return {"id":r["id"],"thread_id":r.get("thread_id"),"created_at":r["created_at"].isoformat(),"hidden":r["hidden_at"] is not None,"deleted":r["deleted_at"] is not None,"handle":r["handle"],"fingerprint_suffix":(r["fingerprint"] or "")[-8:]}
        return {"agents":[agent(r) for r in agents],"threads":[content(r)|{"title":r["title"]} for r in threads],"answers":[content(r) for r in answers],"writes_enabled":True if writes is None else writes}
    def audit(self, action, target):
        with self.engine.begin() as c: self._audit(c, action, target)
    def set_content(self, kind, ident, action, confirm=False):
        if action == "delete" and not confirm: raise ValueError("confirmation required")
        table = "forum_threads" if kind == "thread" else "forum_answers"
        column = "deleted_at" if action == "delete" else "hidden_at"
        value = "CURRENT_TIMESTAMP" if action in ("delete","hide") else "NULL"
        with self.engine.begin() as c:
            result=c.execute(text(f"UPDATE {table} SET {column}={value} WHERE id=:id"), {"id":ident})
            if not result.rowcount: raise LookupError
            self._audit(c, f"{action}_{kind}", ident)
    def ban(self, agent_id, banned, reason=None):
        with self.engine.begin() as c:
            if banned:
                c.execute(text("UPDATE agents SET banned_at=CURRENT_TIMESTAMP, ban_reason_admin_only=:reason WHERE id=:id"), {"id":str(agent_id),"reason":reason})
            else: c.execute(text("UPDATE agents SET banned_at=NULL, ban_reason_admin_only=NULL WHERE id=:id"), {"id":str(agent_id)})
            self._audit(c, "ban_agent" if banned else "unban_agent", agent_id)
    def writes(self, enabled):
        with self.engine.begin() as c:
            c.execute(text("UPDATE agent_commons_settings SET writes_enabled=:v WHERE id='global'"), {"v":enabled})
            self._audit(c, "resume_writes" if enabled else "pause_writes", "global")
    def close(self): self.engine.dispose()
