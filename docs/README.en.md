# VK Comment Manager — User Guide

VKCM is a **local-only**, community-developed tool for reviewing your **own** VK comments from an official VK data export and, following an explicit confirmation, attempting to remove your own wall comments (including multiple replies in the same discussion). Not affiliated with VK.

## Install (macOS / Linux)

Install Git, then run the launcher below. It detects a supported Python 3.10+ automatically. On macOS, if only an old Apple Python is found, it offers to install Python 3.13 with an existing Homebrew installation; otherwise it shows the official installer URL:

```bash
git clone https://github.com/Nelect96/vk-comment-cleaner.git
cd vk-comment-cleaner
bash scripts/app.sh
```

The first run opens a simple Russian-language menu and automatically installs an isolated Python environment and Playwright/Chromium. Run the menu again to open the interface or choose your export. The local interface opens at `http://127.0.0.1:18765`. Windows currently requires Bash/WSL. These commands are for the **upcoming public release**; the repository remains private until a separate release and historical secret audit.

## Languages

The local web interface supports **English and Russian**. It automatically chooses the browser language on first use; use the **Language** switch in the header at any time. Your choice is stored locally. All Terminal prompts are in English and accept ASCII confirmations such as `DELETE BATCH N`, `STOP`, and `CONFIRM DELETED ID`. VK API actions in the browser interface use `DELETE` as the confirmation word.

## Download your own official VK archive

1. Sign into VK itself, open [VK's data-protection area](https://vk.com/data_protection?section=rules), or locate the current "Request data archive" feature in VK settings.
2. Select the **Comments** data category and submit your request. VK may require account verification.
3. When VK makes the export available, download the ZIP to your device. Never share the ZIP publicly.
4. Import the ZIP in the local VKCM web interface. Search and filter your archive, open original comment links or export CSV.

VKCM does not request the protected personal export on your behalf. Archive parsing supports wall, photo and video comments for viewing; automated browser deletion is currently limited to **wall comments**.

## Sign in with VK ID

Register **your own VK ID app** in the official VK ID developer portal. Set the numeric App ID and its exactly registered Redirect URL in VKCM. A localhost callback such as `http://127.0.0.1:18765/oauth/callback` works **only if VK permits this redirect to be registered**. For an HTTPS-only redirect, use your own hosted HTTPS callback and copy its full returned URL into VKCM's local finish-login field. Never share that URL, an OAuth code or your credentials.

Official VK ID login and an active browser login in Playwright Chromium can be separate. Login doesn't prove your token can access VK's legacy delete APIs.

## Delete your own wall comments

Stop the previously running VKCM server and execute:

```bash
bash scripts/cleanup.sh "/full/path/to/YOUR/Archive.zip"
```

VKCM checks VK ID identity, shows the queued IDs and requests `DELETE BATCH N` (with the count actually displayed) before doing anything destructive. All Terminal confirmation phrases are English/ASCII: `DELETE BATCH N`, `CONFIRM DELETED ID`, `STOP` and `DELETED`/`YES` in manual browser mode. Each batch handles at most 1000 comments. The browser checks the exact comment ID, confirmed author and scoped delete control before a single click. Where the export includes them, nested own replies are processed before a matching root comment. Other users' messages are never queued.

A local SQLite journal prevents re-clicks after an attempt, including unknown outcomes. Re-running with your archive picks unattempted entries. `browser_ui_deleted_marker` is a **frontend indicator only**, not independent confirmation from VK servers. Missing/hidden/old comments may not be found; VK website changes may break the automation. Stop with `Ctrl+C`. No CAPTCHA or access-control bypass is attempted.

## Privacy and diagnostics

The local web server binds to `127.0.0.1` only; **never expose it to the public internet**. VKCM does not upload archives or passwords to developers. A per-user local operation journal and a separate persistent Chromium login profile stay on your computer. Never put private archives, SQLite databases, browser cookies, tokens or callback URLs in GitHub issues.

Use `.venv/bin/python -m unittest discover -s tests -q` for offline tests. For macOS/Linux setup details and troubleshooting see [the Russian guide](README.ru.md). For publication requirements see [PUBLISHING.md](PUBLISHING.md).
