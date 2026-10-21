# VARIS approved institution follow-up runner

This repository contains only the public execution code and the six approved institutional contacts for the 2026-10-21 follow-up campaign.

## Safety

- No SMTP password, API token, private CRM export, or local CRM data is stored here.
- The contact list is limited to approved official institutional channels.
- The workflow defaults to dry-run and must be reviewed before a live send.
- Live sending requires repository Actions secrets: SMTP_HOST, SMTP_PORT, SMTP_SECURITY, SMTP_USER, SMTP_PASSWORD, and FROM_EMAIL.
- Do not add personal student contacts or unrelated recipients.
- State and suppression files are committed only by the workflow after a live run.

## Manual run

1. Open Actions → Approved institution follow-up sender → Run workflow.
2. Leave dry_run=true for the first verification.
3. Confirm the six targets, suppression rules, and tracking configuration.
4. Run again with dry_run=false only within the approved scope.

Tracking uses https://mail.varis.kr and campaign key education-followup-20261021.
