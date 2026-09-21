# sf-deploy — Local Salesforce Data-Dictionary deployment

Local, Cursor-driven replacement for the old Google Apps Script + CI deployment
flow. Everything runs from **this** folder: connect your org here, deploy from
here.

```
sf-deploy/
├── scripts/
│   ├── fetch_sheet.py      1. Google Sheet  → temp_updates.json   (Google Workspace MCP)
│   ├── validate_sheet.py   2. temp_updates.json → PASS/FAIL log   (deployment gate)
│   ├── generate_xml.py     3. temp_updates.json → force-app/**    (reused generator)
│   ├── build_manifest.py   4. force-app → manifest/package.xml (+ destructiveChanges)
│   ├── deploy.py           5. sf CLI deploy   (check-only default; real deploy = gated)
│   ├── run.py              steps 1–4 in one command (no deploy)
│   └── generate_*.py       layout / flexipage / permission-set generators (reused)
├── force-app/main/default/ generated metadata source tree
├── manifest/               package.xml, destructiveChanges.xml
├── .build/                 disposable reports (validation_report.json, …)
└── sfdx-project.json
```

## Prerequisites (one-time)

```bash
# Salesforce CLI (already present: /usr/local/bin/sf)
sf --version

# Python 3. Sheet I/O uses Google Workspace MCP (mcp-adaptor), not gcloud ADC.

# Connect your Salesforce org
sf org login web --alias "ERP DEV 02"
sf config set target-org "ERP DEV 02"
```

## Everyday flow

```bash
cd "sf-deploy"

# SCOPE GATE: choose which object tab(s) to work on (required)
python scripts/fetch_sheet.py --spreadsheet-id <SHEET_ID> --list-tabs

# 1–4: fetch + validate (gate) + generate + package  (scope = --tabs)
python scripts/run.py --spreadsheet-id <SHEET_ID> --tabs "輸入・入庫管理明細,成約"
#   (run.py refuses to run without --tabs unless you pass --all-tabs)

# ORG-SELECTION GATE: always pick the target from the connected orgs
python scripts/deploy.py --list-orgs

# 5a: check-only validation against the CHOSEN org (safe, no writes)
python scripts/deploy.py --target-org "ERP DEV 02"     # or --target-org 1 (list index)

# 5b: REAL deploy  — GATED: only after you type SHOOT
python scripts/deploy.py --start --target-org "ERP DEV 02" --test-level RunLocalTests
```

## Automatic JA → English labels (Translation Workbench)

The **same** `prep_deploy.py` command produces English object / Name / custom-field
labels, writes them to the live Data Dictionary (after one batch confirmation),
deploys `CustomObjectTranslation` (`en_US`), and verifies the exact English in
the org. There is no parallel translation command.

```bash
cd sf-deploy

# Preview (one tab or many — comma-separated, same as fetch_sheet / run.py)
python scripts/prep_deploy.py --org "ERPDEV01" --tabs "Deal,Shipping"

# After confirming the printed cell: old → new batch:
python scripts/prep_deploy.py --org "ERPDEV01" --tabs "Deal,Shipping" \
    --apply-translations --phase deploy
```

Provider order (one provider per batch):

1. DeepL MCP (`JA` → `EN-US`) when that server is configured and healthy.
2. Otherwise Google Sheets `=GOOGLETRANSLATE(...)` formulas; calculated values
   (not the formula text) are validated and deployed.

Sheet I/O is `fetch_sheet.py` / `write_back.py` via Google Workspace MCP
(`mcp-adaptor --server google_workspace`) — the same login Claude/Cursor already
uses. gcloud Application Default Credentials are not used.

Optional DeepL MCP: set `MCP_DEEPL_COMMAND` / `MCP_DEEPL_SERVER` / `MCP_DEEPL_URL`.
Google Translate formulas still run in the sheet when DeepL is not configured.
Force a provider with `--force-provider google|deepl`.

Provenance lives in one `Translation Provenance` column immediately to the
right of `Field Label (EN)` (`origin | ja-hash | generated-at`). It is stamped
on custom and standard field rows. Japanese source changes replace previous
English automatically.

### Incremental (only changed / new fields)

```bash
python scripts/fetch_sheet.py --spreadsheet-id <ID> --tabs "MyObject"
python scripts/validate_sheet.py --in temp_updates.json --json .build/validation_report.json
python scripts/generate_xml.py
python scripts/build_manifest.py --only MyObject__c        # package = just this object
python scripts/deploy.py --start --target-org "ERP DEV 02"
```

### Deletions

```bash
cat > deletions.json <<'JSON'
{ "CustomField": ["MyObject__c.Old_Field__c"] }
JSON
python scripts/build_manifest.py --destroy deletions.json
python scripts/deploy.py --start --pre-destructive manifest/destructiveChanges.xml --target-org "ERP DEV 02"
```

## Safety model

- **Org-selection gate.** Every deploy must target an explicitly-chosen
  connected org. `deploy.py` shows the org list and BLOCKS if `--target-org`
  isn't given — it never silently uses the sf default.
- **Validation is a hard gate.** `run.py` and `deploy.py` refuse to proceed if
  `validate_sheet.py` reports any `ERROR`.
- **Deploy is gated.** A real deploy (`deploy.py --start`) requires typing
  `SHOOT` — see `.cursor/rules/require-password-for-commits-and-deploys.mdc`.
- `deploy.py` with no `--start` is **check-only** and never writes to the org.

## Validation rules

Field-type rules live in `scripts/validate_sheet.py` and mirror
`../sheet-editor/context/FIELD_TYPE_VALIDATION_RULES.md` (+ the reconciled specs).
Refinements applied: `;` picklist delimiter, Checkbox `defaultValue` required,
MultiselectPicklist `visibleLines` required, MasterDetail `relationshipName`
required (Lookup optional), Formula Number/Currency/Percent require
`precision`+`scale`.
