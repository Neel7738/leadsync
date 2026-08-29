"""Constants and weightings for the AI Sales Follow-Up Agent."""

# Scoring formula weights
SCORING_WEIGHTS = {
    "urgency_weight": 0.4,
    "deal_value_weight": 0.3,
    "engagement_probability_weight": 0.2,
    "recency_decay_weight": 0.1
}

# SLA thresholds by urgency level (in hours)
SLA_THRESHOLDS = {
    "high": 24,      # High urgency: 24-hour response target
    "medium": 48,    # Medium urgency: 48-hour response target  
    "low": 72        # Low urgency: 72-hour response target
}

# Priority score decay configuration
# Score halves every N days (recency decay)
PRIORITY_DECAY_HALF_LIFE_DAYS = 7.0

# Engagement score calculation weights
ENGAGING_WEIGHTS = {
    "open_weight": 0.3,
    "click_weight": 0.5,
    "reply_weight": 1.0,
    "meeting_weight": 1.5
}

# Default values
DEFAULT_DEAL_VALUE_NORMALIZATION_BASE = 100000.0  # Assume $100k max for normalization
DEFAULT_ENGAGEMENT_PROBABILITY = 0.5  # 50/50 prior when no history available
DEFAULT_URGENCY_MAP = {"high": 1.0, "medium": 0.5, "low": 0.0}

# LLM prompt configuration
MAX_DRAFT_WORDS = 150
MAX_PROMPT_TOKENS = 800
DEFAULT_NUM_DRAFTS = 3  # agreeable, direct, soft

# Queue configuration
QUEUE_DEFAULT_TOP_N = 10  # Number of top prospects to show rep per day
QUEUE_REQUEUE_THRESHOLD = 3  # After N requeues, auto-escalate to manager

# Model cost monitoring (USD per 1K tokens approximately)
MODEL_COSTS = {
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo": {"input": 0.001, "output": 0.002},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
}

# UI configuration
UI_STREAMLIT_PORT = 8501
UI_BOLT_SLIDES_REFRESH_INTERVAL = 30  # seconds

# File paths (defaults)
DEFAULT_SUPPRESSIONS_FILE = ".suppressions.txt"
DEFAULT_CONFIG_FILE = ".env"
DEFAULT_LOGGING_CONFIG = "logging.conf"

# Error messages
ERROR_MESSAGES = {
    "imap_connection": "Failed to connect to IMAP server. Check host, port, and credentials.",
    "imap_authentication": "IMAP authentication failed. Check username and password.",
    "whisper_transcription": "Speech-to-text transcription failed. Check audio quality and model size.",
    "llm_generation": "LLM generation failed. Check API keys and model availability.",
    "queue_full": "Priority queue is at capacity. Some prospects may be delayed.",
    "sla_breach": "SLA breach detected for prospect. Auto-escalation triggered."
}

# Success messages
SUCCESS_MESSAGES = {
    "email_sent": "Follow-up email sent successfully",
    "draft_generated": "Follow-up drafts generated successfully",
    "prospect_scored": "Prospect scored and queued successfully",
    "sla_reset": "SLA deadline reset for prospect"
}

# Logging configuration
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DEFAULT_LOG_LEVEL = "INFO"