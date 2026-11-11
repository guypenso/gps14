"""Mandatory investment thresholds, effective November 7, 2026.

Annual tax evidence is separate from assessment estimates. The ceiling is
inclusive; never round an over-limit amount down into eligibility.
"""
from decimal import Decimal
import json
from pathlib import Path

BEDROOM_PRICE_POLICY=json.loads((Path(__file__).resolve().parent.parent/'data/bedroom-price-policy.json').read_text())
BEDROOM_PRICE_LIMITS={int(k):v for k,v in BEDROOM_PRICE_POLICY['maximum_price_by_bedrooms'].items()}

def bedroom_price_rejection(bedrooms,price=None):
    if isinstance(bedrooms,bool) or not isinstance(bedrooms,(int,float)) or bedrooms not in BEDROOM_PRICE_LIMITS:return 'bedrooms'
    if price is not None and price>BEDROOM_PRICE_LIMITS[bedrooms]:return 'price'
    return None

PRICE_MAX = 200001
TAX_BUFFER = Decimal('100')
TAX_CEILING = Decimal('2400')
POLICY_VERSION = BEDROOM_PRICE_POLICY['version']

def annual_tax_passes(amount):
    return Decimal(str(amount)) + TAX_BUFFER <= TAX_CEILING

def thresholds():
    return dict(policy_version=POLICY_VERSION, price_max=PRICE_MAX,maximum_price_by_bedrooms=BEDROOM_PRICE_LIMITS,
                tax_add=int(TAX_BUFFER), tax_max=int(TAX_CEILING),
                tax_comparison='<=', single_family_only=True)
