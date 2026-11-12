import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import system_proxy
from system_proxy import GNOME_PROXY, SystemProxy


class SystemProxyTests(unittest.TestCase):
    def setUp(self):
        settings = patch.object(SystemProxy, "_original", None)
        settings.start()
        self.addCleanup(settings.stop)

    def test_windows_preserves_missing_values_and_restores_original_after_rotation(self):
        registry = {"ProxyEnable": (0, 4)}
        writes = []

        def read(keys):
            return {key: registry.get(key) for key in keys}

        def write(changes):
            writes.append(changes.copy())
            for key, value in changes.items():
                if value is None:
                    registry.pop(key, None)
                else:
                    registry[key] = value

        with patch.object(system_proxy, "SYSTEM", "Windows"), \
                patch.object(system_proxy, "winreg", SimpleNamespace(REG_SZ=1, REG_DWORD=4),
                             create=True), \
                patch.object(SystemProxy, "_win_read", side_effect=read), \
                patch.object(SystemProxy, "_win_write", side_effect=write):
            self.assertFalse(SystemProxy.enable("1.1.1.1", 1080, "socks5"))
            self.assertEqual(writes, [])
            self.assertTrue(SystemProxy.enable("[2606:4700::1]", 8080, "http"))
            self.assertEqual(registry["ProxyServer"], ("[2606:4700::1]:8080", 1))
            self.assertTrue(SystemProxy.enable("1.0.0.1", 1080, "socks4"))
            self.assertEqual(registry["ProxyServer"], ("socks=1.0.0.1:1080", 1))
            self.assertTrue(SystemProxy.disable())
            self.assertEqual(registry, {"ProxyEnable": (0, 4)})

    def test_linux_switches_protocol_and_restores_proxy_and_authentication(self):
        original = SystemProxy._linux_changes("old-proxy", 9000, "http")
        original[GNOME_PROXY, "mode"] = "'auto'"
        original[f"{GNOME_PROXY}.http", "use-authentication"] = "true"
        settings = original.copy()

        def gsettings(command, schema, key, value=None):
            if command == "get":
                return settings[schema, key]
            settings[schema, key] = value

        with patch.object(system_proxy, "SYSTEM", "Linux"), \
                patch.object(SystemProxy, "_gsettings", side_effect=gsettings):
            self.assertTrue(SystemProxy.enable("1.1.1.1", 8080, "http"))
            self.assertEqual(settings[f"{GNOME_PROXY}.http", "use-authentication"], "false")
            self.assertTrue(SystemProxy.enable("1.0.0.1", 1080, "socks5"))
            self.assertEqual(settings[f"{GNOME_PROXY}.http", "host"], "''")
            self.assertEqual(settings[f"{GNOME_PROXY}.https", "host"], "''")
            self.assertEqual(settings[f"{GNOME_PROXY}.socks", "host"], "'1.0.0.1'")
            self.assertTrue(SystemProxy.disable())
            self.assertEqual(settings, original)

    def test_linux_rolls_back_partial_rotation(self):
        initial = SystemProxy._linux_changes("old-proxy", 8080, "http")
        settings = initial.copy()

        def gsettings(command, schema, key, value=None):
            if command == "get":
                return settings[schema, key]
            if key == "host" and value == "'broken'":
                raise subprocess.CalledProcessError(1, "gsettings", stderr="Setting rejected")
            settings[schema, key] = value

        with patch.object(system_proxy, "SYSTEM", "Linux"), \
                patch.object(SystemProxy, "_gsettings", side_effect=gsettings):
            self.assertFalse(SystemProxy.enable("broken", 1080, "socks5"))
            self.assertEqual(SystemProxy.last_error, "Setting rejected")
            self.assertEqual(settings, initial)
            self.assertFalse(SystemProxy.needs_restore())


if __name__ == "__main__":
    unittest.main()
