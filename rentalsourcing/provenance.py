"""Live transport records are distinct from plain fixture/database dictionaries.

There is no fixture loader or demo fallback in the production entry point.
This guards accidental mixing of data, not malicious changes to Python code.
"""
class LiveRecord(dict):
    pass

class LiveTax(dict):
    pass


def demo_marked(row):
    return any(row.get(k) for k in ('demo', 'isDemo', 'is_demo', 'test', 'isTest', 'synthetic')) or \
        str(row.get('source','')).lower() in {'demo','test','fixture','sample','synthetic'}


def require_live(row, tax):
    if not isinstance(row,LiveRecord) or not isinstance(tax,LiveTax) or demo_marked(row):
        raise PermissionError('Only live-transport listing and tax data may generate real alerts')
