# SPDX-License-Identifier: Apache-2.0
"""Run with: python -m unittest discover -s deploy/huggingface/tests  (needs gradio installed)

Spaces set GRADIO_SSR_MODE=true. With SSR on, mounting Gradio starts a Node server on the
Space's one public port, uvicorn can't bind it, and the Space crash-loops with "address already
in use". That shipped once; this pins SSR off whatever the environment says.
"""

import importlib
import os
import sys
import unittest
from pathlib import Path

SPACE_DIR = Path(__file__).resolve().parents[1] / "databounty"

try:
    import gradio  # noqa: F401
except ImportError:  # pragma: no cover - only where gradio isn't installed
    gradio = None


@unittest.skipIf(gradio is None, "gradio is not installed")
class AppMountTest(unittest.TestCase):
    def test_ssr_stays_off_even_when_spaces_turn_it_on(self):
        os.environ["GRADIO_SSR_MODE"] = "true"
        sys.path.insert(0, str(SPACE_DIR))
        cwd = os.getcwd()
        os.chdir(SPACE_DIR)
        try:
            app = importlib.import_module("app")
        finally:
            os.chdir(cwd)
        self.assertIs(app.demo.ssr_mode, False)
        paths = {getattr(route, "path", "") for route in app.app.routes}
        self.assertTrue({"/oauth/start", "/oauth/callback", "/oauth/client-metadata.json"} <= paths)


if __name__ == "__main__":
    unittest.main()
