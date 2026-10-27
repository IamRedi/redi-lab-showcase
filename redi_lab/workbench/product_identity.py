"""Build-local product identity; a manifest's recipe key is never a title."""
import re


def requested_name(prompt):
    # Only explicit naming syntax, never infer a title from domain keywords.
    match = re.search(r'\b(?:called|named)\s+["\u201c\']?([^\n.!?"\u201d\']{1,80})', prompt or '', re.I)
    if not match: return None
    return re.split(r'\s+(?:with|for|using|that)\b', match.group(1), maxsplit=1, flags=re.I)[0].strip()


def product_name(spec=None, metadata=None, prompt=None):
    for value in ((spec or {}).get('product_name'), requested_name(prompt), (metadata or {}).get('product_name')):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
