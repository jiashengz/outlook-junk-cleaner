# Outlook Junk cleaner for Mac

Keeps the Junk Email folder of an **Outlook.com / Hotmail** account tidy.
Every 30 minutes, mail in Junk that matches your rules is marked as read
and moved to **Deleted Items**. That is all it does: nothing outside Junk is read or
touched, and nothing is deleted permanently unless you list the sender's domain
under `permanently_delete_domains`.

Runs on your Mac (launchd), talks to Microsoft's official Graph API, and has an
optional menu bar icon.

**Needs:** macOS, Python 3.9+, an Outlook.com / Hotmail / Live account.

## Setup

### 1. Register an app with Microsoft (one time, free)

1. Sign up for a free Azure account at https://azure.microsoft.com/free with
   your Outlook/Hotmail address. A personal account needs this to register apps.
2. In https://portal.azure.com, open **App registrations → New registration**:
   - Name: anything, e.g. `Junk cleaner`
   - Supported account types: **Personal Microsoft accounts only**
     (not the default "Single tenant")
   - **Register**, then copy the **Application (client) ID**.
3. **Authentication → Settings → Allow public client flows: Enabled → Save**.
4. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions → Mail.ReadWrite → Add permissions**.

### 2. Install and sign in

```bash
git clone https://github.com/jiashengz/outlook-junk-cleaner.git
cd outlook-junk-cleaner
./setup.sh        # creates rules.toml; paste your client ID into it
./setup.sh        # again: prints a code to enter at microsoft.com/link
```

### 3. Check the rules, then schedule

```bash
./move-junk-to-deleted.sh --survey   # shows what would be moved; changes nothing
./schedule.sh install                # every 30 minutes, and at login
```

Rules live in `rules.toml`; every rule in the example is explained there.
Edits apply on the next run. The log is `logs/cleaner.log`.

When spam starts slipping through, `./move-junk-to-deleted.sh --discover` looks
at what your rules left in Junk and prints ready-to-paste rules, each with the
messages it would catch. You choose what to add.

### 4. Menu bar icon (optional)

```bash
brew install --cask swiftbar
defaults write com.ameba.SwiftBar PluginDirectory -string "$PWD/menubar"
open -a SwiftBar
```

It shows the last run and recent moves, plus Run now, Preview,
Find new spam patterns, Pause/Resume, Open log and Edit rules. If you use a menu bar manager
(Ice, Bartender), the new icon may start out hidden.

### 5. Instant cleaning (optional, needs a domain on Cloudflare)

A free Cloudflare Worker can clean Junk the moment mail arrives, even while
your Mac is asleep: Microsoft notifies it of new Junk mail and it applies the
same rules. It runs on a random URL under `h.yourdomain.com` that is linked
from nowhere.

```bash
npx wrangler login                    # once, signs in to Cloudflare
worker/deploy.sh yourdomain.com       # first time
worker/deploy.sh                      # again after every rules.toml edit
```

The Worker starts from the Mac's sign-in. Its log is in the Cloudflare
dashboard (Workers → junk-cleaner → Logs). The Mac schedule can stay on as a
backup.

## Commands

| Command | What it does |
|---|---|
| `./move-junk-to-deleted.sh` | Mark matching Junk mail read and move it to Deleted Items now |
| `./move-junk-to-deleted.sh --survey` | Preview; changes nothing |
| `./move-junk-to-deleted.sh --discover` | Suggest rules for spam your rules miss; changes nothing |
| `./move-junk-to-deleted.sh --login` | Sign in again |
| `./schedule.sh install` / `uninstall` / `status` | Manage the schedule |

## Good to know

- Your sign-in is stored in `.token_cache.json` (readable only by you) and
  renews itself. If it ever expires, you get a notification; run `--login`.
- If your Mac is asleep or offline, runs are skipped and nothing changes.
- Logs are capped at about 5 MB.
- To remove: `./schedule.sh uninstall`, delete the folder, and revoke
  access at https://account.live.com/consent/Manage.

## License

MIT
