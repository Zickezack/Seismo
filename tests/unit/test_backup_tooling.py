"""pg_dump refuses a server of a newer major version, so client and db image must move together."""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_postgres_client_has_the_major_version_of_the_db_image():
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    script = (REPO_ROOT / "scripts" / "install-postgres-client.sh").read_text()

    db_major = re.search(r"image: postgres:(\d+)", compose).group(1)
    client_major = re.search(r"^POSTGRES_MAJOR=(\d+)$", script, re.MULTILINE).group(1)

    assert client_major == db_major


def test_image_and_dev_container_install_the_client_with_the_same_script():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    devcontainer = (REPO_ROOT / ".devcontainer" / "devcontainer.json").read_text()

    assert "install-postgres-client.sh" in dockerfile
    assert "scripts/install-postgres-client.sh" in devcontainer
