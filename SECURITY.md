# Security & Privacy / Безопасность

**Keep your personal VK archive, SQLite journal, OAuth callback URLs, VK tokens and browser session cookies private.**

**Не публикуйте в GitHub и чатах архив комментариев, историю действий, токены, OAuth URL, сессионные cookie или реальные ссылки на приватные обсуждения.**

## Privacy model

- This is a **local-only** application, not a hosted service. The Python server binds to `127.0.0.1` (default launcher port `18765`).
- Imported ZIP content is parsed in memory with size and file-count limits; never upload it to a public website.
- The server validates the localhost Host and a process-local CSRF token for mutating requests.
- VK ID OAuth access/refresh tokens are kept in process memory rather than intentionally persisted by VKCM. A separate Playwright **Chromium browser profile** keeps its own browser sign-in cookies and must be treated as sensitive local data.
- Local SQLite journals store the authenticated numeric VK user ID, comment identifiers and operation outcomes; not passwords or comment text.
- Browser-based deletion sends actions to VK as the current browser session. The program also contacts VK ID and VK API for login and identity checks. It does **not** send archive contents or analytics to this project's maintainers.
- The app has a Playwright dependency, installed by the user into a local Python environment. We do not claim it has no third-party dependencies.
- Batch deletion requires explicit confirmation, exact comment-ID + author checks, and journal checkpointing before individual delete clicks. A frontend deletion marker is not a verified server result. Unknown outcomes must **not** be reissued blindly.
- Do not expose the local server via public tunnels, reverse proxies, port forwarding, or untrusted network interfaces.
- This is experimental community software, not independently audited, and not affiliated with VK. VK login, terms and website structure may change.

## Report vulnerabilities

Open a GitHub issue with a **synthetic** reproduction that contains no personal data or secrets. For a privately exploitable issue, use GitHub's private vulnerability reporting if enabled or contact maintainers via the repository's supported security channel. Do not publicly post working credentials or authorization callbacks.

## Publishing this repository

Never make the old development repository public before auditing **its full Git history**, including deleted files. The recommended clean public export is documented in [docs/PUBLISHING.md](docs/PUBLISHING.md).
