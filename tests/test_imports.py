"""Every nwdash module must import cleanly (no circular imports, no missing names)."""
import importlib
import unittest

MODULES = [
    "nwdash",
    "nwdash.config",
    "nwdash.secrets",
    "nwdash.auth",
    "nwdash.certs",
    "nwdash.ui",
    "nwdash.models",
    "nwdash.profiles",
    "nwdash.wmi_health",
    "nwdash.restapi",
    "nwdash.nwui",
    "nwdash.sessions",
    "nwdash.snapshots",
    "nwdash.reports",
    "nwdash.emailer",
    "nwdash.server",
    "nwdash.main",
]


class TestImports(unittest.TestCase):
    def test_all_modules_import(self):
        for name in MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_package_exports_version(self):
        import nwdash

        self.assertRegex(nwdash.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_entry_script_compiles(self):
        import py_compile
        from pathlib import Path

        entry = Path(__file__).resolve().parent.parent / "networker_dashboard.py"
        py_compile.compile(str(entry), doraise=True)


if __name__ == "__main__":
    unittest.main()
