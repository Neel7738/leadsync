"""Email open/click tracking."""
from .tracking import (
    EmailTracking,
    generate_tracking_pixel_url,
    generate_tracking_link_url,
    parse_tracking_url,
    log_open_event,
    log_click_event,
    update_prospect_response_status,
    calculate_engagement_score,
    format_pixel_img_tag,
)

__all__ = [
    "EmailTracking",
    "generate_tracking_pixel_url",
    "generate_tracking_link_url",
    "parse_tracking_url",
    "log_open_event",
    "log_click_event",
    "update_prospect_response_status",
    "calculate_engagement_score",
    "format_pixel_img_tag",
]
