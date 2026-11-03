"""Condominium ownership evidence from structured listing fields, never prose alone."""
import re,unicodedata
FIELDS=('propertyType','propertySubType','propertySubtype','structureType','dwellingType','ownershipType','ownership','ownershipDescription','commonInterest','legalOwnership','isCondominium','isCondo')
CONTAINERS=('metadata','listingMetadata','propertyDetails','listing','property')
def evidence(row):
    sources=[row]+[row[k] for k in CONTAINERS if isinstance(row.get(k),dict)]
    observed=[];ambiguous=False
    for source in sources:
        for field in FIELDS:
            value=source.get(field)
            if field in ('isCondominium','isCondo'):
                if value is True:observed.append({'field':field,'value':True})
                continue
            if not isinstance(value,str) or not value.strip():continue
            text=unicodedata.normalize('NFKC',value).casefold()
            if field in ('ownershipType','ownership','ownershipDescription','commonInterest','legalOwnership') and re.sub(r'[^a-z]','',text) in ('unknown','other','mixed','coop','cooperative','undetermined'):ambiguous=True
            words=re.findall(r'[a-z0-9]+',text)
            if not any(w in ('condo','condos','condominium','condominiums','condominal','condominiumownership','condominiumunit','condounit') or w.endswith('condominium') for w in words):continue
            if re.search(r'\b(?:not|non|no)\W+(?:a\W+)?condo(?:minium)?\b',text):continue
            if re.search(r'co\W*op|cooperative|unknown|uncertain',text):ambiguous=True;continue
            observed.append({'field':field,'value':value[:200]})
    return {'policy':'condominium-v1','state':'excluded' if observed else 'pending' if ambiguous else 'clear','reason':'condominium' if observed else 'condominium_ownership_unverified' if ambiguous else None,'evidence':observed}
