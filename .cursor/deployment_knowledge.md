# Deployment Knowledge Base (Self-Learning Memory)

Append-only log of deployment/validation failures, their root causes, the fixes
applied, and the prevention rule added so the same failure never recurs. Newest
entries on top. Written by the self-correction loop (see
`.cursor/rules/sf-deploy-self-correction.mdc`).

## Entry format (copy for each new lesson)

```
### [YYYY-MM-DD] <short title>
- **Error signature:** <error code / message / component failure text>
- **Command:** <the command that failed>
- **Component:** <metadata type> <API name>  (<file>:<line> if known)
- **Category:** Schema | Dependency | Order-of-Execution | Environment/Org | Tooling
- **Root cause:** <one-paragraph diagnosis>
- **Fix applied:** <script/file changed + what changed>
- **Prevention added:** <validate_sheet.py check / generator guard / manifest ordering>
- **Status:** Resolved | Monitoring
```

---

## Lessons

### [2026-09-21] Existing-field EN/provenance edit skipped by name-delta
- **Error signature:** sheet `TI_Fnt_MoveInDestination__c` EN `Delivery destination` (google provenance) while org still had `Move-in destination`; first `--phase deploy` reported complete because the field already existed
- **Command:** `python scripts/prep_deploy.py --org ERPDEV01 --tabs ShipoutMovein --phase deploy`
- **Component:** CustomObjectTranslation `TI_Fnt_ShipoutMovein__c-en_US` / `TI_Fnt_MoveInDestination__c`
- **Category:** Tooling
- **Extracted failure lines:**
    sheet EN/provenance changed; field not in schema delta; verify only checked packaged rows
- **Root cause:** Schema delta is name-only (`sheet fields − org fields`). An existing field whose English or provenance changed is excluded from the create package. Translation classify can pick `CHANGED_TRANSLATION`, but (1) that delta was not printed next to the schema delta, (2) `verify_deploy` only compared packaged rows, (3) an empty plan after a later EN edit still reported success, (4) sync state stored only packaged labels so a Google overwrite on an already-translated field was easy to miss if completeness was inferred from field existence.
- **Fix applied:** `classify_against_org` also diffs sheet EN against last deployed sync-state hash; `print_delta` always prints TRANSLATIONS new/changed; `verify_deploy` compares every in-scope sheet EN to the live org; empty plans still run that verify; sync state stores every verified label.
- **Prevention added:** STEP 2 §3c in `sf-deploy-delta-and-blockers.mdc` — translation EN/provenance drift is a mandatory delta, independent of field name-existence.
- **Status:** Resolved

### [2026-09-21] Empty .sfhome shim → false missing-referenceTo ERROR
- **Error signature:** `dependency.ref.org: referenceTo 'TI_Fnt_Shipping__c' does NOT exist in the target org and is not created in this deploy` while EntityDefinition live shows the object present
- **Command:** `python scripts/prep_deploy.py --org ERPDEV01 --tabs ShipoutMovein --phase deploy`
- **Component:** `validate_sheet.py` `query_org_objects` / env `HOME=.sfhome`
- **Category:** Environment/Org
- **Extracted failure lines:**
    ❌ [TI_Fnt_ShipoutMovein__c::TI_Fnt_Shipping__c] (dependency.ref.org) referenceTo 'TI_Fnt_Shipping__c' does NOT exist in the target org and is not created in this deploy
    NamedOrgNotFoundError: No authorization information found for ERPDEV01
- **Root cause:** `prep_deploy.py` ran `validate_sheet.py` with `HOME=.sfhome`. That shim has empty `.sfdx/alias.json` (`{"orgs":{}}`), so `sf data query --target-org ERPDEV01` returns status=2 JSON with no `result.records`. `query_org_objects` treated that as a successful empty set, so every custom `referenceTo` looked missing. The parent object actually exists in ERPDEV01.
- **Fix applied:** `query_org_objects` returns None (skip live check, fail-open) when `sf` JSON `status != 0`. `prep_deploy.py` `--sf-home` defaults to the real HOME so org aliases resolve.
- **Prevention added:** non-zero `sf` query status is never interpreted as "object absent".
- **Status:** Resolved

### [2026-09-21] nameFieldLabel deploy Succeeds but does not apply without nonempty caseValues
- **Error signature:** live verify `Name: org '' != sheet 'Shipping origin / move-in destination'` after `sf project deploy start` Status: Succeeded for `CustomObjectTranslation TI_Fnt_ShipoutMovein__c-en_US`
- **Command:** `python scripts/prep_deploy.py --org ERPDEV01 --tabs ShipoutMovein --phase deploy`
- **Component:** CustomObjectTranslation `TI_Fnt_ShipoutMovein__c-en_US` / `<nameFieldLabel>`
- **Category:** Schema
- **Extracted failure lines:**
    ✗ Name: org '' != sheet 'Shipping origin / move-in destination'
    VERIFICATION FAILED — the deploy did NOT fully land.
- **Root cause:** Salesforce Metadata API docs: when deploying a change to `nameFieldLabel`, the payload MUST include at least one top-level `<caseValues>` entry with a nonempty `<value>`. Otherwise the deploy reports Succeeded but the Name translation is not applied. The generator emitted only `<nameFieldLabel>` (no Object Label EN on this tab, org caseValues were comment-only `<!-- 日本語 -->`). Custom field labels landed; Name did not. `findtext("nameFieldLabel")` then correctly read empty text from the org.
- **Fix applied:** `generate_object_translation.py` always emits nonempty top-level `caseValues` when packaging `nameFieldLabel`, using (1) sheet Object Label EN, else (2) org comment / sheet Japanese object master as a carrier so we do not invent object English from AJ1, else (3) the Name English as last resort. Guard raises if Name is packaged with blank caseValues. `read_metadata` keeps the raw SOAP fragment so comment-only labels can be used as that carrier without being treated as live English.
- **Prevention added:** generator hard-fail if `nameFieldLabel` would be written without a nonempty `caseValues` value — the same silent Salesforce no-op cannot be packaged again.
- **Status:** Resolved

### [2026-09-21] CustomFieldTranslation for standard Name → Cannot translate standard field
- **Error signature:** `CustomObjectTranslation TI_Fnt_ShipoutMovein__c-en_US: Cannot translate standard field: TI_Fnt_ShipoutMovein__c.Name`
- **Command:** `python scripts/prep_deploy.py --org ERPDEV01 --tabs ShipoutMovein` (check-only)
- **Component:** CustomObjectTranslation `TI_Fnt_ShipoutMovein__c-en_US` / `Name.fieldTranslation-meta.xml`
- **Category:** Schema
- **Extracted failure lines:**
    │ CustomObjectTranslation │ TI_Fnt_ShipoutMovein__c-en_US │ Cannot translate standard field: TI_Fnt_ShipoutMovein__c.Name (4:13) │ 4:13        │
- **Root cause:** Salesforce translates the object's standard Name via `<nameFieldLabel>` on CustomObjectTranslation. Emitting `Name.fieldTranslation-meta.xml` (CustomFieldTranslation) is rejected as translating a standard field.
- **Fix applied:** `generate_object_translation.py` writes Name English only to `<nameFieldLabel>`, never as a field translation file (and deletes a leftover `Name.fieldTranslation-meta.xml`).
- **Prevention added:** generator skip for `name == "Name"` in both org-field copy and field-file emit, so the illegal file cannot be packaged again.
- **Status:** Resolved

### [2026-09-21] Missing sfdx-project.json → InvalidProjectWorkspaceError on dry-run
- **Error signature:** `Error (InvalidProjectWorkspaceError): …/sf-deploy does not contain a valid Salesforce DX project.`
- **Command:** `python scripts/prep_deploy.py --org ERPDEV01 --tabs ShipoutMovein` → `sf project deploy start --manifest manifest/package.xml --dry-run`
- **Component:** Tooling / project workspace (`sfdx-project.json`), not a metadata member
- **Category:** Tooling
- **Extracted failure lines:**
    Error (InvalidProjectWorkspaceError): /Users/ankita.nair/Documents/objectDefinitionDeployment-main/sf-deploy does not contain a valid Salesforce DX project.
- **Root cause:** `sf project deploy` requires a DX project file in the cwd. README listed `sf-deploy/sfdx-project.json` but the file was never in the repo, so check-only failed before any metadata was sent.
- **Fix applied:** added `sf-deploy/sfdx-project.json` (`packageDirectories.path = force-app`, `sourceApiVersion` 60.0, matching `build_manifest.py` default).
- **Prevention added:** keep `sf-deploy/sfdx-project.json` in the repo. `sf project deploy` already errors if it is missing.
- **Status:** Resolved

### [2026-09-11] FLS grant was DELTA/local-file scoped → historical gaps never backfilled (106 fields across 11 objects)
- **Error signature:** Yagai report + live audit — fields deployed in earlier batches lacked `FieldPermissions` on `SalesFrontAdmin` even though later deploys "granted FLS". True classified gap: **106** deployable custom fields with no FLS (e.g. ExportImportRelatedInfo 36, Shipping 33, ShippingDetail 14, DeliveryDestination 9, IncidentalExpenses/StandaloneIE 4 each).
- **Command:** N/A (post-deploy access gap). Surfaced via live `CustomField` (Tooling) vs `FieldPermissions` diff, then classified via `readMetadata(CustomObject)`.
- **Component:** `FieldPermissions` on PermissionSet `SalesFrontAdmin`, across TI_Fnt_ objects.
- **Category:** Order-of-Execution (grant scoped to the wrong input set).
- **Root cause:** `grant_fls.py` granted from the LOCALLY-GENERATED field-meta files of the CURRENT deploy (`force-app/main/default/objects/<Obj>/fields/*.xml`) — i.e. only the delta being deployed. Fields created in a PRIOR batch were never in the current local dir, so the grant never looked at them; if their original grant was incomplete (partial batch error, required→optional flip, or the pre-gate era), the gap was permanent because nothing ever reconciled against the org's FULL live field set. Delta-scoping = no self-healing. NOTE the raw "no FLS" counts (Deal 66, Shipping 52…) were INFLATED by required/MasterDetail/already-granted-readonly fields that legitimately cannot/do not need a new FieldPermissions row — the real actionable gap is smaller (Deal was actually 0) and only visible after live classification.
- **Fix applied:** rewrote `grant_fls.py` to be **LIVE-DRIVEN**: for each object it reads the org's full custom-field set via `readMetadata(CustomObject)` (FLS-independent), classifies each field (required/MD → skip; AutoNumber/Summary/Formula → read-only; else read+edit), diffs against existing `FieldPermissions`, and inserts only the MISSING ones (idempotent, non-destructive — existing/manual read-only untouched). Added `--all-tifnt` for a whole-project reconciliation and a live re-verify per object. Backfill run: **106 inserted, 0 errors, 0 still-missing.**
- **Prevention added:** live-driven `grant_fls.py` (no longer depends on local delta files) — GATE 1 now reconciles the FULL live field set every run, so any historical or future FLS gap self-heals on the next grant. Rule `sf-post-deploy-fls-and-flexipage.mdc` GATE 1 updated to mandate the live-driven helper.
- **Status:** Resolved.

### [2026-09-09] Field FLS granted but OBJECT permissions never granted → object unreachable (ALL 31 TI_Fnt_ objects)
- **Error signature:** user report — cannot open the `容器証明書` (ContainerCertificate) tab/object; "insufficient privileges". Live audit: `ObjectPermissions` on `SalesFrontAdmin` = NONE for **0/31** deployed `TI_Fnt_` objects, despite `FieldPermissions` (FLS) being present.
- **Command:** N/A (post-deploy access gap, not a deploy-command failure). Surfaced via ObjectPermissions SOQL on `SalesFrontAdmin`.
- **Component:** `ObjectPermissions` on PermissionSet `SalesFrontAdmin` for all 31 `TI_Fnt_*__c` objects.
- **Category:** Dependency (access control) / Order-of-Execution (missing pipeline gate).
- **Root cause:** the post-deploy pipeline auto-granted FIELD-level security (GATE 1, `FieldPermissions`) but never granted OBJECT-level permissions (`ObjectPermissions` CRUD). FLS only controls which fields are visible on a record the user can already reach; with no `ObjectPermissions` row the user cannot open/list/create the record at all, so the object (and its tab) is completely inaccessible even though every field is "granted". No gate existed to catch this, so it silently affected the whole project.
- **Fix applied:** (1) immediate — built `scripts/grant_object_perms.py` (idempotent CRUD grant + live verify; supports `--all-tifnt`) and granted Read+Create+Edit+Delete on `SalesFrontAdmin` for all 31 objects. 2 junction objects (`RecevingAndQuoteAndWorkRelation`, `ShippingAndQuoteAndWorkRelation`) failed with `FIELD_INTEGRITY_EXCEPTION: Permission Read <child> depends on permission(s): Read TI_Logi_QuotationAndWork__c` — granted the Master parent's Read first (minimal, cross-namespace), then the children. Final live verify: **31/31** have full R/C/E/D. (2) pipeline — added **GATE 1c** to `sf-post-deploy-fls-and-flexipage.mdc`: object-level CRUD auto-grant on `SalesFrontAdmin` is now MANDATORY + AUTOMATIC for every deployed object, with the Master-Detail-parent-Read dependency handling and mandatory live verification.
- **Prevention added:** GATE 1c in `sf-post-deploy-fls-and-flexipage.mdc` (mandatory object-perm auto-grant + live verify, MD-parent dependency handling) and the reusable `scripts/grant_object_perms.py` helper. A deploy is no longer "done" while any deployed object shows `ObjectPermissions = NONE`.
- **Status:** Resolved.

### [2026-09-09] Bare field-derived relationshipName collides on a SHARED parent (User) → object-scope it
- **Error signature:** dry-run Component Failure — `CustomField TI_Fnt_ExportControlClassification__c.TI_Fnt_SalesDeliveryInCharge__c: There is already a Child Relationship named TI_Fnt_SalesDeliveryInCharge on User. (140:13)`
- **Command:** `deploy.py --package manifest/ecc_package.xml` (check-only), deploying `TI_Fnt_ExportControlClassification__c` + 21 fields.
- **Component:** CustomField `TI_Fnt_SalesDeliveryInCharge__c` (Lookup → User); also `TI_Fnt_SalesRepresentative__c`.
- **Category:** Dependency (metadata uniqueness)
- **Root cause:** `fill_relationship_name.py` derived the relationshipName as the field API name minus `__c`
  (`TI_Fnt_SalesDeliveryInCharge`). Its docstring assumed the namespace made that globally unique, but a
  **child-relationship name must be unique across ALL child objects that look up the same parent**. `User` is a
  shared parent with lookups from many objects; another object already had a `TI_Fnt_SalesDeliveryInCharge__c`
  → same derived child-relationship name → collision.
- **Fix applied:** (1) immediate — changed the two User-lookup relationshipNames to object-scoped values on the
  sheet (col X, gated) + AH `[FIX]`: `ExpControlCls_SalesDeliveryInCharge` / `ExpControlCls_SalesRepresentative`;
  redeploy passed. (2) producer — `fill_relationship_name.py` now OBJECT-SCOPES the generated name whenever the
  parent is a SHARED standard object (`SHARED_PARENTS`: User/Account/Contact/Lead/Group/Case/Opportunity/…): it
  prefixes a short acronym of the owning object (capitals, ≥3, else trimmed component), e.g.
  `ExportControlClassification → ECC_SalesDeliveryInCharge` (capped 40). Custom parents keep the namespaced bare
  name (cross-object collision there is unlikely and namespaced).
- **Prevention added:** the `SHARED_PARENTS` object-scoping in `fill_relationship_name.py::rel_name` (the producer
  that fills blank relationship names) — blank rel names on shared-parent lookups can no longer be auto-filled
  with a collision-prone bare name.
- **Status:** Resolved.

### [2026-09-09] New object's Name field = AutoNumber with a BLANK display format → object cannot deploy (PARK, do not guess a format)
- **Error signature:** pre-deploy blocker (would otherwise fail Metadata deploy). New object
  `TI_Fnt_ExportControlClassification__c` declared its standard `Name` field as **AutoNumber** in the object-meta
  block but left the **display format blank**. Salesforce REJECTS an AutoNumber Name field with no `displayFormat`
  (a CustomObject cannot be created/updated without a valid `<nameField>`), so the whole object is un-deployable
  until a format is supplied.
- **Command:** STEP-0 blocker scan for the 4 new objects (`AppendedTableMaster`, `ExportControlClassification`,
  `ContainerCertificate`, `ConditionsPerformanceReport`); caught before packaging.
- **Component:** CustomObject `<nameField>` (`type=AutoNumber`, `displayFormat` empty) for
  `TI_Fnt_ExportControlClassification__c`.
- **Category:** Schema.
- **Root cause:** the sheet's object-meta `Name Field Type` = `Autonumber` but `Name Field Display Format` is
  empty. AutoNumber requires a format string (e.g. `ECC-{0000000000}`). The format is a client/business decision
  (prefix + width), so it must NOT be auto-invented by the tool — a wrong format would permanently mis-number
  every record.
- **Fix applied (initial):** did NOT deploy the object. Parked it: coupled `flag_blocker.py` note on the tab (AH
  note + Col-A pink highlight on the object's lead field row) recording "OBJECT PARKED — AutoNumber Name field has
  a blank display format; pending Yagai-san". The other 3 objects deployed normally.
- **Update (2026-09-09, later, per explicit user instruction):** user directed us to GENERATE an interim
  AutoNumber format and proceed with the deploy now (rather than keep it parked), while still informing Yagai. We
  set `displayFormat = ECC-{0000000000}` (ECC = Export Control Classification, matching the sibling
  `CPR-{0000000000}` convention: 3-letter prefix + 10-digit zero-padded counter). NOTE: the client had typed the
  same string into col G (the DESCRIPTION column, `データ型に応じて…`), which the pipeline deliberately ignores;
  the real value column is col H (`数式(設定値)`), which was blank — so the format was written to **H12** (the Name
  row's 数式設定値 cell), the cell `fetch_sheet` actually reads. Object then deployed + verified.
- **⚠️ OPEN ACTION — INFORM YAGAI-SAN (Takeaki Yagai, sheet owner):** the `ECC-{0000000000}` format is an
  INTERIM assistant-generated numbering scheme, NOT client-confirmed. Yagai must confirm the prefix + digit width
  (changing an AutoNumber format later renumbers/relabels future records; existing record Names already generated
  are NOT retroactively changed). Keep this reminder until Yagai confirms or supplies the final format.
- **Prevention added:** RULE — **when a new/updated object's Name field is AutoNumber with a blank display
  format, treat it as a HARD Step-0 blocker: PARK the object, flag it via `flag_blocker.py` (AH + Col-A), and ask
  the sheet owner for the format. NEVER auto-generate/guess an AutoNumber display format on your own initiative**
  (unlike a blank formula body, which IS auto-filled with a dummy — a Name numbering scheme is not a safe thing to
  fabricate). The interim-format exception above was a DELIBERATE, explicit user override for this one object and
  does NOT relax the default rule. Codified in `.cursor/rules/sf-object-name-autonumber-format.mdc`.
- **Status:** Monitoring (deployed with interim format; awaiting Yagai-san's confirmation of the numbering scheme).

### [2026-09-09] Drift check skipped the standard Name field — Text-vs-AutoNumber drift went undetected
- **Error signature:** no deploy error — a SILENT data-correctness miss. `attr_drift.py` reported
  `drifted: 0` for `TI_Fnt_Receiving__c` and `TI_Fnt_ReceivingDetail__c`, but both had `Name = Text` in the
  org while the sheet declared **AutoNumber** with display formats `RCV{0000000}` / `RCD{0000000}`. The
  mismatch was invisible to every drift run until the user questioned it.
- **Command:** `attr_drift.py --object TI_Fnt_Receiving__c` / `--object TI_Fnt_ReceivingDetail__c`.
- **Component:** `scripts/attr_drift.py` (`read_org_object`, `main`); standard `Name` field via CustomObject
  `<nameField>`.
- **Category:** Tooling.
- **Root cause:** `attr_drift.py` filtered rows with `if not api.endswith("__c"): continue` and read only the
  CustomObject `<fields>` block. The standard `Name` field is neither a `__c` field nor under `<fields>` (it
  lives in the `<nameField>` block), so it was never compared — a custom-only drift check is a false "all clear".
- **Fix applied:** `read_org_object` now captures `org["__nameField__"] = {type, displayFormat, label}` from the
  CustomObject `<nameField>`; `main` locates the sheet object-meta row (`Name Field Type` / `Name Field Display
  Format`) and emits a `Name` drift entry when the type or (for AutoNumber) the displayFormat differs, and prints
  a WARNING if either side's Name definition is missing instead of silently passing.
- **Prevention added:** new always-apply rule `.cursor/rules/sf-drift-includes-standard-fields.mdc` mandates the
  Name-field comparison on every drift run; the Name fix is flagged DESTRUCTIVE (overwrites existing record Names)
  and requires explicit user approval before deploy.
- **Status:** Resolved.

### [2026-09-09] Pipeline read the wrong value column (G, a description) instead of H (the real value) + attr_drift never compared formula bodies
- **Error signature:** no deploy error — a SILENT data-correctness miss. EIRI Formula Text fields shipped
  with a DUMMY `"TBD"` formula (e.g. `TI_Fnt_SupplierCountryName__c`, 9 more) while the sheet's REAL formula
  (`TI_Logi_TINETAccount__r.CountryCode__c`, `PlaceOfDestination1__r.CountryList__r.CountryCode__c`, …) sat
  in a column we weren't reading. `attr_drift.py` reported `in-sync: 133 drifted: 0` — falsely clean.
- **Command:** `fetch_sheet.py` (all objects) → `generate_xml.py` → `attr_drift.py`.
- **Component:** `scripts/fetch_sheet.py` `build_col_map()`; `scripts/attr_drift.py` formula compare;
  `scripts/fill_formula_placeholder.py` column locator.
- **Category:** Tooling.
- **Root cause:** the client "Format" sheet has TWO polymorphic value columns and the machine API-header row
  is ambiguous/mislabeled: it stamps `defaultValue` on BOTH `数式(設定値)` (col H, the REAL value: formula /
  picklist / referenceTo / displayFormat) AND `デフォルト値` (col L, the real default), and stamps
  `displayFormat / referenceTo / formula / valueSet` on `データ型に応じて…` (col G) which in this sheet is only
  a human DESCRIPTION (often JP prose like `BPマスタ.国コード`). `build_col_map` matched the English header, so
  it mapped Type Specific Value ← G (description) and Default Value ← L (H's data was read then overwritten by
  L). Formula/picklist/referenceTo therefore came from the description column. Compounding it, `attr_drift`
  only checked formula PRESENCE (`exp_formula and not ohf`), never the formula BODY, so a dummy `"TBD"` vs the
  real formula was invisible → false 0-drift.
- **Fix applied:** (1) `fetch_sheet.build_col_map()` now disambiguates by the UNIQUE Japanese header
  substrings — `設定値` → Type Specific Value (col H), `デフォルト` → Default Value (col L) — and no longer reads
  the `displayFormat/…` description column (col G); no fall-back to G when H is blank (blank H → surfaced as a
  blocker), per the sheet owner (2026-09-09). Caller passes the JP header row (`grid[header_idx-1]`).
  (2) `attr_drift.py` now compares normalized formula BODIES when both sides are formulas
  (`formula body differs (org=…)`), catching dummy-vs-real. (3) `fill_formula_placeholder.py` locates the
  value column by the `設定値` JP substring (falls back to the old `displayFormat` header only for legacy tabs).
- **Impact:** re-fetch + re-drift EIRI → 10 Formula Text fields flagged (9 real cross-object formulas to
  deploy in place; `TI_Fnt_LoadingPlaceCountryCode__c` still a client placeholder `”TBD”` with full-width
  quotes → park). Receiving / ReceivingDetail / DeliveryDestination analyses MUST be re-run with the corrected
  reader (their prior numbers were computed from col G and are suspect).
- **Prevention added:** header-driven column mapping is now JP-substring disambiguated (reshuffle-proof) and
  documented in `build_col_map`; `attr_drift` formula-body compare closes the presence-only gap.
- **Status:** Monitoring (EIRI formula redeploy + other-object re-analysis pending).

### [2026-09-09] EIRI picklists shipped broken: newline-delimited values collapsed into one value
- **Error signature:** drift showed 14 EIRI picklists as "values differ (N sheet / 1 org)"; org held ONE
  value literally equal to the whole cell, e.g. `必要\n不要`, `OCEAN B/L\nSURRENDERD\nWAYBILL\nCargo Receipt\nその他`.
  A naive re-deploy then failed check-only with `Duplicate label: Required` (unrestricted picklists MERGE
  new values with the old junk value → label collision).
- **Command:** original EIRI field deploy (`mdapi_deploy.py`, force-app picklists) + attempted redeploy
  `.build/eiri_pfix`.
- **Component:** `scripts/generate_xml.py` `_build_picklist_valueset()` (value split + `<restricted>` placement).
- **Category:** Schema.
- **Root cause:** (1) picklist entries were split on `;` ONLY (`norm_picklist.split(";")`), so newline-
  delimited cells (the client uses `\n`) became a single giant value. (2) Separately, `<restricted>` was
  written under `<valueSetDefinition>` instead of `<valueSet>` → `Element restricted invalid at this
  location in type ValueSetValuesDefinition` when trying a restricted-replace workaround.
- **Fix applied:** `generate_xml.py`: split entries on `[;\n]` for both values and default-values; moved
  `<restricted>` to be a child of `<valueSet>` (before `<valueSetDefinition>`). Because unrestricted picklists
  MERGE (junk value cannot be removed in place — restricted-replace also merged), the 14 broken fields were
  DELETE+RECREATED (EIRI has 0 records → safe): strip page → destructive delete (purgeOnDelete) → recreate
  with correct split values → restore page → re-grant FLS (14/14). Post-fix drift = 0/133.
- **Prevention added:** `attr_drift.py` now maps `boolean`->Checkbox and normalizes full-width `：/；` before
  splitting picklist label:api, so it stops false-flagging Checkbox and colon-labeled picklists; the corrected
  `[;\n]` split in `generate_xml.py` prevents the collapse at the source. (Follow-up TODO: add a
  validate_sheet.py guard that flags a picklist whose parsed value count == 1 while the cell contains a
  newline, catching this pre-deploy.)
- **Status:** Resolved

### [2026-09-09] Attribute-drift gate silently skipped on the SOAP (`mdapi_deploy.py`) path
- **Error signature:** No error was thrown — the MISS was a *silent skip*. `TI_Fnt_Receiving__c`
  (36) + `TI_Fnt_ReceivingDetail__c` (19) = **55 existing fields drifted** in DEFINITION
  (type/formula/referenceTo/picklist/precision/scale/length) between the sheet and the org, and the
  deploy was closed out as "done" without any of it being surfaced. Found only when the user asked
  why drift wasn't checked.
- **Command:** manual `python scripts/mdapi_deploy.py --package .build/newpkg/package.xml --file …`
  (used because the `sf` CLI is auth-locked in this sandbox, so `prep_deploy.py` could not run).
- **Component:** `scripts/mdapi_deploy.py` (had no drift gate); `scripts/attr_drift.py` (only ever
  invoked from `prep_deploy.py` step 6b).
- **Category:** Tooling / Order-of-Execution (mandatory gate wired to only one deploy path).
- **Root cause:** `attr_drift.py` — the mandatory attribute-level drift check per
  `sf-deploy-delta-and-blockers.mdc` — was invoked from EXACTLY ONE place: `prep_deploy.py` build
  step 6b, which routes through `deploy.py` → the `sf` CLI. In this sandbox the CLI cannot rotate its
  auth file, so all real deploys go through the CLI-free SOAP path `mdapi_deploy.py`, which never
  called `attr_drift`. The name-based package only proves a field is being created; it cannot catch a
  field that already exists but whose definition changed. Net effect: on the SOAP path, drift was
  never computed at all.
- **Fix applied:** added a MANDATORY attribute-drift gate directly into `scripts/mdapi_deploy.py`
  (`_customfield_objects()` + `_drift_gate()`): before a real deploy, it extracts the distinct
  objects from every `CustomField` block in `package.xml`, runs `attr_drift.py` per object using the
  token file (`--token-file .build/orgauth.json`, no CLI), ignores genuinely-new absent fields, and
  **aborts the real deploy (exit 2) on any real drift** unless `--ack-drift` is passed. New flags:
  `--rows` (default `temp_updates.json`), `--ack-drift`, `--no-drift-check`. Non-field packages
  (FlexiPage/CustomTab/CustomObject-only) have no `CustomField` members → gate is a clean no-op.
  Verified: field package w/ drift + no ack → exit 2 (no SOAP sent); FlexiPage package → no-op;
  `--no-drift-check` → bypass.
- **Prevention added:** drift is now enforced on BOTH deploy paths — `prep_deploy.py` (CLI) step 6b
  AND `mdapi_deploy.py` (SOAP) `_drift_gate()`. A CustomField SOAP deploy can no longer proceed past
  unreviewed attribute drift. (Follow-up: surface the 55 drifted fields to the user for redeploy
  decisions — most are destructive type/formula changes needing delete+recreate.)
- **Status:** Resolved

### [2026-09-02] verify_deploy.py false-negative: hardcoded `DeveloperName LIKE 'TI_Fnt_%'` misses legacy-named fields
- **Error signature:** post-deploy `verify_deploy.py` reported `fields expected: 3 | present: 0 |
  missing: 3` for `TI_Fnt_RecevingAndQuoteAndWorkRelation__c`, even though a direct Tooling
  `CustomField` query confirmed all 3 fields live. Caused `prep_deploy --phase deploy` to abort with
  "VERIFICATION FAILED" on a deploy that had actually succeeded.
- **Command:** `python scripts/prep_deploy.py --org ERPDEV01 --phase deploy --tabs "RecevingAndQuoteAndWorkRelation"`.
- **Component:** `scripts/verify_deploy.py` `org_fields()` Tooling query.
- **Category:** Tooling
- **Root cause:** `org_fields()` filtered `CustomField` with `AND DeveloperName LIKE 'TI_Fnt_%'`,
  assuming every custom field uses the `TI_Fnt_` prefix. This object's fields are legacy-named
  (`QuotationAndWork__c`, `Receiving__c`, `DeduplicateKey__c`) with NO prefix, so the query returned
  0 rows → false "missing" for fields that were present.
- **Fix applied:** removed the `LIKE 'TI_Fnt_%'` filter — `org_fields()` now fetches ALL custom
  fields for the object and the caller compares against the expected set derived from the generated
  package (which already scopes correctly regardless of naming). Re-ran → `[OK] present: 3 | missing: 0`.
- **Prevention added:** verification no longer assumes any naming convention; expected-vs-present is
  driven purely by generated metadata, so mixed/legacy field prefixes verify correctly. Applies to
  every future object whose fields don't use `TI_Fnt_`.
- **Status:** Resolved

### [2026-09-02] Field trackHistory=true fails when the object lacks enableHistory (+ manifest --only leaked FlexiPage/PermissionSet)
- **Error signature:** `CustomField <Obj>.<Field>: The entity: <Obj> does not have history
  tracking enabled` — 3 component failures on the check-only dry-run of the new object
  `TI_Fnt_RecevingAndQuoteAndWorkRelation__c` (all 3 fields carried `<trackHistory>true`).
- **Command:** `python scripts/prep_deploy.py --org ERPDEV01 --tabs "RecevingAndQuoteAndWorkRelation" --phase build`
  (→ `deploy.py … --dry-run`).
- **Component:** CustomObject `TI_Fnt_RecevingAndQuoteAndWorkRelation__c` + its 3 CustomFields
  (`QuotationAndWork__c`, `Receiving__c`, `DeduplicateKey__c`).
- **Category:** Dependency (Schema dependency: field history requires object history)
- **Root cause:** The sheet marks fields with 履歴管理 (col M = ○) → generator emits
  `<trackHistory>true</trackHistory>` on each field. But object-level `<enableHistory>` was driven
  ONLY by the object header's 項目履歴管理 flag (blank here), so the CustomObject was emitted
  WITHOUT `<enableHistory>true</enableHistory>`. Salesforce rejects field-level history unless the
  parent object enables history → all 3 fields failed. A second, latent issue surfaced in the same
  package: `build_manifest.py --only` filtered CustomObject/CustomField/Layout by object but added
  ALL FlexiPage + PermissionSet unconditionally, so every object deploy silently dragged in 6
  unrelated FlexiPages + the `SalesFrontAdmin` permission set.
- **Fix applied:** (1) `scripts/generate_xml.py` — compute `history_objects` (any field row with
  Track History truthy) parallel to `md_objects`, and force `enableHistory=true` on those objects'
  meta regardless of the header flag. (2) `scripts/build_manifest.py` — under `--only`, skip
  FlexiPage/PermissionSet entirely (they have no object prefix and are deployed by their own gated
  manifests). Re-ran build → package scoped to CustomObject 1 + CustomField 3; dry-run PASSED.
- **Prevention added:** generator now derives object history from its fields, so a history-tracked
  field can never be packaged without object history again. `--only` deploys are now object-scoped
  only (no FlexiPage/PermissionSet leakage). (Optional future validate_sheet guard: warn if a field
  sets Track History while the object header 項目履歴管理 is blank — informational, since the
  generator now self-corrects.)
- **Status:** Resolved

### [2026-08-28] Flipping required=true→false leaves fields INVISIBLE (implicit FLS lost, no explicit FLS)
- **Error signature:** no deploy error — a silent VISIBILITY gap. Fields deployed as
  `required=true` are implicitly visible and cannot carry `fieldPermissions`; when later
  flipped to `required=false` they lose that implicit visibility but have **no explicit FLS**,
  so users (via `SalesFrontAdmin`) can no longer see them.
- **Command:** post-flip of 91 fields on `TI_Fnt_IncidentalExpenses__c` (42),
  `TI_Fnt_StandaloneIncidentalExpenses__c` (41), `TI_Fnt_IncidentalExpensesDetail__c` (8).
- **Component:** PermissionSet `SalesFrontAdmin` fieldPermissions (91 fields missing).
- **Category:** Dependency (FLS lifecycle)
- **Root cause:** The original FLS grant skipped these fields precisely because they were
  `required=true` (required fields reject `fieldPermissions`). After the requiredness flip, the
  post-deploy FLS gate was not re-evaluated for the now-optional fields, so they had zero FLS.
- **Fix applied:** `scripts/_fix_fls_flipped.py` — computes the missing set LIVE per object
  (`all custom − existing FLS − Master-Detail`), upserts Read+Edit `fieldPermissions`, strips
  `<viewAllFields>` (invalid at v62), re-sorts children so fieldPermissions stay contiguous, and
  emits `manifest/fix_fls_flipped.xml`. Deployed → 91 grants; live FLS now 57/46/14.
- **Prevention added:** Whenever a field's `required` flips true→false (a redeploy of an existing
  field), the post-deploy FLS gate (`sf-post-deploy-fls-and-flexipage.mdc` Gate 1) MUST be re-run
  for those fields — they are newly FLS-eligible and start with none. Treat a requiredness flip as
  an FLS-affecting change, not just a schema tweak.
- **Status:** Resolved

### [2026-08-27] Lookup→MasterDetail conversion cannot be validated in check-only (dry-run)
- **Error signature:** `Test only deployment cannot update a field from a Lookup to MasterDetail`
- **Command:** `sf project deploy start --dry-run --manifest manifest/fix_batch1_package.xml`
  (bundling 91 `required` flips + 3 Lookup→MasterDetail conversions)
- **Component:** CustomField (3 fields): `TI_Fnt_DeliveryDestination__c.TI_Fnt_Receiving__c`,
  `TI_Fnt_DocumentDestination__c.TI_Fnt_Shipping__c`, `TI_Fnt_ShipoutMovein__c.TI_Fnt_Shipping__c`
- **Category:** Tooling
- **Root cause:** Salesforce forbids converting a Lookup to Master-Detail inside a **check-only /
  validateOnly** deployment. It is a real-deploy-only operation (and only succeeds when the child
  object has 0 records, or every existing child row already has the lookup populated). The dry-run
  therefore ALWAYS reports these 3 as failures even though the change is valid — the 91 non-conversion
  members validated fine (91/94).
- **Fix applied:** Split the batch — deploy the 91 `required=false` flips as their own dry-run-clean
  real deploy (`manifest/fix_reqflip.xml`); deploy the 3 conversions separately as a REAL deploy
  (`manifest/fix_conversions.xml`) with NO dry-run gate, after live-confirming each child object has
  0 records (`SELECT COUNT() FROM <obj>`). Never bundle a Lookup→MD conversion with fields you want
  dry-run-gated (a real-deploy rollback would take the good members with it).
- **Prevention added:** Deploy playbook rule — a Lookup→MasterDetail (or any relationship-type/
  referenceTo change) is check-only-incompatible: skip the dry-run for those members, verify child
  record count == 0 first, and isolate them in their own manifest so they can't roll back validated
  members.
- **Status:** Resolved

### [2026-08-27] fieldPermissions on a required (or Master-Detail) field → deploy rejected
- **Error signature:** `PermissionSet SalesFrontAdmin — You cannot deploy to a required field:
  TI_Fnt_IncidentalExpenses__c.TI_Fnt_ProductProduct__c`
- **Command:** `sf project deploy start --dry-run --manifest manifest/incexp_fls.xml` (adding FLS
  for the IncidentalExpenses family to SalesFrontAdmin)
- **Component:** PermissionSet SalesFrontAdmin (fieldPermissions)
- **Category:** Schema
- **Root cause:** Universally-required fields (`<required>true</required>`) and Master-Detail
  fields are ALWAYS visible/editable and cannot carry `fieldPermissions`. The FLS builder was
  emitting a fieldPermissions entry for every custom `__c` field, including required ones, so the
  deploy was rejected.
- **Fix applied:** When building fieldPermissions from retrieved field metadata, classify each
  field and SKIP it entirely if `type == MasterDetail` or `required == true`; emit read-only
  (`editable=false, readable=true`) for formula / Summary / AutoNumber; editable otherwise. These
  skipped fields need no FLS — they are always visible. (Also keep the contiguous-grouping fix
  from the earlier lesson.)
- **Prevention added:** Standard FLS-builder rule = "skip Master-Detail + required fields, RO for
  formula/summary/autonumber, else editable". Applies to every permission-set FLS pass.
- **Status:** Resolved

### [2026-08-27] PermissionSet fieldPermissions non-contiguous → "Element ... is duplicated"
- **Error signature:** `PermissionSet SalesFrontAdmin — Error parsing file: Element
  fieldPermissions is duplicated at this location in type PermissionSet (3621:17)`
- **Command:** `deploy.py --start --package manifest/new5_fls.xml` (adding FLS for the 5 new
  objects' 66 fields to SalesFrontAdmin)
- **Component:** PermissionSet SalesFrontAdmin (.permissionset-meta.xml)
- **Category:** Schema
- **Root cause:** New `<fieldPermissions>` elements were appended at the END of the root via
  `ET.SubElement(root, …)`, i.e. AFTER other element types (objectPermissions, etc.) that
  already followed the existing fieldPermissions block. The Metadata API requires all elements
  of the same type to be CONTIGUOUS; a second, separated fieldPermissions group is rejected as
  "duplicated at this location".
- **Fix applied:** When editing a PermissionSet/Profile XML, do NOT append same-type elements at
  the end. Regroup root children by tag (stable, preserving first-appearance tag order and
  within-tag order) and merge new `<fieldPermissions>` INTO the existing fieldPermissions group
  (sorted by `<field>` for a clean diff), so every element type stays contiguous. Verified with a
  contiguity assertion before deploy.
- **Prevention added:** Standard pattern for all future permission-set/profile edits = "regroup by
  tag + merge into the matching group + assert contiguity", never a bare append. (No
  validate_sheet.py guard applies — this is a permission-set XML-shape issue, not a sheet-row
  issue.)
- **Status:** Resolved

### [2026-08-26] referenceTo points at a non-existent object (X__c vs XMaster__c shorthand)
- **Error signature:** dry-run `CustomField …TI_Fnt_Condition__c: referenceTo value of
  'TI_Fnt_PaymentTerms__c' does not resolve to a valid sObject type` (same class earlier
  for `TI_Fnt_BusinessPartner__c`)
- **Command:** `sf project deploy start --manifest … (dry-run)` for Shipping/Receiving fields
- **Component:** CustomField Lookups whose `Type Specific Value` (referenceTo) was the
  shorthand `TI_Fnt_BusinessPartner__c` / `TI_Fnt_PaymentTerms__c` while the org only has
  the `…Master__c` twin (`TI_Fnt_BusinessPartnerMaster__c`, `TI_Fnt_PaymentTermsMaster__c`).
- **Category:** Dependency
- **Root cause:** `validate_sheet.py` only checked referenceTo OFFLINE (non-empty, valid
  API-name pattern, in-deploy-set hint) and emitted a mere INFO for a custom referenceTo
  not in the batch. It never verified the parent EXISTS in the target org, so a referenceTo
  to a non-existent object passed validation and only blew up at the deploy dry-run.
- **Fix applied:** Added an optional LIVE org-existence check to `validate_sheet.py`:
  `--target-org <org>` collects every distinct Lookup/MasterDetail custom referenceTo,
  queries `EntityDefinition` once (case-insensitive), and raises `dependency.ref.org` ERROR
  for any referenceTo that is neither created in this deploy nor present in the org — with a
  `did you mean 'X Master__c'?` hint when the Master twin exists. Wired into
  `prep_deploy.py` step [3/6] so every batch validates referenceTo against the org.
- **Prevention added:** `validate_sheet.py` `dependency.ref.org` guard (live) + prep_deploy
  now always passes `--target-org`. A missing-parent referenceTo is now a pre-deploy ERROR,
  not a dry-run surprise.
- **Status:** Resolved

### [2026-08-26] Case-insensitive API-name collision missed by case-sensitive delta
- **Error signature:** `CustomField TI_Fnt_Shipping__c.TI_Fnt_NoTifyParty__c: Not in
  package.xml` + `returned from org, but not found in the local project`
- **Command:** `sf project deploy start --manifest manifest/ship_recv_bp.xml … (dry-run)`
- **Component:** CustomField `TI_Fnt_Shipping__c.TI_Fnt_NotifyParty__c` (new) vs
  existing org field `TI_Fnt_NoTifyParty__c` (differs only by the case of one letter)
- **Category:** Schema / Tooling
- **Root cause:** The delta (sheet fields − org fields) was computed with a
  CASE-SENSITIVE set difference. Salesforce API names are CASE-INSENSITIVE for
  uniqueness, so a newly-curated `TI_Fnt_NotifyParty__c` was treated as "new" even
  though the org already had `TI_Fnt_NoTifyParty__c` (an earlier odd glossary casing
  "NoTifyParty"). The deploy then tried to create a case-variant duplicate and the
  Metadata API rejected it with the confusing "Not in package.xml" message.
- **Fix applied:** Recompute the field delta case-insensitively — exclude any sheet
  field whose `.lower()` matches an existing org field's `.lower()`; report such
  case-variants as "already in org (SKIP)" rather than deploying them. NotifyParty
  was dropped from the delta; the org keeps its existing `TI_Fnt_NoTifyParty__c`.
- **Prevention added:** Delta computation for existing objects MUST compare field
  API names case-insensitively (both the Tooling-API delta step in
  `sf-deploy-delta-and-blockers.mdc` and any ad-hoc delta script). A case-variant
  match = field already exists → SKIP, never redeploy/rename.
- **Status:** Resolved

### [2026-08-25] META: self-correction loop silently skipped for a whole deploy series
- **Error signature:** No KB entries between 2026-08-10 and 2026-08-25 despite
  multiple real failures during the Aug-20 Deal field + FlexiPage + FLS work.
- **Command:** various (manual `sf project deploy start … --ignore-conflicts`,
  hand-run dry-runs, `sf data query`) — mostly NOT via `scripts/deploy.py`.
- **Component:** process (the self-correction loop itself).
- **Category:** Tooling
- **Root cause:** Two compounding gaps. (1) `sf-deploy-self-correction.mdc` was
  `alwaysApply: false`, so it was not loaded into context during those sessions and
  the loop was invisible. (2) Even when `deploy.py` ran, it only *printed* a
  reminder on failure ("→ Run the SELF-CORRECTION loop…") — advisory text with no
  enforcement — and many failures came from MANUAL `sf` calls that bypassed
  `deploy.py` entirely, so no `last_deploy_failure.json` artifact was even produced.
- **Fix applied:** (a) `sf-deploy-self-correction.mdc` → `alwaysApply: true`.
  (b) New `scripts/log_failure.py` auto-appends a DRAFT KB entry from
  `.build/last_deploy_failure.json`; `deploy.py` now calls it automatically on any
  non-zero exit (KB update can no longer be forgotten). (c) Rule now MANDATES all
  org-touching deploys go through `deploy.py`/`prep_deploy.py` so the artifact +
  auto-log always fire.
- **Prevention added:** Automated KB draft-append on failure (no human step),
  always-applied rule, and a quality gate that refuses to claim deploy success while
  a failure in this run has no KB entry.
- **Status:** Resolved

### [2026-08-20] 28× "Must specify: relationshipName" on Deal lookups/MD
- **Error signature:** `Must specify a non-null value for: relationshipName` ×28 on
  `TI_Fnt_Deal__c` Lookup/MasterDetail fields during field dry-run.
- **Command:** `sf project deploy start … --dry-run` (Deal fields)
- **Component:** CustomField (Lookup/MasterDetail) on `TI_Fnt_Deal__c`
- **Category:** Schema
- **Root cause:** `generate_xml.py` deliberately omits `<relationshipName>` when the
  sheet cell is blank (to avoid collisions), but Salesforce REQUIRES it on every
  Lookup/MasterDetail. 28 Deal relationship rows had a blank relationshipName.
- **Fix applied:** Derived `relationshipName = <FieldAPI minus __c>` into the
  `temp_updates.json` build artifact (not the sheet) for the 28 rows.
- **Prevention added:** `validate_sheet.py` now flags a blank `Relationship Name`
  on Lookup rows as an ERROR (was optional) so it is caught pre-deploy, matching the
  MasterDetail rule.
- **Status:** Resolved

### [2026-08-20] "40 custom relationships max" exceeded on Deal
- **Error signature:** `reached maximum number of custom fields: Each object can have
  no more than 40 custom relationships` on `TI_Fnt_Deal__c`.
- **Command:** `sf project deploy start … --dry-run` (Deal fields, 43 relationships)
- **Component:** CustomField (Lookup/MasterDetail) on `TI_Fnt_Deal__c`
- **Category:** Schema/Limit
- **Root cause:** The Deal package tried to create 43 relationship fields; the org
  hard-caps custom relationships at 40 per object. No pre-deploy counter existed.
- **Fix applied:** Parked 3 relationship fields locally (ShippingDestination,
  SoldToParty, Supplier) to land 40; sheet left untouched per user instruction.
- **Prevention added:** `validate_sheet.py` now counts Lookup+MasterDetail rows per
  object and ERRORs when the deploy set would push an object past 40 relationships.
- **Status:** Resolved

### [2026-08-20] FlexiPage column body 119 > max 100
- **Error signature:** `Component [flexipage:column] attribute [body]: count [119]
  exceeded max [100]` deploying `Shipping_Record_Page`.
- **Command:** FlexiPage dry-run (`gen_record_page.py` output)
- **Component:** FlexiPage `Shipping_Record_Page` (基本情報 section)
- **Category:** Tooling/Limit
- **Root cause:** A single `flexipage:column` body may hold at most 100 items; the
  基本情報 section had 119 fields in one column.
- **Fix applied:** `gen_record_page.py` (and `gen_tabbed_page.py`) now compute the
  number of columns per section dynamically (`ceil(fields/100)`) and split fields
  evenly so no column exceeds 100 items.
- **Prevention added:** Page generators cap-split columns automatically; a section
  can never emit a >100-item column again.
- **Status:** Resolved

### [2026-08-20] Source-tracking conflicts blocked field/FlexiPage deploys
- **Error signature:** `sf project deploy start` refused with conflicts (local vs org
  out of sync) when re-deploying existing fields / FlexiPages.
- **Command:** `sf project deploy start …` (Deal fields, Shipping/Receiving pages)
- **Component:** CustomField / FlexiPage
- **Category:** Tooling
- **Root cause:** Source tracking flagged already-present metadata as conflicting;
  `deploy.py` did not expose `--ignore-conflicts`, so commands were run by hand.
- **Fix applied:** Re-ran with `--ignore-conflicts` to force intended local state.
- **Prevention added:** Documented that intentional redeploys of known metadata use
  `--ignore-conflicts`; TODO to surface the flag through `deploy.py`.
- **Status:** Monitoring

### [2026-08-20] Google ADC failure — HOME clobbered by sf shim
- **Error signature:** `google.auth.exceptions.DefaultCredentialsError: Your default
  credentials were not found.` (Sheets API) and later `sf data query failed: unknown`.
- **Command:** `prep_deploy.py` / `fetch_sheet.py` after `sf`-oriented env was set.
- **Component:** environment (`HOME`, `XDG_DATA_HOME`, `CLOUDSDK_CONFIG`).
- **Category:** Environment/Org
- **Root cause:** `HOME` was pointed at `$PWD/.sfhome` for `sf` CLI, which hid the
  gcloud ADC from `google.auth.default`; conversely a stale `.sfhome` `HOME` broke
  `sf` path computation.
- **Fix applied:** For Google calls, `unset CLOUDSDK_CONFIG GOOGLE_APPLICATION_CREDENTIALS
  XDG_DATA_HOME` and `export HOME=/Users/abhi.chauhan`; for `sf` calls set the
  `.sfhome`/XDG pair. Never share one env for both.
- **Prevention added:** Documented the two distinct env profiles (Google-ADC vs
  sf-shim); `prep_deploy.py` manages them per phase.
- **Status:** Monitoring

### [2026-08-10] "Succeeded" deploy that never ran — wrong flag + stale log
- **Error signature:** deploy reported `Status: Succeeded / 13/13` yet the object
  did not exist in the org (`EntityDefinition` totalSize 0).
- **Command:** `python scripts/deploy.py … --run` (intended real deploy)
- **Component:** CustomObject/CustomField — `Sales_IncidentalExpensesDetail__c`,
  `Sales_StandaloneIncidentalExpenses__c`
- **Category:** Tooling
- **Root cause:** `deploy.py`'s real-deploy flag is `--start`; `--run` does not
  exist, so argparse exited (exit 2) WITHOUT deploying. The success signal was a
  `grep` of `.build/last_deploy.log`, which still held the previous CHECK-ONLY
  (`--dry-run`) run's `Status: Succeeded`. Redirecting deploy output to
  `/dev/null` hid the argparse error.
- **Fix applied:** Re-deployed with the correct `python scripts/deploy.py --start …`.
  Added rule `.cursor/rules/sf-object-existence-precheck.mdc` and helper
  `scripts/verify_deploy.py`.
- **Prevention added:** MANDATORY pre-deploy object-existence check + MANDATORY
  automated live post-deploy verification (`verify_deploy.py`, exit-code gated).
  Never infer real-deploy success from `last_deploy.log`; confirm the log's first
  line is `deploy start` WITHOUT `--dry-run`, check the exit code, and verify live.
- **Status:** Resolved

### [2026-08-10] False "field missing" — FLS-gated verification query
- **Error signature:** `verify_deploy.py` reported 3/12 fields missing
  (`ConditionRate`, `FixedDate`, `IsDeleted`); `SELECT <field>` → "No such column";
  `sf sobject describe` → 9 fields. Tooling `CustomField` → all 12 present (real Ids).
- **Command:** `sf data query "SELECT … FROM FieldDefinition …"` / `sobject describe`
- **Component:** CustomField — 3 fields on `Sales_IncidentalExpensesDetail__c`
- **Category:** Environment/Org (Tooling)
- **Root cause:** `FieldDefinition`, `sObject describe`, and plain SOQL are all
  Field-Level-Security gated. The 3 fields deployed fine but their FLS was not
  auto-granted to the running admin, so they were invisible to those APIs →
  false "missing". The Tooling `CustomField` object is metadata-level and
  FLS-independent, and showed all 12.
- **Fix applied:** `verify_deploy.py::org_fields` now queries Tooling
  `CustomField` (`--use-tooling-api`) and re-adds the `__c` suffix, instead of
  FieldDefinition.
- **Prevention added:** Rule documents that existence checks MUST use the Tooling
  API, never FLS-gated FieldDefinition/describe/SOQL. (Note: if those fields must
  be user-visible, grant FLS separately via a permission set — a deploy-success
  ≠ FLS-visible.)
- **Status:** Resolved

### [2026-08-10] MasterDetail child object rejected sharingModel=ReadWrite
- **Error signature:** `Cannot set sharingModel to ReadWrite on a CustomObject with a MasterDetail relationship field`
- **Command:** `sf project deploy start --manifest manifest/package.xml --dry-run` (check-only against ERPDEV01)
- **Component:** CustomObject — `Sales_DealDetail__c` (has MasterDetail field `TI_Fnt_Deal__c` → `Sales_Deal__c`)
- **Category:** Schema
- **Root cause:** `generate_xml.py::write_object_meta` emitted `<sharingModel>ReadWrite</sharingModel>`
  unconditionally. A detail (child) object in a Master-Detail relationship inherits sharing
  from its parent, so Salesforce forces `ControlledByParent`; ReadWrite is rejected.
- **Fix applied:** `generate_xml.py` — `write_object_meta` now takes `has_master_detail`;
  `process_fields` pre-scans field rows (via `get_sf_field_type`) to find objects owning a
  MasterDetail field and emits `<sharingModel>ControlledByParent</sharingModel>` for them
  (ReadWrite otherwise).
- **Prevention added:** sharingModel is derived from field composition, so any object with a
  MasterDetail field automatically deploys as ControlledByParent — no manual step.
- **Status:** Resolved

### [2026-08-10] Standing default: raw-object prep playbook (no re-ask)
- **Error signature:** Fresh object tab fails validation en masse — `field.api` (blank
  API names), `required.column` (empty formula body / MasterDetail rel name),
  `dependency.ref` (placeholder `referenceTo`), `object.api` (missing `__c`).
  Seen on `成約明細:Sales_DealDetail` (68 ERRORs) — same shape as `Sales_Deal` when raw.
- **Command:** `python scripts/run.py --tabs "<JP:Object>" --only <Object__c>`
- **Component:** Schema/Dependency — whole object tab, unprepped.
- **Category:** Order-of-Execution
- **Root cause:** A "raw" object tab has not been through naming/formula/relationship
  prep, so the deployment gate blocks it. Previously required a per-object strategy
  decision; the user standardized the approach on 2026-08-10.
- **Fix applied:** Documented the fixed prep cycle in `sf-sheet-deployment.mdc`
  ("Raw / unprepped object tab — standard prep playbook (DEFAULT)"): normalize object
  API → name blank fields (`TI_Fnt_`+glossary, `__c`, ≤40, unique) → fill formula
  placeholders by return type → wire relationships (`referenceTo`, Relationship Name)
  → resolve/park lookups → re-validate → deploy (full package if object is new) →
  append report tab.
- **Prevention added:** Apply this playbook automatically for any raw object tab
  without re-asking; every sheet write stays gated; highlight Column A only.
- **Status:** Resolved (standing convention)

### [2026-08-10] Standing default: cross-object lookup resolution (check org, else WIP)
- **Error signature:** `dependency.ref` placeholders like
  `referenceTo '<API名未定(GDC付与)>：数量単位マスタ'`; and at deploy time
  `Entity '<Object>' not found` when a lookup target object is absent from the org.
- **Command:** `python scripts/validate_sheet.py` / `sf project deploy start --dry-run`
- **Component:** Dependency — Lookup/MasterDetail to custom master objects
  (e.g. 成約→`Sales_Deal__c`, 原産国→国マスタ, 保管場所→保管場所マスタ, 数量単位マスタ).
- **Category:** Dependency
- **Root cause:** A lookup can only deploy if its `referenceTo` object already exists
  in the target org. Some masters aren't deployed yet, and some `referenceTo` values
  are still GDC placeholders.
- **Fix applied:** Documented decision in `sf-sheet-deployment.mdc` ("Cross-object
  lookup resolution (DEFAULT)"): resolve the placeholder to a real API, then check the
  target org — if the object exists, keep the lookup; if missing/placeholder, mark the
  row WIP (col AB=`X`) and park it for a later deploy.
- **Prevention added:** Never deploy a lookup whose target object is absent; park as
  WIP (lime-yellow, Column A only) and revisit once the master is deployed.
- **Status:** Resolved (standing convention)

### [2026-08-07] Restrict deleteConstraint rejected on Lookup to User
- **Error signature:** `Cannot add a lookup relationship child with cascade or restrict options to User`
- **Command:** `sf project deploy start --manifest ... --dry-run` (check-only against devpro3)
- **Component:** CustomField — `TI_Fnt_SalesRep__c`, `TI_Fnt_SalesDeliveryPerson__c` (both referenceTo `User`)
- **Category:** Schema
- **Root cause:** The previous fix added `<deleteConstraint>Restrict</deleteConstraint>` to all
  required Lookups. But `User` (and some other standard objects: Group, RecordType, Profile)
  reject Restrict/Cascade child lookups — only `SetNull` is allowed. `SetNull` cannot coexist
  with `required`, so a required Lookup to User is impossible at the DB level.
- **Fix applied:** `generate_xml.py` — for a required Lookup whose referenceTo is in
  `RESTRICT_INCOMPATIBLE_REFS = {User, Group, RecordType, Profile}`, emit
  `<deleteConstraint>SetNull</deleteConstraint>` and DROP `<required>` (degrade to optional).
- **Prevention added:** `validate_sheet.py` INFO now distinguishes required Lookups to these
  standard objects and documents the SetNull/optional degrade.
- **Status:** Resolved

### [2026-08-07] Required Lookup missing deleteConstraint; required LongTextArea
- **Error signature:**
  - `field integrity exception: unknown (must specify either cascade delete or restrict delete for required lookup foreign key)`
  - `Can not specify 'required' for a CustomField of type LongTextArea`
- **Command:** `sf project deploy start --manifest ... --dry-run` (check-only against devpro3)
- **Component:** CustomField — 11 required Lookups + 1 LongTextArea on `Sales_Deal__c`
  (e.g. `TI_Fnt_Incoterms__c`, `TI_Fnt_SalesRep__c`, `TI_Fnt_PackingStyleAfterPacking__c`)
- **Category:** Schema
- **Root cause:** `generate_xml.py` emitted `<required>true</required>` uniformly for
  any non-formula / non-Checkbox / non-AutoNumber / non-Summary field. But
  (a) a *required* Lookup must also declare a `<deleteConstraint>` (SetNull is invalid
  when required), and (b) LongTextArea / Html / Location / MultiselectPicklist / MasterDetail
  cannot be `required` at the schema level.
- **Fix applied:** `generate_xml.py` — required Lookup now emits
  `<deleteConstraint>Restrict</deleteConstraint>`; the `non_settable` set for `required`
  now also excludes LongTextArea, Html, Location, MultiselectPicklist, MasterDetail.
- **Prevention added:** `validate_sheet.py` emits INFO when `Required`=true on a type that
  can't be required (generator drops it) or on a Lookup (generator adds Restrict).
- **Status:** Resolved

### [2026-08-07] Check-only used `deploy validate`, rejected NoTestRun
- **Error signature:** `Expected --test-level=NoTestRun to be one of: RunAllTestsInOrg, RunLocalTests, RunSpecifiedTests, RunRelevantTests`
- **Command:** `sf project deploy validate --manifest ... --test-level NoTestRun`
- **Component:** Tooling — `scripts/deploy.py`
- **Category:** Tooling
- **Root cause:** The check-only path used `sf project deploy validate`, which is
  meant for production quick-deploys and mandates a real Apex test level; it
  rejects `NoTestRun`. Metadata-only (fields) changes to a sandbox do not need
  Apex tests, so `NoTestRun` is legitimate.
- **Fix applied:** `deploy.py` now always uses `sf project deploy start` and adds
  `--dry-run` for check-only. `deploy start --dry-run` validates without writing
  and accepts `NoTestRun`.
- **Prevention added:** Check-only and real deploy share one command builder;
  only the `--dry-run` flag differs, so test-level handling stays consistent.
- **Status:** Resolved

### [2026-08-07] Generator crashed on Python 3.9 (PEP 604 unions)
- **Error signature:** `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` at `def build_field_xml(row: dict) -> ET.Element | None`
- **Command:** `python scripts/generate_xml.py`
- **Component:** Tooling — `scripts/generate_xml.py`
- **Category:** Tooling
- **Root cause:** The generators use `X | None` return annotations (Python 3.10+),
  but the local interpreter is 3.9.6, which evaluates annotations at runtime.
- **Fix applied:** Prepended `from __future__ import annotations` to all copied
  `generate_*.py` so annotations are lazy strings — no behavior change.
- **Prevention added:** Keep the future-import at the top of every generator;
  `scripts/run.py` invokes them via the same interpreter used for the pipeline.
- **Status:** Resolved

### [2026-08-07] Custom object API name missing `__c` suffix
- **Error signature:** validation `object.api` — `Object API 'Sales_ReceivingDetail' invalid (must ... end __c)`
- **Command:** `python scripts/validate_sheet.py`
- **Component:** Schema — object metadata from オブジェクト一覧 index tab
- **Category:** Schema
- **Root cause:** The object-index tab stored the API name without the `__c`
  suffix required for custom objects; would deploy to the wrong/invalid name.
- **Fix applied:** N/A (data fix in sheet).
- **Prevention added:** `validate_sheet.py` errors when an object API name does
  not match the API-name regex AND end with `__c`.
- **Status:** Resolved

### [2026-08-07] Placeholder text in referenceTo (Lookup/MasterDetail)
- **Error signature:** validation `dependency.ref` — `referenceTo '要確認' is not a valid API name`
- **Command:** `python scripts/validate_sheet.py`
- **Component:** Dependency — relationship fields
- **Category:** Dependency
- **Root cause:** Draft sheet rows carried Japanese placeholder `要確認`
  ("to be confirmed") in Column G instead of a real target object API name;
  Salesforce would reject the relationship at deploy time.
- **Fix applied:** N/A (data fix in sheet).
- **Prevention added:** `validate_sheet.py` requires Column G on Lookup/MasterDetail
  to be a valid API name, and flags a custom-object target not present in the
  deploy set (INFO) so missing parents are noticed before deploy.
- **Status:** Resolved

### [2026-08-10] Checkbox with empty Default Value blocked validation
- **Error signature:** validation `required.column` — `Checkbox: required column 'Default Value' is empty`
- **Command:** `python scripts/validate_sheet.py` (Sales_Shipping, 17 checkboxes)
- **Component:** Schema — Checkbox fields with blank Default Value
- **Category:** Schema
- **Root cause:** The validator marked `Default Value` as REQUIRED for Checkbox,
  but `generate_xml.py` already emits `<defaultValue>false</defaultValue>` when the
  cell is blank, so the metadata is always valid. The hard ERROR was over-strict.
- **Fix applied:** `validate_sheet.py` — Checkbox `Default Value` moved from
  `required` to `optional`; an empty value now emits INFO ("generator defaults to
  false") instead of ERROR. A present non-TRUE/FALSE value still errors.
- **Prevention added:** Checkbox default is only validated for format, not presence,
  keeping it in sync with the generator's safe default.
- **Status:** Resolved

### [2026-08-10] Child relationship name collision on a shared master object
- **Error signature:** deploy — `There is already a Child Relationship named
  TI_Fnt_Incoterms on インコタームズマスタ`
- **Command:** `sf project deploy start --manifest ... --dry-run` (ERPDEV01)
- **Component:** CustomField — `Sales_Shipping__c.TI_Fnt_Incoterms__c` (Lookup to
  `TI_Logi_IncotermsMaster__c`)
- **Category:** Dependency
- **Root cause:** `fill_relationship_name.py` derives `relationshipName` as the field
  API minus `__c` (e.g. `TI_Fnt_Incoterms`). When two different child objects both
  have an `Incoterms` lookup to the SAME master (Sales_Deal already deployed
  `TI_Fnt_Incoterms`), the relationshipName collides — child relationship names must
  be unique per parent object.
- **Fix applied:** Set a unique `relationshipName` in the sheet (Column X) for the
  colliding field, qualified by the child object token
  (`TI_Fnt_Incoterms` -> `TI_Fnt_ShippingIncoterms`), then regenerate.
- **Prevention added:** When a lookup targets a master already referenced by an
  earlier-deployed object with the same base relationshipName, qualify the child's
  relationshipName with the object token before deploy.
- **Status:** Resolved

### [2026-08-10] Metadata-only dry-run stalled 7+ min on RunLocalTests
- **Error signature:** dry-run hangs at `⣷ Running Tests` after
  `Components: 179/179 (100%)`
- **Command:** `sf project deploy start --manifest ... --dry-run --test-level RunLocalTests`
- **Component:** Tooling — `scripts/deploy.py` default test level
- **Category:** Tooling
- **Root cause:** ERPDEV01 has a large/slow Apex test suite. For metadata-only
  deploys (custom object + fields, no Apex) `RunLocalTests` runs every local test
  needlessly, taking many minutes and risking failure on unrelated tests.
- **Fix applied:** Use `--test-level NoTestRun` for metadata-only object/field
  deploys and dry-runs (validated 179/179 in ~40s).
- **Prevention added:** Prefer `NoTestRun` for pure schema deploys to sandboxes;
  reserve `RunLocalTests` for changes that touch Apex.
- **Status:** Resolved

### [2026-08-10] Placeholder referenceTo `<API名未定(GDC付与)>：<label>`
- **Error signature:** validation `dependency.ref` — referenceTo not a valid API name
  (11 lookups on `Sales_IncidentalExpenses`)
- **Command:** `python scripts/validate_sheet.py`
- **Component:** Lookup fields whose Column G holds a GDC placeholder instead of a
  real target object API name.
- **Category:** Dependency
- **Root cause:** The sheet author left the lookup target as a placeholder
  (`<API名未定(GDC付与)>：成約`); the label after `：` names the intended object.
- **Fix applied:** Resolve each placeholder by label → real API
  (成約→`Sales_Deal__c`, 輸入・入庫管理→`Sales_Receiving__c`, 輸出・出庫管理→
  `Sales_Shipping__c`, 国マスタ→`CountryList__c`, …), write the API into Column G
  for targets that EXIST in the org; WIP-park lookups whose master is missing.
- **Prevention added:** Standard cross-object flow — map placeholder label→API,
  check org, fill referenceTo (exists) or WIP-park (missing).
- **Status:** Resolved

### [2026-08-10] Number/Currency Precision > 18 rejected
- **Error signature:** validation `constraint` — `'Precision'=20 out of range [1, 18]`
- **Command:** `python scripts/validate_sheet.py` (`TI_Fnt_IVAmount__c`)
- **Component:** Number/Currency field with precision beyond Salesforce max.
- **Category:** Schema
- **Root cause:** Sheet specified precision 20; Salesforce caps total digits at 18.
- **Fix applied:** Set Column T (precision) to 18 for the field.
- **Prevention added:** Cap precision at 18 for Number/Currency; flag any sheet
  value above it for review before deploy.
- **Status:** Resolved

### [2026-08-10] Spurious errors on standard system rows (no `__c`)
- **Error signature:** `required.column` / `forbidden.column` errors on `CreatedDate`,
  `LastModifiedDate`, `CreatedById`, `OwnerId` (rows the sheet lists as system fields)
- **Command:** `python scripts/validate_sheet.py`
- **Component:** Tooling — `scripts/validate_sheet.py`
- **Category:** Tooling
- **Root cause:** These rows have no `__c` suffix, so `generate_xml.py` never deploys
  them, yet the validator still enforced field-definition columns on them, producing
  false blockers.
- **Fix applied:** `validate_sheet.py` now emits only the standard-field WARN and
  skips all further field-def checks for any field whose API name lacks `__c`.
- **Prevention added:** Standard/system rows can never block a deploy.
- **Status:** Resolved

### [2026-08-10] Recurring child-relationship collisions on shared masters
- **Error signature:** `There is already a Child Relationship named <X> on <master>`
  (Incoterms, Currency, Deal, OriginCountry across objects)
- **Command:** `sf project deploy start --dry-run` (ERPDEV01)
- **Component:** Lookup relationshipName derived as field-API-minus-`__c`.
- **Category:** Dependency
- **Root cause:** Multiple objects lookup the SAME shared master (CurrencyMaster,
  IncotermsMaster, CountryList, Sales_Deal…) with the same base relationshipName.
- **Fix applied:** Proactively qualify the relationshipName with the child object
  token when the master is already referenced by an earlier-deployed object
  (e.g. `TI_Fnt_Incoterms`→`TI_Fnt_ReceivingIncoterms`,
  `TI_Fnt_Currency`→`TI_Fnt_IncidentalCurrency`).
- **Prevention added:** For shared masters, object-qualify relationshipNames before
  the first dry-run to avoid the collision loop.
- **Status:** Resolved
