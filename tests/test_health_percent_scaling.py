"""A low CPU or memory reading stays low; it is not mistaken for a fraction.

The defect (review C16): `percent_from_any` multiplied every value <= 1 by 100,
guessing that it was a 0-1 ratio. WMI sends whole-number percentages, so a 1%
CPU sample showed as 100% and turned the server health card Critical. Now a
field named "...Percent" is always a percentage, and only a value strictly
between 0 and 1 in a field that may carry a ratio is scaled.
"""
import unittest

from nwdash.wmi_health import percent_from_any, server_health_from_payload


class TestPercentFieldsAreNeverScaled(unittest.TestCase):
    def test_one_percent_cpu_from_wmi_is_one_percent_and_healthy(self):
        # The shape load_server_health_wmi parses: whole numbers from [int]/Round.
        payload = {"host": "nw.example.com", "cpuUsagePercent": 1, "ramUsagePercent": 40}
        health = server_health_from_payload(payload, "WMI nw.example.com")
        self.assertEqual(health["cpuUsagePercent"], 1)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["label"], "Healthy")

    def test_idle_and_one_percent_memory(self):
        health = server_health_from_payload({"cpuUsagePercent": 0, "ramUsagePercent": 1}, "WMI")
        self.assertEqual((health["cpuUsagePercent"], health["ramUsagePercent"]), (0, 1))
        self.assertEqual(health["status"], "ok")

    def test_a_small_decimal_in_a_percent_field_is_a_percentage(self):
        health = server_health_from_payload({"cpuPercent": 0.6, "memoryUsagePercent": "0.4"}, "/global/serverstatistics")
        self.assertEqual((health["cpuUsagePercent"], health["ramUsagePercent"]), (1, 0))

    def test_percent_text_is_never_scaled(self):
        self.assertEqual(percent_from_any("0.5%", may_be_fraction=True), 0)
        self.assertEqual(percent_from_any("1 %", may_be_fraction=True), 1)


class TestRatioFieldsScaleOnlyTrueFractions(unittest.TestCase):
    def test_a_ratio_is_scaled(self):
        health = server_health_from_payload({"cpu": 0.42, "memoryUsage": "0.8"}, "/nwui/api/health")
        self.assertEqual((health["cpuUsagePercent"], health["ramUsagePercent"]), (42, 80))
        self.assertEqual(health["status"], "warning")

    def test_an_integer_in_a_ratio_field_is_not_scaled(self):
        health = server_health_from_payload({"cpuUsage": 1, "ram": 0}, "/global/serverstatus")
        self.assertEqual((health["cpuUsagePercent"], health["ramUsagePercent"]), (1, 0))
        self.assertEqual(health["status"], "ok")

    def test_values_are_clamped_and_junk_is_none(self):
        self.assertEqual(percent_from_any(250), 100)
        self.assertIsNone(percent_from_any("n/a"))
        self.assertIsNone(percent_from_any(None))


if __name__ == "__main__":
    unittest.main()
