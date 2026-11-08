from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_container_start_migrates_before_starting_the_server():
    dockerfile = (ROOT / "Dockerfile").read_text()
    command = (
        'CMD ["sh", "-c", "cd /app/backend && '
        '/app/backend/.venv/bin/alembic upgrade head && exec '
        '/app/backend/.venv/bin/uvicorn app:app --host 0.0.0.0 '
        '--port ${PORT:-8000} --no-access-log --no-proxy-headers"]'
    )

    assert command in dockerfile
    assert "uv run" not in command


def test_image_installs_and_checks_toronto_zone_data():
    # python:slim has no IANA zone database; timeline.TORONTO needs it.
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "apt-get install -y --no-install-recommends openssh-client tzdata" in dockerfile
    assert """ZoneInfo("America/Toronto")""" in dockerfile
