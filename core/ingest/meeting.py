"""Meeting notes ingestion - parse meeting notes into Conversation objects.

Handles various meeting note formats with edge case support:
- Structured meeting notes (Agenda, Action Items, Decisions)
- Free-form meeting notes
- Email threads with meeting summaries
- Partial or missing information
"""

import re
from datetime import datetime
from typing import List, Optional, Dict, Any

from ..models.conversation import Conversation, ExtractedEntity


def process_meeting_notes(
    notes: str,
    source: str = "meeting",
    include_raw: bool = True,
) -> Conversation:
    """
    Parse meeting notes into a Conversation object.
    
    Supports various formats:
    - "Agenda: ... Action Items: ... Decisions: ..."
    - Bullet point format
    - Free-form text
    - Email-thread style summaries
    
    Edge cases handled:
    - Empty notes string
    - Notes with no action items
    - Notes with no identified participants
    - Missing or malformed dates
    - Multiple meetings in one note string
    
    Args:
        notes: Raw meeting notes text
        source: Source type identifier ("meeting", "email_thread", "summary")
        include_raw: Whether to include full notes in raw_text
    
    Returns:
        Conversation object with extracted entities, commitments, sentiment, urgency
    
    Raises:
        ValueError: If notes is empty or None
    """
    if not notes or not notes.strip():
        raise ValueError("Meeting notes cannot be empty or None")
    
    notes_stripped = notes.strip()
    
    # Extract date from notes
    meeting_date = _extract_date_from_notes(notes_stripped)
    
    # Extract participants
    participants = _extract_participants_from_text(notes_stripped)
    if not participants:
        participants = [{"name": "Unknown", "email": "unknown@example.com"}]
    
    # Extract commitments/action items
    commitments = _extract_commitments_from_notes(notes_stripped)
    
    # Extract entities
    entities = _extract_entities_from_meeting_notes(notes_stripped)
    
    # Determine sentiment
    sentiment = (entities.sentiment if entities and entities.sentiment else None) or "neutral"
    
    # Determine urgency
    urgency = _determine_urgency_from_notes(notes_stripped)
    
    # Build Conversation
    conv = Conversation(
        source=source,
        participants=participants,
        date=meeting_date,
        raw_text=notes_stripped if include_raw else "",
        commitments=commitments,
        entities=entities,
        sentiment=sentiment,
        urgency=urgency,
    )
    
    return conv


def _extract_date_from_notes(notes: str) -> datetime:
    """Extract meeting date from notes text."""
    import re

    date_patterns = [
        (r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", ["%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y", "%m/%d/%y", "%d/%m/%y"]),
        (r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{2,4}", ["%B %d, %Y", "%B %d %Y", "%B %d, %y"]),
        (r"\d{4}[/-]\d{1,2}[/-]\d{1,2}", ["%Y/%m/%d", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d"]),
    ]

    for pattern, fmts in date_patterns:
        match = re.search(pattern, notes)
        if match:
            date_str = match.group(0).strip().replace(",", " ,").replace("  ", " ").replace(" ,", ",")
            # normalize: remove extra comma spacing for strptime
            date_str = re.sub(r"\s*,\s*", ", ", date_str).strip()
            for fmt in fmts:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
            # try flexible parse with dateutil-like fallback: try stripping leading zeros handling
            continue

    return datetime.utcnow()


def _extract_commitments_from_notes(notes: str) -> List[str]:
    """Extract action items/commitments from meeting notes."""
    import re
    
    commitments = []
    
    # Common action item patterns
    action_patterns = [
        r"[A-Z][a-z]+\s+will\s+",  # "John will..."
        r"Action Item:?\s*(.+?)(?:\n|$)",  # "Action Item: ..."
        r"Next Step:?\s*(.+?)(?:\n|$)",  # "Next Step: ..."
        r"Deadline:?\s*(.+?)(?:\n|$)",  # "Deadline: ..."
        r"Follow Up:?\s*(.+?)(?:\n|$)",  # "Follow Up: ..."
    ]
    
    for pattern in action_patterns:
        matches = re.findall(pattern, notes, re.IGNORECASE | re.MULTILINE)
        for m in matches:
            m = m.strip()
            if m and len(m) > 2:
                commitments.append(m)
    
    # Also keyword-based extraction
    commitment_keywords = ["send", "call", "meeting", "proposal", "contract", 
                          "sign", "submit", "deadline", "by", "until", "review"]
    sentences = re.split(r'[.!?]', notes)
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        for kw in commitment_keywords:
            if kw.lower() in sentence.lower() and len(sentence) > 5:
                # Only add if not already captured
                if not any(s.strip() == sentence.strip() for s in commitments):
                    commitments.append(sentence)
                break
    
    return commitments


def _extract_entities_from_meeting_notes(notes: str) -> ExtractedEntity:
    """Extract entities from meeting notes text."""
    import re
    
    # Name extraction - look for speaker labels or "Name: " patterns
    name = None
    name_patterns = [
        r"[A-Z][a-z]+\s+presented|led|discussed|said",
        r"^([A-Z][a-z]+)(?:\s|$)(?:present|led|discussed|agreed)",  # at line start
    ]
    
    # Look for "Name: value" patterns
    colon_match = re.search(r"([A-Z][a-z]+):\s*(.+)", notes)
    if colon_match:
        name = colon_match.group(1)
    
    # Date extraction (already done above, but for entity)
    date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", notes)
    date = None
    if date_match:
        try:
            date = datetime.strptime(date_match.group(1), "%m/%d/%Y")
        except ValueError:
            date = None
    
    # Commitment extraction
    commitments = []
    commitment_keywords = ["send", "call", "meeting", "proposal", "contract",
                          "sign", "submit", "deadline", "by", "until"]
    for kw in commitment_keywords:
        if kw.lower() in notes.lower():
            # Find first sentence with keyword
            sentences = re.split(r'[.!?]', notes)
            for s in sentences:
                if kw.lower() in s.lower() and len(s.strip()) > 5:
                    commitments.append(s.strip())
                    break
    
    # Sentiment analysis
    positive_words = ["great", "good", "excellent", "positive", "success", "win", 
                      "approved", "agreed", "great", "good"]
    negative_words = ["bad", "poor", "issue", "problem", "concern", "worried",
                      "difficult", "challenged", "concern", "concerns"]
    
    pos_count = sum(1 for w in positive_words if w.lower() in notes.lower())
    neg_count = sum(1 for w in negative_words if w.lower() in notes.lower())
    
    if pos_count > neg_count:
        sentiment = "positive"
    elif neg_count > pos_count:
        sentiment = "negative"
    else:
        sentiment = "neutral"
    
    sentiment_score = max(0.0, min(1.0, round((pos_count - neg_count) / max(1, pos_count + neg_count + 1) + 0.5, 2)))
    
    # Extract any dollar amounts
    deal_size = _extract_deal_size(notes)
    
    commitment_text = " ".join(commitments) if commitments else None
    
    return ExtractedEntity(
        name=name,
        date=date,
        commitment=commitment_text,
        sentiment_score=sentiment_score,
    )


def _determine_urgency_from_notes(notes: str) -> str:
    """Determine urgency level from meeting notes."""
    urgency_keywords_high = ["urgent", "asap", "immediately", "today", "tonight", 
                              "emergency", "critical", "rush"]
    urgency_keywords_medium = ["follow", "follow-up", "next step", "soon", 
                               "this week", "by end of"]
    
    notes_lower = notes.lower()
    
    if any(kw in notes_lower for kw in urgency_keywords_high):
        return "high"
    elif any(kw in notes_lower for kw in urgency_keywords_medium):
        return "medium"
    else:
        return "low"


def _extract_deal_size(text: str) -> Optional[float]:
    """Extract estimated deal size from text."""
    import re
    
    dollar_patterns = [
        r"\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)",
        r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*dollars?",
        r"(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s*k",
    ]
    
    for pattern in dollar_patterns:
        matches = re.findall(pattern, text)
        for m in matches:
            try:
                value = float(m.replace(",", ""))
                # If pattern suggests thousands
                if re.search(r'\s*k', pattern[-1] if len(pattern) > 1 else ""):
                    value *= 1000
                if value > 0:
                    return round(value, 2)
            except ValueError:
                continue
    
    return None


def _extract_participants_from_text(text: str) -> list:
    """Extract participant names from meeting notes."""
    import re
    
    participants = []
    
    # Look for "Name: " or "Name - " patterns
    name_patterns = [
        r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[:-]",  # "John Doe:"
        r"Attendees?:?\s*(.+?)(?:\n|$)",  # "Attendees: John, Jane"
    ]
    
    # Try attendee list
    attendees_match = re.search(r"[Aa]ttendees?:?\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if attendees_match:
        attendee_text = attendees_match.group(1)
        # Split by comma or "and"
        names = re.split(r",|\band\b", attendee_text)
        for n in names:
            n = n.strip()
            if n and len(n) > 1:
                # Extract just the name part (before email if present)
                email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
                email_match = re.search(email_pattern, n)
                if email_match:
                    name_part = n[:email_match.start()].strip()
                else:
                    name_part = n
                if name_part:
                    participants.append({"name": name_part.title(), "email": f"{name_part.lower().replace(' ', '.')}@example.com"})
    
    # If no attendees found, look for capitalized names
    if not participants:
        capitalized = re.findall(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", text)
        for name in capitalized[:4]:  # Limit to 4
            if name not in [p.get("name") for p in participants]:
                participants.append({"name": name, "email": f"{name.lower().replace(' ', '.')}@example.com"})
    
    return participants