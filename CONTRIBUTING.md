# Contributing

Thanks for improving VK Comment Manager. Open an issue with a minimal, sanitized reproduction. **Never attach real VK account exports, OAuth callback URLs, tokens, session cookies, SQLite operation journals, or personal comment links.**

## Development

- Python 3.10+; isolate dependencies with `bash scripts/install.sh`.
- Run `.venv/bin/python -m unittest discover -s tests -q` before submitting a PR.
- Prefer offline synthetic DOM and mock-network fixtures.
- Never change an ambiguous write outcome into success; do not retry already-clicked comments.
- A change to comment detection must continue to verify both exact comment ID and current author's numeric ID.

This project is independent of VK. Avoid scraping techniques intended to bypass restrictions, rate limits, or access controls.
