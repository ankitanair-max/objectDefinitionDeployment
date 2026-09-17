# Translation Strategy — Objects and Fields (EN column + automatic delta)

**Status:** Implementation, revised 2026-09-17  
**Scope:** Custom objects and fields only.  
**Source of truth:** each object-definition tab on the live Google Sheet  
[Toray Object Definition Model Document](https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ/edit?gid=495247124#gid=495247124)  
(`1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ`) — **no separate catalog tab**.

Client decisions captured in this revision:

- English lives in **an additional column on each object-definition tab**, not a new sheet tab.
- Future fields added to that tab must be picked up **automatically by the deploy delta** — no separate manual translation pass per field add.

---

## Design Summary

Must keep Japanese as the **master** field/object label (today’s `Field Label` → `CustomField.label` / `CustomObject.label`). Must add **English (`en_US`)** as a Translation Workbench value generated from a new header-driven column on the same object tab.

Must emit **`CustomObjectTranslation`** (`<Obj>__c-en_US`).

Must fold translation into the **existing object/field deploy loop** (`sf-deploy-delta-and-blockers.mdc`). Whenever a field is new, the translation member is included in that same package automatically. Operators do not run a second “translation project” when they add a field.

---

## What to add on the object-definition sheet

Locate every column **by header name** (same as WIP / IsDelete / Field Label). Do not hard-code a column letter for logic. On the **20 objects filled 2026-09-17**, English was inserted as **column D**:

| Col | Header | Maps to |
|---|---|---|
| C | `Field Label` (JA, master) | `CustomField.label` — `generate_xml.py` must not change this |
| **D** | **`Field Label (EN)`** | `CustomObjectTranslation` field `<label>` |
| E | `fullName` (shifted right by the insert) | `Field API Name` |

Object-level English on those tabs is **D1**. Name-row English is column D on the `Name` row.

The pipeline matches the header `Field Label (EN)` (and JP `項目ラベル名 (EN)`). Column D is EN **only when that header is present**; untranslated tabs still have `fullName` in D and must not be read as English.

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

Translation is **Step 2c of every object/field deploy**, not a separate command the operator remembers. Same live-sheet + live-org discipline as field name-delta and `attr_drift.py`.

For each object in the deploy set:

1. Read the object tab **live**. Build:
   - `sheet_fields` = deployable custom fields (skip WIP, skip IsDelete, stop at `END[項目]`).
   - `sheet_en[api] = Field Label (EN)` (normalized).
   - `sheet_ja[api] = Field Label`.
2. Read the org **live** (Metadata API `readMetadata(CustomObjectTranslation)` for `<Obj>__c-en_US`, plus Tooling `CustomField` for field existence). If the translation file does not exist yet, treat every EN-filled field as NEW.
3. Classify:

| Code | When | Package? |
|---|---|---|
| `NEW_TRANSLATION` | Field is new in org **or** exists but has no `en_US` field translation, **and** col D EN is filled | **Yes — automatic** (this is the Japan-adds-a-field case) |
| `CHANGED_EN` | Org `en_US` label ≠ sheet `Field Label (EN)` | **Report only.** Do not auto-package (same as attr_drift — not a silent update). Redeploy only if the user explicitly asks. |
| `MISSING_TRANSLATION` | Deployable custom field, col D blank | WARN; field still deploys in Japanese; no fake EN |
| Org-only translation | Field translated in org, absent from sheet | Report; do not delete |

4. For packaged `NEW_TRANSLATION` rows: retrieve the org's existing `<Obj>__c-en_US` file, **merge the new field EN into it**, write the combined CustomObjectTranslation. A file that contained only the new field would untranslate every sibling.
5. Put `CustomObjectTranslation:<Obj>__c-en_US` in the **same** `package.xml` as that object’s field delta. One dry-run, one real deploy.

**When Japan adds a field later** (the object already has translations):

1. They add the row on the object tab: JA in `Field Label`, EN in **column D** (`Field Label (EN)`), API name in `fullName`.
2. Next `prep_deploy.py` for that tab: field name-delta creates the CustomField; `i18n_drift.py --new-only` marks only that row `NEW_TRANSLATION`.
3. `generate_object_translation.py` merges that one EN into the org COT and packages it. Existing translations are left as they are.

No extra spreadsheet, no extra “run translations” command. Blank col D on the new row → field still deploys; WARN `MISSING_TRANSLATION`.

---

## Cursor / pipeline rule (to implement — do not skip)

Layer onto `sf-deploy-delta-and-blockers.mdc` (and `prep_deploy.py` build step):

> After field name-delta and attr_drift, for every object in the request, compute CustomObjectTranslation delta from `Field Label (EN)` / `Object Label (EN)` / `Name Field Label (EN)`. Include those members in the same package. Never require a separate translation deploy. Never generate English. Blank EN = WARN, not a hard blocker, unless the client later sets ERROR.

Also extend `sf-sheet-columns.mdc`: `Field Label (EN)` is a **field-definition column**, header-driven, like `deleteConstraint`. Discussion columns stay ignored.

Scripts:

| Script | Role |
|---|---|
| `generate_object_translation.py` | From live rows → `<Obj>__c-en_US.objectTranslation-meta.xml` (retrieve-merge org file + new EN) |
| `i18n_drift.py` | Compare sheet EN vs org CustomObjectTranslation |
| `prep_deploy.py` | Always run translation delta for in-scope tabs (Step 4b) |

Salesforce note: `CustomObjectTranslation` is usually deployed as a **whole object-language file**. Practical approach: retrieve-or-rebuild the `en_US` file for that object from **all current sheet EN cells** (not only the new field), so adding one field merges into the existing translation file rather than wiping other fields. That is still “automatic delta” from the operator’s point of view: they only edit the new row.

Retrieve pairing: `CustomObject` + `CustomObjectTranslation` together.

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
4. Add the EN column(s) on ONE pilot object tab (gated sheet write)
5. Fill EN for that object's existing fields (human)
6. Generate CustomObjectTranslation; dry-run; deploy with that object
7. UAT: JP user vs EN user on the record page / related list
8. Wire Step 2c into prep_deploy so the NEXT field added on that tab
   is included automatically
9. Roll the column to the next object tabs the same way
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
| Rebuilding the whole translation file from a partial sheet | Always generate from **all** non-blank EN cells on that tab, merged with org translations for fields not on the sheet |
| Blank EN forgotten | Automatic WARN on every deploy for that object |
| Column letter assumed | Header-driven lookup only |

---

## Handoff to Build

- Add headers `Field Label (EN)`, `Object Label (EN)`, `Name Field Label (EN)` (gated).
- Enable Translation Workbench + English.
- `generate_object_translation.py` + drift compare vs live `CustomObjectTranslation`.
- Hook into `prep_deploy.py` / delta rule so new fields with EN are packaged automatically.
- Cursor rule text: translation delta is mandatory on every object field deploy.
- Do **not** add a Translation Catalog tab.

**Assumptions:** master = Japanese; first language = `en_US`; blank EN = WARN.

---

## Sources

- Live sheet: [Object Definition Model Document](https://docs.google.com/spreadsheets/d/1_TaxDe-Qxl8BAUmuZc01vUoxpBEPxJ4Opx4tEe8ulNQ/edit?gid=495247124#gid=495247124)
- Salesforce Developers: [CustomObjectTranslation](https://developer.salesforce.com/docs/atlas.en-us.api_meta.meta/api_meta/meta_customobjecttranslation.htm)
- This pipeline: `generate_xml.py` (`Field Label` → `<label>`), `attr_drift.py`, `sf-deploy-delta-and-blockers.mdc`, `sf-sheet-columns.mdc`
