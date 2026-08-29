# Changelog

All notable changes to LeadSync will be documented in this file.

---

**Hackathon Project** — Problem statement by [Product Space](https://in.linkedin.com/company/theproductspace) via [Unstop](https://unstop.com)

---

## [1.0.0] - 2026-08-29

### Added

#### Core Pipeline
- Autonomous conversation processing pipeline
- Email ingestion with IMAP and LLM entity extraction
- Speech-to-text transcription via Whisper
- Meeting notes parsing with NLP extraction
- Weighted priority scoring (urgency, deal value, engagement, recency)
- Action engine (close, re-engage, nurture, escalate)
- 3-tone draft generation (agreeable, direct, soft)
- Email tracking with open/click pixels
- Conversation deduplication

#### LLM Integration
- Multi-provider fallback (OpenAI, Anthropic, Google, Groq, NVIDIA NIM, Ollama)
- Automatic health tracking per provider
- Dynamic model discovery from local Ollama
- Cost and latency monitoring
- Zero-downtime provider switching

#### Alerting
- 7-channel alerting: Telegram, Email, Slack, Discord, Teams, PagerDuty, Opsgenie
- Exponential backoff retry with jitter
- Webhook payload inspector
- Test endpoint for all channels

#### Security
- TOTP two-factor authentication
- Backup codes for account recovery
- Recovery email with one-time use links
- Mandatory 2FA enforcement for admin accounts
- Role-based access control (admin, rep, viewer)
- API key authentication
- Session-based auth with brute-force protection
- Rate limiting per IP and per endpoint

#### Real-Time
- WebSocket queue updates
- Server-Sent Events (SSE) fallback
- Connection authentication via API key

#### Database
- PostgreSQL with SQLAlchemy ORM
- SQLite fallback for development
- Alembic migrations
- Automated backup and restore
- CSV/PDF export for compliance

#### Dashboard
- Streamlit-based UI
- Live queue with SLA tracking
- Draft review and selection
- User management with 2FA setup
- Webhook payload inspector
- WebSocket test client

#### Infrastructure
- Docker Compose setup
- Prometheus metrics
- Grafana dashboards
- Redis queue backend

#### Testing
- 490 unit and integration tests
- pytest with async support
- Coverage reporting

### Security
- No hardcoded credentials
- Environment-based configuration
- SQL injection prevention
- XSS protection
- CSRF protection
