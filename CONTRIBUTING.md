# Contributing to LeadSync

Thank you for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone
git clone https://github.com/demolished-lab/leadsync.git
cd leadsync

# Install with dev dependencies
pip install -e ".[dev]"

# Copy environment config
cp .env.example .env

# Run tests
pytest tests/unit/ -v
```

## Code Style

- Python 3.10+
- Type hints on all public functions
- Docstrings for classes and public methods
- `ruff` for linting and formatting

```bash
ruff check .
ruff format .
```

## Testing

- Write tests for all new features
- Maintain or improve coverage
- Run the full suite before submitting:

```bash
pytest tests/unit/ -v
```

## Pull Requests

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Add tests
5. Ensure all tests pass
6. Submit a pull request

## Commit Messages

- Use clear, descriptive commit messages
- Start with a type: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
- Reference issues when applicable

## Reporting Issues

- Use GitHub Issues
- Include steps to reproduce
- Include Python version and OS
- Include error messages and stack traces

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
