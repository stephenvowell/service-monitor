"""Unit tests for the pure logic in service_monitor.

These avoid any real network/subprocess I/O so they run fast and offline
(the network checks themselves are thin wrappers around the standard library).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import service_monitor as sm  # noqa: E402


class TestClassifyHttp:
    def test_ok_is_up(self):
        assert sm.classify_http(200, 399) == "UP"

    def test_upper_bound_is_up(self):
        assert sm.classify_http(399, 399) == "UP"

    def test_expect_max_can_allow_405(self):
        assert sm.classify_http(405, 405) == "UP"

    def test_client_error_is_warn(self):
        assert sm.classify_http(404, 399) == "WARN"
        assert sm.classify_http(403, 399) == "WARN"

    def test_server_error_is_down(self):
        assert sm.classify_http(500, 399) == "DOWN"
        assert sm.classify_http(503, 399) == "DOWN"


class TestMbps:
    def test_one_megabyte_per_second(self):
        # 1,000,000 bytes in 1 s == 8 Mbps.
        assert sm.mbps(1_000_000, 1.0) == 8.0

    def test_zero_seconds_is_none(self):
        assert sm.mbps(1000, 0) is None

    def test_negative_seconds_is_none(self):
        assert sm.mbps(1000, -1) is None

    def test_zero_bytes_is_none(self):
        assert sm.mbps(0, 1.0) is None


class TestTargetLabel:
    def test_http(self):
        assert sm.target_label({"type": "http", "target": "https://x/"}) == "https://x/"

    def test_tcp_includes_port(self):
        assert sm.target_label({"type": "tcp", "target": "1.2.3.4", "port": 22}) == "1.2.3.4:22"

    def test_netperf_mentions_upload(self):
        label = sm.target_label({"type": "netperf", "target": "1.1.1.1"})
        assert "upload" in label and "1.1.1.1" in label

    def test_netdown_mentions_download(self):
        label = sm.target_label({"type": "netdown", "target": "1.1.1.1"})
        assert "download" in label and "1.1.1.1" in label


class TestElide:
    def test_short_unchanged(self):
        assert sm.elide("abc", 10) == "abc"

    def test_long_truncated_with_ellipsis(self):
        out = sm.elide("abcdefghij", 5)
        assert len(out) == 5
        assert out.endswith("\u2026")


class TestNewResult:
    def test_default_shape(self):
        r = sm.new_result()
        assert r["state"] == "CHECKING"
        assert set(r) == {"state", "detail", "value", "checked"}


class TestCheckersRegistry:
    def test_expected_types_registered(self):
        assert {"http", "tcp", "ping", "netperf", "netdown"}.issubset(sm.CHECKERS)

    def test_run_check_swallows_errors(self, monkeypatch):
        def boom(_svc):
            raise RuntimeError("kaboom")

        monkeypatch.setitem(sm.CHECKERS, "boom", boom)
        result = sm.run_check({"type": "boom"})
        assert result["state"] == "DOWN"
        assert "kaboom" in result["detail"]


class TestConfig:
    def test_roundtrip(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.json"
        monkeypatch.setattr(sm, "CONFIG_PATH", str(cfg))
        sm.save_config({"settings": {"interval_label": "1 min"}, "services": []})
        loaded = sm.load_config()
        assert loaded["settings"]["interval_label"] == "1 min"

    def test_missing_file_writes_default(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.json"
        monkeypatch.setattr(sm, "CONFIG_PATH", str(cfg))
        loaded = sm.load_config()
        assert "services" in loaded and cfg.exists()

    def test_defaults_backfilled(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg.json"
        cfg.write_text('{"services": []}', encoding="utf-8")
        monkeypatch.setattr(sm, "CONFIG_PATH", str(cfg))
        loaded = sm.load_config()
        # Missing settings keys are filled from DEFAULT_CONFIG.
        assert "interval_label" in loaded["settings"]
