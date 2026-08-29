"""
2FA Recovery Email Module

Provides one-time use recovery links for 2FA account access.
When users lose access to their authenticator app, they can request
a recovery link via email to regain access.

Features:
- Cryptographically secure token generation
- Time-limited links (configurable TTL)
- One-time use with atomic redemption
- Rate limiting on link generation
- Integration with existing auth system
"""

import hashlib
import hmac
import logging
import os
import secrets
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Auth.Recovery")

# Default settings
DEFAULT_LINK_TTL_HOURS = 1
DEFAULT_MAX_RECOVERY_LINKS = 3
DEFAULT_RECOVERY_LOCKOUT_HOURS = 24

# Internal storage for recovery links
_recovery_links: Dict[str, Dict[str, Any]] = {}  # token_hash -> link data


def _hash_token(token: str) -> str:
    """Hash a recovery token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_recovery_link(
    username: str,
    config_path: Optional[str] = None,
    ttl_hours: Optional[int] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a one-time use recovery link for 2FA recovery.

    Args:
        username: The user to generate the link for
        config_path: Optional auth config path
        ttl_hours: Link validity in hours (default: from settings)
        base_url: Base URL for the recovery link

    Returns:
        Dict with token, link, expires_at, and metadata
    """
    from ..config import get_settings
    settings = get_settings()

    if ttl_hours is None:
        ttl_hours = getattr(settings, "recovery_link_ttl_hours", DEFAULT_LINK_TTL_HOURS)

    # Verify user exists and has 2FA enabled
    from . import get_user, get_2fa_status
    user = get_user(username, config_path)
    if user is None:
        return {"error": "User not found"}

    status = get_2fa_status(username, config_path)
    if not status.get("configured") and not status.get("enabled"):
        return {"error": "2FA not configured for this user"}

    # Rate limiting: check recent recovery link count
    max_links = getattr(settings, "recovery_max_links", DEFAULT_MAX_RECOVERY_LINKS)
    recent_links = _count_recent_links(username, max_age_hours=DEFAULT_RECOVERY_LOCKOUT_HOURS)
    if recent_links >= max_links:
        return {
            "error": f"Too many recovery links requested. Max {max_links} per {DEFAULT_RECOVERY_LOCKOUT_HOURS}h.",
            "retry_after_hours": DEFAULT_RECOVERY_LOCKOUT_HOURS,
        }

    # Generate cryptographically secure token
    token = secrets.token_urlsafe(48)
    token_hash = _hash_token(token)

    # Calculate expiry
    created_at = time.time()
    expires_at = created_at + (ttl_hours * 3600)

    # Store hashed token
    _recovery_links[token_hash] = {
        "username": username,
        "created_at": created_at,
        "expires_at": expires_at,
        "used": False,
        "ttl_hours": ttl_hours,
    }

    # Build recovery URL
    if base_url is None:
        base_url = getattr(settings, "recovery_base_url", "http://localhost:8000")

    recovery_url = f"{base_url.rstrip('/')}/auth/2fa/recovery/redeem?token={token}"

    logger.info(f"Recovery link generated for '{username}', expires in {ttl_hours}h")

    return {
        "token": token,
        "link": recovery_url,
        "username": username,
        "expires_at": expires_at,
        "expires_in_hours": ttl_hours,
        "message": "Recovery link generated. Link expires in {} hour(s).".format(ttl_hours),
    }


def redeem_recovery_link(
    token: str,
    new_totp_secret: Optional[str] = None,
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Redeem a one-time use recovery link to reset 2FA.

    Args:
        token: The recovery token from the URL
        new_totp_secret: Optional new TOTP secret to set (if None, just disables 2FA)
        config_path: Optional auth config path

    Returns:
        Dict with success status and next steps
    """
    token_hash = _hash_token(token)

    link_data = _recovery_links.get(token_hash)
    if link_data is None:
        return {"error": "Invalid or expired recovery link", "success": False}

    # Check expiry
    if time.time() > link_data["expires_at"]:
        del _recovery_links[token_hash]
        return {"error": "Recovery link has expired", "success": False}

    # Check if already used
    if link_data["used"]:
        return {"error": "Recovery link already used", "success": False}

    username = link_data["username"]

    # Mark as used (atomic)
    link_data["used"] = True

    # Update user's 2FA
    from . import get_user, _load_config, save_config
    config = _load_config(config_path)
    user = config.get("users", {}).get(username)

    if user is None:
        return {"error": "User not found", "success": False}

    if new_totp_secret:
        # Set new secret but require verification
        user["totp_secret"] = new_totp_secret
        user["totp_enabled"] = False
        save_config(config, config_path)
        logger.info(f"Recovery link used for '{username}' - new TOTP secret set, awaiting verification")
        return {
            "success": True,
            "message": "Recovery link redeemed. New 2FA secret set. Please verify with your authenticator app.",
            "username": username,
            "action": "verify_2fa_setup",
        }
    else:
        # Disable 2FA entirely
        user.pop("totp_secret", None)
        user.pop("totp_backup_codes", None)
        user["totp_enabled"] = False
        save_config(config, config_path)
        logger.info(f"Recovery link used for '{username}' - 2FA disabled")
        return {
            "success": True,
            "message": "2FA has been disabled. You can now log in without 2FA.",
            "username": username,
            "action": "2fa_disabled",
        }


def send_recovery_email(
    username: str,
    recovery_link: str,
    to_email: Optional[str] = None,
    smtp_config: Optional[Dict[str, str]] = None,
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a recovery link via email.

    Args:
        username: User to send to
        recovery_link: The full recovery URL
        to_email: Recipient email (if None, uses user's email from config)
        smtp_config: SMTP settings override
        config_path: Optional auth config path

    Returns:
        Dict with send status
    """
    from ..config import get_settings
    settings = get_settings()

    # Get recipient email
    if to_email is None:
        from . import get_user
        user = get_user(username, config_path)
        if user is None:
            return {"error": "User not found", "success": False}
        to_email = user.get("email")
        if not to_email:
            return {"error": "No email address for user", "success": False}

    # SMTP config
    if smtp_config is None:
        smtp_config = {
            "host": getattr(settings, "smtp_host", "smtp.gmail.com"),
            "port": getattr(settings, "smtp_port", 587),
            "username": getattr(settings, "smtp_username", None),
            "password": getattr(settings, "smtp_password", None),
            "from_address": getattr(settings, "email_sending_domain", "sfa@yourcompany.com"),
        }

    if not smtp_config.get("username") or not smtp_config.get("password"):
        return {"error": "SMTP not configured", "success": False, "send_mode": "manual"}

    # Compose email
    subject = "SFA Account Recovery Link"
    body = f"""Hello,

You requested a 2FA recovery link for your Sales Follow-Up Agent account.

Click the link below to reset your 2FA:
{recovery_link}

This link expires in 1 hour and can only be used once.

If you did not request this link, please ignore this email and
consider changing your password.

---
Sales Follow-Up Agent
Security Team"""

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = smtp_config["from_address"]
        msg["To"] = to_email
        msg["Subject"] = subject
        msg["X-Mailer"] = "SFA-Recovery/1.0"

        with smtplib.SMTP(smtp_config["host"], int(smtp_config["port"]), timeout=30) as server:
            server.ehlo()
            if int(smtp_config["port"]) == 587:
                server.starttls()
                server.ehlo()
            server.login(smtp_config["username"], smtp_config["password"])
            server.sendmail(smtp_config["from_address"], [to_email], msg.as_string())

        logger.info(f"Recovery email sent to '{to_email}' for user '{username}'")
        return {
            "success": True,
            "to": to_email,
            "message": "Recovery email sent successfully.",
        }

    except Exception as e:
        logger.error(f"Failed to send recovery email: {e}")
        return {
            "error": f"Email send failed: {str(e)}",
            "success": False,
            "to": to_email,
        }


def get_pending_recovery_links(
    username: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Get pending (unused, unexpired) recovery links.

    Args:
        username: Optional filter by username

    Returns:
        List of link metadata (without tokens for security)
    """
    now = time.time()
    results = []

    for link_hash, data in _recovery_links.items():
        if data["used"]:
            continue
        if now > data["expires_at"]:
            continue
        if username and data["username"] != username:
            continue

        results.append({
            "username": data["username"],
            "created_at": data["created_at"],
            "expires_at": data["expires_at"],
            "ttl_hours": data["ttl_hours"],
        })

    return results


def cleanup_expired_links() -> int:
    """Remove expired recovery links. Returns count removed."""
    now = time.time()
    expired = [h for h, d in _recovery_links.items() if now > d["expires_at"]]
    for h in expired:
        del _recovery_links[h]
    return len(expired)


def get_recovery_stats() -> Dict[str, Any]:
    """Get statistics about recovery links."""
    now = time.time()
    total = len(_recovery_links)
    pending = sum(1 for d in _recovery_links.values() if not d["used"] and now <= d["expires_at"])
    used = sum(1 for d in _recovery_links.values() if d["used"])
    expired = total - pending - used

    return {
        "total_links": total,
        "pending": pending,
        "used": used,
        "expired": expired,
    }


def _count_recent_links(username: str, max_age_hours: float = 24) -> int:
    """Count recovery links created within the time window."""
    cutoff = time.time() - (max_age_hours * 3600)
    return sum(
        1 for d in _recovery_links.values()
        if d["username"] == username and d["created_at"] > cutoff
    )


# ── Recovery Link Email Sender (for alerts integration) ────────

class RecoveryEmailSender:
    """
    High-level class for sending recovery emails.
    Wraps generate_recovery_link + send_recovery_email.
    """

    def __init__(self, config_path: Optional[str] = None, smtp_config: Optional[Dict] = None):
        self._config_path = config_path
        self._smtp_config = smtp_config

    def request_recovery(
        self,
        username: str,
        base_url: Optional[str] = None,
        send_email: bool = True,
    ) -> Dict[str, Any]:
        """
        Request a recovery link for a user.

        Args:
            username: User to generate recovery for
            base_url: Optional base URL override
            send_email: Whether to send email (False = just return link)

        Returns:
            Dict with link info and send status
        """
        result = generate_recovery_link(
            username,
            config_path=self._config_path,
            base_url=base_url,
        )

        if "error" in result:
            return result

        if send_email:
            email_result = send_recovery_email(
                username,
                result["link"],
                smtp_config=self._smtp_config,
                config_path=self._config_path,
            )
            result["email_sent"] = email_result.get("success", False)
            result["email_status"] = email_result

            # If SMTP not configured, include link in response for manual use
            if not email_result.get("success"):
                result["manual_link"] = result["link"]
                result["message"] += (
                    " Note: Email not configured. Please provide the link manually."
                )

        return result
