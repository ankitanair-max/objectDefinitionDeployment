# Translation Strategy — Objects and Fields (EN column + automatic delta)

**Status:** Implementation, revised 2026-09-18  
**Scope:** Custom objects and fields only.  
**Source of truth:** each object-definition tab on the live Google Sheet  
[Toray Object Definition Model Document](https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ/edit?gid=495247124#gid=495247124)  
(`1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ`) — **no separate catalog tab**.

Client decisions captured in this revision:

- English lives in **an additional column on each object-definition tab**, not a new sheet tab. Locate it by header **`Field Label (EN)`**, never by a fixed letter.
- Future fields added to that tab must be picked up **automatically by the deploy delta** — no separate manual translation pass per field add.

---

## Design Summary

Must keep Japanese as the **master** field/object label (today’s `Field Label` → `CustomField.label` / `CustomObject.label`). Must add **English (`en_US`)** as a Translation Workbench value generated from a new header-driven column on the same object tab.

Must emit **`CustomObjectTranslation`** (`<Obj>__c-en_US`).

Must fold translation into the **existing object/field deploy loop** (`sf-deploy-delta-and-blockers.mdc`). Whenever a field is new, the translation member is included in that same package automatically. Operators do not run a second “translation project” when they add a field.

---

## What to add on the object-definition sheet

Locate every column **by header name** (same as WIP / IsDelete / Field Label / `fullName`). Do not hard-code a column letter for logic.

| Header | Maps to |
|---|---|
| `Field Label` (JA, master) | `CustomField.label` — `generate_xml.py` must not change this |
| `fullName` | `Field API Name` |
| **`Field Label (EN)`** / `項目ラベル名 (EN)` | `CustomObjectTranslation` field `<label>` |

The pipeline matches the header `Field Label (EN)` (and JP `項目ラベル名 (EN)`). Untranslated tabs do not have that header — do **not** read `fullName` as English.

Rules for the EN cells:

- Blank EN on a deployable (non-WIP, non-IsDelete) custom field → `MISSING_TRANSLATION`. Default = **WARN**: still deploy the field in Japanese; do **not** invent English. Flip to ERROR only when the client requires EN before go-live.
- Filled EN → include that field in the `CustomObjectTranslation` delta automatically.
- WIP rows: ignore (same as field deploy).
- `IsDelete=TRUE`: exclude from create/update translation; if the field is deleted, the translation goes with the field (no extra EN cleanup).
- Standard fields (`Name`, `OwnerId`, …): do not emit `CustomField` XML; `Name` EN is only via `Name Field Label (EN)` on the object translation.
- Do **not** auto-machine-translate blank EN cells.

`generate_xml.py` must **not** write `Field Label (EN)` into `CustomField.label`. Master stays Japanese.

---

## Automatic delta (this is the “rule”)

Translation is **part of every object/field deploy** (`deploy.py`, computed in the deployment plan), not a separate command the operator remembers. Same live-sheet + live-org discipline as field name-delta and `attr_drift.py`.

For each object in the deploy set:

1. Read the object tab **live**. Build:
   - `sheet_fields` = deployable custom fields (skip WIP, skip IsDelete, stop at `END[項目]`).
   - `sheet_en[api] = Field Label (EN)` (normalized).
   - `sheet_ja[api] = Field Label`.
2. Read the org **live** (Metadata API `readMetadata(CustomObjectTranslation)` for `<Obj>__c-en_US`, plus Tooling `CustomField` for field existence). If the translation file does not exist yet, treat every EN-filled field as NEW.
3. Classify:

| Code | When | Package? |
|---|---|---|
| `NEW_TRANSLATION` | Field is new in org **or** exists but has no `en_US` field translation, **and** `Field Label (EN)` is filled | **Yes — automatic** (this is the Japan-adds-a-field case) |
| `CHANGED_EN` | Org `en_US` label ≠ sheet `Field Label (EN)` | **Report only.** Do not auto-package (same as attr_drift — not a silent update). Redeploy only if the user explicitly asks. |
| `MISSING_TRANSLATION` | Deployable custom field, `Field Label (EN)` blank | WARN; field still deploys in Japanese; no fake EN |
| Org-only translation | Field translated in org, absent from sheet | Report; do not delete |

4. For packaged `NEW_TRANSLATION` rows: retrieve the org's existing `<Obj>__c-en_US` file, **merge the new field EN into it**, write the combined CustomObjectTranslation. A file that contained only the new field would untranslate every sibling.
5. Put `CustomObjectTranslation:<Obj>__c-en_US` in the **same** `package.xml` as that object’s field delta. One dry-run, one real deploy.

**When Japan adds a field later** (the object already has translations):

1. They add the row on the object tab: JA in `Field Label`, EN in **`Field Label (EN)`**, API name in `fullName`.
2. Next `deploy.py` for that tab: the field delta creates the CustomField and the plan marks only that row `NEW_TRANSLATION`.
3. `generate_object_translation.py` merges that one EN into the org COT and packages it. Existing translations are left as they are.

No extra spreadsheet, no extra “run translations” command. Blank `Field Label (EN)` on the new row → field still deploys; WARN `MISSING_TRANSLATION`.

---

## Pipeline rule (implemented)

This file is the CANONICAL technical contract for object/field translations.
The operator-facing command and pipeline overview live in
[`../sf-deploy/README.md`](../sf-deploy/README.md); the standing rules
(`sf-object-translation-deploy.mdc`, `sf-deploy-delta-and-blockers.mdc`,
`sf-sheet-columns.mdc`) point here rather than restating it.

Translations are computed inside the deployment plan (`plan_deploy.py`) and
ride in the SAME package as the fields — there is no separate translation
step and no second org read.

> After field name-delta and attr_drift, for every object in the request, compute CustomObjectTranslation delta from `Field Label (EN)` / `Object Label (EN)` / `Name Field Label (EN)`. Include those members in the same package. Never require a separate translation deploy. Never generate English. Blank EN = WARN, not a hard blocker, unless the client later sets ERROR.

Scripts:

| Script | Role |
|---|---|
| `translation_lib.py` | Shared hash / classify / sheet-row → catalog (no CLI) |
| `org_snapshot.py` | ONE bulk `readMetadata` of every object's `CustomObjectTranslation`; the raw record XML is kept |
| `plan_deploy.py` | Classifies the sheet's EN against that snapshot and writes the translation members into `.build/deploy_plan.json` |
| `generate_object_translation.py` | PATCHES the org's own translation tree with the planned entries (reads the snapshot, never the org) |
| `deploy.py` | Packages, deploys and VERIFIES the translation alongside the fields |

Salesforce note: `CustomObjectTranslation` deploys as a **whole object-language
file**, so anything missing from the file we write is ERASED in the org. The
generator therefore PATCHES the org's own element tree — `recordTypes`,
`layouts`, `validationRules`, `fieldSets`, `quickActions`, `webLinks`,
`sharingReasons`, `workflowTasks`, `gender`/`startsWith` and the plural/case
`caseValues` are written back verbatim, and only the nodes the plan marks new
are touched. Never rebuild the file from a reduced model of field labels.

The standard `Name` field is the parent `<nameFieldLabel>`, not a
`Name.fieldTranslation-meta.xml`.

After the deploy, `verify_deploy.py` reads `CustomObjectTranslation` back from
the org and confirms every packaged entry's value before the sync state (keyed
by the immutable org Id) is recorded.

---

## Scope

- `TI_Fnt_*` (and any other in-scope) object tabs: object label, Name label, custom field labels → `en_US` CustomObjectTranslation.
- Automatic inclusion on every subsequent field add/deploy.
- Translation Workbench enabled; English Active.

**Out of scope**

- A `Translation Catalog` tab.
- Translating record **data**.
- Machine-filling blank EN.

---

## Enablement

| Setting | Where | Why |
|---|---|---|
| Translation Workbench | Setup → Translation Language Settings → Enable | Object translations will not retrieve/deploy without this |
| English Active | Same page | `en_US` members |
| User Language = English | Test user | UAT of record pages |

---

## How to start (objects and fields only)

```
1. Confirm column header text with sheet owner: Field Label (EN)
   (+ Object Label (EN), Name Field Label (EN) in the object-meta block)
2. Enable Translation Workbench; activate English
3. Retrieve one object's CustomObjectTranslation (likely missing) — baseline
4. Ensure the EN header exists on the object tab (gated sheet write if adding)
5. Fill EN for that object's existing fields (human) under Field Label (EN)
6. python scripts/deploy.py --org <ORG> --tabs "<tab>" --start  (translations are automatic)
7. UAT: JP user vs EN user on the record page / related list
8. Next field on that tab: JA + EN + fullName on the new row; one normal deploy
```

---

## Channel routing

| String | Sheet cell | Org metadata |
|---|---|---|
| Field label (JA, master) | `Field Label` | `CustomField.label` (existing `generate_xml.py`) |
| Field label (EN) | `Field Label (EN)` | `CustomObjectTranslation` field label |
| Object label (JA) | `Object Label` | `CustomObject.label` |
| Object label (EN) | `Object Label (EN)` | `CustomObjectTranslation` |
| Name (JA / EN) | Name Field Label / Name Field Label (EN) | `nameField` / object translation |

---

## Test

| Test | Must prove |
|---|---|
| EN user on record page | Field labels show `Field Label (EN)` |
| JP user on same page | Field labels still show `Field Label` |
| New field added later | Fill JA + EN on the new row; one normal deploy lands field **and** EN label — no extra process |
| New field, EN left blank | Field deploys; WARN MISSING_TRANSLATION; UI stays Japanese for EN users for that field only |
| EN cell edited | Reported as CHANGED_EN; not auto-packaged (`--new-only`) |
| WIP / IsDelete | No translation create |

---

## Risks

| Risk | Mitigation |
|---|---|
| Rebuilding the whole translation file from a partial sheet | The generator patches the org's own tree; unmentioned nodes are written back verbatim |
| Blank EN forgotten | Automatic WARN on every deploy for that object |
| Column letter assumed | Header-driven lookup only (`Field Label (EN)` / `fullName`) |

---

## Handoff to Build

- Headers `Field Label (EN)`, `Object Label (EN)`, `Name Field Label (EN)` (gated).
- Enable Translation Workbench + English.
- `plan_deploy.py` classifies EN against the org snapshot; `generate_object_translation.py` patches the org's tree.
- `deploy.py` packages new fields with their EN automatically and verifies both live.
- Standing rules: `sf-object-translation-deploy.mdc`, plus Step 3c and `Field Label (EN)` on the deploy and sheet-column rules.
- Do **not** add a Translation Catalog tab.

**Assumptions:** master = Japanese; first language = `en_US`; blank EN = WARN.

---

## Sources

- Live sheet: [Object Definition Model Document](https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ/edit?gid=495247124#gid=495247124)
- Salesforce Developers: [CustomObjectTranslation](https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_customobjecttranslation.htm)
- This pipeline: `deploy.py`, `org_snapshot.py`, `plan_deploy.py`, `generate_object_translation.py`, `generate_xml.py` (`Field Label` → `<label>`), `attr_drift.py`
