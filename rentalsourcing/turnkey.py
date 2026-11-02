"""Deterministic current-listing marketing exclusion; not a renovation inference."""
import re,unicodedata
FIELDS=('description','propertyDescription','listingDescription','remarks','publicRemarks','listingRemarks','marketingRemarks','title','listingTitle','investmentType','marketingType','propertyClassification','propertySubType','propertySubtype')
META=('listingMetadata','marketing','classification','metadata')
NEG=re.compile(r"\b(?:not|no|non|never|isn't|isnt|aren't|wasn't|cannot be)\s+(?:(?:a|an|fully|complete|completely|truly|really|yet|currently|quite|necessarily|considered|at|all|longer)\s+){0,6}$")
TERM=re.compile(r'\bturn\s*key\b')
def normalize(s):
    return re.sub(r'[-\u2010-\u2015\u2212]+',' ',unicodedata.normalize('NFKC',s).lower().replace('\u2019',"'"))
def evidence(row):
    matches=[]
    for flag in ('turnkey','isTurnkey','isTurnKey'):
        if row.get(flag) is True:matches.append(flag)
    for field in FIELDS:
        value=row.get(field)
        if not isinstance(value,str):continue
        text=normalize(value)
        for m in TERM.finditer(text):
            prefix=re.split(r'[.!?;\n]',text[max(0,m.start()-100):m.start()])[-1]
            if not NEG.search(prefix):matches.append(field);break
    for field in META:
        value=row.get(field)
        if isinstance(value,dict) and any(evidence({k:v})['classified'] for k,v in value.items() if k in FIELDS or k in ('turnkey','isTurnkey','isTurnKey')):matches.append(field)
    return {'classified':bool(matches),'reason':'marketed_as_turnkey' if matches else None,'fields':sorted(set(matches)),'rule_version':'turnkey-v1'}
