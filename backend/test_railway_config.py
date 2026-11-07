import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_container_start_migrates_before_starting_the_server():
    dockerfile = (ROOT / "Dockerfile").read_text()
    command = (
        'CMD ["sh", "-c", "cd /app/backend && '
        '/app/backend/.venv/bin/alembic upgrade head && exec '
        '/app/backend/.venv/bin/uvicorn app:app --host 0.0.0.0 '
        '--port ${PORT:-8000} --no-access-log"]'
    )

    assert command in dockerfile
    assert "uv run" not in command


def test_railway_config_uses_schema_aware_readiness_without_predeploy():
    config = json.loads((ROOT / "railway.json").read_text())
    deploy = config["deploy"]

    assert "preDeployCommand" not in deploy
    assert "preDeployTimeoutSeconds" not in deploy
    assert deploy["healthcheckPath"] == "/readyz"
    assert deploy["healthcheckTimeout"] == 30
