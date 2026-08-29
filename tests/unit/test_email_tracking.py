"""Tests for email tracking."""

import pytest
from core.email_tracking import (
    EmailTracking,
    generate_tracking_pixel_url,
    generate_tracking_link_url,
    parse_tracking_url,
    log_open_event,
    log_click_event,
    calculate_engagement_score,
    format_pixel_img_tag,
)


class TestEmailTracking:
    def test_init(self):
        et = EmailTracking("p1", "test@example.com")
        assert et.prospect_id == "p1"
        assert et.opens == 0
        assert et.clicks == 0
        assert et.is_suppressed() is False

    def test_log_open(self):
        et = EmailTracking("p1", "test@example.com")
        et.log_open()
        assert et.opens == 1
        assert et.last_opened is not None
        et.log_open()
        assert et.opens == 2

    def test_log_click(self):
        et = EmailTracking("p1", "test@example.com")
        et.log_click("https://example.com")
        assert et.clicks == 1
        assert et.last_clicked is not None

    def test_engagement_score_no_activity(self):
        et = EmailTracking("p1", "test@example.com")
        assert et.engagement_score() == 0.0

    def test_engagement_score_with_activity(self):
        et = EmailTracking("p1", "test@example.com")
        for _ in range(5):
            et.log_open()
        et.log_click("https://example.com")
        score = et.engagement_score()
        assert 0.0 < score <= 1.0

    def test_suppress(self):
        et = EmailTracking("p1", "test@example.com")
        et.suppress()
        assert et.is_suppressed() is True

    def test_pixel_img_tag(self):
        et = EmailTracking("p1", "test@example.com")
        tag = et.pixel_img_tag("https://track.example.com/pixel")
        assert "<img" in tag
        assert "https://track.example.com/pixel" in tag


class TestTrackingURLs:
    def test_generate_pixel_url(self):
        url = generate_tracking_pixel_url(
            "https://example.com/track", "p1", "h1"
        )
        assert "p=p1" in url
        assert "h=h1" in url

    def test_generate_link_url(self):
        url = generate_tracking_link_url(
            "https://example.com/track", "p1", "h1",
            "https://destination.com"
        )
        assert "p=p1" in url
        assert "h=h1" in url
        assert "l=" in url

    def test_parse_tracking_url(self):
        url = generate_tracking_pixel_url("https://example.com/track", "p1", "h1")
        params = parse_tracking_url(url)
        assert params["p"] == "p1"
        assert params["h"] == "h1"


class TestEventLogging:
    def test_log_open_event(self):
        result = log_open_event("p1", "test@example.com")
        assert result["event"] == "open"
        assert result["prospect_id"] == "p1"
        assert "timestamp" in result

    def test_log_click_event(self):
        result = log_click_event("p1", "test@example.com", "https://example.com")
        assert result["event"] == "click"
        assert result["link_url"] == "https://example.com"


class TestEngagementScore:
    def test_no_activity(self):
        assert calculate_engagement_score(0, 0) == 0.0

    def test_with_opens(self):
        score = calculate_engagement_score(5, 0)
        assert 0.0 < score <= 1.0

    def test_with_clicks(self):
        score = calculate_engagement_score(0, 3)
        assert 0.0 < score <= 1.0

    def test_combined(self):
        score = calculate_engagement_score(3, 2)
        assert 0.0 < score <= 1.0

    def test_high_values_capped(self):
        score = calculate_engagement_score(100, 100)
        assert score == 1.0


class TestPixelTag:
    def test_format(self):
        tag = format_pixel_img_tag("https://example.com/pixel")
        assert tag.startswith("<img")
        assert "display:none" in tag
