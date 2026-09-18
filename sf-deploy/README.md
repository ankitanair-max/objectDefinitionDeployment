# sf-deploy — Local Salesforce Data-Dictionary deployment

Local, Cursor-driven replacement for the old Google Apps Script + CI deployment
flow. Everything runs from **this** folder: connect your org here, deploy from
here.

```
sf-deploy/
├── scripts/
│   ├── deploy.py           ★ THE deployment command (imports the stages below)
│   ├── fetch_sheet.py      1. Google Sheet → rows JSON            (your ADC creds)
│   ├── validate_sheet.py   2. rows JSON → PASS/FAIL log           (hard gate)
│   ├── org_snapshot.py     3. ONE bulk org read (existence, fields,
│   │                          CustomObject metadata, translations)
│   ├── attr_drift.py       4. sheet definition vs org metadata — pure, run
│   │                          locally against the snapshot
│   ├── plan_deploy.py      5. the delta → .build/deploy_plan.json
│   ├── generate_xml.py     6. rows + plan → .build/staging/force-app/**
│   ├── generate_object_translation.py
│   │                       6b. patches the org's own CustomObjectTranslation tree
│   ├── build_manifest.py   7. plan → package.xml (or package.part1..N.xml)
│   ├── sf_deployer.py      8. INTERNAL: one `sf` CLI deploy of one package
│   ├── verify_deploy.py    9. live verification (objects, fields, translations)
│   ├── build_destructive.py   the IsDelete → destructiveChanges.xml flow
│   ├── prep_deploy.py      deprecated shim → deploy.py
│   └── generate_*.py       layout / flexipage / permission-set generators (reused)
├── tests/                  pytest suite (org-free; two-sandbox test opt-in)
├── .build/
│   ├── org_snapshot.json   the single target-org read
│   ├── deploy_plan.json    the single source of deploy scope
│   └── staging/force-app/  STAGED generation (the tracked tree is never touched)
├── manifest/               destructiveChanges.xml for the delete flow
└── .build/                 disposable reports (validation_report.json, …)
```

## The deployment command

One command does the whole chain — including object translations. There is no
separate translation step and no second pipeline:

```bash
python scripts/deploy.py --org "<TARGET_ORG>" --tabs "<OBJECT_TABS>" --start
```

Three modes, one plan, so a dry run and the real deploy can never disagree
about scope:

| mode | what it does |
|---|---|
| `--plan` | delta only: fetch, validate, snapshot, plan. No files, no org writes. |
| *(default)* `--check-only` | additionally generate, package and validate every package against the org. Still no writes. |
| `--start` | the real deploy: every package part in order, each verified live. |

Add `--deletes` to also execute the IsDelete set (destructive — deleting a
field destroys its data).

```
live sheet fetch
  → static validation (hard gate)
  → one target-org snapshot          .build/org_snapshot.json
  → attribute drift (existing objects)
  → deployment plan / delta          .build/deploy_plan.json
  → staged metadata generation       .build/staging/force-app
  → one explicit manifest FROM THE PLAN   package.xml / package.part1..N.xml
  → check-only deployment of every part
  → real deployment of every part, each verified before the next
  → destructive deploy of the IsDelete set  (--deletes)
  → live verification: objects, fields AND translations
  → verified translation state + report refresh
```

What the delta packages:

| target | packaged |
|---|---|
| object missing in the org | the CustomObject + all deployable fields + all new translations |
| object exists | only fields the org does not have + their new translations |
| existing object whose new field needs object metadata (history tracking, Master-Detail) | the CustomObject too — never its existing fields |
| standard `Name` drift approved | the CustomObject (Name is not a CustomField member) |
| field already in the org | nothing — never redeployed silently |
| field definition changed (drift) | nothing, until you pass `--include-drift Obj__c.Field__c` |
| row flagged `WIP` | nothing — ignored entirely |
| row flagged `IsDelete` | the separate destructive flow (`build_destructive.py`) |

Re-running with no sheet changes is a no-op: the plan comes out empty, no
package is built and no org write is attempted. `--lang` picks the Translation
Workbench language (`off` skips translations). A plan larger than
`--max-components` (default 9000) splits into `package.part1..N.xml`, and
**every part is deployed, in order** — each verified before the next is sent.

The deep technical contract for translations (patch-never-rebuild, the standard
`Name` field, the delta codes) is in
[`../docs/TRANSLATION_STRATEGY.md`](../docs/TRANSLATION_STRATEGY.md).

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
python scripts/sf_deployer.py --list-orgs

# the delta only — no files built, no org writes
python scripts/deploy.py --org "ERP DEV 02" --tabs "輸入・入庫管理明細" --plan
cat .build/deploy_plan.json

# build + validate every package against the org (still no writes)
python scripts/deploy.py --org "ERP DEV 02" --tabs "輸入・入庫管理明細"

# REAL deploy of exactly that plan, then live verification
python scripts/deploy.py --org "ERP DEV 02" --tabs "輸入・入庫管理明細" --start
```

The delta is automatic: only fields the org lacks are packaged, so an
"incremental" run is just the same command again. To redeploy a field whose
DEFINITION changed, review the reported drift and opt in explicitly:

```bash
python scripts/deploy.py --org "ERP DEV 02" --tabs "MyObject" \
    --include-drift "MyObject__c.Changed_Field__c" --start
```

### Deletions (the `IsDelete` column)

Rows flagged `IsDelete` never enter the additive package. The command builds
them into `destructiveChanges.xml` and reports them on every run; the
destructive deploy itself only happens when you ask for it, because deleting a
field also destroys its data:

```bash
python scripts/deploy.py --org "ERP DEV 02" --tabs "MyObject" --start --deletes
```

## Safety model

- **Org-selection gate.** Every deploy must target an explicitly-chosen
  connected org; the target org's immutable Id is recorded in the plan.
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
- **Every package part is deployed.** A split plan records its manifest index
  in the plan; the command iterates all parts and stops if one does not land,
  rather than leaving parts 2..N behind.
- **Translations are verified too.** A translation-only deploy is confirmed by
  reading `CustomObjectTranslation` back from the org before any sync state is
  written.
- `deploy.py` with no `--start` is **check-only** and never writes to the org.

## Tests

```bash
python3 -m pytest tests -q          # org-free: plan, manifest, verification
SEAP_TEST_ORG_A=<alias> SEAP_TEST_ORG_B=<alias> SEAP_TEST_TABS="<tab>" \
    python3 -m pytest tests/test_sandbox_integration.py -v   # check-only, 2 sandboxes
```

## Validation rules

Field-type rules live in `scripts/validate_sheet.py` and mirror
`../sheet-editor/context/FIELD_TYPE_VALIDATION_RULES.md` (+ the reconciled specs).
Refinements applied: `;` picklist delimiter, Checkbox `defaultValue` required,
MultiselectPicklist `visibleLines` required, MasterDetail `relationshipName`
required (Lookup optional), Formula Number/Currency/Percent require
`precision`+`scale`.
