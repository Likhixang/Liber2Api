"""Deployment and local UI assets must not depend on remote JavaScript."""
from pathlib import Path
import json

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_local_static_assets_are_served():
    import app.main as main
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        for path in ("/static/ui.css", "/static/lucide.min.js"):
            response = await client.get(path)
            assert response.status_code == 200
            assert len(response.content) > 100


def test_template_does_not_execute_remote_scripts():
    html = (ROOT / "app/templates/index.html").read_text()
    assert 'src="https://' not in html
    assert "fonts.googleapis.com" not in html


def test_docker_entry_reads_port_and_host():
    dockerfile = (ROOT / "Dockerfile").read_text()
    line = next(line for line in dockerfile.splitlines() if line.startswith("CMD ["))
    command = json.loads(line[4:])
    assert "${HOST:-0.0.0.0}" in command[2]
    assert "${PORT:-8000}" in command[2]


def test_socks_client_dependency_present():
    # Construction is deliberately offline and catches missing httpx[socks].
    client = httpx.AsyncClient(proxy="socks5://127.0.0.1:9")
    import asyncio
    asyncio.run(client.aclose())
