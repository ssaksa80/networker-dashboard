"""An SMTP failure quotes the server's reply as text, never as b'...'.

The defect (review C24): smtplib hands over the server's reply as bytes. Every
branch of smtp_exception_detail decoded it except "sender refused", so that
message read `response=b'5.7.1 Sender rejected'` in the schedule's last result,
the log and the test-email error.
"""
import smtplib
import unittest

from nwdash.emailer import smtp_exception_detail

SENDER = "dashboard@example.com"


class TestSmtpReplyText(unittest.TestCase):
    def test_a_refused_sender_quotes_the_reply_as_text(self):
        detail = smtp_exception_detail(smtplib.SMTPSenderRefused(550, b"5.7.1 Sender rejected", SENDER))
        self.assertIn("response=5.7.1 Sender rejected", detail)
        self.assertNotIn("b'", detail)
        self.assertIn(f"sender={SENDER}", detail)
        self.assertIn("code=550", detail)

    def test_every_failure_with_a_reply_quotes_it_as_text(self):
        failures = {
            "authentication": smtplib.SMTPAuthenticationError(535, b"5.7.8 the reply"),
            "sender": smtplib.SMTPSenderRefused(550, b"5.7.8 the reply", SENDER),
            "data": smtplib.SMTPDataError(554, b"5.7.8 the reply"),
            "connect": smtplib.SMTPConnectError(421, b"5.7.8 the reply"),
        }
        for name, exc in failures.items():
            with self.subTest(name):
                detail = smtp_exception_detail(exc)
                self.assertIn("response=5.7.8 the reply", detail)
                self.assertNotIn("b'", detail)

    def test_a_reply_that_is_not_utf8_still_reads(self):
        detail = smtp_exception_detail(smtplib.SMTPSenderRefused(550, b"5.1.0 caf\xe9 refused", SENDER))
        self.assertIn("response=5.1.0 caf� refused", detail)

    def test_a_reply_given_as_text_is_kept(self):
        detail = smtp_exception_detail(smtplib.SMTPSenderRefused(550, "5.7.1 already text", SENDER))
        self.assertIn("response=5.7.1 already text", detail)


if __name__ == "__main__":
    unittest.main()
