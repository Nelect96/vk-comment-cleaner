# VK Comment Manager

**A privacy-first tool to review and remove your own VK comments.** · [Русская инструкция](docs/README.ru.md) · [English user guide](docs/README.en.md) · [Security](SECURITY.md)

VK Comment Manager runs **locally on your own computer**. Import the official VK data export, search and filter comments, and optionally run a carefully confirmed browser workflow to delete **your own wall comments**, including several replies in the same discussion. Nothing is uploaded to the project's maintainers.

> **Community-built, independent of VK.** Browser removal is experimental. It can fail if VK changes its interface, restricts an account or hides a thread. A visual deletion marker is not independent confirmation from VK's servers.

## Get started (macOS / Linux)

Install Git and open a Terminal:

```bash
git clone https://github.com/Nelect96/vk-comment-cleaner.git
cd vk-comment-cleaner
bash scripts/app.sh
```

The first run finds Python 3.10+ or, on a Mac with Homebrew, offers to install a supported Python version. Then it installs an isolated Playwright Chromium and runs offline safety tests. It never modifies the system Python. Windows currently requires Bash/WSL. The above destination will become available **after** the separate public repository is published.

**Terminal menu is English-only.** The web interface supports **English and Russian**. It defaults to the browser's language and has a visible language selector.

## 1. Request your own VK archive

1. Sign in at VK itself.
2. Open VK's [Data Protection page](https://vk.com/data_protection?section=rules) or find **Request data archive** in your VK account settings.
3. Select the **Comments** category, request an export and complete VK's own identity verification.
4. Download the ZIP once VK provides it. **Never upload a personal archive to GitHub Issues.**
5. In VKCM, choose your archive from Downloads or browse to a ZIP. The application displays and filters comments from the export locally.

VKCM cannot request or download your protected personal export on your behalf. Wall, photo and video links may be viewed, but **automated browser deletion currently supports only wall comments**.

## 2. Sign in to VK ID

The current VK ID setup requires registering **your own VK ID application** with a numeric App ID and exactly registered redirect URL in the [official VK ID developer portal](https://id.vk.ru/). Add those values in the local VKCM interface. If the provider allows a local redirect, use `http://127.0.0.1:18765/oauth/callback`. If only HTTPS is supported, you need a redirect URL **under your own control** and can paste the returned callback URL into the local finish-login field. **Never share OAuth callback URLs or tokens.**

VK ID authentication and the separate Chromium browser login may both be required. Legacy VK API methods may reject the authenticated token (e.g. error 1051). VKCM does not attempt to bypass those restrictions.

## 3. Review and delete your own wall comments

Open a Terminal in the same project folder and run:

```bash
bash scripts/cleanup.sh "/full/path/to/YOUR/Archive.zip"
```

Alternatively select **2. Delete my own comments from an archive** in the launcher menu. On macOS, the launcher opens a native ZIP file chooser.

VKCM verifies the VK ID account, reads only comment links from the supplied archive, groups related comments, and asks you to enter **`DELETE BATCH N`**, where `N` is the batch count displayed. No delete click occurs without explicit confirmation. The browser checks each comment's exact identifier and author and journals the attempt before clicking.

- **Nested replies** belonging to your account are processed before their own parent/root comment when present in the archive.
- **Previously clicked or uncertain results** are never automatically retried, to avoid accidental second actions.
- A **missing comment before click** is recorded separately. An independent review is possible without retrying already attempted deletions.
- **`browser_ui_deleted_marker`** means a VK webpage indicated removal, **not server-side confirmation**.
- **`Ctrl+C`** stops the batch. Never publish the generated journals or browser profile.

Terminal confirmation phrases only use ASCII: **`DELETE BATCH N`, `CONFIRM DELETED ID`, `STOP`, `DELETED`, `YES`**. Optional API deletion through the web interface uses **`DELETE`** as confirmation. The same local application prevents repeat clicks on ambiguous outcomes.

## Privacy and limitations

The server binds only to `127.0.0.1` (default `18765`) and must **never** be deployed to the public internet. Archives, local SQLite deletion journals, browser cookies, tokens and callback URLs are sensitive; do not send them to project maintainers, GitHub issues or forums.

This is a **beta** requiring manual VK ID application configuration and an external browser for deletion. We do not claim this is a one-click app, complete recovery of every hidden reply, or independently audited production software.

## Development and contributing

Offline tests:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

See the [full English guide](docs/README.en.md), [Russian guide](docs/README.ru.md), [security notes](SECURITY.md), [contribution guide](CONTRIBUTING.md) and [clean-history publication checklist](docs/PUBLISHING.md).

**License: MIT.**
