# Safe public release / Безопасная публикация

## Why a separate GitHub repository?

The private development repo contains historical commits with the developer's own VK application settings and example account identifiers. Changing existing files or deleting old branches **does not remove that history**. Never simply toggle that repository to public.

The safe approach is to export the **current vetted release tree only**, build a **new empty Git repository**, run a private-data scan and offline tests, then push **one clean initial commit** to a new GitHub repository. Original development history is preserved privately.

## Maintainer: one controlled command (macOS or Linux)

Prerequisites: Git, Python 3.10+, Bash and the [GitHub CLI](https://cli.github.com/). Login to the GitHub CLI as the repository owner with `gh auth login`, then in the **original private** checkout:

```bash
cd /path/to/vk-comment-manager
git fetch origin release/public-onboarding-0.4.0
bash <(git show origin/release/public-onboarding-0.4.0:scripts/publish-clean.sh)
```

The script will:

1. Check the currently authenticated GitHub owner, abort if the destination repository already exists.
2. Export only `release/public-onboarding-0.4.0`, excluding the publishing helper itself. **No old Git objects** are included.
3. Replace historical private-repo documentation URLs with those of `Nelect96/vk-comment-cleaner`.
4. Block known private account/domain identifiers, recognizable secrets, absolute user home paths, symbolic links and binary/unusually large files.
5. Perform a fresh isolated Playwright/Chromium installation and run the Python unit tests. **No publication if these fail.**
6. Create an independent Git repository with **one initial commit**, upload it to a new GitHub repository **as private**, and verify that status.
7. Only after a second explicit confirmation change that new repository to public and verify its final visibility.

The release helper does not push any commits to the old repository's `main` branch and never pushes the old history. It does not touch local VK browser profiles or SQLite journals.

## Caveats before describing this as stable

- Test the **new clean build** from another user account. Browser deletion is experimental and UI-dependent.
- Check VK ID registration and callback URL with a **separate registered app**. Without an approved OAuth redirect, a new user cannot complete login.
- Check every new release for accidental personal data, even if the first public version passed the audit.
- Linux and macOS use Bash; Windows currently requires WSL.
- GitHub Actions may be unavailable due to repository/account limits. The offline tests run locally in the release helper; do not assume CI has passed until verified.
- The release script intentionally requires local owner-side GitHub CLI authentication. The ChatGPT GitHub connector cannot change repository visibility or create a clean public repository in this session.

After publishing, verify `https://github.com/Nelect96/vk-comment-cleaner`, set its description/topics, enable private vulnerability reporting, and publish a clearly labeled **beta** release rather than claiming production-quality deletion.
