# Roadmap

This is community software and **not affiliated with VK**.

## Ready for public testing

- Import official downloaded ZIPs locally, search and export detected comments.
- Browser queue for own wall comments with explicit confirmation, author checks and SQLite journal.
- Own nested replies can be grouped before parent comments.

## Priorities

1. Verify first-install experience and both English/Russian UI locales from clean macOS and Linux user profiles.
2. Improve supported account sign-in. VK ID currently requires each user to configure their own registered application/redirect, which is an onboarding limitation.
3. Integrate a user-friendly GUI for reviewing the deletion queue; current batch mode uses Terminal.
4. Improve read-only discovery of hidden replies and inaccessible posts; never assume all replies were found.
5. Add repeatable integration tests using a permitted VK test environment.
6. Evaluate Windows installer support.

## Non-goals

- No platform restriction/captcha circumvention.
- No bulk deletion of other users' content.
- No central data collection or hosted archive ingestion.
- No public deployment of the local server.
