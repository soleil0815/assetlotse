#!/usr/bin/env bash
# Shows the Trivy findings (JSON) in the run summary and as annotations on the run page.
set -euo pipefail
f="${1:-trivy.json}"
[ -s "$f" ] || { echo "No Trivy report (scan did not run to the end)."; exit 0; }
rows="$(jq -r '
  .Results[]? | .Target as $t |
  ( .Vulnerabilities[]? | [.Severity, .VulnerabilityID, .PkgName, .InstalledVersion, (.FixedVersion // "-"), $t] ),
  ( .Secrets[]?         | [.Severity, "secret", .RuleID, (.Title // ""), "-", $t] )
  | @tsv' "$f")"
n=0; [ -z "$rows" ] || n="$(printf '%s\n' "$rows" | wc -l)"
{
  echo "### ${HEADING:-Trivy}"
  if [ "$n" -eq 0 ]; then
    echo "No HIGH/CRITICAL findings with a fix."
  else
    echo "| Severity | ID | Package | Installed | Fixed in | Where |"
    echo "|---|---|---|---|---|---|"
    printf '%s\n' "$rows" | awk -F'\t' '{printf "| %s | %s | %s | %s | %s | %s |\n",$1,$2,$3,$4,$5,$6}'
  fi
} >> "${GITHUB_STEP_SUMMARY:-/dev/stdout}"
[ "$n" -eq 0 ] && { echo "Trivy: no findings."; exit 0; }
echo "::error title=Trivy::${n} finding(s) – release stopped. Details in the run summary."
printf '%s\n' "$rows" | head -n 40 | while IFS=$'\t' read -r sev id pkg inst fix where; do
  echo "::warning title=${sev} ${id}::${pkg} ${inst} → fixed in ${fix} (${where})"
done
