"""Next-best-action determination engine."""

from typing import Dict, Any, Optional
from datetime import datetime
from ..models.conversation import Conversation
from ..models.prospect import ScoredProspect
from ..constants import SLA_THRESHOLDS, DEFAULT_URGENCY_MAP


def determine_next_best_action(
    conversation: Conversation,
    pipeline_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Determine the next best action for a follow-up prospect.
    
    First scores the conversation, then determines the action based on
    priority score, SLA status, and context.
    
    Decision logic based on priority score, SLA status, and context:
    
    Score ranges and actions:
    - Score >= 0.8 AND no SLA breach: "close" (warm, ready to commit)
    - 0.5 <= Score < 0.8 AND no SLA breach: "re-engage" (warm interest)
    - 0.2 <= Score < 0.5 AND no SLA breach: "nurture" (early stage)
    - Score < 0.2 OR SLA breached: "escalate" (needs attention)
    
    Additional considerations:
    - High deal value + medium urgency → promote to re-engage
    - Rep workload overload → bump lower priorities up
    - SLA breach → immediate escalation regardless of score
    
    Args:
        conversation: Conversation object with raw_text, sentiment, urgency, etc.
        pipeline_context: Optional dict with rep context:
            - "rep_workload": int (0-50), number of active prospects
            - "rep_closing_deals": int, deals currently in close phase
            - "average_deal_cycle": float, days typical sales cycle
    
    Returns:
        Dict with keys:
        - "action_type": "close" | "re-engage" | "nurture" | "escalate"
        - "timing_recommendation": human-readable timing suggestion
        - "rationale": string explanation of the decision
        - "priority_score": the prospect's priority score
        - "sla_deadline": ISO-format SLA deadline
        - "escalation_level": "none" | "manager" | "executive"
    
    Raises:
        ValueError: If conversation is invalid
    """
    # Score the conversation first
    from .scorer import score_prospect
    
    scored_prospect = score_prospect(conversation=conversation)
    
    # Now use the original logic with scored_prospect
    if scored_prospect is None:
        raise ValueError("ScoredProspect cannot be None")
    
    # Initialize result variables
    action_type: str
    timing: str
    rationale: str
    escalation_level: str = "none"
    
    # Check SLA breach first - always escalate if breached
    if scored_prospect.sla_breached:
        action_type = "escalate"
        timing = "immediate (within 2 hours)"
        rationale = (
            f"SLA breached for prospect {scored_prospect.conversation_id} "
            f"— immediate executive escalation required. "
            f"Priority score: {scored_prospect.priority_score:.2f}"
        )
        return {
            "action_type": action_type,
            "timing_recommendation": timing,
            "rationale": rationale,
            "priority_score": scored_prospect.priority_score,
            "sla_deadline": scored_prospect.sla_deadline.isoformat() if scored_prospect.sla_deadline else None,
            "escalation_level": "executive",
        }
    
    # Get priority score and urgency
    priority_score = scored_prospect.priority_score
    urgency_str = (
        scored_prospect.conversation.urgency or "low"
    ).lower()
    
    # Get deal value normalization
    deal_value_normalized = scored_prospect.deal_value_normalized
    
    # Get pipeline context if available
    rep_workload = 0
    if pipeline_context and "rep_workload" in pipeline_context:
        rep_workload = pipeline_context["rep_workload"]
    
    # Decision tree based on priority score
    if priority_score >= 0.8:
        # High score - warm prospect ready to close
        action_type = "close"
        timing = "within 24 hours — prospect is warm and ready to commit"
        rationale = (
            f"High priority score ({priority_score:.2f}) indicates warm prospect "
            f"ready to commit. Urgency: {urgency_str}. "
            f"Focus on closing the deal with a clear CTA."
        )
        
        # Adjust for very high deal value - may need softer approach
        if deal_value_normalized and deal_value_normalized > 0.8:
            rationale += " (High-value prospect — consider softer close approach)"
        
    elif priority_score >= 0.5:
        # Moderate-high score - warm interest detected
        action_type = "re-engage"
        timing = "within 24 hours — warm interest detected"
        rationale = (
            f"Moderate-high priority ({priority_score:.2f}) — re-engage "
            f"with personalized follow-up. Urgency: {urgency_str}. "
            f"Reference previous commitments and propose next steps."
        )
        
        # If high urgency, prioritize
        if urgency_str == "high":
            timing = "within 4 hours — high urgency warm interest"
            rationale = f"High urgency ({urgency_str}) with moderate-high priority ({priority_score:.2f}) — immediate re-engagement required"
            escalation_level = "manager" if rep_workload > 20 else "none"
    
    elif priority_score >= 0.2:
        # Moderate score - early stage, nurture needed
        action_type = "nurture"
        timing = "within 48 hours — early stage, educational content"
        rationale = (
            f"Lower priority ({priority_score:.2f}) — nurture with educational content. "
            f"Urgency: {urgency_str}. Focus on providing value and building interest."
        )
        
        # Adjust escalation based on rep workload
        escalation_level = "manager" if rep_workload > 30 else "none"
    
    else:
        # Low score - needs review
        action_type = "escalate"
        timing = "within 24 hours — low priority, review needed"
        rationale = (
            f"Low priority ({priority_score:.2f}) — review for potential closure "
            f"or removal from queue. Urgency: {urgency_str}. "
            f"Consider if prospect still qualifies as viable."
        )
        escalation_level = "manager"
    
    # Adjust for rep workload (if very high, bump some actions up)
    if pipeline_context and rep_workload > 30:
        # If rep is overwhelmed, promote some nurture/re-engage to re-engage
        if action_type == "nurture" and priority_score >= 0.3:
            action_type = "re-engage"
            rationale += " (bumped due to rep workload)"
            if escalation_level == "none":
                escalation_level = "manager"
    
    # High urgency always trumps workload considerations
    if urgency_str == "high" and action_type in ("nurture", "escalate") and priority_score >= 0.3:
        if action_type == "nurture":
            action_type = "re-engage"
            rationale = f"High urgency ({urgency_str}) overrides normal priority flow — re-engage immediately"
        # close action stays as close if score >= 0.8
    
    return {
        "action_type": action_type,
        "timing_recommendation": timing,
        "rationale": rationale,
        "priority_score": priority_score,
        "sla_deadline": scored_prospect.sla_deadline.isoformat() if scored_prospect.sla_deadline else None,
        "escalation_level": escalation_level,
    }