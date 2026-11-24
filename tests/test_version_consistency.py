from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

import gateway
import remote_agent
from remote_agent.core.version import AGENT_VERSION, CORE_VERSION, PROTOCOL_VERSION


class VersionConsistencyTests(unittest.TestCase):
    def test_all_public_version_sources_are_synchronized(self) -> None:
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]
        manifest = json.loads(
            (root / "remote_agent" / "manifest.json").read_text(encoding="utf-8")
        )
        readme = (root / "README.md").read_text(encoding="utf-8")
        index = (root / "gateway" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertEqual(gateway.__version__, version)
        self.assertEqual(remote_agent.__version__, version)
        self.assertEqual(AGENT_VERSION, version)
        self.assertEqual(manifest["agent_version"], version)
        self.assertEqual(manifest["core_version"], CORE_VERSION)
        self.assertIn(f"version-{version}-2f6feb", readme)
        self.assertIn(f"| Remote Agent | `{version}` |", readme)
        self.assertIn(f"| Agent Core | `{CORE_VERSION}` |", readme)
        self.assertIn(f"| WebSocket Protocol | `{PROTOCOL_VERSION}` |", readme)
        self.assertEqual(
            re.findall(r'/assets/(?:styles\.css|app\.js)\?v=([^"&\s]+)', index),
            [version, version],
        )


if __name__ == "__main__":
    unittest.main()
