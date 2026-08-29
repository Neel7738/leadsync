"""Ingestion pipeline: email, call STT, meeting notes."""
from .email import fetch_emails, parse_email_to_conversation, send_email
from .stt import transcribe_audio, process_call_audio
from .meeting import process_meeting_notes

__all__ = [
    "fetch_emails",
    "parse_email_to_conversation",
    "send_email",
    "transcribe_audio",
    "process_call_audio",
    "process_meeting_notes",
]
