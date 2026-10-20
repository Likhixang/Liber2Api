"""Offline browser regressions: execute the shipped template JS, never contact an API."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile

from jinja2 import Template
import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for offline WebUI regressions")
def test_webui_offline_regressions():
    tests_dir = Path(__file__).resolve().parent
    node = shutil.which("node")
    assert node is not None
    template = Template((tests_dir.parent / "app/templates/index.html").read_text())
    pages = {
        "locked": template.render(has_admin_pass=True, is_authenticated=False, api_key="disabled-legacy-secret"),
        "open": template.render(has_admin_pass=True, is_authenticated=True, api_key="disabled-legacy-secret"),
    }
    with tempfile.TemporaryDirectory(prefix="liber2api-webui-") as root:
        rendered = Path(root) / "pages.json"
        rendered.write_text(json.dumps(pages), encoding="utf-8")
        result = subprocess.run(
            [node, "--test", str(tests_dir / "webui_regressions.js")],
            cwd=tests_dir.parent,
            env={**os.environ, "WEBUI_RENDERED_PAGES_FILE": str(rendered)},
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr
