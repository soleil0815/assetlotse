# Changes to AssetLotse

The current version is shown at the bottom left of the sidebar after signing in.

## 2.4.2 – 2026-11-08

**Changed**
- **HTTPS behind the reverse proxy follows the same rules as Catalytix and Helfi.Space:**
  - HTTP is only redirected to HTTPS once the address has been reached over HTTPS through the proxy (remembered in
    `https-hosts.txt` in the data folder). A proxy without any HTTPS is no longer redirected into nothing.
  - HSTS is only sent where AssetLotse had to redirect itself. Behind a proxy that redirects and sends HSTS on its
    own (Nginx Proxy Manager with “Force SSL” and “HSTS Enabled”) there is no second HSTS header any more – it
    overrode the proxy's setting (browsers use the first one) and is reported as an error by hstspreload.org.
  - Reading requests are redirected with 301, everything else with 308 (forms keep their data).
  - `ASSETLOTSE_FORCE_HTTPS=1` is new: redirect right away and always send HSTS.
  - `X-Forwarded-Port` without `X-Forwarded-Proto` now also counts with 443 as HTTPS (before: only 80 as HTTP).

## 2.4.1 – 2026-11-08

**Changed**
- **Only complete Debian snapshots are used for new package versions:** snapshot.debian.org lists a new import
  (and serves its Release file) before all of its package lists can be fetched. On 8 Nov 2026 a relock started
  right after a security import therefore failed with 404. `scripts/lock.py` now checks, newest first, that the
  package lists of every suite the build uses (`trixie`, `trixie-updates`, `trixie-security`) exist for amd64 and
  arm64, and takes the newest complete import. Nothing changes in the image itself.

## 2.4.0 – 2026-11-08

**Added**
- **HTTPS is enforced for the proxy's host name:** a request that reaches the reverse proxy over plain HTTP
  (`X-Forwarded-Proto: http`, or only `X-Forwarded-Port: 80`, from a trusted proxy) is redirected permanently (308) to `https://<host>/…`, and
  every HTTPS response carries `Strict-Transport-Security: max-age=31536000` (this host name only). An HTTP rule in
  the proxy no longer serves the app unencrypted. Direct access via `http://IP:9343`, requests without
  `X-Forwarded-Proto` and IP-address host names are left alone. `ASSETLOTSE_FORCE_HTTPS=0` switches both off.
- `Cross-Origin-Opener-Policy: same-origin` on all responses.

**Security**
- `zlib1g`: Debian fixed CVE-2026-27171, CVE-2026-76844 and CVE-2026-85091 on 8 Nov 2026 (DSA-6613-1), but
  snapshot.debian.org could not deliver the fixed package list yet. 2.4.0 therefore still has the same zlib as
  2.3.0; the scan exception for these three expires on 10 Nov 2026, and the weekly security update brings the
  fixed package as soon as it can be fetched.

## 2.3.0 – 2026-11-06

**Changed**
- **German interface uses the formal address ("Sie")** throughout: all German texts of the interface, the e-mails
  (greeting "Guten Tag …"), the help pages, the news entries and the German README. The English texts are
  unchanged.

**Added**
- Test that keeps it that way: informal pronouns (du/dich/dein…, ihr/euch/euer) in German translations, help
  pages, news or the German README fail the build.

## 2.2.1 – 2026-11-06

**Changed**
- **Image build survives short outages of snapshot.debian.org:** a missing package list is now an error
  (`apt-get update --error-on=any`), and the update/upgrade step is tried up to 4 times with growing pauses
  (30, 60, 90 s). Before, a 504 from snapshot.debian.org only gave a warning, and the build stopped later with the
  misleading message that the Debian packages differ from the lock file. If all attempts fail, the build now says
  that snapshot.debian.org did not deliver the packages. Nothing changes in the image itself.

## 2.2.0 – 2026-11-06

**Added**
- **Automatic sign-out after inactivity** (*Administration → Settings → Automatic sign-out*): a limit for
  everyone (default 12 hours, start value `ASSETLOTSE_SESSION_HOURS` in `.env`, 0 = off) and optionally shorter
  limits for administrators and for access from outside – the shortest that applies wins. Every page opened and
  every saved form counts as activity. When the time is up, the open page also switches to the sign-in page, so
  nothing confidential stays on the screen.
- **Notice 2 minutes before** ("You will be signed out in 2 minutes – continue working?", on by default) –
  *Continue working* extends the session, so nobody is thrown out while typing. Activity in another tab counts.
- **Sign out when the browser is closed** (optional): "Stay signed in" is then no longer offered, and existing
  remembered sign-ins end at the next browser restart.

**Changed**
- **"Stay signed in"** now keeps the session across browser restarts (at most 14 days) but no longer beyond the
  inactivity limit. Before, it kept people signed in for 14 days regardless of activity.
- The configured 12-hour session lifetime had no effect (sessions were never marked permanent); it is replaced by
  the inactivity limit above.

## 2.1.1 – 2026-11-05

**Changed**
- **Smaller image, fewer scanner warnings:** after the package check, the build also removes the `openssl`
  command-line tool and `openssl-provider-legacy` (old algorithms such as MD4). AssetLotse never used them; Python
  and its packages only need the library `libssl3t64`, which stays. The OpenSSL configuration and the CA certificate links
  Python uses stay unchanged, and the build checks that TLS still finds the CA certificates. Nothing to change on
  the server.

## 2.1.0 – 2026-11-04

The repository is now in English: code, comments, log messages, command-line output, CHANGELOG and commit
messages. The interface stays bilingual (German and English, default German), as do README, help and news.

**Added**
- **Source code link by default:** *About this program* links to https://github.com/soleil0815/assetlotse unless
  `ASSETLOTSE_SOURCE_URL` names another address (for a modified version – AGPL). Before, the link was empty unless
  set.
- **SECURITY.md:** how to report security problems privately.
- **"Buy me a coffee"** link in the sidebar, for administrators only – opens buymeacoffee.com/soleil0815 in a new
  tab. Managers and staff never see it.

**Changed**
- **Public repository and image:** installing no longer needs `docker login ghcr.io`. Checks of pushes and pull
  requests (also from forks) no longer sign in to the registry at all – no token reaches the registry in those runs.
- **Translations:** English is now the source language – the texts in the code are English, the German ones are in
  `assetlotse/translations/de.json` (replaces `en.json`). The interface looks the same in both languages.
- **Log and command-line output in English**, e.g. `docker compose run --rm assetlotse backups` / `restore NAME`
  and the messages in the container log. The message when an older version starts on a newer database stays
  bilingual.
- Default texts and labels stored in German by earlier versions (login page, backup types, AD group sources,
  certificate types) are still shown in English in the English interface.

## 2.0.2 – 2026-11-04

Results of the monthly code audit (partial review of the changes since 1.12.0).

**Fixed**
- **`./update.sh --cleanup` and mounted drives:** If a drive or NAS folder was mounted below a program folder (e.g.
  `packages/`, `docs/`), its contents were deleted too. Mount points are now detected and skipped; if deletion does
  not fully succeed, the script reports that instead of "Cleaned up".
- **`./update.sh --cleanup` without a container:** Folders mounted by `docker-compose.yml` or
  `docker-compose.override.yml` were only protected while a container existed (no longer after `docker compose down`).
  The script now also reads the mounts from `docker compose config`; if they cannot be read, it deletes nothing.
- **`./update.sh` with a stopped container:** The last version that ran was not detected – no offer to go back, and
  `--rollback` started the version from two updates ago. Stopped containers now count, leftover containers from
  `docker compose run` do not.
- **"docker-compose.yml out of date" notice:** showed `%(found)s`/`%(want)s` instead of the version numbers.
- **Older version on a newer database:** The message could name the wrong backup (`-vor-2.0.10` for 2.0.1). Now
  only the backup of exactly this version is named.
- README: When moving from 1.x, the old script fetches the newest version (not a fixed 2.0.0).

## 2.0.1-s1 – 2026-11-02

Security update: same program as 2.0.1, only updated packages.

- Debian: tzdata 2026e-0+deb13u1 → 2026f-0+deb13u1
- Python: wtforms 3.3.1 → 3.3.2

## 2.0.1 – 2026-11-01

**Fixed**
- **AD sign-in with a wrong password:** Instead of "Incorrect username or password." the message "Sign-in is currently
  not possible" appeared, and every time a program error was logged. Likewise "Test connection" with a wrong service
  account password ended in an error instead of the domain controller's message. Found by the new type checking.

**Internal**
- **Type checking with mypy**, introduced step by step: backup, network and proxy detection, sign-in lockout and the
  build scripts are checked strictly – a finding there makes the tests fail, also in the check run on GitHub. All
  other modules are checked as well, but their findings are only listed for now. More modules will follow over time.
  The Docker image does not contain mypy.

## 2.0.0 – 2026-10-29

Switch to the Docker standard: the program lives entirely in the image. The server only holds
`docker-compose.yml`, `.env` and the `data` folder.

**New**
- **Installation:** `docker-compose.yml` and `env.example` (as `.env`) from the release, `docker compose up -d` –
  no zip, no program folder, nothing is built on the server.
- **Updates like any Docker program:** with Dockhand, Watchtower, Tugtainer & co. or
  `docker compose pull && docker compose up -d`. The `docker-compose.yml` follows `latest`.
- **Images for amd64 and arm64** (e.g. Raspberry Pi, ARM NAS) – each architecture built and tested on its own
  hardware, both scanned.
- **Older version on a newer database:** AssetLotse then does not start and names the backup to restore – instead of
  running on with follow-up errors. Restoring also works when the program does not start:
  `docker compose run --rm assetlotse backups` / `restore NAME`.
- **The backup before each new version** is now called `…-vor-<Version>.db`; the three newest of them are never
  deleted automatically.
- **Version number of `docker-compose.yml`:** If it is too old, AssetLotse shows administrators a notice saying what
  to do. The image ships the current file.
- **Without internet:** The release has the images attached as files (`assetlotse-<Version>-amd64.tar.gz`,
  `…-arm64.tar.gz`) with checksum; `./update.sh --offline DATEI`.

**Changed**
- **New `update.sh`** (runs with `/bin/sh`, also on a NAS): remembers the running image as `previous`, pulls the
  version, starts it, waits for the health check and, if it fails to start, restarts the previous version on request
  (`-y` automatically, later `--rollback`). A newer `docker-compose.yml` from the image is put in place, the old one
  is kept as `docker-compose.yml.alt`. No more unpacking, no more building.
- **`update.sh --cleanup`** cleans up installations from before 2.0.0: shows the program files no longer needed, old
  zips and old locally built images, asks and deletes only those – never data, backups, `.env`,
  `docker-compose.yml` or mounted folders, not even via links.
- **Publishing is now manual only** (*Run workflow → release*). Every push and every pull request is only checked
  (build, test, Trivy). The weekly security updates still appear automatically.
- Every GitHub release contains the installation files (`docker-compose.yml`, `env.example`, `update.sh`) and the
  image files.

- Packages up to date (including Python 3.13.18).

**Removed**
- The zip route (`./update.sh assetlotse-X.Y.Z.zip`), building locally on the server and the instructions for
  running without Docker. Docker is required.

**Migration:** once `./update.sh --pull`, then `./update.sh --cleanup` (see README, "Moving from an installation
before 2.0.0").

## 1.12.0 – 2026-10-28

Result of a full code review.

**Changed**
- **"Manages the entire inventory" is a separate checkbox** on the user. Until now a manager without a group was
  automatically responsible for everything – if they lost their last group (e.g. through the AD sync), they suddenly
  saw the whole inventory. Now they then manage nothing. Managers who have no group at the time of the update get
  the checkbox once automatically, so nothing changes for them. If someone (also via AD) gets a different role, the
  checkbox is removed.
- **Initial setup only directly:** `/setup` can only be reached from localhost or directly from the internal network;
  proxy headers (`X-Forwarded-For`) are no longer considered for it.
- **Image without apt:** The package manager (apt, libapt-pkg) is removed after installation.
- Notices at the top of the page disappear after 20 seconds (they stay while the mouse is over them).
  Errors stay.

**Fixed**
- **Changing the email address in the profile:** at most three confirmation emails per hour; only the newest link is
  valid, and only once; domain and uniqueness are checked again on click; the pending address is shown in the
  profile. The email no longer contains freely chosen text. Each address can now be used only once across all local
  accounts.
- Items in groups with "No lending" no longer count as "available" (filter and start page) and cannot be renewed by
  borrowers themselves.
- Issue desk: If the selected person already has the item, "Issue" is disabled ("already has it"); an unreachable AD
  leads to a message instead of an error; after a rejected issue the person stays selected; the maximum number of
  loans is also enforced for simultaneous issues (PostgreSQL).
- Retention periods only delete if the backup on the same day succeeded and none failed afterwards.
- The daily summary for managers keeps running if it fails for a single manager.
- The note on turning off notifications now only appears in loan emails.
- Usability: labels for screen readers on search and filter fields, photos and the issue row also on narrow screens,
  "Remove photo" stays selected after an error.

**Build and update**
- A release must not contain older packages than the most recently published security update.
- Repeated runs do not publish twice; missing GitHub releases are added later; a security update is not published
  if `main` changed during the run.
- Check runs (scan, manual) no longer wait for publishing. Errors show a readable message.
- pip is treated as a calendar version (a new year's version is not a version jump).
- `update.sh` knows security updates (`--version 1.12.0-s1`), returns to the previously running build on problems
  and checks that all build files are present.

## 1.11.0 – 2026-10-27

**New**
- **Issue from the inventory:** In the inventory list every available item has **Issue**, and the page of an item has
  **Find person**. Both open the issue desk with the item already selected – find the person (also in AD), confirm,
  done; afterwards you return to the item.
- **Groups with "No lending":** New checkbox in the group. Its items are never issued – not at the desk (not even
  offered there), not from the inventory and not by self-checkout. The inventory shows "no lending". Items already
  issued can still be returned.
- **Suggestions when adding items:** Name, manufacturer and model are suggested from the existing inventory. Choosing
  a known name or a known model (e.g. "Vostro 3400") fills empty fields such as manufacturer and category to match.
- **"Add similar" takes over the photo** (also "Save & next"); can be deselected.
- **Own details:** Local and self-registered accounts change their name and email address themselves under
  *My profile → My details*. For AD accounts both still come from Active Directory. Self-registered accounts follow
  the same rules as at registration: allowed domain, each address only once, and – if confirmation is required
  there – the new address only takes effect after clicking the link in the email.

**Fixed**
- **Correcting a wrongly assigned inventory or serial number:** If a number is accidentally on another device, the
  form offers to remove it there and enter it here – in one step. Swapped numbers can thus be corrected without
  deleting and re-creating a device.

## 1.10.2 – 2026-10-25

**Changed**
- **The main branch is now called `main`** (previously `master`). Workflow, scripts, self-tests and docs have been
  switched; publishing only happens from `main`. On GitHub, restrict the `release` environment to `main` accordingly.
  Local copies: `git branch -m master main && git fetch origin && git branch -u origin/main main`.

Nothing changes for the app itself.

## 1.10.1 – 2026-10-25

**New**
- **Releases on GitHub:** Every new version and every security update (`<Version>-s<Zähler>`) gets its own page under
  *Releases* after publishing – with the matching section from this CHANGELOG. Only what is also `latest` is marked
  "Latest". Only after passed tests and scan, like the image.

Nothing changes for the app itself.

## 1.10.0 – 2026-10-25

**Security – the image stays secure and up to date automatically, without version jumps**
- **Reproducible build:** base image with fixed version and checksum, **all Debian packages** (including those of
  the base image) with exact versions in `build/debian-packages.lock` (fixed state of snapshot.debian.org), all
  Python packages including sub-dependencies pinned exactly in `requirements.txt`, `pip` as `ARG PIP_VERSION`.
  `scripts/lock.py refresh` pins new versions – only within the base; on GitHub:
  *Run workflow → relock*.
- **Limits:** same Debian release (trixie), same Python line (3.13), every package within its major version
  (before 1.0: minor version), calendar versions (root certificates, time zones) without limit.
  `requirements.in` widened to the major versions accordingly.
- **Security update every week** (Mondays; manually *Run workflow → security-update*): new versions within the
  limits, build without cache, tests in the image, Trivy – only if everything passes, publication as
  `<Version>-s<Zähler>` (e.g. `1.10.0-s1`) and `latest`, entry here in the CHANGELOG with the changed packages.
  Nothing new → nothing happens. Replaces the previous Monday rebuild (`<Version>-<Datum>`).
- **Trivy without GitHub Action:** fixed version, verified by SHA-256 (trivy-action/setup-trivy were compromised in
  March 2026, CVE-2026-33634). `.trivyignore` entries need a reason and an expiry date.
- **Daily scan** of the published `latest`; **base warning** on Mondays via endoflife.date
  (notice from 12, red run from 6 months before the end of security updates).
- **Workflow:** release only from the `master` branch (no more releases via tag pushes or `main`), never two at
  the same time, published versions are never overwritten, `latest` only for the highest version,
  no stored Git credentials, tests and scans never in jobs with write permissions.
- `ensurepip` is removed from the image in addition to `pip`; Python 3.13.17 with newly pinned Debian packages.
- Half-aborted runs are completed by the next one (`latest`/`<Minor>` are only moved forward).
- Local build (`./update.sh` with zip) now needs HTTPS access to `snapshot.debian.org`.
- Once on GitHub: restrict the `release` environment to the `master` branch (see README).

**Documentation:** README (section "Security and freshness of the image", including "When a run is red") and
help *Updates and maintenance*.

Nothing changes for using the app.

## 1.9.5 – 2026-10-24

**Security**
- **The Monday rebuild now also updates the Python packages:** each to the newest version within the ranges
  from `requirements.in`, sub-dependencies included. Tests and Trivy run against exactly these versions;
  only if both pass is it published. Jumps beyond the ranges still come as a new version.
- Pinned package versions updated: cryptography 50.0.2, SQLAlchemy 2.1.6, WTForms 3.3.1, MarkupSafe 3.0.4,
  certifi 2026.10.23, charset-normalizer 3.5.2, pycparser 3.12.

## 1.9.4 – 2026-10-24

**Security**
- **Weekly security rebuild instead of a mere re-scan:** Every Monday GitHub rebuilds the highest version
  (from its tag, newest Python image of the line, all Debian updates), runs all tests in the finished image
  and scans it with Trivy. Only if both pass and something has changed is the image published as `latest`, `<Minor>`
  and `<Version>-<Datum>`. The exact version tag stays unchanged. Manually: *Run workflow → weekly*.
- **Tests now also run on every release** in the built image (previously only locally) – publishing only happens
  after passed tests and a passed scan.
- `Dockerfile`: new `ARG OS_UPGRADE` for the rebuild; if not given, nothing happens, normal builds stay exactly
  reproducible.

Nothing changes for the app itself.

## 1.9.3 – 2026-10-24

**Security**
- **Vulnerability scanning with Trivy:** The GitHub workflow scans every image before publishing. Known
  vulnerabilities of severity HIGH or CRITICAL with an available fix (Debian and Python packages) stop the
  release – `latest` then stays on the previous version. Exactly the scanned image is published.
- Every Monday `latest` is scanned again; on a finding the run fails and GitHub sends an email.
- Deliberate exceptions with reason and expiry date in `.trivyignore`. Trivy is used in a version from after the
  supply chain incident of March 2026 and, like all actions, pinned by commit SHA.
- The workflow is split into build → scan → publish: the scanner runs without write permissions and without
  registry login, only the last step has write permissions. A tampered scanner could thus change neither
  images nor tags.
- **pip is no longer in the image:** It is only needed for installing and removed afterwards. pip ships its own,
  older copies of urllib3, msgpack and setuptools, in which the first scan found four HIGH vulnerabilities
  (AssetLotse itself uses urllib3 2.8.0 and had none).

Nothing changes for the app itself.

## 1.9.2 – 2026-10-24

Code audit: fixes in the app, user interface and update path. No new features, no database change.

**Security**
- Deleting a group whose manager would otherwise be left without a group is rejected – a manager without a group
  manages the entire inventory. Saving a manager without a group shows a notice.
- The limit on registration attempts now applies to all processes together (previously per worker).
- Backup downloads are no longer cached.

**Fixed**
- AD: If the directory server is briefly unreachable or busy, this counts as an error instead of "user
  unknown" or "no groups" – nobody loses groups or gets locked out because of it.
- The first sign-in of two sessions at the same time no longer leads to an error 500.
- Users with open loans are never deleted (not even by cleanup or retention periods).
- A double return (two clicks, two devices) is only booked once.
- Turning off the AD sync no longer unlocks accounts of people who have left.
- Reminder emails: an error for one person no longer stops the run; failed runs are retried up to
  three times on the same day. The background tasks run independently of each other.
- With backups enabled, retention only runs after the daily backup.
- Desk: inventory numbers with umlauts are found regardless of upper/lower case; the "automatic" checkbox
  is remembered.
- Editing an item at the same time and conflicts when saving result in a message instead of error 500.
- DNS errors for network ranges with host names are cached (no waiting time on every request).
- Faster user list and AD sync with many accounts.
- Backup messages and emails in English in the English interface, sizes with the matching decimal separator.
- Interface: no horizontal scrolling on phones, menu can be closed with Escape and is correct for screen readers,
  all form fields linked to their labels, better contrast, error messages stay, an expired session is
  reported.

**Update path**
- `update.sh`: aborting (Ctrl+C) in the middle of the swap is rolled back cleanly; two simultaneous updates are
  prevented; `latest` is reset when an image is rejected; no silent downgrade; the recovery folder is kept on an
  error; registry addresses with a port work.
- Health check and stop timeout of the container match the startup time; the workflow sets the minor tag and
  `latest` only for the highest version.

## 1.9.1 – 2026-10-24

**Changed**
- **New versions without a tag:** The GitHub workflow now builds the image itself on every push to `master` with a
  new version number, sets `latest` (if it is the highest version) and creates the tag `v<Version>`.
  Pushes without a new version number build nothing. Setting a tag manually still works.
- Servers that follow `latest` (`./update.sh --pull` or an update tool such as Dockhand) thus get new versions
  without any further step – after the upload it takes a minute or two until the image is available.

## 1.9.0 – 2026-10-24

**New**
- **News in the app:** A small dot next to the version number at the bottom left shows when there is something new
  after an update; it leads to the *What's new* page (also via *About this program*). Short notes on new
  features, security updates and fixes, in German and English, filtered by role. After reading, the dot
  disappears until the next update. Content: `assetlotse/news.json`.

## 1.8.2 – 2026-10-23

**New**
- **English manual** `docs/Manual-AssetLotse.pdf` with screenshots of the English interface. In the English
  interface the help now links to it instead of the German one.

## 1.8.1 – 2026-10-23

Replaces 1.8.0 (which had its own Dockhand mode – removed, now it works with any tool).

**New: updates with other tools too**
- After `./update.sh --pull` the container follows the image tag **`latest`**. New versions can thus be installed as
  before with `./update.sh --pull` – or with any tool that updates containers. There are no more settings for a
  specific tool; the Dockhand label in `docker-compose.yml` has been removed.
- `./update.sh --pull --version X.Y.Z` pins a version (tools then leave it alone), the next
  `./update.sh --pull` follows the newest again.
- **Backup on the first start of a new version:** AssetLotse remembers its version in the database. When a new
  version runs for the first time, the app creates a backup **before** any change to the database
  (`…-vor-update.db`, SQLite only) – no matter how the update came. If an older version starts on a newer
  database, a warning is written to the log.

**Note:** An update tool only replaces the container, not `update.sh` and `docker-compose.yml` on the
server. If these change, it says so here in the CHANGELOG – then run `./update.sh --pull` once.

## 1.7.0 – 2026-10-23

**New: ready-made images from GitHub**
- **GitHub Actions** (`.github/workflows/docker.yml`): A version tag such as `v1.7.0` builds the image and stores it at
  `ghcr.io/soleil0815/assetlotse` (tags `1.7.0`, `1.7`, `latest`). The run aborts if the tag and the version in
  `assetlotse/__init__.py` do not match. Started manually, only a test image `test-<commit>` is created.
- **`./update.sh --pull`** pulls the image instead of building: backup → pull image → take over `update.sh`,
  `docker-compose.yml` & co. from the image → restart → clean up old images. `--version X.Y.Z` for a specific
  version (also to go back), `--image` for a different address. Once on the server: `docker login ghcr.io`.
- **Both routes remain:** A zip is still built locally, then as `assetlotse:X.Y.Z` – never under the
  GitHub name, so that Dockhand does not replace it. Which image runs is managed by `update.sh` in `.env`
  (`ASSETLOTSE_IMAGE`, `ASSETLOTSE_TAG`, `ASSETLOTSE_REGISTRY_IMAGE`). Cleanup always keeps the running and the
  previously running image, whether built locally or from GitHub.
- **Dockhand:** still no automatic update (`dockhand.update=false`), because only `update.sh` makes a backup first.
- **Trust:** `--pull` checks that the image was built from the matching GitHub repository, shows version and
  digest and always asks for a "yes" for a new image address (even with `-y`). The GitHub actions are pinned to
  fixed commits. `latest` only goes to the highest version – a patch for an older line
  does not move it.
- The image now also contains the installation files (`/app/update.sh` etc.). `.gitignore` extended (packages,
  archive), the zip no longer contains a `.env` (`update.sh` creates it if needed).

**First switch:** Install this zip as usual with `./update.sh assetlotse-1.7.0.zip` (the old script does not know
`--pull` yet). Then push the code with tag `v1.7.0` to GitHub, and on the server run once
`docker login ghcr.io -u soleil0815` and `./update.sh --pull`.

## 1.6.3 – 2026-10-23

**Changed**
- **Installation files in English:** `update.sh`, `docker-entrypoint.sh`, `docker-compose.yml`, `.env.example`,
  `Dockerfile`, `gunicorn.conf.py`, `wsgi.py` and `requirements*` – comments and all messages. The prompt of
  `update.sh` is now `[Y/n]` (a "j" is still accepted). Interface, help and manual remain
  bilingual or German.
- The package archive is now called **`packages/`** instead of `pakete/`. An existing `pakete/` folder is renamed
  automatically on the next update or `./update.sh --cleanup`. Back to the previous version:
  `./update.sh packages/assetlotse-ALT.zip`.

## 1.6.2 – 2026-10-23

**Notes on updating from 1.6.1:**
- The old `update.sh` does not know cleanup yet. After this update run `./update.sh --cleanup` once –
  from then on every update cleans up by itself.
- If self-registration with email confirmation is on, the **address of AssetLotse** (as reachable from outside)
  must be entered under *Administration → Email* – otherwise registration is blocked.
- `./data` is now only readable by the app user. If a script on the server copies the backups, it must
  run as root or as `PUID`.

**New: cleanup on update**
- After a successful update the zip goes into the archive `./pakete` (the current and the previous package are kept
  for going back, older ones are deleted). Loose `assetlotse-*.zip` in the program folder move to the archive,
  remains of aborted updates and older AssetLotse images are removed; the image of the previous version is kept.
  Other Docker images on the server are never touched.
- New options: `--no-cleanup` (do not clean up), `--cleanup` (only clean up). `--prune` also deletes the
  previous image. Back to the previous version: `./update.sh pakete/assetlotse-ALT.zip`.
- After the files have been replaced, the **new** `update.sh` takes over building, starting and cleaning up –
  improvements to the script thus already take effect during the update itself.

**Security** (result of a full review – nothing critical, the following points fixed)
- **Links in emails** (confirmation, admin notice, approval) are no longer built from the host header of the
  request – otherwise a stranger could trigger emails with links to their own site. Without an entered address of
  AssetLotse, registration with email confirmation is blocked; when registration is turned on, AssetLotse takes
  the address under which the admin opens the page.
- **Confidential details (BitLocker keys) for managers** – viewing, changing, deleting and sharing with the
  borrower – only with a confirmed second factor in the current session, even if the password is enough to sign in
  on the internal network (as already for one's own device).
- **No caching of signed-in pages** in proxies/CDNs or on the browser's disk (including
  CSV export, 2FA setup, recovery codes); photos and manual only cacheable as "private".
- **Registration:** at most 30 attempts per hour and IP (slows down probing for existing usernames and
  addresses).
- **Initial setup** (`/setup`) only from the internal network or via localhost – otherwise `create-admin`.
- **Loopback no longer automatically counts as internal network** (tunnels and forwarding without a visitor address
  would otherwise have received the 2FA exemption for the internal network). If needed, enter `127.0.0.0/8`.
- **The first second factor** signs out all other devices; **change password** is throttled against guessing.
- **Locations:** Group managers no longer see free-text locations of other groups and cannot
  guess them or take them over as a fixed location.
- **Downloading backups:** is never cached.
- **Images:** only PNG/JPEG/WebP/GIF are read at all; photos are decoded in a memory-saving way.
  **Pillow 13.0.0** (fixes, among others, memory and CPU time attacks via crafted images), Werkzeug 3.1.9,
  certifi 2026.10.19.
- **Database:** deleted data is overwritten (`secure_delete`), so that e.g. removed keys do not remain in the free
  space of the SQLite file (older backups still contain them).
- **Container:** `./data` only readable by the app user, new files with 0600; `PUID`/`PGID` 0 or not a number
  are rejected; `no-new-privileges`.
- **update.sh:** works on its own copy of the package, shows the SHA-256 checksum and compares it with
  `PAKET.zip.sha256` if present; rejects absolute paths, `..`, symbolic links and special files;
  does not take over write permissions from the zip; recovery after an error only touches entries that were
  demonstrably replaced; a newly created `.env` is only readable by its owner.
- Warning at startup if `ASSETLOTSE_SECRET_KEY` is shorter than 32 characters.

## 1.6.1 – 2026-10-23

**New**
- **Manual as PDF** (`docs/Handbuch-AssetLotse.pdf`, 30 pages with screenshots): part A for everyone, part B for
  managers, part C for administrators, plus troubleshooting, emergency commands and all `.env` settings. The help
  links to it at the top; the English interface gets the German manual as long as there is no English one.

## 1.6.0 – 2026-10-23

**New**
- **AD sync in the background** (with service account; on by default, hourly – selectable up to daily;
  "Synchronise now"):
  - **Fast person search at the desk:** People without an AssetLotse account who would be allowed to sign in under
    the AD rules are cached with username, name and email. The desk finds them immediately, already while
    typing (suggestions with "AD – no account yet", one click takes over the account). If the cache finds
    nobody, AD is searched live as before. "Take over" still checks directly in AD.
  - **Existing AD accounts are kept up to date** – even without signing in: name, email, AssetLotse groups (with
    "Assign groups automatically") and renames in AD (detected by objectGUID). The role only for accounts that
    have already signed in themselves.
  - **Disabled, expired, deleted or no longer authorized people** are removed from the cache;
    their accounts are deactivated (loans remain) and automatically reactivated when the person returns.
    On the first sync and with conspicuous numbers (more than 20 %, all accounts or the last administrator)
    nobody is deactivated automatically – the AD page lists the names for confirmation. Accounts locked by the
    sync are also reactivated immediately on the next sign-in if AD allows them.
  - Renames are also detected at sign-in and account takeover (no second account); a username newly assigned
    in AD never takes over the account of a former person. Aborted read queries never count as complete.
  - Protection of administrators: AD admins are never deactivated automatically; no demotion if afterwards no
    administrator would remain or many accounts would lose rights at once; an AD admin cannot save AD settings
    that would lock themselves out.
  - AD accounts can be renamed under "Users" (e.g. to move an old account aside when a name has been reassigned).
    Locked legacy accounts without an AD identifier are never reactivated automatically.
  - Efficient: two large queries (all groups, all people) instead of one per person.
- Person field at the issue desk with suggestions while typing (replaces the old selection list).

## 1.5.0 – 2026-10-22

**New**
- **Suggestions while typing in the search at the top:** matching items with inventory/serial number, category and
  "x of y available"; a click (or ↓ and Enter) opens the device, "Show all results" or Enter the full list.
- **Suggestions while typing at the issue desk:** e.g. "hard drive" immediately shows the matching devices that the
  selected person may receive – one click issues. Barcode scanners work as before.
- **Inventory number when adding:** Below the field it says whether the number is free or already taken (with link),
  which similar numbers exist and the next free one (e.g. IT-0044) to take over. The same for the
  serial number (free/taken).

**Changed**
- **Duplicate inventory and serial numbers are rejected** (previously only a warning), regardless of upper and
  lower case (including umlauts) and leading/trailing spaces; the input stays in the form. In addition, a
  unique index in the database (SQLite, PostgreSQL, MariaDB/MySQL) prevents two people from assigning the same number
  at the same time. Duplicate numbers from older versions do not block editing
  (warning); as long as they exist, only the database index for that kind of number is omitted (notice in the log at
  startup). New columns are added automatically on the first start.

## 1.4.1 – 2026-10-22

**Changed**
- **No minimum version for Docker Compose any more** (previously 2.24): `docker-compose.yml` now only uses settings
  that every Compose version understands – tested with 1.29, 2.20 and 5.6. `update.sh` works with the plugin
  `docker compose` and with the older standalone `docker-compose` (selectable with `ASSETLOTSE_COMPOSE`).
- The package contains an empty `.env` so that `docker compose up` runs right away; if it is missing, `update.sh`
  creates it. An existing `.env` is still never overwritten.

## 1.4.0 – 2026-10-22

**Changed**
- **By default staff members no longer see the inventory**, only their own items under "My items"
  (including the detail page, as long as the item is issued to them). The menu item "Inventory", the search field and
  "New arrivals" are gone for them. Configurable under *Settings → Inventory visibility* (new:
  "own items only"; still "own groups" and "entire inventory"). Managers and administrators see
  the inventory as before. Self-checkout only works if staff members can see the inventory.
  Anyone who has saved the visibility setting before keeps their setting.

## 1.3.0 – 2026-10-22

**New**
- **Confidential details for the borrower:** Per item it can be allowed that the person to whom it is currently
  issued may view the confidential details themselves – e.g. the BitLocker key of their
  drive if they have forgotten it. Default: off. Only while the issue is running (it ends with the return),
  only after re-entering one's own password (AD accounts are checked against AD, failed attempts
  count towards the sign-in lockout). Every retrieval is logged as "viewed (borrower)", as is turning the
  sharing on and off. Other staff members still see nothing; only someone who may see the details
  themselves may switch it. The person sees their item even if it belongs to another group.
  So that nobody can "borrow" the key: only for single items and only if the issue was made by
  someone who may see the details themselves (not with self-checkout). Anyone who signed in without a second factor
  confirms it first; failed attempts also count across several addresses.

## 1.2.1 – 2026-10-22

**Changed**
- **The default port is now 9343** (previously 9300). To keep 9300: `ASSETLOTSE_PORT=9300` in `.env`.
- **Port freely selectable with one setting:** `ASSETLOTSE_PORT` in `.env` now applies outside on the server *and*
  inside the container (gunicorn, health check). Previously the container always listened on 9300 – anyone who had
  adjusted `docker-compose.yml` quickly ended up nowhere. Allowed are 1024–65535; an invalid value
  is rejected with a clear message at startup and by `update.sh`.
- `update.sh` shows the correct address at the end and warns if `ASSETLOTSE_BIND=127.0.0.1` is set
  (then only reachable on the server itself) and, when updating from older versions, about the new default port.

## 1.2.0 – 2026-10-22

**New**
- **People from Active Directory at the issue desk:** The person search also finds staff who have
  never signed in – but only those who have access under the AD rules (base DN, user filter, OUs/groups for
  staff members, managers, administrators); accounts disabled in AD remain invisible.
  Also hidden: expired accounts, computer and group objects. "Take over" checks the person again in AD
  on the server side and creates the account – initially as a staff member, the AD role applies from their first
  own sign-in. Requires the service account; searches are logged.
- **Inventory search in the issue view:** search text and category, by default only available items; issue directly
  from the result list, the search stays in place afterwards.
- If the person search matches several accounts, a selection list appears (instead of "not unique").

## 1.1.0 – 2026-10-22

**New**
- **Labels with barcode** as PDF in the format **62 × 29 mm** (e.g. Brother QL with DK-11209 or 62 mm continuous
  tape):
  name, category/model, serial number, barcode (Code 128) with the inventory number and name of the organization.
  Individually on the detail page ("Label") or for the whole current selection in the inventory ("Labels", at most 500).
  Without an inventory number (or with umlauts in it) `#<interne Nummer>` is encoded – the issue desk finds both.
- **Start page with key figures** for managers: Inventory, Available, Issued (with utilization), Overdue,
  confidential details; table "Inventory by category" with utilization bars, "Recent activity", "Due back soon".

**Changed**
- **New look** in the style of asset management software: slate gray and petrol, a plain
  typeface (Inter) for everything, denser tables, smaller radii. The display typeface Fraunces has been dropped.
- **Inventory** opens as a **table** by default (tiles can still be switched on) – with an "Issued to" column.
- Default colors in the branding, group color, favicon and emails to match the new design.

## 1.0.0 – 2026-10-22

First version: management and issuing of work equipment (laptops, USB sticks, headsets, keys, cables …).

- **Items** with name, category (free, with suggestions), manufacturer, model, serial number,
  inventory number, description/accessories, location, group, quantity and photo.
- **Confidential details** per item (e.g. BitLocker recovery key): encrypted with the
  `secret_key`, visible only to administrators and managers of the item's group via "Reveal"; every
  view/change/deletion is logged, never in the export. The access log is deleted after the retention period.
- **Issuing** with a return date or **indefinitely** (default). Indefinite issues are never overdue and
  get no reminders. **Issue desk** for barcode scanners (inventory number, serial number, name, `#Nummer`).
- "Add similar" and "Save & next"; warning on duplicate inventory or serial numbers.
- Filters by category, group, location and status; CSV export with an "Issued to" column.
- Roles staff member, manager, administrator; groups (e.g. departments); locations.
- Active Directory/LDAP, two-factor sign-in (TOTP, security keys), email notifications,
  self-registration, custom branding, automatic data backup, retention periods (GDPR),
  German and English, Docker with `update.sh`.
