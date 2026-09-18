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
│   ├── org_snapshot.py     3. org read (existence, fields, object metadata;
│   │                          COT only for tabs with Field Label (EN))
│   ├── attr_drift.py       library: sheet definition vs the snapshot's object
│   │                          metadata (also a standalone CLI for ad-hoc checks)
│   ├── plan_deploy.py      4. the delta + drift + manifest index → deploy_plan.json
│   ├── generate_xml.py     5. rows (+ optional plan) → force-app or staging
│   ├── generate_object_translation.py
│   │                       5b. patches the org's own CustomObjectTranslation tree
│   ├── build_manifest.py   6. plan or source scan → package.xml
│   ├── deploy.py           7. sf CLI deploy (check-only default)
│   ├── verify_deploy.py    8. live verification: Tooling CustomField + translations
│   ├── run.py              org-free sheet → package (fetch, validate, generate, manifest)
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
  → deployment plan / delta          .build/deploy_plan.json
      (attribute drift computed LOCALLY from that snapshot)
  → staged metadata generation       .build/staging/force-app
  → explicit manifest FROM THE PLAN  package.xml, or package.part1..N.xml
  → check-only deployment of every package
  → real deployment of every package, each verified before the next is sent
  → live verification of objects, fields AND packaged translations
```

One org read means one authentication: `org_snapshot.py` brings back existence,
existing field names, each object's CustomObject metadata and the translation
snapshot together, and the planner compares definitions locally — there is no
per-object drift call over the wire.

What the delta packages:

| target | packaged |
|---|---|
| object missing in the org | the CustomObject + all deployable fields + all new translations |
| object exists | only fields the org does not have + their new translations |
| field already in the org | nothing — never redeployed silently |
| field definition changed (drift) | nothing, until you pass `--include-drift Obj__c.Field__c` |
| English label changed (`Field Label (EN)` ≠ org) | the object's `CustomObjectTranslation` (label-only; not destructive) |
| English label unchanged | nothing |
| row flagged `WIP` | nothing — ignored entirely |
| row flagged `IsDelete` | the separate destructive flow — built every run, deployed only with `--deletes` |
| standard `Name` drift | the CustomObject (Obj__c.Name is not a CustomField member) |
| a new field needing object metadata | the CustomObject too (history tracking, Master-Detail sharing) — object metadata only, never the object's existing fields |

Re-running with no sheet changes is a no-op: the plan comes out empty, no
package is built and no org write is attempted. Use `--phase build` (the
default) for everything except the real deploy, and `--lang` to pick the
Translation Workbench language (`off` to skip translations). Pass `--new-only`
to report changed English labels without packaging them (new translations are
still packaged).

A plan larger than `--max-components` splits into `package.part1.xml` …
`package.partN.xml`; the plan records that index and **every part is deployed,
in order**, with each one verified in the org before the next is sent. A part
that fails, or that cannot be confirmed live, stops the run rather than leaving
the remaining parts unsent and unreported.

Fields flagged `IsDelete` never join the additive package. They are built into
`destructiveChanges.xml` on every run and reported, but the destructive deploy
only runs when you pass `--deletes`, because deleting a field also destroys its
data.

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

Rows flagged `IsDelete` on the sheet are picked up by the canonical command:
every run builds `destructiveChanges.xml` for them and prints the set, and the
destructive deploy runs only when you add `--deletes` (deleting a field also
destroys its data):

```bash
python scripts/prep_deploy.py --org "ERP DEV 02" --tabs "MyObject" \
    --phase deploy --deletes
```

For a one-off deletion that is not on the sheet:

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

## English translations (Translation Workbench)

English is part of the **same** `prep_deploy.py` command. There is no
`deploy-translations` / `translation-deploy` step. The live Data Dictionary is
the source of truth:

https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ

Locate English by the header **`Field Label (EN)`** (JP `項目ラベル名 (EN)`).
Japanese `Field Label` stays `CustomField.label`. Tabs without that header are
untranslated: the translation step is skipped entirely and a field deploy does
not need Translation Workbench.

| Sheet change | What the next `prep_deploy.py` does |
|---|---|
| New object tab with EN filled | packages the CustomObject, its fields, and `<Obj>__c-en_US` |
| New field row with EN filled | packages the CustomField **and** patches that EN into the org's CustomObjectTranslation |
| Existing field, EN filled for the first time | packages only the translation member (the field is already in the org) |
| EN cell edited | packages the translation delta (`CHANGED`) |
| EN unchanged / blank | nothing to translate (blank EN is a WARN, Japanese still deploys) |
| Duplicate EN rows, unparseable picklist EN, missing object/field id on an EN-filled row | validation **ERROR** — the deploy is blocked. The message names the object, the field, and the reason. |

Salesforce prerequisites: Setup → Translation Language Settings → enable
Translation Workbench and activate **English** (`en_US`). The running user
needs Metadata API access. If the org cannot serve translations, the command
fails with an actionable message (or continues field-only with
`--on-translation-unavailable skip`).

Re-running with no sheet/org translation changes is a no-op: the plan is empty
and no Translation Workbench write is attempted.

## Validation rules

Field-type rules live in `scripts/validate_sheet.py` and mirror
`../sheet-editor/context/FIELD_TYPE_VALIDATION_RULES.md` (+ the reconciled specs).
Refinements applied: `;` picklist delimiter, Checkbox `defaultValue` required,
MultiselectPicklist `visibleLines` required, MasterDetail `relationshipName`
required (Lookup optional), Formula Number/Currency/Percent require
`precision`+`scale`.
