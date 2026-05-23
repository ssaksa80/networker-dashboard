import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path

import networker_dashboard as nd


class JsonFormatterTests(unittest.TestCase):
    def test_format_basic(self):
        fmt = nd._JsonLogFormatter()
        rec = logging.LogRecord("networker_dashboard", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        obj = json.loads(fmt.format(rec))
        self.assertEqual(obj["level"], "INFO")
        self.assertEqual(obj["logger"], "networker_dashboard")
        self.assertEqual(obj["msg"], "hello world")
        self.assertIn("ts", obj)

    def test_format_request_id(self):
        fmt = nd._JsonLogFormatter()
        rec = logging.LogRecord("networker_dashboard", logging.INFO, __file__, 1, "m", (), None)
        rec.request_id = "abc123"
        obj = json.loads(fmt.format(rec))
        self.assertEqual(obj["request_id"], "abc123")


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self):
        self._handlers = list(nd.LOG.handlers)
        self._level = nd.LOG.level
        self._propagate = nd.LOG.propagate
        self._dir = nd.LOG_DIR
        self._file = nd.LOG_FILE
        self._tmp = Path(tempfile.mkdtemp())
        nd.LOG_DIR = self._tmp
        nd.LOG_FILE = self._tmp / "test.log"

    def tearDown(self):
        for h in list(nd.LOG.handlers):
            try:
                h.close()
            except Exception:
                pass
        nd.LOG.handlers[:] = self._handlers
        nd.LOG.setLevel(self._level)
        nd.LOG.propagate = self._propagate
        nd.LOG_DIR = self._dir
        nd.LOG_FILE = self._file
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_idempotent_handlers(self):
        nd.configure_logging(True)
        n1 = len(nd.LOG.handlers)
        nd.configure_logging(True)
        n2 = len(nd.LOG.handlers)
        self.assertEqual(n1, n2)
        self.assertGreaterEqual(n1, 1)

    def test_level_reflects_debug(self):
        nd.configure_logging(True)
        self.assertEqual(nd.LOG.level, logging.DEBUG)
        nd.configure_logging(False)
        self.assertEqual(nd.LOG.level, logging.INFO)

    def test_writes_to_file(self):
        nd.configure_logging(False)
        nd.LOG.info("hello-file-test")
        for h in nd.LOG.handlers:
            try:
                h.flush()
            except Exception:
                pass
        self.assertTrue(nd.LOG_FILE.exists())
        self.assertIn("hello-file-test", nd.LOG_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
