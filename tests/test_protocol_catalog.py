from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.protocol_catalog import (  # noqa: E402
    describe_protocol_selection,
    persona_preset_catalog,
    protocol_wrapper_catalog,
)


class ProtocolCatalogTests(unittest.TestCase):
    def test_catalog_contains_expected_wrapper_and_persona_values(self) -> None:
        wrappers = {item["value"] for item in protocol_wrapper_catalog()}
        personas = {item["value"] for item in persona_preset_catalog()}

        self.assertEqual(wrappers, {"none", "websocket", "tls"})
        self.assertIn("browser_ws", personas)
        self.assertIn("low_noise_enterprise", personas)

    def test_describe_protocol_selection_emits_browser_note(self) -> None:
        payload = describe_protocol_selection("none", "browser_ws", False)

        self.assertEqual(payload["wrapper"]["value"], "none")
        self.assertEqual(payload["persona"]["value"], "browser_ws")
        self.assertTrue(any("browser_ws" in note for note in payload["notes"]))


if __name__ == "__main__":
    unittest.main()
