"""Prompt templates and LLM-powered follow-up message generation."""

from typing import Dict, Any, List, Optional
from datetime import datetime
import json
import logging

logger = logging.getLogger("Generation")

# Tone configuration
TONE_GUIDES: Dict[str, str] = {
    "agreeable": "warm, collaborative, relationship-focused, soft CTA",
    "direct": "straightforward, business-like, single clear CTA, minimal small talk",
    "soft": "gentle, curious, open-ended, zero pressure",
}

SEQUENCE_POSITIONS: Dict[int, str] = {
    1: "first follow-up since the meeting",
    2: "second touch — reference first follow-up non-pressuring",
    3: "final touch — polite breakup tone",
}

PROMPT_TEMPLATES: Dict[str, str] = {
    "agreeable": (
        "You are a high-converting B2B sales rep writing a follow-up email.\n\n"
        "TONE: warm, collaborative, relationship-focused, soft CTA.\n\n"
        "CONTEXT:\n"
        "- Prospect: {prospect_name} ({role} at {company})\n"
        "- Last interaction: {last_interaction_date} via {last_interaction_type}\n"
        "- Commitments from conversation: {commitments}\n"
        "- Pain points: {pain_points}\n"
        "- Sequence position: {sequence_position}\n"
        "- Urgency: {urgency_level}\n\n"
        "REQUIREMENTS:\n"
        "1. Start with Subject: on the first line\n"
        "2. Reference a specific commitment or pain point\n"
        "3. Warm, friendly, collaborative language\n"
        "4. Single soft CTA with a proposed time\n"
        "5. 80-150 words, plain text email body\n"
        "6. Use the prospect's first name naturally\n"
        "7. No generic filler — be specific to this conversation\n"
    ),
    "direct": (
        "You are a high-converting B2B sales rep writing a follow-up email.\n\n"
        "TONE: straightforward, business-like, single clear CTA, minimal small talk.\n\n"
        "CONTEXT:\n"
        "- Prospect: {prospect_name} ({role} at {company})\n"
        "- Last interaction: {last_interaction_date} via {last_interaction_type}\n"
        "- Commitments from conversation: {commitments}\n"
        "- Pain points: {pain_points}\n"
        "- Sequence position: {sequence_position}\n"
        "- Urgency: {urgency_level}\n\n"
        "REQUIREMENTS:\n"
        "1. Start with Subject: on the first line\n"
        "2. Reference the specific commitment\n"
        "3. Direct, no-nonsense, business-like\n"
        "4. Clear single CTA (no ambiguity)\n"
        "5. 80-150 words, plain text email body\n"
        "6. Respect their time — get to the point\n"
    ),
    "soft": (
        "You are a high-converting B2B sales rep writing a follow-up email.\n\n"
        "TONE: gentle, curious, open-ended, zero pressure.\n\n"
        "CONTEXT:\n"
        "- Prospect: {prospect_name} ({role} at {company})\n"
        "- Last interaction: {last_interaction_date} via {last_interaction_type}\n"
        "- Commitments from conversation: {commitments}\n"
        "- Pain points: {pain_points}\n"
        "- Sequence position: {sequence_position}\n"
        "- Urgency: {urgency_level}\n\n"
        "REQUIREMENTS:\n"
        "1. Start with Subject: on the first line\n"
        "2. Gentle, inquisitive language\n"
        "3. Open-ended question, no pressure\n"
        "4. Focus on their needs/challenges\n"
        "5. 80-150 words, plain text email body\n"
        "6. Easy to respond to — low effort for prospect\n"
    ),
}

URGENCY_GUIDELINES = {
    "high": "Emphasize urgency and time-sensitive nature. Be direct about deadlines.",
    "medium": "Balance urgency with relationship-building. Suggest a timeframe.",
    "low": "Focus on relationship and gentle follow-up. No pressure.",
}


def generate_llm_completion(
    prompt: str,
    system_message: str = "You are a concise, effective B2B sales rep.",
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 500,
) -> Optional[str]:
    """
    Call the LLM manager for a completion. Returns text or None on failure.
    Uses the autonomous fallback chain (cloud -> local).
    """
    try:
        from ..intelligence.llm_manager import llm_manager
        result = llm_manager.generate(
            prompt=prompt,
            system_message=system_message,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return result.content
    except Exception as e:
        logger.warning(f"LLM completion failed: {e}")
        return None


def generate_prompt(
    tone: str,
    conversation: Any,
    prospect_name: str,
    company: str = "",
    role: str = "",
    pain_points: Optional[List[str]] = None,
    followup_count: int = 1,
    last_followup_date: Optional[datetime] = None,
    urgency_level: str = "medium",
) -> str:
    """Build the LLM prompt for one tone variant."""
    if tone not in PROMPT_TEMPLATES:
        raise ValueError(f"Unsupported tone: {tone}. Choose from: {list(PROMPT_TEMPLATES.keys())}")

    commitments = ", ".join(getattr(conversation, "commitments", []) or []) or "none explicitly captured"
    pains = "; ".join(pain_points or []) or "unspecified"
    last_date = conversation.date.strftime("%B %d, %Y") if getattr(conversation, "date", None) else "recently"
    source = getattr(conversation, "source", "conversation")
    seq_num = min(followup_count + 1, 3)
    sequence = SEQUENCE_POSITIONS.get(seq_num, SEQUENCE_POSITIONS[3])

    return PROMPT_TEMPLATES[tone].format(
        prospect_name=prospect_name,
        role=role or "their role",
        company=company or "the company",
        last_interaction_date=last_date,
        last_interaction_type=source,
        commitments=commitments,
        pain_points=pains,
        sequence_position=sequence,
        urgency_level=URGENCY_GUIDELINES.get(urgency_level, "Follow standard guidelines"),
    )


def _generate_deterministic_draft(
    tone: str,
    prospect_name: str,
    conversation: Any,
    pain_points: Optional[List[str]] = None,
) -> str:
    """Generate a fallback draft without LLM when all providers are down."""
    commitments = ", ".join(getattr(conversation, "commitments", []) or []) or "our recent discussion"
    pain_str = "; ".join(pain_points or []) if pain_points else None

    openers = {
        "agreeable": f"Hi {prospect_name},\n\nI hope you're doing well.",
        "direct": f"Hi {prospect_name},\n\nQuick follow-up on our conversation.",
        "soft": f"Hi {prospect_name},\n\nHope this finds you well.",
    }

    bodies = {
        "agreeable": f"I wanted to circle back on {commitments}. I know things get busy, so I wanted to make sure we stay aligned.",
        "direct": f"Following up on our commitments: {commitments}. Please confirm next steps at your earliest convenience.",
        "soft": f"Just checking in on {commitments}. No rush — whenever you have a moment, I'd love to hear your thoughts.",
    }

    ctas = {
        "agreeable": f"Would next Tuesday or Wednesday work for a quick 15-minute sync?",
        "direct": f"Please confirm a 15-minute slot this week. I'll send a calendar invite.",
        "soft": f"If the timing works, I'm happy to reconnect. Otherwise, no pressure at all.",
    }

    pain_line = ""
    if pain_str:
        pain_line = f"\n\nI also haven't forgotten about {pain_str} — I have some ideas I'd love to share."

    subject = f"Follow-up: {commitments.split(',')[0].strip()}" if commitments else "Following up"
    return f"Subject: {subject}\n\n{openers[tone]}\n\n{bodies[tone]}{pain_line}\n\n{ctas[tone]}"


def generate_drafts(
    conversation: Any,
    prospect_name: str,
    company: str = "",
    role: str = "",
    pain_points: Optional[List[str]] = None,
    followup_count: int = 1,
    last_followup_date: Optional[datetime] = None,
    urgency_level: str = "medium",
    use_llm: bool = True,
) -> Dict[str, str]:
    """
    Generate 3 follow-up drafts (agreeable, direct, soft).
    Uses LLM when available, falls back to deterministic templates.
    """
    drafts: Dict[str, str] = {}

    for tone in ["agreeable", "direct", "soft"]:
        text = None
        if use_llm:
            prompt = generate_prompt(
                tone, conversation, prospect_name, company, role,
                pain_points, followup_count, last_followup_date, urgency_level,
            )
            text = generate_llm_completion(prompt)

        if not text:
            text = _generate_deterministic_draft(tone, prospect_name, conversation, pain_points)

        drafts["variant_" + tone] = text

    return drafts


def select_draft(
    drafts: Dict[str, str],
    rep_preference: Optional[str] = None,
    prospect_history: Optional[dict] = None,
    urgency_level: str = "medium",
) -> str:
    """
    Pick the best variant key based on rep preference, prospect history, and urgency.
    Returns the key name (e.g. "variant_direct").
    """
    if rep_preference and ("variant_" + rep_preference) in drafts:
        return "variant_" + rep_preference

    if prospect_history:
        last = prospect_history.get("last_tone")
        if last and ("variant_" + last) in drafts:
            return "variant_" + last

    urgency_map = {
        "high": "variant_direct",
        "medium": "variant_agreeable",
        "low": "variant_soft",
    }
    return urgency_map.get(urgency_level, "variant_agreeable")
