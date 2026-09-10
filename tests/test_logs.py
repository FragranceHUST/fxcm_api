import logging
import unittest

from fxcm_api.logs import SendRequestThreadNoiseFilter


class TestSendRequestThreadNoiseFilter(unittest.TestCase):
    def _record(self, msg: str) -> logging.LogRecord:
        return logging.LogRecord("root", logging.WARNING, __file__, 1, msg, None, None)

    def test_wrapper_warning_dropped(self):
        f = SendRequestThreadNoiseFilter()
        self.assertFalse(f.filter(self._record(
            "Calling the send_request method is not from the main thread. "
            "If you call the send_request method from a callback, ...")))

    def test_other_records_pass(self):
        f = SendRequestThreadNoiseFilter()
        self.assertTrue(f.filter(self._record("guard 循环已启动")))
        self.assertTrue(f.filter(self._record("send_request 成功")))


if __name__ == "__main__":
    unittest.main()
