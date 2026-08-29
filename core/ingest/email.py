"""Email ingestion — parse emails from IMAP into Conversation objects and send via SMTP."""

import email
import email.mime.text
import imaplib
import smtplib
import hashlib
import re
from datetime import datetime
from typing import List, Optional, Any, Dict

from ..models.conversation import Conversation, ExtractedEntity
from ..dedup import get_dedup_store


def _extract_entities_from_text(text: str) -> ExtractedEntity:
    """Extract entities from email text using LLM with heuristic fallback."""
    if not text or not text.strip():
        return ExtractedEntity()

    # Try LLM first for high-fidelity extraction
    try:
        from ..intelligence.llm_manager import llm_manager
        import json

        # Skip LLM if no cloud keys configured and Ollama not reachable
        try:
            if not llm_manager.is_local_available():
                import os
                has_cloud_key = any([
                    os.environ.get("OPENAI_API_KEY"),
                    os.environ.get("ANTHROPIC_API_KEY"),
                    os.environ.get("GROQ_API_KEY"),
                    os.environ.get("NVIDIA_API_KEY"),
                    os.environ.get("GOOGLE_API_KEY"),
                ])
                if not has_cloud_key:
                    return _heuristic_extract(text)
        except Exception:
            return _heuristic_extract(text)

        system_prompt = (
            "You are a sales intelligence extractor. Analyze the email text and return ONLY "
            "a JSON object with these keys: "
            '{"name": "string or null", "company": "string or null", '
            '"sentiment": "positive/negative/neutral", "sentiment_score": float_0_to_1, '
            '"commitments": ["list of strings"], "urgency": "high/medium/low", '
            '"deal_size": float_or_null, "pain_points": ["list of strings"]}'
        )

        response = llm_manager.generate(
            text[:3000],  # Truncate for context window
            system_message=system_prompt,
            temperature=0,
            max_tokens=500,
        )

        data = json.loads(response.content)
        return ExtractedEntity(
            name=data.get("name"),
            company=data.get("company"),
            sentiment=data.get("sentiment", "neutral"),
            sentiment_score=float(data.get("sentiment_score", 0.5)),
            pain_point=data.get("pain_points", [None])[0] if data.get("pain_points") else None,
        )
    except Exception:
        # Fallback to heuristic extraction when LLM is unavailable
        pass

    return _heuristic_extract(text)


def _heuristic_extract(text: str) -> ExtractedEntity:
    """Fallback heuristic entity extraction when LLM is unavailable."""
    if not text:
        return ExtractedEntity()

    # Sentiment analysis via keyword matching
    positive_words = {
        "great", "excellent", "perfect", "wonderful", "fantastic", "amazing",
        "love", "excited", "happy", "interested", "impressed", "thank",
        "agree", "approved", "confirmed", "yes", "absolutely", "definitely",
    }
    negative_words = {
        "bad", "poor", "terrible", "disappointed", "concern", "worried",
        "problem", "issue", "difficult", "unfortunately", "no", "cannot",
        "reject", "decline", "unhappy", "frustrated", "unfortunately", "but",
    }

    text_lower = text.lower()
    words = set(re.findall(r'\b\w+\b', text_lower))
    pos_count = len(words & positive_words)
    neg_count = len(words & negative_words)

    if pos_count > neg_count:
        sentiment = "positive"
        sentiment_score = min(0.5 + (pos_count - neg_count) * 0.1, 1.0)
    elif neg_count > pos_count:
        sentiment = "negative"
        sentiment_score = max(0.5 - (neg_count - pos_count) * 0.1, 0.0)
    else:
        sentiment = "neutral"
        sentiment_score = 0.5

    # Urgency detection
    urgency_high = ["urgent", "asap", "immediately", "today", "tonight", "deadline", "critical", "rush"]
    urgency_medium = ["soon", "this week", "follow up", "follow-up", "next step", "by friday", "by end"]
    if any(kw in text_lower for kw in urgency_high):
        urgency = "high"
    elif any(kw in text_lower for kw in urgency_medium):
        urgency = "medium"
    else:
        urgency = "low"

    # Deal size extraction
    deal_size = None
    dollar_patterns = [
        r'\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*(?:dollars?|usd)',
        r'(\d+(?:\.\d+)?)\s*k\b',
    ]
    for pat in dollar_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = float(m.group(1).replace(",", ""))
            if 'k' in pat:
                val *= 1000
            if val > 0:
                deal_size = val
                break

    return ExtractedEntity(
        sentiment=sentiment,
        sentiment_score=round(sentiment_score, 2),
    )


def _extract_commitments_from_text(text: str) -> List[str]:
    """Extract commitments/action items from email text."""
    if not text:
        return []

    commitments = []
    commitment_patterns = [
        r'(?:will|shall|going to|plan to|commit to|agree to|promise to)\s+(.{10,100}?)(?:\.|,|\n|$)',
        r'(?:deadline|due date|by|before|until)\s*[:\-]?\s*(.{5,60}?)(?:\.|,|\n|$)',
        r'(?:next step|action item|to[- ]do)\s*[:\-]?\s*(.{10,100}?)(?:\.|,|\n|$)',
        r'(?:send|deliver|provide|share|schedule|book|set up|arrange)\s+(.{5,80}?)(?:\.|,|\n|$)',
    ]

    for pat in commitment_patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        for m in matches:
            m = m.strip()
            if m and len(m) > 5 and m not in commitments:
                commitments.append(m)

    return commitments[:10]  # Cap at 10


def _parse_email_body(raw_email: email.message.Message) -> str:
    """Extract plain text body from raw email message."""
    if raw_email.is_multipart():
        for part in raw_email.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="replace")
        # Fallback to HTML
        for part in raw_email.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    import bleach
                    html = payload.decode(errors="replace")
                    return bleach.clean(html, tags=[], strip=True)
        return ""
    else:
        payload = raw_email.get_payload(decode=True)
        if payload:
            return payload.decode(errors="replace")
        return raw_email.get_payload() or ""


def _parse_email_address(header_value: str) -> List[Dict[str, str]]:
    """Parse email addresses from a From/To header."""
    email_pattern = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
    matches = re.findall(email_pattern, header_value)
    participants = []
    for m in matches:
        name_part = m.split("@")[0].replace(".", " ").replace("_", " ").title()
        participants.append({"email": m, "name": name_part})
    return participants


def fetch_emails(
    imap_host: str,
    imap_port: int,
    username: str,
    password: str,
    mailbox: str = "INBOX",
    since_days: Optional[int] = None,
    limit: int = 100,
) -> List[Conversation]:
    """
    Fetch emails from IMAP server and convert to Conversation objects.

    Handles: auth failures, empty mailboxes, non-UTF8 encoding,
    multipart messages, missing participants.
    """
    conversations: List[Conversation] = []

    try:
        if imap_port == 993:
            mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        else:
            mail = imaplib.IMAP4(imap_host, imap_port)
    except Exception as e:
        raise ConnectionError(f"Cannot connect to IMAP server {imap_host}:{imap_port}: {e}")

    try:
        mail.login(username, password)
    except imaplib.IMAP4.error as e:
        raise PermissionError(f"IMAP authentication failed for {username}: {e}")

    try:
        mail.select(mailbox)

        search_criteria = "ALL"
        if since_days:
            since_date = (datetime.utcnow().replace(hour=0, minute=0, second=0)).strftime("%d-%b-%Y")
            search_criteria = f'SINCE {since_date}'

        status, messages = mail.search(None, search_criteria)
        if status != "OK" or not messages[0]:
            return conversations

        email_ids = messages[0].split()[-limit:]

        for eid in email_ids:
            try:
                status, msg_data = mail.fetch(eid, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                if not raw_email:
                    continue

                msg = email.message_from_bytes(raw_email)
                body = _parse_email_body(msg)

                if not body or not body.strip():
                    continue

                # ── Deduplication check ──────────────────
                message_id_header = msg.get("Message-ID", "") or msg.get("Message-Id", "")
                sender_addr = msg.get("From", "")
                subject = msg.get("Subject", "")

                dedup = get_dedup_store()
                if dedup.is_duplicate(
                    message_id=message_id_header,
                    sender=sender_addr,
                    subject=subject,
                    body=body,
                ):
                    logger.debug(f"Skipping duplicate email {eid} (Message-ID: {message_id_header[:50]})")
                    continue

                # Parse date
                date_str = msg["Date"]
                conv_date = datetime.utcnow()
                if date_str:
                    try:
                        from email.utils import parsedate_to_datetime
                        conv_date = parsedate_to_datetime(date_str).replace(tzinfo=None)
                    except Exception:
                        pass

                # Parse participants
                participants = []
                for header in ["From", "To", "Cc"]:
                    val = msg.get(header, "")
                    if val:
                        participants.extend(_parse_email_address(val))
                # Deduplicate
                seen = set()
                unique = []
                for p in participants:
                    if p["email"].lower() not in seen:
                        seen.add(p["email"].lower())
                        unique.append(p)
                participants = unique or [{"email": "unknown", "name": "Unknown"}]

                # Extract entities
                entities = _extract_entities_from_text(body)
                commitments = _extract_commitments_from_text(body)

                # Build conversation
                conv = Conversation(
                    source="email",
                    participants=participants,
                    date=conv_date,
                    raw_text=body.strip(),
                    commitments=commitments,
                    entities=entities,
                    sentiment=entities.sentiment or "neutral",
                    deal_size=None,
                    urgency="low",  # Will be refined by scoring
                )
                conversations.append(conv)

                # ── Mark as seen ─────────────────────────
                dedup.mark_seen(
                    message_id=message_id_header,
                    sender=sender_addr,
                    subject=subject,
                    body=body,
                )

            except Exception as e:
                logger.warning(f"Failed to process email {eid}: {e}")
                continue

    finally:
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass

    conversations.sort(key=lambda c: c.date, reverse=True)
    return conversations


import logging
logger = logging.getLogger("EmailIngest")


def parse_email_to_conversation(email_data: dict, source: str = "email") -> Conversation:
    """
    Parse email dict data into a Conversation object.

    email_data keys: from, to, date, subject, body, cc, etc.
    """
    participants = []
    email_pattern = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'

    for key in ["from", "to", "sender", "cc"]:
        val = email_data.get(key, "")
        if val:
            matches = re.findall(email_pattern, str(val))
            for m in matches:
                name_part = m.split("@")[0].replace(".", " ").replace("_", " ").title()
                if not any(p["email"].lower() == m.lower() for p in participants):
                    participants.append({"email": m, "name": name_part})

    if not participants:
        participants = [{"email": "unknown", "name": "Unknown"}]

    # Parse date
    conv_date = datetime.utcnow()
    date_val = email_data.get("date")
    if date_val:
        if isinstance(date_val, datetime):
            conv_date = date_val
        else:
            try:
                conv_date = datetime.fromisoformat(str(date_val))
            except (ValueError, TypeError):
                pass

    body = email_data.get("body", "") or ""
    entities = _extract_entities_from_text(body) if body else ExtractedEntity()
    commitments = _extract_commitments_from_text(body)

    # Subject line may contain urgency signals
    subject = email_data.get("subject", "") or ""
    urgency = "low"
    combined = (subject + " " + body).lower()
    if any(kw in combined for kw in ["urgent", "asap", "immediately", "deadline", "critical"]):
        urgency = "high"
    elif any(kw in combined for kw in ["soon", "follow up", "follow-up", "this week"]):
        urgency = "medium"

    # ── Deduplication check ──────────────────────────────────
    message_id = email_data.get("message_id") or email_data.get("Message-ID") or email_data.get("id") or ""
    sender = email_data.get("from") or email_data.get("sender") or ""
    subject = email_data.get("subject") or ""

    dedup = get_dedup_store()
    is_dup = dedup.is_duplicate(
        message_id=str(message_id) if message_id else None,
        sender=str(sender),
        subject=str(subject),
        body=body,
    )

    conv = Conversation(
        source=source,
        participants=participants,
        date=conv_date,
        raw_text=body,
        commitments=commitments,
        entities=entities,
        sentiment=entities.sentiment or "neutral",
        urgency=urgency,
    )

    # Mark as seen (even if duplicate — updates timestamp for expiry)
    dedup.mark_seen(
        message_id=str(message_id) if message_id else None,
        sender=str(sender),
        subject=str(subject),
        body=body,
    )

    # Attach dedup status to conversation metadata
    conv.entities = entities  # Ensure entities are set
    return conv


def send_email(
    to_address: str,
    subject: str,
    body: str,
    from_address: Optional[str] = None,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_username: Optional[str] = None,
    smtp_password: Optional[str] = None,
    include_tracking_pixel: bool = False,
    tracking_pixel_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send an email via SMTP. Returns status dict.

    Args:
        to_address: Recipient email
        subject: Email subject line
        body: Plain text body
        from_address: Sender address (defaults to config)
        smtp_host/port/username/password: Override SMTP settings
        include_tracking_pixel: Append invisible tracking pixel
        tracking_pixel_url: URL for the tracking pixel

    Returns:
        Dict with status, message_id, timestamp
    """
    from ..config import get_settings
    settings = get_settings()

    smtp_h = smtp_host or settings.smtp_host
    smtp_p = smtp_port or settings.smtp_port
    smtp_u = smtp_username or settings.smtp_username
    smtp_pw = smtp_password or settings.smtp_password
    sender = from_address or settings.email_sending_domain

    if not smtp_u or not smtp_pw:
        return {
            "status": "error",
            "message": "SMTP credentials not configured. Set SMTP_USERNAME and SMTP_PASSWORD.",
            "timestamp": datetime.utcnow().isoformat(),
        }

    # Build message
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["X-Mailer"] = "SalesFollowUpAgent/1.0"

    # Add tracking pixel if requested
    if include_tracking_pixel and tracking_pixel_url:
        pixel_html = f'<img src="{tracking_pixel_url}" width="1" height="1" style="display:none;" alt="">'
        html_part = email.mime.text.MIMEText(
            f"<html><body>{body}<br>{pixel_html}</body></html>",
            "html",
            "utf-8",
        )
        msg = email.mime.text.MIMEText(body, "plain", "utf-8")
        msg.attach(html_part)

    try:
        with smtplib.SMTP(smtp_h, smtp_p, timeout=30) as server:
            server.ehlo()
            if smtp_p == 587:
                server.starttls()
                server.ehlo()
            server.login(smtp_u, smtp_pw)
            server.sendmail(sender, [to_address], msg.as_string())

        return {
            "status": "sent",
            "message_id": msg["Message-ID"] or hashlib.md5(f"{to_address}{subject}".encode()).hexdigest(),
            "from": sender,
            "to": to_address,
            "subject": subject,
            "timestamp": datetime.utcnow().isoformat(),
        }

    except smtplib.SMTPAuthenticationError as e:
        return {
            "status": "error",
            "message": f"SMTP authentication failed: {e}",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except smtplib.SMTPRecipientsRefused as e:
        return {
            "status": "bounced",
            "message": f"Recipient refused: {e}",
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Email send failed: {e}",
            "timestamp": datetime.utcnow().isoformat(),
        }


def add_suppression(email_address: str, suppressions_path: Optional[str] = None) -> bool:
    """Add an email to the suppression list file."""
    from ..config import get_settings
    settings = get_settings()
    path = suppressions_path or settings.suppressions_list_path or ".suppressions.txt"
    try:
        existing = set()
        try:
            with open(path, "r") as f:
                existing = {line.strip().lower() for line in f if line.strip()}
        except FileNotFoundError:
            pass

        if email_address.lower() not in existing:
            with open(path, "a") as f:
                f.write(email_address.lower() + "\n")
        return True
    except Exception:
        return False


def is_suppressed(email_address: str, suppressions_path: Optional[str] = None) -> bool:
    """Check if an email is on the suppression list."""
    from ..config import get_settings
    settings = get_settings()
    path = suppressions_path or settings.suppressions_list_path or ".suppressions.txt"
    try:
        with open(path, "r") as f:
            suppressed = {line.strip().lower() for line in f if line.strip()}
        return email_address.lower() in suppressed
    except FileNotFoundError:
        return False
