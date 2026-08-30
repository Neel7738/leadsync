"""Prospect scoring engine with weighted priority formula."""

from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from ..models.conversation import Conversation
from ..models.prospect import ScoredProspect
from ..constants import (
    SCORING_WEIGHTS,
    SLA_THRESHOLDS,
    PRIORITY_DECAY_HALF_LIFE_DAYS,
    DEFAULT_DEAL_VALUE_NORMALIZATION_BASE,
    DEFAULT_ENGAGEMENT_PROBABILITY,
    DEFAULT_URGENCY_MAP,
)


def score_prospect(
    conversation: Conversation,
    recency_days: Optional[float] = None,
    engagement_probability: Optional[float] = None,
    deal_value: Optional[float] = None,
) -> ScoredProspect:
    """
    Calculate priority score for a conversation/prospect using weighted formula.
    
    Weighted formula:
    priority_score = (urgency × 0.4) + (deal_value × 0.3) + 
                    (engagement_probability × 0.2) + (recency_decay × 0.1)
    
    Recency decay: score halves every 7 days since last contact.
    
    Edge cases handled:
    - None/null values for optional fields
    - Extremely large/small deal values
    - Negative recency days
    - Missing conversation urgency
    - Engagement probability outside 0-1 range
    
    Args:
        conversation: Conversation object with extracted data
        recency_days: Days since last contact (auto-calculated if None)
        engagement_probability: 0-1 probability of engagement (0.5 default)
        deal_value: Raw deal value in USD (normalized internally)
    
    Returns:
        ScoredProspect with priority_score, sla_deadline, and all derived fields
    
    Raises:
        ValueError: If conversation is None or invalid
    """
    if conversation is None:
        raise ValueError("Conversation cannot be None")
    
    # Use provided recency_days or calculate from conversation date (fractional days)
    if recency_days is None:
        from datetime import datetime
        now = datetime.utcnow()
        conv_date = conversation.date if conversation.date else now
        if isinstance(conv_date, str):
            try:
                conv_date = datetime.fromisoformat(conv_date)
            except ValueError:
                conv_date = now
        delta = now - conv_date
        recency_days = max(0.0, delta.total_seconds() / 86400.0)
    
    # Clamp recency to non-negative
    recency_days = max(0, recency_days)
    
    # Use provided engagement_probability or default
    if engagement_probability is None:
        engagement_probability = DEFAULT_ENGAGEMENT_PROBABILITY
    else:
        # Clamp to 0-1 range
        engagement_probability = max(0.0, min(1.0, engagement_probability))
    
    # Use provided deal_value or extract from conversation
    if deal_value is None:
        deal_value = conversation.deal_size or 0.0
    
    # Normalize deal value (assume max $100k for normalization, cap at 1.0)
    value_normalized = min(deal_value / DEFAULT_DEAL_VALUE_NORMALIZATION_BASE, 1.0) if deal_value else 0.0
    
    # Map urgency string to numeric (0-1) using DEFAULT_URGENCY_MAP
    urgency_str = conversation.urgency or "low"
    urgency_map = DEFAULT_URGENCY_MAP.copy()
    urgency_num = urgency_map.get(urgency_str.lower(), 0.0)
    
    # Recency decay: score halves every 7 days (PRIORITY_DECAY_HALF_LIFE_DAYS)
    # Formula: decay = 1 / (1 + days / half_life)
    recency_decay = 1.0 / (1.0 + recency_days / PRIORITY_DECAY_HALF_LIFE_DAYS)
    
    # Weighted calculation
    priority_score = (
        SCORING_WEIGHTS["urgency_weight"] * urgency_num +
        SCORING_WEIGHTS["deal_value_weight"] * value_normalized +
        SCORING_WEIGHTS["engagement_probability_weight"] * engagement_probability +
        SCORING_WEIGHTS["recency_decay_weight"] * recency_decay
    )
    
    # Round to 4 decimal places
    priority_score = round(priority_score, 4)
    
    # Calculate SLA deadline
    sla_deadline = _calculate_sla_deadline(urgency_str)
    
    # Determine if SLA is already breached (if score was calculated long ago)
    # SLA is based on urgency from time of scoring, so we just set the deadline
    sla_breached = False  # Set when queue checks SLA later
    
    # Create ScoredProspect
    scored = ScoredProspect(
        conversation_id=conversation.id,
        priority_score=priority_score,
        conversation=conversation,
        recency_days=recency_days,
        engagement_probability=engagement_probability,
        deal_value_normalized=value_normalized,
        urgency_score=urgency_num,
        sla_deadline=sla_deadline,
        sla_breached=sla_breached,
        times_requeued=0,
        status="queued",
    )
    
    return scored


def _calculate_sla_deadline(urgency_str: str) -> datetime:
    """Calculate SLA deadline based on urgency level."""
    from datetime import datetime, timedelta
    
    now = datetime.utcnow()
    urgency = urgency_str.lower()
    
    thresholds = {
        "high": SLA_THRESHOLDS["high"],     # 24 hours
        "medium": SLA_THRESHOLDS["medium"], # 48 hours
        "low": SLA_THRESHOLDS["low"],       # 72 hours
    }
    
    hours = thresholds.get(urgency, SLA_THRESHOLDS["low"])
    return now + timedelta(hours=hours)


def calculate_recency_decay(days: float, half_life: float = PRIORITY_DECAY_HALF_LIFE_DAYS) -> float:
    """
    Calculate recency decay factor.
    
    Decay formula: 1 / (1 + days / half_life)
    - After 0 days: score = 1.0
    - After 7 days (half-life): score ≈ 0.57
    - After 14 days: score ≈ 0.33
    - After 21 days: score ≈ 0.25
    
    Args:
        days: Number of days since last contact
        half_life: Days after which score halves (default: 7)
    
    Returns:
        Decay factor between 0 and 1
    """
    if days < 0:
        days = 0
    return round(1.0 / (1.0 + days / half_life), 4)


def validate_score_inputs(
    urgency: str,
    deal_value: float,
    engagement: float,
    recency_days: float,
) -> Dict[str, Any]:
    """Validate and clamp scoring inputs to valid ranges."""
    errors = []
    
    # Validate urgency
    if urgency and urgency.lower() not in ("high", "medium", "low"):
        errors.append(f"Invalid urgency: {urgency}. Must be high/medium/low")
    
    # Clamp engagement to 0-1
    engagement = max(0.0, min(1.0, engagement))
    
    # Clamp deal value to non-negative
    deal_value = max(0.0, deal_value)
    
    # Clamp recency to non-negative
    recency_days = max(0, recency_days)
    
    return {
        "urgency": urgency or "low",
        "deal_value": deal_value,
        "engagement": engagement,
        "recency_days": recency_days,
        "has_errors": len(errors) > 0,
        "error_messages": errors,
    }