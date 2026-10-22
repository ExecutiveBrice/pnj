"""Exercise offline JavaScript behavior when the optional Node runtime is present."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_offline_library_javascript():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is optional; needed for the mobile JavaScript tests")
    script = Path(__file__).with_name("mobile_offline.test.cjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True,
                            timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
