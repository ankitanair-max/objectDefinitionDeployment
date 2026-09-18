# sf-deploy — Local Salesforce Data-Dictionary deployment

Local, Cursor-driven replacement for the old Google Apps Script + CI deployment
flow. Everything runs from **this** folder: connect your org here, deploy from
here.

```
sf-deploy/
├── scripts/
│   ├── prep_deploy.py      ★ THE canonical entry point (whole pipeline)
│   ├── fetch_sheet.py      1. Google Sheet → rows JSON            (your ADC creds)
│   ├── validate_sheet.py   2. rows JSON → PASS/FAIL log           (hard gate)
│   ├── org_snapshot.py     3. ONE bulk org read (existence + fields + translations)
│   ├── attr_drift.py       4. sheet definition vs org metadata, per existing object
│   ├── plan_deploy.py      5. the delta → .build/deploy_plan.json
│   ├── generate_xml.py     6. rows + plan → .build/staging/force-app/**
│   ├── generate_object_translation.py
│   │                       6b. patches the org's own CustomObjectTranslation tree
│   ├── build_manifest.py   7. plan → package.xml (planned members only)
│   ├── deploy.py           8. sf CLI deploy (check-only default)
│   ├── verify_deploy.py    9. live post-deploy verification (Tooling API)
│   ├── run.py              alias for `prep_deploy.py --phase build`
│   └── generate_*.py       layout / flexipage / permission-set generators (reused)
├── .build/
│   ├── org_snapshot.json   the single target-org read
│   ├── deploy_plan.json    the single source of deploy scope
│   └── staging/force-app/  STAGED generation (the tracked tree is never touched)
├── manifest/               destructiveChanges.xml for the delete flow
└── .build/                 disposable reports (validation_report.json, …)
```

## The canonical command

One command does the whole chain — including object translations. There is no
separate translation step and no second pipeline:

```bash
python scripts/prep_deploy.py --org "<TARGET_ORG>" --tabs "<OBJECT_TABS>" --phase deploy
```

```
live sheet fetch
  → static validation (hard gate)
  → one target-org snapshot          .build/org_snapshot.json
  → attribute drift (existing objects)
  → deployment plan / delta          .build/deploy_plan.json
  → staged metadata generation       .build/staging/force-app
  → one explicit manifest FROM THE PLAN
  → check-only deployment
  → real deployment
  → live post-deployment verification
```

What the delta packages:

| target | packaged |
|---|---|
| object missing in the org | the CustomObject + all deployable fields + all new translations |
| object exists | only fields the org does not have + their new translations |
| field already in the org | nothing — never redeployed silently |
| field definition changed (drift) | nothing, until you pass `--include-drift Obj__c.Field__c` |
| row flagged `WIP` | nothing — ignored entirely |
| row flagged `IsDelete` | the separate destructive flow (`build_destructive.py`) |

Re-running with no sheet changes is a no-op: the plan comes out empty, no
package is built and no org write is attempted. Use `--phase build` (the
default) for everything except the real deploy, and `--lang` to pick the
Translation Workbench language (`off` to skip translations).

## Prerequisites (one-time)

```bash
# Salesforce CLI (already present: /usr/local/bin/sf)
sf --version

# Python deps for the fetch step
pip3 install google-api-python-client google-auth

# Google auth — deploy/read the sheet AS YOU (not a service account)
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/drive.readonly

# Connect your Salesforce org
sf org login web --alias "ERP DEV 02"
sf config set target-org "ERP DEV 02"
```

## Everyday flow

```bash
cd "sf-deploy"

# SCOPE GATE: choose which object tab(s) to work on (required)
python scripts/fetch_sheet.py --spreadsheet-id <SHEET_ID> --list-tabs

# ORG-SELECTION GATE: always pick the target from the connected orgs
python scripts/deploy.py --list-orgs

# plan + validate + stage + dry-run (no org writes) — review the plan
python scripts/prep_deploy.py --org "ERP DEV 02" --tabs "輸入・入庫管理明細"
cat .build/deploy_plan.json

# REAL deploy of exactly that plan, then live verification
python scripts/prep_deploy.py --org "ERP DEV 02" --tabs "輸入・入庫管理明細" --phase deploy
```

The delta is automatic: only fields the org lacks are packaged, so an
"incremental" run is just the same command again. To redeploy a field whose
DEFINITION changed, review the reported drift and opt in explicitly:

```bash
python scripts/prep_deploy.py --org "ERP DEV 02" --tabs "MyObject" \
    --include-drift "MyObject__c.Changed_Field__c" --phase deploy
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
- **Validation is a hard gate.** The pipeline stops if `validate_sheet.py`
  reports any `ERROR`; nothing is generated and nothing is packaged.
- **Nothing is deployed that the plan does not name.** The manifest is built
  from `.build/deploy_plan.json`, not from a directory scan, so a stale file
  from an earlier run can never ride along.
- **Generation is staged.** Metadata is written under `.build/staging`, so a
  build never dirties the tracked source tree.
- **Existing fields are never silently redeployed.** Attribute drift is
  reported and needs an explicit `--include-drift` decision, because some type
  changes require delete+recreate and destroy the field's data.
- **The deploy log is never trusted alone.** `verify_deploy.py` re-checks every
  planned member live through the Tooling API (FLS-independent).
- **Translation state is recorded only after verification**, keyed by the org's
  immutable Id, so a failed deploy can never look "already deployed" and one
  sandbox's history can never mask another's.
- `deploy.py` with no `--start` is **check-only** and never writes to the org.

## Validation rules

Field-type rules live in `scripts/validate_sheet.py` and mirror
`../sheet-editor/context/FIELD_TYPE_VALIDATION_RULES.md` (+ the reconciled specs).
Refinements applied: `;` picklist delimiter, Checkbox `defaultValue` required,
MultiselectPicklist `visibleLines` required, MasterDetail `relationshipName`
required (Lookup optional), Formula Number/Currency/Percent require
`precision`+`scale`.
