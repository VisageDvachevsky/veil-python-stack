from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.provisioning import (  # noqa: E402
    ClientConnectionProfile,
    export_client_profile,
    generate_psk_hex,
    profile_summary,
)


class ProvisioningTests(unittest.TestCase):
    def test_generate_psk_hex_has_expected_length(self) -> None:
        psk = generate_psk_hex()
        self.assertEqual(len(psk), 64)

    def test_export_client_profile_roundtrip(self) -> None:
        profile = export_client_profile(
            server_host="vpn.example",
            server_port=4433,
            psk_hex="12" * 32,
            tunnel_mode="dynamic",
            protocol_wrapper="websocket",
            persona_preset="browser_ws",
            enable_http_handshake_emulation=True,
            rotation_interval_seconds=45,
            handshake_timeout_ms=7000,
            session_idle_timeout_ms=12000,
            transport_mtu=1500,
        )
        self.assertEqual(profile.server_host, "vpn.example")
        self.assertEqual(profile.tunnel_mode, "dynamic")
        self.assertEqual(profile.protocol_wrapper, "websocket")
        self.assertEqual(profile_summary(profile)["psk_hex"], "12" * 32)
        self.assertTrue(profile_summary(profile)["enable_http_handshake_emulation"])
        self.assertEqual(profile_summary(profile)["protocol_details"]["wrapper"]["value"], "websocket")
        self.assertTrue(profile_summary(profile)["protocol_details"]["notes"])

        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "profile.json"
            profile.write(path)
            loaded = ClientConnectionProfile.from_path(path)
            self.assertEqual(loaded.psk_hex, "12" * 32)
            self.assertEqual(loaded.persona_preset, "browser_ws")
            self.assertEqual(loaded.transport_mtu, 1500)

    def test_profile_import_token_roundtrip(self) -> None:
        profile = export_client_profile(
            server_host="vpn.example",
            server_port=4433,
            psk_hex="34" * 32,
        )
        token = profile.to_import_token()
        loaded = ClientConnectionProfile.from_import_token(token)
        self.assertEqual(loaded.server_host, "vpn.example")
        self.assertEqual(loaded.psk_hex, "34" * 32)
        self.assertTrue(profile_summary(profile)["import_token"].startswith("veil://profile/"))


if __name__ == "__main__":
    unittest.main()
