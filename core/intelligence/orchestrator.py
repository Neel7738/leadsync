"""
Autonomous Orchestration Engine.
Ties together Ingestion, Intelligence, and Generation into a seamless loop.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
import logging

from ..models.conversation import Conversation
from ..models.prospect import ScoredProspect
from .scorer import score_prospect
from .action_engine import determine_next_best_action
from ..generation.prompt import generate_drafts
from .llm_manager import llm_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AutonomousOrchestrator")

class SalesAutonomousAgent:
    """
    The "Brain" of the system.
    Automatically processes a conversation from raw input to finalized follow-up drafts.
    """
    
    def __init__(self, rep_context: Optional[Dict[str, Any]] = None):
        self.rep_context = rep_context or {
            "rep_workload": 0,
            "rep_closing_deals": 0,
            "average_deal_cycle": 30.0
        }

    def process_conversation(self, conversation: Conversation, prospect_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Complete autonomous pipeline:
        1. Score Prospect -> 2. Determine Action -> 3. Generate Tailored Drafts
        """
        # Fix Pydantic circular reference for ScoredProspect
        try:
            ScoredProspect.model_rebuild()
        except Exception:
            pass

        logger.info(f"Starting autonomous processing for conversation {conversation.id}")
        
        try:
            # Step 1: Intelligent Scoring
            # Use metadata for high-fidelity scoring
            scored_prospect = score_prospect(
                conversation=conversation,
                deal_value=prospect_metadata.get("deal_value"),
                engagement_probability=prospect_metadata.get("engagement_prob")
            )
            
            # Step 2: Next Best Action determination
            action_plan = determine_next_best_action(
                conversation=conversation,
                pipeline_context=self.rep_context
            )
            
            # Step 3: Autonomous Generation
            # Only generate drafts if the action is to engage/nurture/close
            drafts = {}
            if action_plan["action_type"] in ["close", "re-engage", "nurture"]:
                drafts = generate_drafts(
                    conversation=conversation,
                    prospect_name=prospect_metadata.get("name", "Prospect"),
                    company=prospect_metadata.get("company", "the company"),
                    role=prospect_metadata.get("role", "their role"),
                    pain_points=prospect_metadata.get("pain_points", []),
                    followup_count=prospect_metadata.get("followup_count", 0),
                    last_followup_date=prospect_metadata.get("last_followup_date"),
                    urgency_level=conversation.urgency or "low"
                )
            
            return {
                "status": "success",
                "scored_prospect": scored_prospect,
                "action_plan": action_plan,
                "drafts": drafts,
                "timestamp": datetime.utcnow().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Autonomous pipeline failed: {str(e)}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }

# Singleton for global access
autonomous_agent = SalesAutonomousAgent()
