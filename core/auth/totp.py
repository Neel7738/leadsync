"""
TOTP (Time-based One-Time Password) for two-factor authentication.

Implements RFC 6238 TOTP with HMAC-SHA1, compatible with:
  - Google Authenticator
  - Authy
  - Microsoft Authenticator
  - Any TOTP-compatible app

Provides:
  - TOTP generation and verification
  - QR code URL generation (otpauth:// URI)
  - Backup codes for account recovery
  - Configurable time window and digits

Usage:
    totp = TOTPManager()
    secret = totp.generate_secret()
    uri = totp.get_uri(secret, "user@company.com", "SalesFollowUpAgent")
    codes = totp.generate_backup_codes()

    # Verify a code
    is_valid = totp.verify(secret, "123456")
"""

import hashlib
import hmac
import math
import os
import secrets
import struct
import time
from base64 import b32encode
from typing import List, Optional, Tuple


class TOTPManager:
    """
    TOTP manager for generating and verifying time-based one-time passwords.

    Compatible with Google Authenticator and other TOTP apps.
    """

    def __init__(
        self,
        digits: int = 6,
        interval: int = 30,
        algorithm: str = "sha1",
    ):
        """
        Args:
            digits: Number of digits in the OTP (default: 6)
            interval: Time step in seconds (default: 30)
            algorithm: Hash algorithm (default: sha1)
        """
        self.digits = digits
        self.interval = interval
        self.algorithm = algorithm

    def generate_secret(self, length: int = 20) -> str:
        """
        Generate a random secret key for TOTP.

        Returns a base32-encoded string (Google Authenticator compatible).
        """
        random_bytes = secrets.token_bytes(length)
        return b32encode(random_bytes).decode("utf-8").rstrip("=")

    def _hmac_hash(self, key: bytes, message: bytes) -> bytes:
        """Compute HMAC hash."""
        if self.algorithm == "sha1":
            return hmac.new(key, message, hashlib.sha1).digest()
        elif self.algorithm == "sha256":
            return hmac.new(key, message, hashlib.sha256).digest()
        elif self.algorithm == "sha512":
            return hmac.new(key, message, hashlib.sha512).digest()
        else:
            raise ValueError(f"Unsupported algorithm: {self.algorithm}")

    def _generate_otp(self, secret: str, counter: int) -> str:
        """Generate OTP for a given counter value."""
        # Decode base32 secret
        # Add padding if needed
        padding = 8 - (len(secret) % 8) if len(secret) % 8 else 0
        key = self._base32_decode(secret + "=" * padding)

        # Convert counter to 8-byte big-endian
        counter_bytes = struct.pack(">Q", counter)

        # Compute HMAC
        hash_result = self._hmac_hash(key, counter_bytes)

        # Dynamic truncation
        offset = hash_result[-1] & 0x0F
        code = (
            ((hash_result[offset] & 0x7F) << 24)
            | ((hash_result[offset + 1] & 0xFF) << 16)
            | ((hash_result[offset + 2] & 0xFF) << 8)
            | (hash_result[offset + 3] & 0xFF)
        )

        # Trim to desired digits
        otp = code % (10 ** self.digits)
        return str(otp).zfill(self.digits)

    def _base32_decode(self, s: str) -> bytes:
        """Decode base32 string (RFC 4648) using stdlib with fallback."""
        import base64
        s = s.strip().replace(" ", "").upper()
        # pad to multiple of 8
        pad = (-len(s)) % 8
        s_padded = s + ("=" * pad)
        try:
            return base64.b32decode(s_padded, casefold=True)
        except Exception:
            # fallback legacy path
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
            s_nopad = s.rstrip("=")
            result = 0
            for char in s_nopad:
                result = result * 32 + alphabet.index(char)
            byte_length = math.ceil(len(s_nopad) * 5 / 8)
            return result.to_bytes(byte_length, byteorder="big")

    def generate(self, secret: str, time_offset: int = 0) -> str:
        """
        Generate current TOTP code.

        Args:
            secret: Base32-encoded secret
            time_offset: Time step offset (for window tolerance)
        """
        counter = int(time.time()) // self.interval + time_offset
        return self._generate_otp(secret, counter)

    def verify(
        self,
        secret: str,
        code: str,
        window: int = 1,
    ) -> bool:
        """
        Verify a TOTP code.

        Args:
            secret: Base32-encoded secret
            code: The OTP code to verify
            window: Number of time steps to check before/after current

        Returns:
            True if the code is valid
        """
        if not code or len(code) != self.digits:
            return False

        # Check current window and adjacent windows
        for offset in range(-window, window + 1):
            expected = self.generate(secret, time_offset=offset)
            if hmac.compare_digest(code, expected):
                return True

        return False

    def get_uri(
        self,
        secret: str,
        account_name: str,
        issuer: str = "SalesFollowUpAgent",
    ) -> str:
        """
        Generate an otpauth:// URI for QR code generation.

        This URI can be encoded as a QR code and scanned by
        Google Authenticator, Authy, etc.

        Args:
            secret: Base32-encoded secret
            account_name: Account identifier (e.g., email)
            issuer: Application name

        Returns:
            otpauth:// URI string
        """
        encoded_issuer = issuer.replace(" ", "%20")
        encoded_name = account_name.replace(" ", "%20").replace("@", "%40")
        return (
            f"otpauth://totp/{encoded_issuer}:{encoded_name}"
            f"?secret={secret}"
            f"&issuer={encoded_issuer}"
            f"&algorithm={self.algorithm.upper()}"
            f"&digits={self.digits}"
            f"&period={self.interval}"
        )

    def get_qr_code_url(self, uri: str) -> str:
        """
        Get a URL for QR code image generation.

        In air-gapped mode this raises — use get_qr_code_data_uri instead.
        Otherwise falls back to local generation if qrcode installed.
        """
        # Prefer local generation; only use Google as last resort and never in air-gapped
        try:
            from ..config import get_settings
            if getattr(get_settings(), "air_gapped", False):
                raise RuntimeError("QR via external URL disabled in AIR_GAPPED mode — use get_qr_code_data_uri")
        except Exception:
            pass
        # Try local first
        try:
            return self.get_qr_code_data_uri(uri)
        except Exception:
            pass
        import urllib.parse
        encoded_uri = urllib.parse.quote(uri, safe="")
        return f"https://chart.googleapis.com/chart?cht=qr&chs=200x200&chl={encoded_uri}"

    def get_qr_code_data_uri(self, uri: str) -> str:
        """
        Generate a local QR code as a data: URI (no external call).

        Requires `qrcode[pil]` or `segno`. Falls back to uri itself if neither installed.
        """
        # Try qrcode
        try:
            import qrcode
            import io, base64
            img = qrcode.make(uri)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            return f"data:image/png;base64,{b64}"
        except Exception:
            pass
        # Try segno
        try:
            import segno
            import io, base64
            buf = io.BytesIO()
            segno.make(uri).save(buf, kind="png", scale=4)
            b64 = base64.bencode(buf.getvalue()).decode() if hasattr(base64, 'bencode') else base64.b64encode(buf.getvalue()).decode()
            return f"data:image/png;base64,{b64}"
        except Exception:
            pass
        # Fallback: return uri itself (client can render locally)
        return uri


class BackupCodes:
    """
    Generate and verify backup codes for 2FA account recovery.

    Backup codes are one-time-use codes that can be used when
    the user loses access to their authenticator app.
    """

    def __init__(self, length: int = 8, count: int = 10):
        """
        Args:
            length: Length of each backup code
            count: Number of backup codes to generate
        """
        self.length = length
        self.count = count

    def generate(self) -> List[str]:
        """
        Generate backup codes.

        Returns list of formatted codes (e.g., ["ABCD-1234", "EFGH-5678"]).
        """
        codes = []
        for _ in range(self.count):
            # Generate random bytes and encode as uppercase hex
            raw = secrets.token_bytes(self.length // 2)
            code_hex = raw.hex().upper()

            # Format with dash in middle
            mid = len(code_hex) // 2
            formatted = f"{code_hex[:mid]}-{code_hex[mid:]}"
            codes.append(formatted)

        return codes

    def hash_codes(self, codes: List[str]) -> List[str]:
        """
        Hash backup codes for storage.

        Returns list of salted SHA-256 hashes.
        """
        hashed = []
        for code in codes:
            # Normalize: remove dash, lowercase
            normalized = code.replace("-", "").lower()
            salt = secrets.token_hex(16)
            h = hashlib.sha256(f"sfa:backup:{salt}:{normalized}".encode()).hexdigest()
            hashed.append(f"{salt}:{h}")
        return hashed

    def verify_code(self, code: str, stored_hashes: List[str]) -> Tuple[bool, Optional[int]]:
        """
        Verify a backup code against stored hashes.

        Args:
            code: The backup code to verify
            stored_hashes: List of salted hashes

        Returns:
            (is_valid, index_of_used_code) or (False, None)
        """
        normalized = code.replace("-", "").lower()

        for i, stored in enumerate(stored_hashes):
            try:
                salt, expected_hash = stored.split(":", 1)
                actual_hash = hashlib.sha256(
                    f"sfa:backup:{salt}:{normalized}".encode()
                ).hexdigest()
                if hmac.compare_digest(actual_hash, expected_hash):
                    return True, i
            except (ValueError, AttributeError):
                continue

        return False, None

    def remove_used(self, stored_hashes: List[str], index: int) -> List[str]:
        """Remove a used backup code from the list."""
        return [h for i, h in enumerate(stored_hashes) if i != index]


# ── Global instance ────────────────────────────────────────────

_totp_manager: Optional[TOTPManager] = None
_backup_codes: Optional[BackupCodes] = None


def get_totp_manager() -> TOTPManager:
    global _totp_manager
    if _totp_manager is None:
        _totp_manager = TOTPManager()
    return _totp_manager


def get_backup_codes() -> BackupCodes:
    global _backup_codes
    if _backup_codes is None:
        _backup_codes = BackupCodes()
    return _backup_codes
