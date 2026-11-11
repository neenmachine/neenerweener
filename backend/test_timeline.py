import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import app
from timeline import TimelineStore, render_timeline, validate_timeline_create


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def list(self, before, limit):
        self.calls.append((before, limit))
        return self.rows

    def get_profile(self):
        return {"display_name": "neenerweener", "handle": None, "avatar_asset_id": None}


def test_non_attachment_integrity_errors_are_not_reclassified():
    class Orig:
        class diag:
            constraint_name = "other_constraint"

    class Connection:
        def execute(self, _query):
            raise IntegrityError("insert", {}, Orig())

    class Transaction:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    class Engine:
        def begin(self):
            return Transaction()

    with pytest.raises(IntegrityError, match="insert"):
        TimelineStore("unused", engine=Engine()).create("body")


def test_store_create_validates_timeline_before_opening_transaction():
    class Engine:
        def begin(self):
            raise AssertionError("validation did not run")

    with pytest.raises(ValueError, match="unsafe link"):
        TimelineStore("unused", engine=Engine()).create("[bad](javascript:alert(1))")


def test_store_close_disposes_engine():
    class Engine:
        def dispose(self):
            self.disposed = True

    engine = Engine()
    store = TimelineStore("unused", engine=engine)
    store.close()
    assert engine.disposed is True


def test_timeline_markdown_disables_html_and_images():
    html = render_timeline('<strong>no</strong>\n\n![image](https://example.test/a.png)')
    assert '<strong>' not in html
    assert '&lt;strong&gt;' in html
    assert '<img' not in html
    assert 'image' in html


def test_timeline_markdown_rejects_unsafe_links():
    with pytest.raises(ValueError, match='unsafe link'):
        render_timeline('[bad](javascript:alert(1))')


@pytest.mark.parametrize(
    ("body", "alts", "valid"),
    [
        ("", [], False),
        (" \t\n\u00a0\u2003\x1c\u0085", [], False),
        ("bad\x00body", [], False),
        ("x" * 10_000, [], True),
        ("x" * 10_001, [], False),
        ("[x](javascript:alert(1))", [], False),
        ("[x](https://example.com)", [], True),
        ("text", ["a" * 1_000], True),
        ("text", ["a" * 1_001], False),
        ("text", ["😀" * 1_000], True),
        ("text", ["e\u0301" * 500], True),
        ("text", ["e\u0301" * 501], False),
        ("text", ["bad\x00alt"], False),
        ("text\ud800", [], False),
        ("text", ["bad\ud800alt"], False),
    ],
)
def test_validate_timeline_create(body, alts, valid):
    if valid:
        validate_timeline_create(body, alts)
    else:
        with pytest.raises(ValueError):
            validate_timeline_create(body, alts)


def test_public_profile_returns_defaults_and_no_cache(monkeypatch):
    monkeypatch.setattr(app, "timeline_store", FakeStore([]))
    response = TestClient(app.app).get("/api/profile")
    assert response.status_code == 200
    assert response.json() == {"display_name": "neenerweener", "handle": None, "avatar_id": None, "avatar_initial": "N"}
    assert response.headers["cache-control"] == "no-cache"


def test_public_profile_returns_configured_values_and_storage_failure_is_503(monkeypatch):
    avatar_id = "7b150c14-6f3d-4d73-a642-621acb840cea"

    class ConfiguredStore:
        def get_profile(self):
            return {"display_name": " configured ", "handle": "valid_handle", "avatar_asset_id": avatar_id}

    monkeypatch.setattr(app, "timeline_store", ConfiguredStore())
    configured = TestClient(app.app).get("/api/profile")
    assert configured.status_code == 200
    assert configured.json() == {
        "display_name": " configured ",
        "handle": "valid_handle",
        "avatar_id": avatar_id,
        "avatar_initial": "C",
    }
    assert configured.headers["cache-control"] == "no-cache"

    class FailingStore:
        def get_profile(self):
            raise RuntimeError("storage unavailable")

    monkeypatch.setattr(app, "timeline_store", FailingStore())
    unavailable = TestClient(app.app).get("/api/profile")
    assert (unavailable.status_code, unavailable.json()) == (503, {"detail": "profile unavailable"})


def test_list_timeline_is_newest_first_and_paginated(monkeypatch):
    rows = [
        {"id": 8, "body_markdown": "new", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc), "edited_at": None, "media": []},
        {"id": 7, "body_markdown": "old", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "edited_at": None, "media": []},
        {"id": 6, "body_markdown": "next", "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc), "edited_at": None, "media": []},
    ]
    store = FakeStore(rows)
    monkeypatch.setattr(app, "timeline_store", store)
    response = TestClient(app.app).get('/api/timeline?limit=2')
    assert response.status_code == 200
    assert [entry['id'] for entry in response.json()['entries']] == [8, 7]
    assert response.json()['next_cursor'] == 7
    assert store.calls == [(None, 2)]


def test_list_timeline_validates_cursor_and_limit(monkeypatch):
    store = FakeStore([])
    monkeypatch.setattr(app, "timeline_store", store)
    client = TestClient(app.app)
    assert client.get('/api/timeline?before=0').status_code == 422
    assert client.get('/api/timeline?limit=21').status_code == 422
    assert client.get('/api/timeline?limit=0').status_code == 422
    assert client.get('/api/timeline?before=nope').status_code == 422
    assert store.calls == []


def test_timeline_is_unavailable_without_database(monkeypatch):
    monkeypatch.setattr(app, "timeline_store", None)
    response = TestClient(app.app).get('/api/timeline')
    assert response.status_code == 503
    assert response.json() == {'detail': 'timeline unavailable'}


def test_timeline_omits_invalid_entry_without_leaking_body(monkeypatch, caplog):
    rows = [
        {"id": 8, "body_markdown": "[bad](javascript:alert(1))", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc), "edited_at": None, "media": []},
        {"id": 7, "body_markdown": "valid", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "edited_at": None, "media": []},
    ]
    monkeypatch.setattr(app, "timeline_store", FakeStore(rows))
    with caplog.at_level("WARNING"):
        response = TestClient(app.app).get('/api/timeline?limit=2')
    assert response.status_code == 200
    assert [entry['id'] for entry in response.json()['entries']] == [7]
    assert response.json()['next_cursor'] is None
    assert "invalid timeline entry id=8 error=ValueError" in caplog.messages[0]
    assert "javascript" not in caplog.text


def test_timestamps_are_utc():
    from timeline import serialize_entry
    entry = serialize_entry({"id": 1, "body_markdown": "x", "created_at": datetime(2026, 1, 2, 5, tzinfo=timezone.utc), "edited_at": None, "media": []})
    assert entry["created_at"] == "2026-01-02T05:00:00+00:00"


def test_alembic_has_one_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(Path(__file__).with_name('alembic.ini')))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ['007_media_sweep_claims']
    assert script.get_revision('001_timeline').down_revision is None
    assert script.get_revision('002_admin_auth').down_revision == '001_timeline'
    assert script.get_revision('003_machine_auth').down_revision == '002_admin_auth'
    assert script.get_revision('004_media').down_revision == '003_machine_auth'
    assert script.get_revision('005_profile').down_revision == '004_media'
    assert script.get_revision('006_web_posts').down_revision == '005_profile'
    assert script.get_revision('007_media_sweep_claims').down_revision == '006_web_posts'


@pytest.mark.skipif(not os.environ.get('DATABASE_URL'), reason='DATABASE_URL not configured')
def test_alembic_upgrade_from_empty():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    config = Config(str(Path(__file__).with_name('alembic.ini')))
    engine = create_engine(os.environ['DATABASE_URL'])
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP TABLE IF EXISTS media_sweep_claims CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS web_post_revisions CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS web_post_media CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS web_post_tags CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS web_post_slugs CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS web_posts CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS site_profile CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS timeline_entry_media CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS media_assets CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS machine_auth_events CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS machine_auth_tokens CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS machine_auth_challenges CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS admin_login_failures CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS admin_sessions CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS timeline_entries CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS alembic_version')
        command.upgrade(config, 'head')
        assert not logging.getLogger("app").disabled
        assert 'timeline_entries' in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        app.configure_logging()


@pytest.mark.skipif(not os.environ.get('DATABASE_URL'), reason='DATABASE_URL not configured')
def test_alembic_upgrade_down_up_is_repeatable():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    config = Config(str(Path(__file__).with_name('alembic.ini')))
    command.downgrade(config, 'base')
    command.upgrade(config, 'head')
    command.downgrade(config, '001_timeline')
    command.upgrade(config, 'head')
    engine = create_engine(os.environ['DATABASE_URL'])
    try:
        with engine.connect() as connection:
            assert connection.execute(text('select version_num from alembic_version')).scalar() == '007_media_sweep_claims'
    finally:
        engine.dispose()
        app.configure_logging()
