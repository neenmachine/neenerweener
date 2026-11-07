import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_railway_deploy_config_keeps_migration_and_readiness_atomic():
    config = json.loads((ROOT / "railway.json").read_text())
    deploy = config["deploy"]

    assert deploy["preDeployCommand"] == [
        "cd /app/backend && /app/backend/.venv/bin/alembic upgrade head"
    ]
    assert deploy["preDeployTimeoutSeconds"] == 300
    assert deploy["healthcheckPath"] == "/readyz"
    assert deploy["healthcheckTimeout"] == 30
