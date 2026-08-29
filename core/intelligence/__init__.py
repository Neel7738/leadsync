"""Intelligence engine: scoring, action determination, orchestration."""
from .scorer import score_prospect, validate_score_inputs, calculate_recency_decay
from .action_engine import determine_next_best_action
from .llm_manager import llm_manager, LLMManager, LLMResponse

__all__ = [
    "score_prospect",
    "validate_score_inputs",
    "calculate_recency_decay",
    "determine_next_best_action",
    "llm_manager",
    "LLMManager",
    "LLMResponse",
]
