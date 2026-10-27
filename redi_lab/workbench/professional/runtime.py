"""Portable local-product runtime v1. No product names, provider SDK or factory imports.

Generated modules provide schemas, workflows, domain rules and presentation.
This standard-library framework supplies safe persistence, transport and UI primitives.
"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit


class ValidationError(ValueError):
    def __init__(self, message, field=None):
        super().__init__(message); self.field = field


FIELD_CONTRACT_VERSION = 'product_fields_v2'


def field_contract(field):
    """Compile v1 field declarations into explicit, executable storage semantics."""
    kind = field['type']; required = field.get('required', False)
    nullable = field.get('nullable', not required)
    if type(required) is not bool or type(nullable) is not bool or required and nullable:
        raise ValueError('Invalid required/nullable declaration: ' + field['id'])
    default = field.get('default', '' if kind in {'text','long_text','email','date','enum'} and not required else None)
    return {'version': FIELD_CONTRACT_VERSION, 'id': field['id'], 'type': kind,
            'required': required, 'nullable': nullable, 'has_default': 'default' in field,
            'default': default, 'validation': {'min': field.get('min', 0), 'max': field.get('max'),
                'choices': field.get('choices'), 'unique': field.get('unique', False),
                'references': field.get('references'), 'empty_string': 'reject' if required else 'preserve_text_else_null'},
            'serialization': 'decimal_string_major_units_2dp' if kind == 'money' else 'boolean' if kind == 'boolean' else
                'integer' if kind in {'integer','reference'} else 'iso_date_YYYY-MM-DD' if kind == 'date' else 'string',
            'storage': 'integer_minor_units_scale_100' if kind == 'money' else 'integer' if kind in {'integer','reference','boolean'} else 'text'}


def field_value(field, value):
    """Validate before conversion. NULL, zero and an absent default are distinct."""
    c = field_contract(field); key = field['id']; kind = field['type']
    if value is None or isinstance(value, str) and not value.strip():
        if c['required'] or not c['nullable']:
            raise ValidationError(field['label'] + ' is required.', key)
        return '' if value == '' and kind in {'text','long_text','email','date','enum'} else None
    if kind == 'boolean':
        if type(value) is bool: return int(value)
        if type(value) is int and value in (0, 1): return value
        if isinstance(value, str): value = value.strip().lower()
        if isinstance(value, str) and value in ('true','false','0','1'): return int(value in ('true','1'))
        raise ValidationError('Enter true or false for ' + field['label'] + '.', key)
    if type(value) is bool or kind == 'money' and isinstance(value, float):
        raise ValidationError('Use an exact decimal string for ' + field['label'] + '.', key)
    value = str(value).strip()
    if len(value) > 100000: raise ValidationError(field['label'] + ' is too long.', key)
    if kind in {'text', 'long_text', 'email'}:
        lower, upper = c['validation']['min'], c['validation']['max']
        if lower is not None and len(value) < lower:
            raise ValidationError(field['label'] + ' is too short.', key)
        if upper is not None and len(value) > upper:
            raise ValidationError(field['label'] + ' must be at most ' + str(upper) + ' characters.', key)
    if kind in {'integer','reference','money'}:
        try:
            number = Decimal(value)
            if not number.is_finite(): raise InvalidOperation
            if kind != 'money' and number != number.to_integral_value(): raise InvalidOperation
            scaled = number * 100 if kind == 'money' else number
            if scaled != scaled.to_integral_value(): raise ValidationError('Use no more than two decimal places.', key)
            lower, upper = c['validation']['min'], c['validation']['max']
            if lower is not None and number < Decimal(str(lower)): raise ValidationError(field['label'] + ' is below its minimum.', key)
            if upper is not None and number > Decimal(str(upper)): raise ValidationError(field['label'] + ' is too large.', key)
            if not -(2**63) <= scaled < 2**63: raise ValidationError(field['label'] + ' exceeds SQLite integer range.', key)
            return int(scaled)
        except (InvalidOperation, ValueError) as exc:
            if isinstance(exc, ValidationError): raise
            raise ValidationError('Enter a valid number for ' + field['label'] + '.', key) from None
    if kind == 'enum' and value not in field['choices']: raise ValidationError('Choose a valid ' + field['label'].lower() + '.', key)
    if kind == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value): raise ValidationError('Enter a valid email address.', key)
    if kind == 'date':
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value): raise ValueError()
            datetime.strptime(value, '%Y-%m-%d')
        except ValueError: raise ValidationError('Enter a valid date.', key) from None
    return value


def database_contract(spec):
    entities=[]
    for entity in spec['entities']:
        fields=[]
        for field in entity['fields']:
            contract=field_contract(field)
            flow=next((w for w in spec['workflows'] if w['entity']==entity['id'] and w['field']==field['id']),None)
            if flow and not contract['has_default']:
                contract.update(has_default=True,default=flow['initial'],default_source='workflow_initial')
            fields.append(contract)
        entities.append({'id':entity['id'],'fields':fields})
    return {'version': FIELD_CONTRACT_VERSION,
        'entities': entities,
        'ids': 'positive integer primary keys; never nullable in record/update/delete calls',
        'connect': 'connect(path)->sqlite3.Connection; sqlite3.Row; foreign_keys ON; caller closes; no implicit demo data',
        'api': {'save(entity,values,record_id=None)':'int ID; omission preserves existing values on update; defaults apply only on insert',
                'record(entity,id)':'dict with all declared fields plus id/created_at/updated_at',
                'records(entity,filters=None,month=None,date_field=None,search=None)':'list of record dicts; typed exact field filters and declared date-field YYYY-MM prefix; search text fields', 'delete(entity,id)':'None; references restrict parent deletion',
                'state()':'{records: entity->list, metrics: metric_id->exact value}', 'close()':'idempotent'},
        'errors':'ValidationError with field before coercion; rejected writes leave state unchanged',
        'money':'Input/output major-unit decimal strings; SQLite signed integer minor units, scale 100. No float inputs or arithmetic. Empty sums are 0.00. Income/expense direction is a declared enum/filter, never inferred from names.',
        'schema_changes':'Idempotent initialization. Incompatible existing schemas fail closed; no automatic migration or data loss.'}


def ident(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value): raise ValueError("Invalid schema identifier")
    return '"' + value + '"'


def open_database(path, schema):
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        for entity in schema:
            fields = ['id INTEGER PRIMARY KEY', 'created_at TEXT NOT NULL', 'updated_at TEXT NOT NULL']
            for f in entity["fields"]:
                contract = field_contract(f)
                kind = "INTEGER" if f["type"] in {"integer", "money", "reference", "boolean"} else "TEXT"
                column = ident(f["id"]) + " " + kind
                if not contract['nullable']: column += " NOT NULL"
                if contract['has_default']:
                    default = field_value(f, contract['default'])
                    column += ' DEFAULT ' + ('NULL' if default is None else str(default) if type(default) is int else "'" + default.replace("'", "''") + "'")
                if f.get("unique"): column += " UNIQUE"
                if f["type"] == "reference": column += " REFERENCES " + ident(f["references"]) + "(id) ON DELETE RESTRICT"
                fields.append(column)
            conn.execute("CREATE TABLE IF NOT EXISTS " + ident(entity["id"]) + "(" + ",".join(fields) + ")")
            columns = {r['name']:r for r in conn.execute('PRAGMA table_info('+ident(entity['id'])+')')}
            expected = {'id','created_at','updated_at'} | {f['id'] for f in entity['fields']}
            if set(columns) != expected or any(columns[f['id']]['type'] != ('INTEGER' if f['type'] in {'integer','money','reference','boolean'} else 'TEXT')
                    or bool(columns[f['id']]['notnull']) != (not field_contract(f)['nullable']) for f in entity['fields']):
                raise ValidationError('Existing database schema differs from declared contract; explicit migration required.')
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def transition_allowed(workflows, entity, field, old, new):
    flow = next((w for w in workflows if w["entity"] == entity and w["field"] == field), None)
    return flow is None or old == new or new in flow["transitions"].get(old, [])


class DataService:
    def __init__(self, path, spec, connector=None, workflows=None, rules=None):
        self.spec = spec
        self.entities = {e["id"]: e for e in spec["entities"]}
        self.workflows = workflows if workflows is not None else spec["workflows"]
        self.rules = rules if rules is not None else spec["business_rules"]
        self.conn = connector(path) if connector else open_database(path, spec["entities"])

    def close(self):
        if self.conn is not None: self.conn.close(); self.conn = None

    def entity(self, name):
        if name not in self.entities: raise ValidationError("This collection does not exist.")
        return self.entities[name]

    def records(self, name, *, filters=None, month=None, date_field=None, search=None):
        self.entity(name)
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM " + ident(name) + " ORDER BY id DESC")]
        for row in rows:
            for f in self.entities[name]["fields"]:
                if f["type"] == "money" and row[f["id"]] is not None: row[f["id"]] = format(Decimal(row[f["id"]]) / 100, ".2f")
                if f['type'] == 'boolean' and row[f['id']] is not None: row[f['id']] = bool(row[f['id']])
        fields={f['id']:f for f in self.entities[name]['fields']}
        for key,value in (filters or {}).items():
            if key not in fields: raise ValidationError('Unknown filter field.', key)
            expected=field_value(fields[key],value)
            if fields[key]['type']=='money' and expected is not None: expected=format(Decimal(expected)/100,'.2f')
            rows=[r for r in rows if r[key]==expected]
        if month is not None:
            if date_field not in fields or fields[date_field]['type']!='date': raise ValidationError('Select a declared date field.')
            try:
                if not re.fullmatch(r'\d{4}-\d{2}',month): raise ValueError()
                datetime.strptime(month,'%Y-%m')
            except (ValueError,TypeError): raise ValidationError('Enter a valid month.') from None
            rows=[r for r in rows if r[date_field] and r[date_field].startswith(month+'-')]
        if search:
            rows=[r for r in rows if any(str(r[f['id']] or '').casefold().find(str(search).casefold())>=0 for f in fields.values() if f['type'] in {'text','long_text','email'})]
        return rows

    def record(self, name, record_id):
        if record_id is None or type(record_id) is bool or not re.fullmatch(r'[1-9][0-9]*', str(record_id)):
            raise ValidationError('A positive record ID is required.', 'id')
        record = next((r for r in self.records(name) if r["id"] == int(record_id)), None)
        if record is None: raise ValidationError("This record could not be found. It may have been removed.")
        return record

    def save(self, name, values, record_id=None):
        entity = self.entity(name)
        old = self.record(name, record_id) if record_id is not None else None
        fields = entity["fields"]
        unknown=set(values)-{f['id'] for f in fields}
        if unknown: raise ValidationError('Unknown field: ' + sorted(unknown)[0], sorted(unknown)[0])
        incoming = dict(old or {})
        incoming.update(values)
        result = {}
        for f in fields:
            key, kind = f["id"], f["type"]
            flow = next((w for w in self.workflows if w["entity"] == name and w["field"] == key), None)
            contract = field_contract(f)
            default = f.get('default', flow['initial'] if flow else contract['default'])
            value = field_value(f, incoming.get(key, default))
            if flow and value is None: raise ValidationError(f['label'] + ' is required.', key)
            if kind == 'enum' and value is not None:
                if old and not transition_allowed(self.workflows, name, key, old[key], value):
                    raise ValidationError('This status change is not allowed. Choose the next available status.', key)
                if not old and flow and value != flow['initial']:
                    raise ValidationError('New records must start in ' + flow['initial'] + '.', key)
            if kind == 'reference' and value is not None: self.record(f['references'], value)
            result[key] = value
        for rule in self.rules:
            if rule["entity"] != name: continue
            if rule["kind"] == "required_when" and str(result.get(rule["when_field"])) == str(rule["when_value"]) and result.get(rule["field"]) in (None, ""):
                raise ValidationError("Complete " + rule["field"].replace("_", " ") + " before this status change.", rule["field"])
            if rule["kind"] == "compare":
                left, right = result.get(rule["left"]), result.get(rule["right"])
                if left is not None and right is not None and not (left <= right if rule["op"] == "lte" else left >= right):
                    raise ValidationError("These values do not satisfy the required business rule.", rule["left"])
            if rule["kind"] == "unique":
                query = " AND ".join(ident(k) + " IS ?" for k in rule["fields"])
                row = self.conn.execute("SELECT id FROM " + ident(name) + " WHERE " + query,
                                        [result[k] for k in rule["fields"]]).fetchone()
                if row and row[0] != record_id: raise ValidationError("A record with these details already exists.")
        now = datetime.now(timezone.utc).isoformat()
        result["updated_at"] = now
        try:
            if old:
                self.conn.execute("UPDATE " + ident(name) + " SET " + ",".join(ident(k) + "=?" for k in result) + " WHERE id=?", [*result.values(), record_id])
            else:
                result["created_at"] = now
                cursor = self.conn.execute("INSERT INTO " + ident(name) + "(" + ",".join(ident(k) for k in result) + ") VALUES (" + ",".join("?" for _ in result) + ")", list(result.values()))
                record_id = cursor.lastrowid
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            raise ValidationError("A record with these details already exists, or a linked record is missing.") from None
        return int(record_id)

    def append_note(self, name, record_id, field, note):
        if not any(f["id"] == field and f.get("append") for f in self.entity(name)["fields"]):
            raise ValidationError("Notes are not available for this field.")
        if not note.strip(): raise ValidationError("Write a note before saving.", field)
        old = self.record(name, record_id)
        return self.save(name, {field: (old[field] + "\n" if old[field] else "") + note.strip()}, record_id)

    def delete(self, name, record_id):
        self.record(name, record_id)
        if any(r["kind"] == "restrict_delete" and r["entity"] == name for r in self.rules):
            raise ValidationError("These records must be retained and cannot be deleted.")
        try:
            self.conn.execute("DELETE FROM " + ident(name) + " WHERE id=?", (int(record_id),)); self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback(); raise ValidationError("Remove or reassign linked records before deleting this record.") from None

    def state(self):
        data = {name: self.records(name) for name in self.entities}
        return {"records": data, "metrics": metric_values(self.spec, data)}


def metric_values(spec, records):
    """One definition for persisted and filtered display summaries."""
    entities = {entity['id']: entity for entity in spec['entities']}
    metrics = {}
    for metric in spec['metrics']:
        rows = records[metric['entity']]
        where = metric.get('where')
        if where: rows = [row for row in rows if row[where['field']] in where['values']]
        if metric['operation'] == 'count': metrics[metric['id']] = len(rows)
        else:
            field = next(f for f in entities[metric['entity']]['fields'] if f['id'] == metric['field'])
            total = sum((Decimal(str(row[metric['field']])) for row in rows if row[metric['field']] is not None), Decimal(0))
            metrics[metric['id']] = format(total, '.2f') if field['type'] == 'money' else int(total)
    return metrics


def presentation_contract(spec):
    """Compile UI capabilities from structured entities/views, independent of view kind.

    The display field is the declared searchable identity. Exact enum and
    reference fields have bounded, meaningful filter options. This contract
    lives in the trusted runtime so historical v1 ProductSpecs need no rewrite.
    """
    entities = {entity['id']: entity for entity in spec['entities']}
    primary = spec['navigation'][0]
    result = {}
    for view in spec['views']:
        entity = entities.get(view.get('entity'))
        if entity is None:
            result[view['id']] = {'search_fields': [], 'filter_fields': [], 'metrics': view['id'] == primary or view['kind'] == 'dashboard'}
            continue
        display = next(field for field in entity['fields'] if field['id'] == entity['display_field'])
        searchable = [display['id']] if display['type'] in {'text', 'long_text', 'email'} else []
        filterable = [field['id'] for field in entity['fields'] if field['type'] in {'enum', 'reference', 'boolean'}]
        workflow_field = next((flow['field'] for flow in spec['workflows'] if flow['entity'] == entity['id']), None)
        if workflow_field is None and view['kind'] == 'board':
            workflow_field = next((field['id'] for field in entity['fields'] if field['type'] == 'enum'), None)
        result[view['id']] = {'search_fields': searchable, 'filter_fields': filterable,
                              'metrics': view['id'] == primary or view['kind'] == 'dashboard',
                              'workflow_field': workflow_field,
                              'detail_panel': view['kind'] in {'board', 'gallery', 'feed'}}
    return result


def select_records(entity, rows, contract, search='', filters=None):
    """Read-only collection selection; never changes persisted records."""
    filters = filters or {}
    fields = {field['id']: field for field in entity['fields']}
    if set(filters) - set(contract['filter_fields']): raise ValidationError('Unknown collection filter.')
    selected = list(rows)
    for key, raw in filters.items():
        if raw == '': continue
        expected = field_value(fields[key], raw)
        selected = [row for row in selected if row[key] == expected]
    query = str(search).strip().casefold()
    if query:
        selected = [row for row in selected if any(query in str(row[key] or '').casefold() for key in contract['search_fields'])]
    return selected


def calculate(contract, values):
    numbers = {}
    def visit(node):
        if isinstance(node, ast.Expression): return visit(node.body)
        if isinstance(node, ast.Name) and node.id in numbers: return numbers[node.id]
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}: return Decimal(str(node.value))
        if isinstance(node, ast.BinOp):
            a, b = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add): return a+b
            if isinstance(node.op, ast.Sub): return a-b
            if isinstance(node.op, ast.Mult): return a*b
            if isinstance(node.op, ast.Div): return a/b
        raise ValidationError("Unsupported calculation.")
    try:
        numbers.update({f["id"]: Decimal(str(values.get(f["id"], "0"))) for f in contract["fields"]})
        value = visit(ast.parse(contract["expression"], mode="eval"))
        if not value.is_finite(): raise ValueError()
        return format(value.quantize(Decimal('.01')), 'f')
    except (ArithmeticError, ValueError): raise ValidationError("Enter valid numbers; division by zero is not allowed.") from None


CSS = '''
:root{--ink:#152a38;--muted:#526574;--line:#d7e0e5;--canvas:#f4f7f9;--accent:#155e75;--radius:14px}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:15px;line-height:1.55}a{color:var(--accent);text-underline-offset:4px}button,input,select,textarea{font:inherit}button,a,input,select,textarea,summary{outline-offset:4px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid #0369a1}button,.button,summary.action{min-height:44px;display:inline-flex;align-items:center;justify-content:center;padding:10px 18px;border-radius:9px;background:var(--accent);color:white;border:1px solid transparent;font-weight:650;cursor:pointer;text-decoration:none;gap:8px}button:disabled{opacity:.65;cursor:wait}.secondary{background:white;color:var(--ink);border-color:var(--line)}.danger{color:#9f1239;background:#fff1f2;border-color:#fecdd3}.shell{display:grid;grid-template-columns:244px minmax(0,1fr);min-height:100vh}.sidebar{padding:30px 20px;background:#132b39;color:#edf5fa}.brand{font-size:22px;font-weight:780;line-height:1.25;overflow-wrap:anywhere;margin:0 0 8px}.local-label{font-size:12px;color:#b9d4e0;letter-spacing:.08em;text-transform:uppercase}nav{margin-top:32px;display:grid;gap:8px}nav a{color:#d8e6ee;min-height:44px;padding:11px 14px;text-decoration:none;border-radius:9px;font-weight:600}nav a[aria-current=page]{background:#284655;color:#fff}main{max-width:1360px;width:100%;padding:42px 48px;min-width:0}.eyebrow{text-transform:uppercase;letter-spacing:.1em;font-size:12px;font-weight:750;color:var(--accent);margin:0 0 8px}h1{font-size:clamp(28px,3vw,40px);line-height:1.15;letter-spacing:-.035em;margin:0 0 14px}h2{font-size:20px;margin:0 0 18px}h3{font-size:17px;margin:0 0 10px}.muted{color:var(--muted)}.intro{max-width:70ch;margin:0 0 28px}.hero{display:flex;align-items:flex-start;justify-content:space-between;gap:24px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin:24px 0 32px}.metric,.panel{background:white;border:1px solid var(--line);border-radius:var(--radius);padding:24px}.metric-value{font-size:34px;font-weight:750;letter-spacing:-.04em}.metric-label{font-size:13px;color:var(--muted);font-weight:600}.panel{margin-bottom:24px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:20px}.toolbar h2{margin:0}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.field{display:flex;flex-direction:column;gap:7px;min-width:0}.field.wide{grid-column:1/-1}label{font-size:13px;font-weight:650}input,select,textarea{min-height:46px;width:100%;max-width:100%;border:1px solid #9cabb6;border-radius:8px;padding:11px 12px;background:white;color:var(--ink)}textarea{min-height:110px;resize:vertical}.form-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}.help{font-size:12px;color:var(--muted);margin:0}.alert{padding:16px 20px;border-radius:10px;border:1px solid #a7d5c4;background:#ecfdf5;color:#14532d;margin-bottom:24px}.alert.error{background:#fff1f2;border-color:#fecdd3;color:#881337}.empty{text-align:center;padding:48px 20px;background:#fff;border:1px dashed #b9c8d1;border-radius:var(--radius)}.empty p{max-width:50ch;margin:8px auto 24px;color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--muted);font-size:12px;letter-spacing:.02em;padding:12px;border-bottom:1px solid var(--line)}td{padding:16px 12px;border-bottom:1px solid #e8eef2;overflow-wrap:anywhere;max-width:240px}tbody tr:last-child td{border-bottom:0}.table-wrap{overflow:auto}.badge{display:inline-block;padding:4px 9px;border-radius:6px;background:#e8f0f5;color:#29485c;font-size:12px;font-weight:700;white-space:normal}.record-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}.record-card{background:white;border:1px solid var(--line);border-radius:var(--radius);padding:24px}.notes{white-space:pre-wrap;overflow-wrap:anywhere}.back{display:inline-block;margin-bottom:24px;min-height:44px;padding:10px 0}.footnote{font-size:12px;color:var(--muted);margin-top:32px}.skip{position:absolute;top:-100px;left:12px;background:white;padding:12px;z-index:3}.skip:focus{top:12px}details>summary{cursor:pointer}details[open]>summary{margin-bottom:24px}.loading{font-size:13px;color:var(--muted)}
@media(max-width:1000px){main{padding:32px 24px}.shell{grid-template-columns:210px minmax(0,1fr)}}
@media(max-width:680px){.shell{display:block}.sidebar{padding:20px}.brand{font-size:22px}nav{margin-top:18px;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}nav a{font-size:13px;padding:10px}main{padding:28px 18px}.hero{display:block}.hero .button{margin-bottom:18px}.field-grid{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.metric{padding:16px}.metric-value{font-size:28px}.panel{padding:18px}.record-grid{grid-template-columns:1fr}thead{display:none}table,tbody,tr,td{display:block;width:100%;max-width:none}tr{padding:12px 0;border-bottom:1px solid var(--line)}td{display:grid;grid-template-columns:100px minmax(0,1fr);gap:12px;padding:7px 0;border:0}td:before{content:attr(data-label);font-weight:650;color:var(--muted);font-size:12px}.form-actions>button{flex:1}.empty{padding:32px 18px}.toolbar{align-items:flex-start}}
.shell>* , main>* , .panel, .record-card, .metric, .board-column{min-width:0}
main, h1, h2, h3, p, .metric-value, .record-card, .panel{overflow-wrap:anywhere}
.filters{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:0 0 24px;padding:18px;background:white;border:1px solid var(--line);border-radius:var(--radius)}
.filters .field{flex:1 1 165px}.filters .field.search-field{flex:2 1 220px}.filters .form-actions{margin:0;align-items:center}
.filters .form-actions a{min-height:44px;display:inline-flex;align-items:center;padding:10px}
.board{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;align-items:start}
.board-column{padding:12px;background:#e8eef2;border-radius:var(--radius)}.board-column h2{font-size:15px;display:flex;justify-content:space-between;gap:8px}
.board-column .record-card{padding:14px;margin:0 0 12px}.board-column .record-card h3{font-size:15px}
.swatch{display:inline-block;width:12px;height:12px;border:1px solid #5b6b74;border-radius:50%;vertical-align:middle;margin-right:5px}
.detail-backdrop{position:fixed;inset:0;background:#0b223678;z-index:8}
.detail-drawer{position:fixed;top:0;right:0;bottom:0;width:min(620px,100%);z-index:9;background:var(--canvas);padding:28px;overflow:auto;box-shadow:-10px 0 35px #10233033}
.detail-drawer .panel{padding:20px}.detail-drawer .field-grid{grid-template-columns:1fr}
@media(max-width:1100px){.board{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:680px){.board{grid-template-columns:1fr}.detail-drawer{padding:18px}.filters{display:block}.filters .field{margin-bottom:12px}}
.generated-visual{margin:0 0 24px;min-width:0;max-width:100%}.generated-visual img{display:block;max-width:100%;height:auto;border-radius:12px}.hero .generated-visual{flex:0 1 42%;min-width:0}.hero .generated-visual img{width:100%;object-fit:cover}
'''

SCRIPT = '''document.addEventListener('submit',async function(event){const form=event.target;if(form.method.toLowerCase()!=='post')return;if(form.dataset.confirm&&!window.confirm(form.dataset.confirm)){event.preventDefault();return;}event.preventDefault();const button=event.submitter||form.querySelector('button[type=submit]');const original=button.textContent;button.disabled=true;button.textContent='Saving…';form.setAttribute('aria-busy','true');let status=form.querySelector('.loading');if(!status){status=document.createElement('p');status.className='loading';status.setAttribute('role','status');form.appendChild(status);}status.textContent='Saving your changes…';try{const response=await fetch(form.action,{method:'POST',body:new URLSearchParams(new FormData(form))});if(response.ok){window.location.href=response.url;return;}const doc=new DOMParser().parseFromString(await response.text(),'text/html');const next=doc.querySelector('main');if(next){document.querySelector('main').replaceWith(next);const alert=document.querySelector('[role=alert]');if(alert){alert.tabIndex=-1;alert.focus();}}else throw Error('Unable to save');}catch(error){status.setAttribute('role','alert');status.textContent='We could not save your changes. Check the connection and try again.';}finally{button.disabled=false;button.textContent=original;form.removeAttribute('aria-busy');}});document.addEventListener('keydown',function(event){if(event.key==='Escape'){const panel=document.querySelector('[data-detail-panel]');if(panel){event.preventDefault();window.location.href=panel.dataset.closeUrl;}}});if(document.querySelector('[data-detail-panel]'))document.querySelector('[data-detail-panel] h1')?.focus();'''


def e(value): return escape(str(value), quote=True)


def field_control(field, records, value="", prefix="create"):
    key, kind = field["id"], field["type"]
    fid = prefix + "-" + key
    required = " required" if field.get("required") else ""
    attrs = ' id="' + e(fid) + '" name="' + e(key) + '"' + required
    if kind in {'text', 'long_text', 'email'} and field.get('max') is not None:
        attrs += ' maxlength="' + e(int(field['max'])) + '"'
    if kind in {"enum", "reference", "boolean"}:
        options = [(str(x), str(x).replace('_', ' ').title()) for x in field.get("choices", [])]
        if kind == 'boolean':
            options = [('true', 'Yes'), ('false', 'No')]
            if type(value) is bool: value = str(value).lower()
        if kind == "reference":
            target = records.get(field["references"], [])
            options = [(str(r["id"]), str(r["_label"])) for r in target]
        control = '<select' + attrs + '><option value="">Choose ' + e(field["label"].lower()) + '</option>'
        control += ''.join('<option value="' + e(v) + '"' + (' selected' if str(value) == v else '') + '>' + e(label) + '</option>' for v, label in options) + '</select>'
    elif kind == "long_text": control = '<textarea' + attrs + '>' + e(value or '') + '</textarea>'
    else:
        input_type = {"money": "number", "integer": "number", "email": "email", "date": "date"}.get(kind, "text")
        extra = ' min="' + e(field.get('min', 0)) + '" step="' + ('.01' if kind == 'money' else '1') + '"' if kind in {"money", "integer"} else ''
        control = '<input type="' + input_type + '"' + attrs + extra + ' value="' + e('' if value is None else value) + '">'
    return '<div class="field' + (' wide' if kind == 'long_text' else '') + '"><label for="' + e(fid) + '">' + e(field["label"]) + (' <span aria-hidden="true">*</span>' if required else '') + '</label>' + control + '</div>'


def render_app(spec, design, state, *, view=None, record_id=None, notice="", error="", values=None, search="", filters=None):
    view = view or spec['navigation'][0]
    page = next((item for item in spec['views'] if item['id'] == view), None)
    if page is None: raise ValidationError('This page does not exist.')
    copy = next((item for item in design['views'] if item['id'] == view), {})
    visual_file = Path(__file__).with_name('visual_asset_handoff.json')
    visual_assets = json.loads(visual_file.read_text(encoding='utf-8')).get('assets', []) if visual_file.is_file() else []
    visual_by_id = {asset['asset_id']: asset for asset in visual_assets}
    def visual(slot):
        asset_id = design.get('visual_placements', {}).get(view + ':' + slot)
        asset = visual_by_id.get(asset_id)
        if not asset or asset.get('view_id') != view or asset.get('slot_id') != slot or asset.get('placement') != view + ':' + slot:
            return ''
        return '<figure class="generated-visual ' + e(slot) + '" data-asset-id="' + e(asset_id) + '"><img src="' + e(asset['path']) + '" width="' + str(asset['width']) + '" height="' + str(asset['height']) + '" alt="' + e(asset['purpose']) + '"></figure>'
    entity = next((item for item in spec['entities'] if item['id'] == page.get('entity')), None)
    contract = presentation_contract(spec)[view]
    records = {name: [dict(row) for row in rows] for name, rows in state['records'].items()}
    for declared in spec['entities']:
        for row in records[declared['id']]: row['_label'] = row[declared['display_field']]
    active_filters = {key: value for key, value in (filters or {}).items() if value != ''}
    selected = select_records(entity, records[entity['id']], contract, search, active_filters) if entity else []
    def url(vid, rid=None, preserve=False):
        query = {'view': vid}
        if rid is not None: query['id'] = rid
        if preserve and vid == view:
            if search: query['search'] = search
            query.update({'filter_' + key: value for key, value in active_filters.items()})
        return '/?' + urlencode(query)
    def entity_view(name): return next(item['id'] for item in spec['views'] if item.get('entity') == name)
    def label(field, value):
        if field['type'] == 'reference':
            return next((row['_label'] for row in records[field['references']] if row['id'] == value), '—')
        return value if value not in (None, '') else '—'
    def form(declared, row=None):
        rid = row['id'] if row else None
        data = dict(row or {})
        if values: data.update(values)
        path = '/records/' + declared['id'] + ('/' + str(rid) if rid else '')
        controls = ''
        for field in declared['fields']:
            default = field.get('default', '')
            flow = next((item for item in spec['workflows'] if item['entity'] == declared['id'] and item['field'] == field['id']), None)
            if flow: default = flow['initial']
            controls += field_control(field, records, data.get(field['id'], default), 'edit' if rid else 'create')
        return '<form method="post" action="' + path + '"><input type="hidden" name="_view" value="' + e(view) + '"><div class="field-grid">' + controls + '</div><div class="form-actions"><button type="submit">' + ('Save changes' if rid else 'Create ' + e(declared['label'].lower())) + '</button></div><p class="help">Fields marked * are required.</p></form>'
    def card(row):
        details = ''
        for field in entity['fields']:
            key = field['id'];value = row[key]
            if key == entity['display_field'] or field['type'] == 'long_text' or value in (None, ''): continue
            swatch = ''
            if ('color' in key or 'colour' in key) and isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                swatch = '<span class="swatch" style="background:' + value + '" aria-label="' + e(field['label']) + ' ' + e(value) + '"></span>'
            if field['type'] == 'reference':
                linked = next((item for item in records[field['references']] if item['id'] == value), None)
                if linked:
                    target = next(item for item in spec['entities'] if item['id'] == field['references'])
                    for color_field in target['fields']:
                        color = linked.get(color_field['id'])
                        if ('color' in color_field['id'] or 'colour' in color_field['id']) and isinstance(color, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                            swatch = '<span class="swatch" style="background:' + color + '" aria-label="' + e(color_field['label']) + ' ' + e(color) + '"></span>'
                            break
            details += '<p><span class="muted">' + e(field['label']) + '</span><br>' + swatch + e(label(field, value)) + '</p>'
        return '<article class="record-card"><h3><a href="' + url(view, row['id'], True) + '">' + e(row['_label']) + '</a></h3>' + details + '</article>'
    body = ''
    if notice: body += '<div class="alert" role="status">' + e(notice) + '</div>'
    if error: body += '<div class="alert error" role="alert">' + e(error) + '</div>'
    if record_id and entity:
        row = next((item for item in records[entity['id']] if item['id'] == int(record_id)), None)
        if not row: raise ValidationError('This record could not be found.')
        if contract['detail_panel']:
            body += '<div class="detail-backdrop" aria-hidden="true"></div><section class="detail-drawer" role="dialog" aria-modal="true" aria-label="' + e(entity['label']) + ' details" data-detail-panel data-close-url="' + e(url(view, preserve=True)) + '">'
        body += '<a class="back" href="' + url(view, preserve=True) + '">Close and return to ' + e(entity['plural'].lower()) + '</a>'
        body += '<p class="eyebrow">' + e(entity['label']) + ' · #' + str(row['id']) + '</p><h1 tabindex="-1">' + e(row['_label']) + '</h1><p class="intro muted">Review details, update the record and keep everything in sync.</p>'
        body += '<section class="panel"><h2>Details</h2>' + form(entity, row) + '</section>'
        for field in entity['fields']:
            if field.get('append'):
                body += '<section class="panel"><h2>' + e(field['label']) + '</h2><p class="notes">' + e(row.get(field['id']) or 'No notes yet.') + '</p><form method="post" action="/records/' + entity['id'] + '/' + str(row['id']) + '/notes"><input type="hidden" name="_view" value="' + e(view) + '"><input type="hidden" name="_field" value="' + e(field['id']) + '"><label for="append-note">New note</label><textarea id="append-note" name="note" required></textarea><div class="form-actions"><button type="submit">Add note</button></div></form></section>'
        body += '<details class="panel"><summary>Record actions</summary><p>Deletion is permanent. Linked records must be handled first.</p><form method="post" action="/records/' + entity['id'] + '/' + str(row['id']) + '/delete" data-confirm="Permanently delete this record? This cannot be undone."><input type="hidden" name="_view" value="' + e(view) + '"><input type="hidden" name="_confirm" value="yes"><button class="danger" type="submit">Delete record</button></form></details>'
        if contract['detail_panel']: body += '</section>'
    else:
        body += '<header class="hero"><div><p class="eyebrow">' + e(copy.get('eyebrow', spec['product_type'].replace('_', ' '))) + '</p><h1>' + e(page['title']) + '</h1><p class="intro muted">' + e(copy.get('intro', spec['primary_goal'])) + '</p></div>' + ''.join(visual(slot['id']) for slot in page.get('slots',[]) if slot['region']=='header') + '</header>'
        body += ''.join(visual(slot['id']) for slot in page.get('slots',[]) if slot['region']=='content')
        if contract['metrics'] and spec['metrics']:
            metric_records = dict(records)
            if entity: metric_records[entity['id']] = selected
            display_metrics = metric_values(spec, metric_records)
            body += '<div class="metrics">' + ''.join('<section class="metric"><div class="metric-label">' + e(metric['label']) + '</div><div class="metric-value" data-metric="' + e(metric['id']) + '">' + e(display_metrics[metric['id']]) + '</div></section>' for metric in spec['metrics']) + '</div>'
        if page['kind'] == 'dashboard':
            body += '<section class="panel"><h2>Workspace</h2><div class="record-grid">'
            for declared in spec['entities']:
                body += '<div><h3>' + e(declared['plural']) + '</h3><p class="muted">' + str(len(records[declared['id']])) + ' saved records</p><a class="button secondary" href="' + url(entity_view(declared['id'])) + '">Manage ' + e(declared['plural'].lower()) + '</a></div>'
            body += '</div></section>'
            if not any(records.values()): body += '<section class="empty"><h2>' + e(copy.get('empty_heading', 'Your workspace is ready')) + '</h2><p>' + e(copy.get('empty_body', 'Start with your first record. Your data will be saved locally.')) + '</p></section>'
        elif page['kind'] == 'calculator':
            calc = spec['calculator']
            body += '<section class="panel"><form method="post" action="/calculate"><input type="hidden" name="_view" value="' + e(view) + '"><div class="field-grid">' + ''.join(field_control(field, records, (values or {}).get(field['id'], ''), 'calc') for field in calc['fields']) + '</div><div class="form-actions"><button type="submit">Calculate</button></div></form></section>'
        elif entity:
            body += '<section class="panel"><details' + ('' if records[entity['id']] and not error else ' open') + '><summary class="action">New ' + e(entity['label'].lower()) + '</summary>' + form(entity) + '</details></section>'
            if contract['search_fields'] or contract['filter_fields']:
                body += '<form class="filters" method="get" action="/"><input type="hidden" name="view" value="' + e(view) + '">'
                if contract['search_fields']:
                    body += '<div class="field search-field"><label for="collection-search">Search ' + e(entity['plural'].lower()) + '</label><input id="collection-search" type="search" name="search" value="' + e(search) + '"></div>'
                for key in contract['filter_fields']:
                    field = next(item for item in entity['fields'] if item['id'] == key)
                    options = [(str(choice), str(choice).replace('_', ' ').title()) for choice in field.get('choices', [])] if field['type'] == 'enum' else [(str(item['id']), str(item['_label'])) for item in records[field['references']]] if field['type'] == 'reference' else [('true', 'Yes'), ('false', 'No')]
                    body += '<div class="field"><label for="filter-' + e(key) + '">Filter by ' + e(field['label'].lower()) + '</label><select id="filter-' + e(key) + '" name="filter_' + e(key) + '"><option value="">All ' + e(field['label'].lower()) + '</option>'
                    body += ''.join('<option value="' + e(value) + '"' + (' selected' if active_filters.get(key) == value else '') + '>' + e(title) + '</option>' for value, title in options) + '</select></div>'
                body += '<div class="form-actions"><button type="submit">Apply filters</button><a href="' + url(view) + '">Clear filters</a></div></form>'
            if not selected:
                filtered = bool(str(search).strip() or active_filters)
                body += '<section class="empty"><h2>' + ('No matching records' if filtered else e(copy.get('empty_heading', 'No ' + entity['plural'].lower() + ' yet'))) + '</h2><p>' + ('Try another search or clear the filters.' if filtered else e(copy.get('empty_body', 'Create the first record using the form above.'))) + '</p></section>'
            elif page['kind'] == 'board' and contract['workflow_field']:
                field = next(item for item in entity['fields'] if item['id'] == contract['workflow_field'])
                body += '<section class="board" aria-label="' + e(page['title']) + '">'
                for choice in field['choices']:
                    column = [row for row in selected if row[field['id']] == choice]
                    body += '<section class="board-column" data-stage="' + e(choice) + '"><h2>' + e(choice) + ' <span data-stage-count="' + e(choice) + '">' + str(len(column)) + '</span></h2>' + (''.join(card(row) for row in column) or '<p class="muted">No records in this stage.</p>') + '</section>'
                body += '</section>'
            elif page['kind'] in {'feed', 'gallery', 'board'}:
                body += '<section class="record-grid">' + ''.join(card(row) for row in selected) + '</section>'
            else:
                body += '<section class="panel"><div class="toolbar"><h2>' + e(entity['plural']) + '</h2><span class="muted">' + str(len(selected)) + ' records</span></div><div class="table-wrap"><table><thead><tr>' + ''.join('<th scope="col">' + e(field['label']) + '</th>' for field in entity['fields']) + '</tr></thead><tbody>'
                for row in selected:
                    body += '<tr>'
                    for field in entity['fields']:
                        cell = e(label(field, row[field['id']]))
                        if field['id'] == entity['display_field']: cell = '<a href="' + url(view, row['id'], True) + '">' + cell + '</a>'
                        elif field['type'] == 'enum': cell = '<span class="badge">' + cell.replace('_', ' ') + '</span>'
                        body += '<td data-label="' + e(field['label']) + '">' + cell + '</td>'
                    body += '</tr>'
                body += '</tbody></table></div></section>'
    nav = ''.join('<a href="' + url(vid) + '"' + (' aria-current="page"' if vid == view else '') + '>' + e(next(item['title'] for item in spec['views'] if item['id'] == vid)) + '</a>' for vid in spec['navigation'])
    accent = design.get('accent', '#155e75')
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', accent): raise ValueError('Invalid design color')
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + e(spec['product_name']) + ' · ' + e(page['title']) + '</title><style>' + CSS + ':root{--accent:' + accent + '}</style></head><body><a class="skip" href="#main">Skip to content</a><div class="shell"><aside class="sidebar"><p class="brand">' + e(spec['product_name']) + '</p><span class="local-label">Local workspace</span><nav aria-label="Main navigation">' + nav + '</nav></aside><main id="main" tabindex="-1">' + body + '<footer class="footnote">Stored on this device · Your changes persist across restarts</footer></main></div><script>' + SCRIPT + '</script></body></html>'


def sample_records(service, *, omitted=None):
    pending = list(service.spec['entities'])
    created = {}
    while pending:
        ready = [e for e in pending if all(f['references'] in created for f in e['fields'] if f['type']=='reference')]
        if not ready: raise ValueError('Cyclic fixture relationships')
        for ent in ready:
            values = {f['id']: (created[f['references']] if f['type']=='reference' else f['sample']) for f in ent['fields'] if f['id'] not in (omitted or {}).get(ent['id'], set())}
            for w in service.workflows:
                if w['entity']==ent['id'] and w['field'] not in (omitted or {}).get(ent['id'], set()): values[w['field']]=w['initial']
            created[ent['id']] = service.save(ent['id'], values)
            pending.remove(ent)
    return created


def acceptance(service_class, spec, private=False):
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory)/'acceptance.sqlite3')
        service = service_class(path)
        try:
            ids = sample_records(service)
            for ent in spec['entities']:
                required = next((f for f in ent['fields'] if f.get('required')), None)
                if required:
                    try: service.save(ent['id'], {required['id']:''}, ids[ent['id']])
                    except ValueError: pass
                    else: raise AssertionError('required data accepted')
            if private:
                for ent in spec['entities']:
                    for f in ent['fields']:
                        if f['type'] in {'money','integer'}:
                            try: service.save(ent['id'], {f['id']:'-1'}, ids[ent['id']])
                            except ValueError: pass
                            else: raise AssertionError('negative amount accepted')
                        if f['type']=='reference':
                            try: service.save(ent['id'], {f['id']:999999}, ids[ent['id']])
                            except ValueError: pass
                            else: raise AssertionError('missing relationship accepted')
            state = service.state()
        finally: service.close()
        service = service_class(path)
        try:
            if service.state()!=state: raise AssertionError('restart persistence mismatch')
        finally: service.close()
    return {'entities':len(spec['entities']), 'views':len(spec['views']), 'verified':True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database'); parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--ready-file',type=Path); parser.add_argument('--open-browser',action='store_true')
    parser.add_argument('--audit-script',type=Path)
    args=parser.parse_args(); sys.dont_write_bytecode=True
    project=Path(__file__).resolve().parents[1]
    manifest=json.loads((project/'package.json').read_text())
    for name,expected in manifest['files'].items():
        p=(project/name).resolve()
        if not p.is_relative_to(project) or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
            raise ValueError('Package integrity failed: '+name)
    fingerprint=hashlib.sha256((project/'package.json').read_bytes()).hexdigest()
    service=importlib.import_module(__package__+'.service')
    database=str(Path(args.database or project/'data'/'app.sqlite3').resolve())
    Path(database).parent.mkdir(parents=True,exist_ok=True)
    identity={'pid':os.getpid(),'project_path':str(project),'database':database,'package_sha256':fingerprint}
    class Handler(BaseHTTPRequestHandler):
        def reply(self,body,status=200,kind='text/html; charset=utf-8'):
            data=body.encode();self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
        def reply_bytes(self,data,status=200,kind='image/png'):
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
        def render(self,**kwargs): return service.run_ui(path=database,**kwargs)
        def do_GET(self):
            try:
                parsed=urlsplit(self.path);query={k:v[0] for k,v in parse_qs(parsed.query).items()}
                if parsed.path=='/api/health': return self.reply(json.dumps(identity),kind='application/json')
                if parsed.path=='/api/state':
                    app=service.build_app(database)
                    try: state=app.state()
                    finally: app.close()
                    return self.reply(json.dumps(state),kind='application/json')
                if re.fullmatch(r'/assets/[a-z0-9_]+\.png',parsed.path):
                    asset=(project/parsed.path.lstrip('/')).resolve()
                    if not asset.is_relative_to(project/'assets') or not asset.is_file():return self.reply('Asset not found',404)
                    return self.reply_bytes(asset.read_bytes())
                if parsed.path!='/': return self.reply('Page not found',404)
                html=self.render(view=query.get('view'),record_id=query.get('id'),notice=query.get('notice',''),
                                 search=query.get('search',''),filters={key[7:]: value for key,value in query.items() if key.startswith('filter_')})
                if args.audit_script and query.get('_audit')=='1': html=html.replace('</body>','<script>'+args.audit_script.read_text(encoding='utf-8')+'</script></body>')
                self.reply(html)
            except (ValueError,KeyError): self.reply('<h1>Page unavailable</h1><p>Check the link and return to your workspace.</p><a href="/">Go home</a>',404)
            except Exception: self.reply('<h1>Something went wrong</h1><p>Your saved data is safe. Please try again.</p><a href="/">Go home</a>',500)
        def do_POST(self):
            app=None;view=None;record_id=None;form={}
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536: raise ValidationError('The submitted form is too large or empty.')
                form={k:v[0] for k,v in parse_qs(self.rfile.read(length).decode(),keep_blank_values=True).items()}
                origin=self.headers.get('Origin')
                if origin and origin!='http://'+self.headers.get('Host',''): return self.reply('Origin rejected',403)
                view=form.pop('_view',None)
                if self.path=='/calculate':
                    from .definition import SPEC
                    result=calculate(SPEC['calculator'],form)
                    return self.reply(self.render(view=view,notice=SPEC['calculator']['result_label']+': '+result,values=form))
                match=re.fullmatch(r'/records/([a-z][a-z0-9_]*)(?:/(\d+))?(?:/(notes|delete))?',self.path)
                if not match: return self.reply('Action not found',404)
                name,rid,action=match.groups();record_id=int(rid) if rid else None
                app=service.build_app(database)
                if action=='notes': app.append_note(name,record_id,form['_field'],form['note']);notice='Note added.'
                elif action=='delete':
                    if form.get('_confirm')!='yes': raise ValidationError('Please confirm before deleting.')
                    app.delete(name,record_id);record_id=None;notice='Record deleted.'
                else: record_id=app.save(name,form,record_id);notice='Changes saved.'
                location='/?'+urlencode({'view':view or next(v['id'] for v in app.spec['views'] if v.get('entity')==name),**({'id':record_id} if record_id else {}),'notice':notice})
                self.send_response(303);self.send_header('Location',location);self.send_header('Content-Length','0');self.end_headers()
            except (ValueError,KeyError) as exc:
                self.reply(self.render(view=view,record_id=record_id,error=str(exc),values=form),422)
            except Exception:
                self.reply(self.render(view=view,record_id=record_id,error='We could not save these changes. Your previous data is safe.',values=form),500)
            finally:
                if app: app.close()
        def log_message(self,*args): pass
    class Server(ThreadingHTTPServer): allow_reuse_address=False
    server=Server(('127.0.0.1',args.port),Handler)
    info={'url':'http://127.0.0.1:'+str(server.server_port)+'/',**{k:identity[k] for k in ('pid','project_path','database')}}
    if args.ready_file: args.ready_file.write_text(json.dumps(info),encoding='utf-8')
    print(json.dumps(info),flush=True)
    if args.open_browser:
        import webbrowser
        webbrowser.open(info['url'])
    try: server.serve_forever()
    finally: server.server_close()
