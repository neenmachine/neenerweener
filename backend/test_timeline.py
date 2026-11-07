import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
from timeline import TimelineStore, render_timeline


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def list(self, before, limit):
        self.calls.append((before, limit))
        return self.rows


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
    assert script.get_heads() == ['004_media']
    assert script.get_revision('001_timeline').down_revision is None
    assert script.get_revision('002_admin_auth').down_revision == '001_timeline'
    assert script.get_revision('003_machine_auth').down_revision == '002_admin_auth'
    assert script.get_revision('004_media').down_revision == '003_machine_auth'


@pytest.mark.skipif(not os.environ.get('DATABASE_URL'), reason='DATABASE_URL not configured')
def test_alembic_upgrade_from_empty():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    config = Config(str(Path(__file__).with_name('alembic.ini')))
    engine = create_engine(os.environ['DATABASE_URL'])
    try:
        with engine.begin() as connection:
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
            assert connection.execute(text('select version_num from alembic_version')).scalar() == '004_media'
    finally:
        engine.dispose()
        app.configure_logging()
