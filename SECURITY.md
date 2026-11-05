# Security policy

**Deutsch:** Sicherheitslücken bitte **nicht** als öffentliches Issue melden, sondern vertraulich über
[„Report a vulnerability“](https://github.com/soleil0815/assetlotse/security/advisories/new) (Reiter *Security*
dieses Repositorys). Meldungen auf Deutsch oder Englisch sind willkommen.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Report them privately via
[“Report a vulnerability”](https://github.com/soleil0815/assetlotse/security/advisories/new) on the *Security* tab
of this repository. Reports in English or German are welcome.

Helpful in a report:

- the AssetLotse version (bottom left in the sidebar, or the image tag, e.g. `2.1.0` or `2.1.0-s1`),
- what an attacker can do and which role they need (none, staff, manager, administrator),
- steps to reproduce – ideally against a fresh installation with test data, never with real personal data.

You will get an answer within a few days. Confirmed problems are fixed in a new version; the report stays
private until servers can update, and the CHANGELOG then credits you if you like.

## Supported versions

Only the newest version receives fixes – the image tag `latest` (and the newest `<major>.<minor>`, e.g. `2.1`).
Security updates of the packages inside the image appear automatically every week as `<version>-s<n>`; servers that
follow `latest` get them by themselves.

## Out of scope

- Vulnerabilities in the base image or in Python packages that a weekly security update already fixes – the
  published image is scanned with Trivy every day.
- Problems that need administrator rights on the server or in AssetLotse, or a modified installation.
- Missing hardening of your own environment (reverse proxy, TLS, firewall) – see the README, *Reverse proxy and
  HTTPS* and *Security*.
