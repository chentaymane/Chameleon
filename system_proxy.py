"""Apply and restore desktop proxy settings on supported platforms."""

import os
import platform
import subprocess

SYSTEM = platform.system()
if SYSTEM == "Windows":
    import ctypes
    import winreg

GNOME_PROXY = "org.gnome.system.proxy"
GNOME_SCHEMES = ("http", "https", "socks")


class SystemProxy:
    _original = None
    last_error = ""

    @staticmethod
    def protocols():
        # The system SOCKS settings imply different versions on these platforms.
        if SYSTEM == "Linux" and "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":"):
            return ()
        return {
            "Windows": ("http", "socks4"),
            "Linux": ("http", "socks5"),
            "Darwin": ("http", "socks4", "socks5"),
        }.get(SYSTEM, ())

    @staticmethod
    def _win_key():
        return winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        )

    @classmethod
    def _win_read(cls, names):
        values = {}
        with cls._win_key() as key:
            for name in names:
                try:
                    values[name] = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    values[name] = None
        return values

    @classmethod
    def _win_write(cls, settings):
        with cls._win_key() as key:
            for name, value in settings.items():
                if value is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    data, kind = value
                    winreg.SetValueEx(key, name, 0, kind, data)
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)  # Settings changed
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)  # Refresh

    @staticmethod
    def _gsettings(*args):
        result = subprocess.run(["gsettings", *args], check=True, timeout=4,
                                capture_output=True, text=True)
        if "failed to commit changes" in result.stderr.lower():
            raise OSError(result.stderr.strip())
        return result.stdout.strip()

    @classmethod
    def _linux_read(cls, keys):
        return {key: cls._gsettings("get", *key) for key in keys}

    @classmethod
    def _linux_write(cls, settings):
        for key, value in settings.items():
            cls._gsettings("set", *key, value)
        if cls._linux_read(settings) != settings:
            raise OSError("GSettings did not retain the requested proxy settings.")

    @staticmethod
    def _linux_changes(host, port, protocol):
        enabled = ("http", "https") if protocol == "http" else ("socks",)
        changes = {}
        for scheme in GNOME_SCHEMES:
            schema = f"{GNOME_PROXY}.{scheme}"
            changes[schema, "host"] = repr(host if scheme in enabled else "")
            changes[schema, "port"] = str(port if scheme in enabled else 0)
        changes[f"{GNOME_PROXY}.http", "use-authentication"] = "false"
        changes[GNOME_PROXY, "mode"] = "'manual'"
        return changes

    @staticmethod
    def _mac_services():
        out = subprocess.run(["networksetup", "-listallnetworkservices"],
                             capture_output=True, text=True, check=True, timeout=4)
        return [line for line in out.stdout.splitlines()[1:] if line and not line.startswith("*")]

    @classmethod
    def _mac_enable(cls, host, port, protocol):
        for service in cls._mac_services():
            commands = ("-setwebproxy", "-setsecurewebproxy") if protocol == "http" else (
                "-setsocksfirewallproxy",
            )
            for command in commands:
                subprocess.run(["networksetup", command, service, host, str(port)],
                               check=True, timeout=4, capture_output=True)

    @classmethod
    def _mac_disable(cls):
        for service in cls._mac_services():
            for command in ("-setwebproxystate", "-setsecurewebproxystate", "-setsocksfirewallproxystate"):
                subprocess.run(["networksetup", command, service, "off"],
                               check=True, timeout=4, capture_output=True)

    @classmethod
    def _settings(cls, host, port, protocol):
        if SYSTEM == "Windows":
            endpoint = f"{host}:{port}"
            if protocol == "socks4":
                endpoint = f"socks={endpoint}"
            settings = {"ProxyServer": (endpoint, winreg.REG_SZ), "ProxyEnable": (1, winreg.REG_DWORD)}
            return settings, cls._win_read, cls._win_write
        return cls._linux_changes(host, port, protocol), cls._linux_read, cls._linux_write

    @classmethod
    def enable(cls, host, port, protocol):
        cls.last_error = ""
        if protocol not in cls.protocols():
            cls.last_error = "Use Copy selected for this proxy type on this platform."
            return False
        try:
            if SYSTEM == "Darwin":
                cls._mac_enable(host, port, protocol)
                return True
            settings, read, write = cls._settings(host, port, protocol)
            previous = read(settings)
            # Capture the user's settings once. Each later change can also roll
            # back to the route Chameleon had applied immediately before it.
            try:
                write(settings)
            except (OSError, subprocess.SubprocessError):
                try:
                    write(previous)
                except (OSError, subprocess.SubprocessError) as rollback_error:
                    if cls._original is None:
                        cls._original = previous
                    cls.last_error = f"Proxy rollback failed: {cls._describe(rollback_error)}"
                raise
            if cls._original is None:
                cls._original = previous
            return True
        except (OSError, subprocess.SubprocessError) as error:
            message = cls._describe(error)
            cls.last_error = f"{message}. {cls.last_error}" if cls.last_error else message
            return False

    @classmethod
    def needs_restore(cls):
        return cls._original is not None

    @classmethod
    def disable(cls):
        cls.last_error = ""
        try:
            if SYSTEM == "Darwin":
                cls._mac_disable()
            elif cls._original is not None:
                write = cls._win_write if SYSTEM == "Windows" else cls._linux_write
                write(cls._original)
                cls._original = None
            return True
        except (OSError, subprocess.SubprocessError) as error:
            cls.last_error = cls._describe(error)
            return False

    @staticmethod
    def _describe(error):
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            if isinstance(error.stderr, bytes):
                return error.stderr.decode(errors="replace").strip()
            return error.stderr.strip()
        return str(error)

    @staticmethod
    def platform_note():
        if SYSTEM == "Linux" and not SystemProxy.protocols():
            return "Use Copy selected to configure a proxy in your application on KDE."
        return {
            "Windows": "Windows system proxy supports HTTP and SOCKS4. Copy SOCKS5 to an app.",
            "Linux": "GNOME-compatible system proxy supports HTTP and SOCKS5. Copy SOCKS4 to an app.",
            "Darwin": "macOS uses networksetup for the system proxy.",
        }.get(SYSTEM, "Copy verified proxies into your browser or application.")
