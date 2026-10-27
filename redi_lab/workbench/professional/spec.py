"""Provider-independent ProjectSpec v1 and requirement-derived Ora plans."""
from __future__ import annotations

import hashlib
import json
import re
from .spec_schema import VERSION, REQUIRED, PROJECT_SPEC_SCHEMA, VISUAL_VERSION, VISUAL_PROJECT_SPEC_SCHEMA
from .runtime import field_contract, field_value, database_contract, FIELD_CONTRACT_VERSION
from .visual import validate_requirements, visual_task, VISUAL_MANIFEST_VERSION

CONTRACT = "professional_python_product_v1"
BACKEND = "openai-live-professional-product-v1"
MODEL = "gpt-5.6-terra"
FIELDS = {"text", "long_text", "email", "integer", "money", "date", "enum", "reference", "boolean"}


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value):
        raise ValueError("invalid machine identifier: " + str(value))
    return value


def validate_spec(spec):
    if spec.get("schema_version") not in (VERSION,VISUAL_VERSION):
        raise ValueError("unsupported ProjectSpec version")
    missing = [key for key in REQUIRED if key not in spec]
    if missing: raise ValueError("ProjectSpec missing: " + ", ".join(missing))
    for key in ("product_name", "primary_goal", "target_user"):
        if not isinstance(spec[key], str) or not spec[key].strip(): raise ValueError("missing " + key)
    if spec["product_type"] not in {"business_workflow", "utility", "visual_social"}:
        raise ValueError("unsupported product family")
    if not isinstance(spec["entities"], list) or len(spec["entities"]) > 12:
        raise ValueError("entities must be a bounded list")
    entities = {}
    for entity in spec["entities"]:
        name = identifier(entity["id"])
        if name in entities: raise ValueError("duplicate entity")
        entities[name] = entity
        if not entity.get("label") or not entity.get("plural") or not entity.get("fields"):
            raise ValueError("entity labels and fields required")
        names = set()
        for field in entity["fields"]:
            key = identifier(field["id"])
            if key in names or key in {"id", "created_at", "updated_at"}: raise ValueError("duplicate/reserved field")
            names.add(key)
            if field.get("type") not in FIELDS or not field.get("label"):
                raise ValueError("invalid field type/label")
            if field["type"] == "enum" and not field.get("choices"):
                raise ValueError("enum choices required")
            if field['type'] in {'text','long_text','email'}:
                lower, upper = field.get('min'), field.get('max')
                if lower is not None and (type(lower) is not int or lower < 0):
                    raise ValueError('Text minimum must be a nonnegative character count')
                if upper is not None and (type(upper) is not int or upper < 1 or lower is not None and lower > upper):
                    raise ValueError('Text maximum must be a positive character count')
            if "sample" not in field: raise ValueError("representative field sample required")
            field_contract(field)
            field_value(field, field['sample'])
            if 'default' in field: field_value(field, field['default'])
        if entity.get("display_field") not in names: raise ValueError("invalid display field")
    for entity in entities.values():
        for field in entity["fields"]:
            if field["type"] == "reference" and field.get("references") not in entities:
                raise ValueError("unknown relationship target")
    # Explicitly bound v1 to acyclic ownership relationships.
    ordered = []
    while len(ordered) < len(entities):
        ready = [name for name, e in entities.items() if name not in ordered and all(
            f.get("references") in ordered for f in e["fields"] if f["type"] == "reference")]
        if not ready: raise ValueError("cyclic entity relationships require a later contract")
        ordered.extend(ready)
    for flow in spec["workflows"]:
        e = entities[flow["entity"]]
        f = next(f for f in e["fields"] if f["id"] == flow["field"])
        if f["type"] != "enum" or set(flow["transitions"]) != set(f["choices"]):
            raise ValueError("workflow must cover the declared enum")
        if flow["initial"] not in f["choices"] or any(
                s not in f["choices"] for targets in flow["transitions"].values() for s in targets):
            raise ValueError("invalid workflow transition")
    views = {}
    for view in spec["views"]:
        key = identifier(view["id"])
        if key in views or not view.get("title"): raise ValueError("invalid/duplicate view")
        if view["kind"] not in {"dashboard", "table", "board", "feed", "gallery", "calculator"}:
            raise ValueError("unsupported view kind")
        if view["kind"] not in {"dashboard", "calculator"} and view.get("entity") not in entities:
            raise ValueError("view references unknown entity")
        views[key] = view
    if not views or set(spec["navigation"]) != set(views) or len(spec["navigation"]) != len(views):
        raise ValueError("every view must be reachable exactly once from navigation")
    if set(entities) - {v.get('entity') for v in views.values()}:
        raise ValueError('every entity requires a reachable management view')
    if len({(w['entity'],w['field']) for w in spec['workflows']}) != len(spec['workflows']):
        raise ValueError('duplicate workflow contract')
    for state in ("empty", "loading", "success", "validation_error", "backend_error", "destructive_confirmation"):
        if not spec["states"].get(state): raise ValueError("missing application state: " + state)
    for metric in spec["metrics"]:
        identifier(metric["id"])
        if metric["entity"] not in entities or metric.get("operation", "count") not in {"count", "sum"}:
            raise ValueError("unsupported derived metric")
        if metric.get('operation') == 'sum' and not any(f['id'] == metric.get('field') and f['type'] in {'money','integer'} for f in entities[metric['entity']]['fields']):
            raise ValueError('sum metric requires a declared money/integer field')
        where = metric.get("where")
        if where and (where["field"] not in {f["id"] for f in entities[metric["entity"]]["fields"]} or not isinstance(where["values"], list)):
            raise ValueError("invalid metric filter")
    if spec["integrations"]:
        raise ValueError("external integrations need an explicitly provisioned adapter; v1 is local-only")
    if not spec["journeys"] or not spec["acceptance_criteria"] or not spec["provenance"]:
        raise ValueError("journeys, acceptance and provenance are mandatory")
    if any(v["kind"] == "calculator" for v in views.values()) and not spec.get("calculator"):
        raise ValueError("calculator contract required")
    supported_rules = {"required_when", "compare", "unique", "restrict_delete"}
    if any(rule.get("kind") not in supported_rules for rule in spec["business_rules"]):
        raise ValueError("unsupported executable business rule")
    for rule in spec['business_rules']:
        if rule.get('entity') not in entities: raise ValueError('unknown business rule entity')
        fields = {f['id']: f for f in entities[rule['entity']]['fields']}
        keys = rule.get('fields', []) if rule['kind']=='unique' else [rule['field'],rule['when_field']] if rule['kind']=='required_when' else [rule['left'],rule['right']] if rule['kind']=='compare' else []
        if any(k not in fields for k in keys) or rule['kind']=='unique' and not keys:
            raise ValueError('unknown business rule field')
        if rule['kind']=='compare' and (rule.get('op') not in {'lte','gte'} or any(fields[k]['type'] not in {'integer','money'} for k in keys)):
            raise ValueError('invalid numeric comparison rule')
    if spec['persistence'].get('engine') != 'sqlite': raise ValueError('unsupported persistence engine')
    if spec['persistence'].get('enabled') and not spec['persistence'].get('restart_required'):
        raise ValueError('Persistent storage must require restart persistence')
    if spec.get('schema_version') == VERSION and spec.get('visual_requirements'):
        raise ValueError('Visual requirements require ProjectSpec v2')
    if spec.get('schema_version') == VISUAL_VERSION and 'visual_requirements' not in spec:
        raise ValueError('ProjectSpec v2 requires visual_requirements')
    validate_requirements(spec)
    for rule in spec['business_rules']:
        if rule['kind']=='restrict_delete' and not rule.get('reason'):
            raise ValueError('Unconditional deletion prohibition requires an explicit source-grounded reason')
    return spec


def source_record(text, *, kind="owner_prompt", name="owner request"):
    return {"kind": kind, "name": name, "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}


def visual_request_intent(prompt):
    return bool(re.search(r'\b(image|imagery|illustrat\w*|visual\w*|photo\w*|picture\w*|graphic\w*|artwork|banner|thumbnail|background|cover art)\b',prompt,re.I))


def spec_prompt(prompt, sources, *, visual=False):
    version = VISUAL_VERSION if visual else VERSION
    schema = VISUAL_PROJECT_SPEC_SCHEMA if visual else PROJECT_SPEC_SCHEMA
    required = list(REQUIRED) + (['visual_requirements'] if visual else [])
    return ("Turn the owner request into one complete, conservative ProjectSpec JSON object. "
            "This is a local small web product. Preserve every requested entity, field, relationship, "
            "workflow, price and journey. Do not add authentication, cloud services or integrations unless requested. "
            "State reasonable assumptions explicitly; do not fabricate owner requirements. "
            "All lists below are required even when empty. Use schema_version=" + version + ". "
            "Required keys: " + json.dumps(required) + ". "
            "product_type is business_workflow, utility or visual_social. Requirements, journeys, validation_rules, "
            "security_privacy, responsive, accessibility, acceptance_criteria, assumptions, uncertainties are lists of strings. "
            "persistence is {enabled: boolean, engine: 'sqlite', restart_required: boolean}. "
            "states is an object with nonempty strings for empty, loading, success, validation_error, backend_error, destructive_confirmation. "
            "Entities: [{id: snake_case_singular, label: singular title, plural: plural title, display_field: field id, "
            "fields:[{id: snake_case, label: title, type: text|long_text|email|integer|money|date|enum|reference|boolean, "
            "required: boolean, unique: boolean, sample: representative valid value, "
            "default: optional default, min: optional numeric lower bound, choices: enum string list, "
            "references: referenced entity id for reference fields, append: true for an appendable notes field}]}]. "
            "Do not declare id/created_at/updated_at fields; framework provides them. Reference samples are integer 1. "
            "Relationships must be acyclic. Money samples/defaults are major-unit decimal STRINGS, e.g. '180.00', never cents. Only SQLite internals use integer cents. "
            "Required fields reject missing/null/blank before conversion; optional fields are nullable. Zero and false are values, not missing. "
            "For text, long_text and email, min/max are integer character-length limits enforced by the runtime. "
            "The v1 local UI searches each entity's display_field and filters enum/reference/boolean fields. "
            "If an owner requests search by another field or an unsupported filter, list it as an uncertainty rather than promising unsupported behavior. "
            "A board over a workflow entity renders every declared enum stage as a column; metrics render on the primary view and follow active search/filters. "
            "Defaults apply only to omitted insert fields; omitted update fields retain their previous value. Null optional wire metadata means no declared default/bound. "
            "Income/expense direction must be an explicit enum; transaction and budget amounts share scale 100. Dates use YYYY-MM-DD. "
            "workflows:[{entity: entity id, field: enum field id, initial: enum value, transitions: [{from_state: enum value, to_states: [allowed_next_values]}]}]. "
            "Do not invent transitions beyond the requested forward workflow; same-state edits are allowed. "
            "business_rules executable kinds: {kind:'required_when',entity,field,when_field,when_value}, "
            "{kind:'compare',entity,left,op:'lte'|'gte',right}, {kind:'unique',entity,fields:[field ids]}, "
            "{kind:'restrict_delete',entity,reason}. restrict_delete forbids ALL deletion and needs an explicit source-grounded reason. "
            "Ordinary foreign-key references already prevent deletion of referenced parents; do not add restrict_delete for this. "
            "Do not promise deletion in acceptance if an unconditional prohibition is declared. "
            "When persistence.enabled=true, restart_required must be true. "
            "Other human rules belong in validation_rules; enforce required, unique and min in fields. "
            "views:[{id, title, kind: dashboard|table|board|feed|gallery|calculator, entity: entity id if applicable"
            + (", slots:[{id: stable snake_case slot id, purpose: concise semantic description, region: header|content, accepts_visual: boolean}]" if visual else "") + "}]. "
            "navigation: ordered view ids. Include one manageable view for every entity. "
            "metrics:[{id,label,entity,operation:'count',where: optional {field,values:[matching enum values]}} "
            "or {id,label,entity,operation:'sum',field:money/integer field id,where:optional filter}]. "
            "Sum metrics use exact arithmetic, serialize money as decimal strings, and return zero on an empty collection. "
            "Metric values must be derived from real records. Prefer a few useful domain metrics. "
            "Optional calculator:{fields:[{id,label,type:'money'|'integer',sample}], expression: arithmetic expression using field ids, result_label}. "
            "provenance: list of source-reference STRINGS; integrations: [] for local apps. "+
            ("Establish product structure, views and their declared slots first. Then create visual_requirements only for explicitly requested imagery. Each visual requirement must copy view_id and slot_id exactly from one declared visual-capable slot in its view; these IDs are the only valid placement targets. Do not use prose placement or invent an undeclared target. Check the finite view/slot list you just declared before finalizing requirements. Use bounded purpose, aspect_ratio [width,height], minimum pixel size, style and subject, content and forbidden constraints, text_in_image, transparent, variants, png format, blocking flag and layout_dependency parallel or visual_first. Put consequential ambiguity in uncertainties. " if visual else "")+
            "The following strict schema is authoritative. Include every property, including schema_version; use null for inapplicable optional properties. "
            "Return JSON only, without Markdown. OUTPUT SCHEMA:\n"+json.dumps(schema)+
            "\nOWNER REQUEST:\n" + prompt + "\nSOURCE PROVENANCE:\n" + json.dumps(sources))


def build_plan(spec, run_id=None):
    validate_spec(spec)
    tasks = []
    def add(tid, persona, purpose, deps, paths, checks, behavior):
        tasks.append({"task_id": tid, "persona": persona, "station": persona,
            "purpose": purpose, "dependencies": deps, "owned_paths": paths,
            "input_contract": {"project_spec_sha256": sha(spec), "interface_version": CONTRACT,
                               "field_contract_version": FIELD_CONTRACT_VERSION,
                               "database_contract_sha256": sha(database_contract(spec)),
                               "producer_tasks": deps},
            "output_contract": {"format": "json_patches_v1", "paths": paths},
            "required_checks": checks, "expected_user_behavior": behavior,
            "acceptance_criteria": spec["acceptance_criteria"],
            "worker_profile": {"id": "professional_worker_v1", "model": MODEL, "reasoning_effort": "low"},
            "parallelism_eligible": not deps, "integration_dependencies": deps})
    foundation = []
    if spec["entities"] and spec["persistence"].get("enabled"):
        add("data_model", "andinus", "Implement declared entities and SQLite relationships", [], ["database.py"],
            ["schema", "foreign_keys", "persistence"], "Records persist and relationships remain valid")
        foundation.append("data_model")
    if spec["workflows"]:
        add("workflow", "bindus", "Implement declared lifecycle transitions", [], ["workflow.py"],
            ["transition_matrix", "invalid_transition"], "Only legitimate workflow transitions succeed")
        foundation.append("workflow")
    add("domain", "agron", "Implement business operations and validation", foundation, ["core.py"],
        ["contracts", "required_data", "duplicates", "relationships", "business_rules"], spec["journeys"])
    visual_ids=[]
    visual_first=[]
    for req in spec.get('visual_requirements', []):
        tid='visual_'+req['id'];visual_ids.append(tid)
        if req['layout_dependency']=='visual_first':visual_first.append(tid)
        task=visual_task(spec,req,run_id or sha(spec)[:12])
        tasks.append({"task_id":tid,"persona":"zana","station":"zana",
            "purpose":req['purpose'],"dependencies":[],"owned_paths":["assets/"+req['id']+".png"],
            "input_contract":{"project_spec_sha256":sha(spec),"visual_task":task},
            "output_contract":{"format":VISUAL_MANIFEST_VERSION,"paths":["assets/"+req['id']+".png"]},
            "required_checks":task['verification_requirements'],"expected_user_behavior":req['purpose'],
            "acceptance_criteria":spec['acceptance_criteria'],
            "worker_profile":{"id":"authorized_visual_worker_required","capability_rank":999},
            "parallelism_eligible":True,"integration_dependencies":[],"visual_requirement_id":req['id']})
    add("experience", "teuta", "Compose a product-specific responsive experience", ["domain",*visual_first], ["ui.py"],
        ["safe_rendering", "browser_desktop", "browser_mobile", "accessibility", "navigation", "states"], spec["journeys"])
    add("assembly", "gentius", "Assemble verified producer interfaces into runnable product", ["experience",*visual_ids], ["service.py"],
        ["public_contract", "integration", "launch"], "Owner can open the complete product")
    return {"schema_version": "ora_professional_plan_v1", "project_spec_sha256": sha(spec), "tasks": tasks,
            "planned_calls": len(tasks) + 1, "retry_policy": "autonomous_recovery_v1",
            "reviewer": {"persona": "medaurus", "version": "medaurus_requirement_review_v2", "execution_mode": "deterministic_offline",
                         "inputs": ["professional_product_review_evidence_v2"]}}
