from __future__ import annotations
import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET

NS = "http://soap.sforce.com/2006/04/metadata"
ET.register_namespace("", NS)


def qname(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def get_or_create(parent: ET.Element, tag: str) -> ET.Element:
    el = parent.find(qname(tag))
    if el is None:
        el = ET.SubElement(parent, qname(tag))
    return el


def set_text(parent: ET.Element, tag: str, text: str):
    clean_text = str(text).strip()
    el = get_or_create(parent, tag)
    el.text = clean_text if clean_text else None


def set_text_if(parent: ET.Element, tag: str, value) -> bool:
    """Write <tag> only when `value` is non-blank after trimming.

    Blank cells (None, "", or whitespace-only) cause the element to be
    omitted entirely so Salesforce keeps whatever default / existing
    value the org already has. See `docs/PUSH_SPEC.md §3.4.b` for the
    round-trip rationale.
    """
    clean_text = str(value or "").strip()
    if not clean_text:
        return False
    el = get_or_create(parent, tag)
    el.text = clean_text
    return True


def is_truthy(value):
    text = str(value or "").strip().lower()
    return text in {"true", "y", "yes", "1", "○", "〇"}


def is_falsy(value):
    """Explicit FALSE only (not blank). Lets the sheet turn requiredness OFF."""
    text = str(value or "").strip().lower()
    return text in {"false", "n", "no", "0", "×", "x", "✕", "✗"}


def write_object_meta(
    obj_api: str,
    obj_label: str,
    obj_desc: str = "",
    enable_reports: str = "",
    enable_activities: str = "",
    enable_history: str = "",
    enable_search: str = "",
    name_field_label: str = "",
    name_field_type: str = "",
    name_field_display_format: str = "",
    has_master_detail: bool = False,
) -> None:
    """Write (or overwrite) the .object-meta.xml for an object.

    Always regenerates the file so that changes to object properties in the
    spreadsheet (rows 1-6) are deployed on every push.

    The `nameField` block is built from the spreadsheet's Name row —
    the field-list row whose `fullName` cell equals ``Name``.  That row
    carries the Name field's label (col C), type (col E), and
    displayFormat (col H, only when type is AutoNumber).
    """
    obj_dir = Path("force-app/main/default/objects") / obj_api
    obj_dir.mkdir(parents=True, exist_ok=True)
    filepath = obj_dir / f"{obj_api}.object-meta.xml"

    root = ET.Element(qname("CustomObject"))
    set_text(root, "fullName", obj_api)
    set_text(root, "label", obj_label or obj_api)
    if obj_desc:
        set_text(root, "description", obj_desc)
    set_text(root, "deploymentStatus", "Deployed")
    # A detail (child) object in a Master-Detail relationship inherits sharing
    # from its parent, so Salesforce forces sharingModel = ControlledByParent.
    # Emitting ReadWrite on such an object fails deploy:
    #   "Cannot set sharingModel to ReadWrite on a CustomObject with a
    #    MasterDetail relationship field".
    set_text(root, "sharingModel",
             "ControlledByParent" if has_master_detail else "ReadWrite")

    if is_truthy(enable_reports):
        set_text(root, "enableReports", "true")
    if is_truthy(enable_activities):
        set_text(root, "enableActivities", "true")
    if is_truthy(enable_history):
        set_text(root, "enableHistory", "true")
    if is_truthy(enable_search):
        set_text(root, "enableSearch", "true")

    _append_name_field(
        root,
        obj_api=obj_api,
        obj_label=obj_label,
        name_field_label=name_field_label,
        name_field_type=name_field_type,
        name_field_display_format=name_field_display_format,
    )

    if hasattr(ET, "indent"):
        ET.indent(root, space="    ")
    tree = ET.ElementTree(root)
    with filepath.open("wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
        f.write(b"\n")
    print(f"Generated object metadata: {obj_api}")


def ensure_object_meta(obj_api: str, obj_label: str) -> None:
    """Fallback stub — only writes if the file doesn't already exist."""
    filepath = Path("force-app/main/default/objects") / obj_api / f"{obj_api}.object-meta.xml"
    if not filepath.exists():
        write_object_meta(obj_api, obj_label)


def _append_name_field(
    root: ET.Element,
    *,
    obj_api: str,
    obj_label: str,
    name_field_label: str,
    name_field_type: str,
    name_field_display_format: str,
) -> None:
    """Append a nested <nameField> block to the CustomObject root.

    Salesforce requires `<nameField>` to be a structured element with
    `<label>`, `<type>`, and — only for AutoNumber — `<displayFormat>`.
    A flat ``<nameField>FieldApi__c</nameField>`` is invalid at deploy time.

    Defaulting rules:
    - blank type  → ``Text``
    - blank label → ``{obj_label} Name`` (or ``{obj_api} Name`` if no label)
    - AutoNumber with blank displayFormat → hard error (SFDC requires it).
    """
    raw_type = (name_field_type or "").strip()
    normalized_type = _TYPE_MAP.get(raw_type.lower(), raw_type) if raw_type else "Text"
    if normalized_type not in ("Text", "AutoNumber"):
        # Salesforce only accepts Text or AutoNumber for the Name field.
        # Coerce anything else back to Text so the deploy doesn't silently fail;
        # surface the coercion to the operator via stdout.
        print(
            f"⚠️  Object {obj_api}: nameField type '{raw_type}' is not Text/AutoNumber — coerced to Text."
        )
        normalized_type = "Text"

    label = (name_field_label or "").strip()
    if not label:
        label = f"{obj_label} Name" if obj_label else f"{obj_api} Name"

    display_format = (name_field_display_format or "").strip()
    if normalized_type == "AutoNumber" and not display_format:
        # Degrade gracefully instead of crashing the whole generator run.
        # A stale "AutoNumber" in the sheet's Name-row type cell (e.g. left
        # over from a Pull against an old flat <nameField> element) should not
        # block every other object in the same Push. Coerce to Text and warn
        # so the operator can correct the sheet.
        print(
            f"⚠️  Object {obj_api}: nameField type is AutoNumber but displayFormat is blank "
            "— coerced to Text. Fill the 'Type Specific Value' column on the Name row "
            "(fullName='Name') of the sheet to keep AutoNumber."
        )
        normalized_type = "Text"

    nf = ET.SubElement(root, qname("nameField"))
    # Child order: displayFormat (if AutoNumber), label, type — matches the
    # canonical SFDC metadata ordering shown in docs/PUSH_SPEC.md.
    if normalized_type == "AutoNumber":
        set_text(nf, "displayFormat", display_format)
    set_text(nf, "label", label)
    set_text(nf, "type", normalized_type)


# ---------------------------------------------------------------------------
# Data-type normalisation
# ---------------------------------------------------------------------------

# Maps lower-cased, whitespace-stripped type names → Salesforce API type name.
_TYPE_MAP: dict[str, str] = {
    # Text
    "text": "Text",
    "string": "Text",
    "テキスト": "Text",
    "textarea": "TextArea",
    "テキストエリア": "TextArea",
    "longtextarea": "LongTextArea",
    "richtextarea": "Html",
    "richtext": "Html",
    "htmlarea": "Html",
    "リッチテキスト": "Html",
    # Numeric
    "number": "Number",
    "数値": "Number",
    "integer": "Number",
    "currency": "Currency",
    "通貨": "Currency",
    "percent": "Percent",
    "パーセント": "Percent",
    # Contact / identity
    "email": "Email",
    "メール": "Email",
    "phone": "Phone",
    "電話": "Phone",
    "url": "Url",
    # Date / time
    "date": "Date",
    "日付": "Date",
    "datetime": "DateTime",
    "日付/時間": "DateTime",
    "日時": "DateTime",
    "time": "Time",
    "時刻": "Time",
    # Picklist
    "picklist": "Picklist",
    "選択リスト": "Picklist",
    "multipicklist": "MultiselectPicklist",
    "multiselect": "MultiselectPicklist",
    "複数選択リスト": "MultiselectPicklist",
    # Boolean
    "checkbox": "Checkbox",
    "チェックボックス": "Checkbox",
    "boolean": "Checkbox",
    # Relationship
    "lookup": "Lookup",
    "参照関係": "Lookup",
    "masterdetail": "MasterDetail",
    "主従関係": "MasterDetail",
    "hierarchy": "Hierarchy",
    "階層関係": "Hierarchy",
    "externalrelationship": "ExternalRelationship",
    "外部関係": "ExternalRelationship",
    # Other
    "autonumber": "AutoNumber",
    "自動採番": "AutoNumber",
    "encryptedtext": "EncryptedText",
    "暗号化テキスト": "EncryptedText",
    "location": "Location",
    "geolocation": "Location",
    "geolocation(latitude/longitude)": "Location",
    "rollup": "Summary",
    "rollupsummary": "Summary",
    "集計": "Summary",
}

# Return types allowed inside "Formula (…)" wrappers.
_FORMULA_RETURN_MAP: dict[str, str] = {
    "text": "Text",
    "テキスト": "Text",
    "number": "Number",
    "数値": "Number",
    "currency": "Currency",
    "通貨": "Currency",
    "percent": "Percent",
    "パーセント": "Percent",
    "date": "Date",
    "日付": "Date",
    "datetime": "DateTime",
    "date/time": "DateTime",
    "日時": "DateTime",
    "日付/時間": "DateTime",
    "checkbox": "Checkbox",
    "チェックボックス": "Checkbox",
    "time": "Time",
    "時刻": "Time",
}

# Dummy formula bodies keyed by the formula's RETURN type. Salesforce rejects a
# formula field with an empty formula, so when the sheet leaves the formula body
# (col H) blank we inject a return-type-appropriate placeholder to keep the field
# deployable. The real formula is filled in later by the client / a follow-up.
_DUMMY_FORMULA: dict[str, str] = {
    "Text": '"TBD"',
    "Number": "0",
    "Currency": "0",
    "Percent": "0",
    "Date": "TODAY()",
    "DateTime": "NOW()",
    "Time": "TIMENOW()",
    "Checkbox": "false",
}


def _key(s: str) -> str:
    """Collapse whitespace/　 for dictionary look-up."""
    return s.lower().replace(" ", "").replace("\u3000", "")


def normalize_type(raw_type: str) -> tuple[str, bool]:
    """Return (salesforce_type, is_formula).

    Handles plain type names as well as the two formula-wrapper spellings
    our Google Sheets are known to use:

    - paren-wrapped, e.g. ``Formula(Text)`` / ``数式（テキスト）``
    - space-separated, e.g. ``Formula Date`` / ``Formula Date/Time``

    The space-separated variant is the authoritative spelling per
    ``docs/GOOGLE_SHEET_STRUCTURE.md`` → "Data Validation on Type Column"
    and matches ``DD_FIELD_TYPE_VALUES`` in
    ``app-scripts/PullSelectedDataDictionary.gs``.
    """
    raw = str(raw_type or "").strip()
    k = _key(raw)

    # Formula wrapper: "Formula(Text)", "数式（テキスト）", …
    m = re.match(r"^(?:formula|数式)\s*[（(](.+)[)）]$", k)
    if m:
        inner = m.group(1).strip()
        return_type = _FORMULA_RETURN_MAP.get(inner, "Text")
        return return_type, True

    # Formula space-separated: "Formula Date", "Formula Date/Time", …
    # Matched against `raw` (not `k`) because `_key()` strips whitespace
    # and would merge the prefix and the return type into a single token.
    m = re.match(r"^(?:formula|数式)\s+(.+)$", raw, re.IGNORECASE)
    if m:
        inner = _key(m.group(1).strip())
        return_type = _FORMULA_RETURN_MAP.get(inner, "Text")
        return return_type, True

    if k in ("formula", "数式"):
        return "Text", True

    # Direct look-up (pre-normalised key)
    if k in _TYPE_MAP:
        return _TYPE_MAP[k], False

    # Fall back: try every key after applying the same normalisation
    for map_key, sf_type in _TYPE_MAP.items():
        if k == _key(map_key):
            return sf_type, False

    # Unknown type — pass through as-is so the deploy surfaces the error
    return raw, False


def get_sf_field_type(raw_type: str) -> str:
    sf_type, _ = normalize_type(raw_type)
    return sf_type


# ---------------------------------------------------------------------------
# Picklist helpers
# ---------------------------------------------------------------------------

def _build_picklist_valueset(
    root: ET.Element,
    picklist_str: str,
    restricted: bool,
    global_set: str,
    default_val: str = "",
) -> bool:
    """Populate or replace the <valueSet> element on a picklist field.

    Returns True if a valid valueSet was written, False if it was skipped
    (no values and no global set — the caller should skip the field entirely).
    """
    if global_set:
        vs = get_or_create(root, "valueSet")
        existing_def = vs.find(qname("valueSetDefinition"))
        if existing_def is not None:
            vs.remove(existing_def)
        set_text(vs, "valueSetName", global_set)
        return True

    # Normalize full-width JP punctuation so Japanese sheets parse correctly:
    # full-width semicolon '；'(U+FF1B) -> ';' (entry delimiter),
    # full-width colon     '：'(U+FF1A) -> ':' (Label:ApiName separator).
    _JP_PUNCT = {0xFF1B: ord(";"), 0xFF1A: ord(":")}
    norm_picklist = str(picklist_str or "").translate(_JP_PUNCT)
    # Entries may be delimited by ';' OR by newlines (client sheets use both) —
    # split on either so a newline-separated cell does NOT collapse into one
    # giant value (that bug shipped EIRI picklists as a single '値1\n値2' value).
    values = [e.strip() for e in re.split(r"[;\n]", norm_picklist) if e.strip()]
    if not values:
        # Salesforce requires at least one <value>; skip generating this field.
        return False
    default_values = {
        e.strip()
        for e in re.split(r"[;\n]", str(default_val or "").translate(_JP_PUNCT))
        if e.strip()
    }

    vs = get_or_create(root, "valueSet")
    # Remove stale global reference if switching to inline
    stale = vs.find(qname("valueSetName"))
    if stale is not None:
        vs.remove(stale)

    # <restricted> is a child of <valueSet> (NOT <valueSetDefinition>) and must
    # precede <valueSetDefinition> in the MDAPI schema. Placing it inside
    # valueSetDefinition raises "Element restricted invalid at this location in
    # type ValueSetValuesDefinition".
    old_r = vs.find(qname("restricted"))
    if old_r is not None:
        vs.remove(old_r)
    if restricted:
        set_text(vs, "restricted", "true")

    vsd = get_or_create(vs, "valueSetDefinition")
    set_text(vsd, "sorted", "false")

    for v in list(vsd.findall(qname("value"))):
        vsd.remove(v)

    _ESC = "\x00"  # sentinel for an escaped colon "\:" inside a label
    for entry in values:
        # "Label:ApiName" (value differs from label) or just "Label".
        # A literal colon inside the LABEL can be escaped as "\:" so labels like
        # "比率 1\:2:Ratio12" -> label "比率 1:2", api "Ratio12".
        e = entry.replace("\\:", _ESC)
        if ":" in e:
            label, api = [p.strip() for p in e.split(":", 1)]
        else:
            label = api = e
        label = label.replace(_ESC, ":")
        api = api.replace(_ESC, ":")

        val_el = ET.SubElement(vsd, qname("value"))
        set_text(val_el, "fullName", api)
        is_default = api in default_values or label in default_values
        set_text(val_el, "default", "true" if is_default else "false")
        set_text(val_el, "label", label)

    return True


# ---------------------------------------------------------------------------
# Field XML builder
# ---------------------------------------------------------------------------

def build_field_xml(row: dict) -> ET.Element | None:
    """Return a <CustomField> element built from one data-dictionary row.

    Returns None when the field should be skipped (e.g. a picklist with no
    values defined — Salesforce requires at least one value).
    """
    field_api = row.get("Field API Name", "").strip()
    raw_type = row.get("Data Type", "").strip()
    label = (row.get("Field Label") or row.get("Field Label (JA)") or "").strip()
    type_specific = str(row.get("Type Specific Value") or "").strip()

    sf_type, is_formula = normalize_type(raw_type)

    # Reference target (used by Lookup/MasterDetail and the required-gate below).
    ref_to_val = str(row.get("Reference To") or type_specific or "").strip()
    # Standard objects that reject a Restrict/Cascade child lookup — only
    # SetNull is allowed there, and SetNull cannot coexist with `required`.
    # A required Lookup to one of these must degrade to an optional SetNull
    # lookup (requiredness is then enforced via page layout / validation rule).
    RESTRICT_INCOMPATIBLE_REFS = {"User", "Group", "RecordType", "Profile"}
    lookup_to_restrict_incompat = (
        sf_type == "Lookup" and ref_to_val in RESTRICT_INCOMPATIBLE_REFS
    )

    # Optional per-field override from the sheet's `deleteConstraint` column.
    # Blank => fall back to the smart default below.
    _DC_CANON = {"setnull": "SetNull", "restrict": "Restrict",
                 "cascade": "Cascade", "cascadedelete": "Cascade"}
    explicit_dc = _DC_CANON.get(
        str(row.get("Delete Constraint") or "").strip().lower(), "")

    # Resolve the effective deleteConstraint for Lookup fields (MasterDetail
    # manages its own delete behavior and takes no <deleteConstraint>).
    lookup_delete_constraint = ""   # "" => omit element (SF default = SetNull)
    if sf_type == "Lookup":
        if explicit_dc:
            lookup_delete_constraint = explicit_dc
        elif is_truthy(row.get("Required", "")):
            # A required Lookup must declare a delete constraint; Restrict is
            # the safe default (blocks deleting a parent still referenced).
            lookup_delete_constraint = "Restrict"
        # User/Group/RecordType/Profile reject Restrict/Cascade child lookups.
        if lookup_to_restrict_incompat and lookup_delete_constraint in ("Restrict", "Cascade"):
            lookup_delete_constraint = "SetNull"
    # A SetNull lookup (explicit or degraded) cannot be `required`.
    suppress_required = (sf_type == "Lookup" and lookup_delete_constraint == "SetNull")

    root = ET.Element(qname("CustomField"))
    set_text(root, "fullName", field_api)
    set_text(root, "label", label or field_api)

    if is_formula:
        set_text(root, "type", sf_type)
        # Column H ("Type Specific Value") is the formula body for Formula
        # fields.  The separate defaultValue column must not be used as a
        # formula fallback.
        formula_expr = type_specific
        if not formula_expr:
            # A formula field CANNOT deploy with an empty formula. Inject a
            # return-type-appropriate DUMMY so the field is deployable; the real
            # expression is supplied later by the client.
            formula_expr = _DUMMY_FORMULA.get(sf_type, '"TBD"')
            print(f"  ⚠️  {field_api}: empty formula body — injected dummy "
                  f"({sf_type}) formula {formula_expr!r} to keep it deployable.")
        set_text(root, "formula", formula_expr)
        set_text(root, "formulaTreatBlanksAs", "BlankAsZero")
        if sf_type in ("Number", "Currency", "Percent"):
            set_text_if(root, "precision", row.get("Precision"))
            set_text_if(root, "scale", row.get("Scale"))

    elif sf_type == "Text":
        set_text(root, "type", "Text")
        set_text_if(root, "length", row.get("Length"))

    elif sf_type == "TextArea":
        set_text(root, "type", "TextArea")
        set_text_if(root, "length", row.get("Length"))

    elif sf_type == "LongTextArea":
        set_text(root, "type", "LongTextArea")
        set_text_if(root, "length", row.get("Length"))
        # Salesforce REQUIRES visibleLines for LongTextArea; default blank -> 3.
        set_text(root, "visibleLines", str(row.get("Visible Lines") or "3").strip() or "3")

    elif sf_type == "Html":
        set_text(root, "type", "Html")
        set_text_if(root, "length", row.get("Length"))
        # Salesforce REQUIRES visibleLines for Html (Rich Text); default blank -> 3.
        set_text(root, "visibleLines", str(row.get("Visible Lines") or "3").strip() or "3")

    elif sf_type in ("Number", "Currency", "Percent"):
        set_text(root, "type", sf_type)
        set_text_if(root, "precision", row.get("Precision"))
        set_text_if(root, "scale", row.get("Scale"))

    elif sf_type in ("Email", "Phone", "Url"):
        set_text(root, "type", sf_type)
        length_val = str(row.get("Length") or "").strip()
        if length_val:
            set_text(root, "length", length_val)

    elif sf_type in ("Date", "DateTime", "Time"):
        set_text(root, "type", sf_type)

    elif sf_type == "Checkbox":
        set_text(root, "type", "Checkbox")
        default_val = row.get("Default Value", "")
        set_text(root, "defaultValue", "true" if is_truthy(default_val) else "false")

    elif sf_type in ("Picklist", "MultiselectPicklist"):
        set_text(root, "type", sf_type)
        if sf_type == "MultiselectPicklist":
            set_text_if(root, "visibleLines", row.get("Visible Lines"))
        restricted = is_truthy(row.get("Restricted", ""))
        global_set = str(row.get("Global Value Set") or "").strip()
        picklist_str = str(row.get("Picklist Values") or type_specific or "").strip()
        default_val = str(row.get("Default Value") or "").strip()
        if not _build_picklist_valueset(root, picklist_str, restricted, global_set, default_val):
            field_api = row.get("Field API Name", "").strip()
            print(f"Skipping {field_api}: picklist has no values and no Global Value Set defined.")
            return None

    elif sf_type in ("Lookup", "MasterDetail", "Hierarchy"):
        set_text(root, "type", sf_type)
        # deleteConstraint: honor the sheet's explicit value when present,
        # otherwise the smart default resolved above (required custom lookup ->
        # Restrict; required lookup to User/Group/RecordType/Profile -> SetNull;
        # optional lookup -> omit, i.e. SF default SetNull).
        if lookup_delete_constraint:
            set_text(root, "deleteConstraint", lookup_delete_constraint)
        ref_to = str(row.get("Reference To") or type_specific or "").strip()
        if ref_to:
            set_text(root, "referenceTo", ref_to)
        rel_label = str(row.get("Relationship Label") or "").strip()
        rel_name = str(row.get("Relationship Name") or "").strip()
        if rel_label:
            set_text(root, "relationshipLabel", rel_label)
        if rel_name:
            set_text(root, "relationshipName", rel_name)
        else:
            # Salesforce REQUIRES <relationshipName> for Lookup /
            # MasterDetail / Hierarchy fields, and the canonical value is
            # ALWAYS dictated by the Org (visible to Pull-from-Org as
            # `<relationshipName>` in the field-meta XML).  Auto-deriving
            # one (e.g. `Account__c` → `Account`) is unsafe — it collides
            # with built-in child relationship names on standard objects
            # and the sheet's other Lookup fields.  We surface the
            # missing value loudly so the operator can populate column
            # `relationshipName` from the Org instead of letting the
            # generator invent a name.
            field_api = str(row.get("Field API Name") or "").strip()
            obj_api = str(
                row.get("Object API Name") or row.get("_SheetName") or ""
            ).strip()
            print(
                f"⚠️  {obj_api}.{field_api}: blank 'relationshipName' for "
                f"a {sf_type} field. Salesforce REQUIRES this value and we "
                f"will NOT auto-derive it (collisions are silent). Run "
                f"'Import Selected Sheets from Org to Repo' for this object "
                f"so the canonical relationshipName flows into the sheet, "
                f"then re-run Push."
            )

    elif sf_type == "AutoNumber":
        set_text(root, "type", "AutoNumber")
        display_fmt = str(row.get("Type Specific Value") or "").strip()
        if display_fmt:
            set_text(root, "displayFormat", display_fmt)
        set_text(root, "startingNumber", "1")

    elif sf_type == "EncryptedText":
        set_text(root, "type", "EncryptedText")
        set_text(root, "length", str(row.get("Length") or "175").strip() or "175")
        set_text(root, "maskChar", str(row.get("Mask Character") or "asterisk").strip() or "asterisk")
        set_text(root, "maskType", str(row.get("Mask Type") or "all").strip() or "all")

    elif sf_type == "Location":
        set_text(root, "type", "Location")
        set_text(root, "scale", str(row.get("Scale") or "5").strip() or "5")

    elif sf_type == "Summary":
        set_text(root, "type", "Summary")

    else:
        # Unknown / pass-through — surface it to the deployer
        set_text(root, "type", sf_type or "Text")

    # ---- Common optional metadata ----------------------------------------

    help_text = str(row.get("Help Text") or row.get("Inline Help Text") or "").strip()
    if help_text:
        set_text(root, "inlineHelpText", help_text)

    description = str(row.get("Description") or "").strip()
    if description:
        set_text(root, "description", description)

    # `required` is invalid at the schema level for these field types.
    # LongTextArea / Html (Rich Text) / Location / MultiselectPicklist cannot
    # be required; MasterDetail is implicitly required (never set explicitly).
    non_settable = {
        "Checkbox", "AutoNumber", "Summary",
        "LongTextArea", "Html", "Location",
        "MultiselectPicklist", "MasterDetail",
    }
    if sf_type not in non_settable and not is_formula:
        required_raw = row.get("Required", "")
        if is_truthy(required_raw) and not suppress_required:
            set_text(root, "required", "true")
        elif is_falsy(required_raw):
            # Explicit FALSE in the sheet → emit <required>false</required> so a
            # redeploy deterministically turns requiredness OFF on an existing
            # field (instead of relying on the element being omitted). Blank is
            # still left out (no opinion).
            set_text(root, "required", "false")

        if sf_type in ("Text", "Email", "Phone", "Url", "Number", "Currency", "Percent"):
            if is_truthy(row.get("Unique", "")):
                set_text(root, "unique", "true")
            if is_truthy(row.get("External ID", "")):
                set_text(root, "externalId", "true")

        default_val = str(row.get("Default Value") or "").strip()
        if default_val and sf_type not in ("Picklist", "MultiselectPicklist"):
            set_text(root, "defaultValue", default_val)

    if is_truthy(row.get("Track History", "")):
        set_text(root, "trackHistory", "true")

    return root


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_fields(rows: list[dict]) -> None:
    objects_dir = Path("force-app/main/default/objects")
    processed_objects: set[str] = set()

    # Objects that own at least one MasterDetail field must be ControlledByParent.
    md_objects: set[str] = set()
    for row in rows:
        if row.get("_type") == "object_meta":
            continue
        obj_api = row.get("Object API Name", row.get("_SheetName", "")).strip()
        if obj_api and get_sf_field_type(row.get("Data Type", "").strip()) == "MasterDetail":
            md_objects.add(obj_api)

    # Objects that own at least one history-tracked field MUST have object-level
    # history enabled — otherwise the field deploy fails with "The entity: <Obj>
    # does not have history tracking enabled". Field-level <trackHistory>true
    # REQUIRES the CustomObject to carry <enableHistory>true</enableHistory>, so
    # derive it from the fields regardless of the object header's 項目履歴管理 flag.
    history_objects: set[str] = set()
    for row in rows:
        if row.get("_type") == "object_meta":
            continue
        obj_api = row.get("Object API Name", row.get("_SheetName", "")).strip()
        if obj_api and is_truthy(row.get("Track History", "")):
            history_objects.add(obj_api)

    # ---- Pass 1: write object-level metadata from header rows (rows 1-6) ----
    for row in rows:
        if row.get("_type") != "object_meta":
            continue
        obj_api = row.get("Object API Name", "").strip()
        if not obj_api:
            continue
        write_object_meta(
            obj_api,
            row.get("Object Label", ""),
            row.get("Object Description", ""),
            row.get("enableReports", ""),
            row.get("enableActivities", ""),
            # force object history ON when any field tracks history (dependency)
            row.get("enableHistory", "") or ("true" if obj_api in history_objects else ""),
            row.get("enableSearch", ""),
            name_field_label=row.get("Name Field Label", ""),
            name_field_type=row.get("Name Field Type", ""),
            name_field_display_format=row.get("Name Field Display Format", ""),
            has_master_detail=obj_api in md_objects,
        )
        processed_objects.add(obj_api)

    # ---- Pass 2: write custom field metadata ----
    for row in rows:
        if row.get("_type") == "object_meta":
            continue

        obj_api = row.get("Object API Name", row.get("_SheetName", "")).strip()
        obj_label = row.get("Object Label", "").strip()
        field_api = row.get("Field API Name", "").strip()
        raw_type = row.get("Data Type", "").strip()

        if not obj_api or not field_api or not raw_type:
            continue

        # Standard fields (no __c suffix) cannot be deployed via CustomField
        # metadata — Salesforce rejects labels and other overrides on them.
        if not field_api.endswith("__c"):
            continue

        # Fallback: ensure object dir exists even if no object_meta row arrived
        if obj_api not in processed_objects:
            ensure_object_meta(obj_api, obj_label)
            processed_objects.add(obj_api)

        fields_dir = objects_dir / obj_api / "fields"
        fields_dir.mkdir(parents=True, exist_ok=True)

        filepath = fields_dir / f"{field_api}.field-meta.xml"
        root = build_field_xml(row)
        if root is None:
            continue

        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")

        tree = ET.ElementTree(root)
        with filepath.open("wb") as f:
            f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            tree.write(f, encoding="utf-8", xml_declaration=False)
            f.write(b"\n")

        print(f"Generated field: {obj_api}.{field_api}")

    # Note: the nameField block is now written authoritatively inside Pass 1
    # (via _append_name_field). The previous Pass-3 fallback that scanned the
    # fields directory for a Text/AutoNumber field to retrofit `<nameField>`
    # has been removed because (a) it produced an invalid flat element and
    # (b) it was a guess rather than a sourced value from the sheet.


def main() -> None:
    input_path = Path("temp_updates.json")
    if not input_path.exists():
        print("No temp_updates.json found — skipping field XML generation.")
        return

    with input_path.open("r", encoding="utf-8") as f:
        rows = json.load(f)

    if not rows:
        print("No rows in temp_updates.json — nothing to generate.")
        return

    print(f"Processing {len(rows)} row(s) for custom field XML generation...")
    process_fields(rows)
    print("Custom field XML generation complete.")


if __name__ == "__main__":
    main()
