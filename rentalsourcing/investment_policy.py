"""Mandatory investment thresholds, effective November 7, 2026.

Annual tax evidence is separate from assessment estimates. The ceiling is
inclusive; never round an over-limit amount down into eligibility.
"""
from decimal import Decimal
import json
import math
from pathlib import Path

BEDROOM_PRICE_POLICY=json.loads((Path(__file__).resolve().parent.parent/'data/bedroom-price-policy.json').read_text())
BEDROOM_PRICE_LIMITS={int(k):v for k,v in BEDROOM_PRICE_POLICY['maximum_price_by_bedrooms'].items()}
BEDROOM_TAX_LIMITS={int(k):Decimal(str(v)) for k,v in BEDROOM_PRICE_POLICY['maximum_annual_tax_by_bedrooms'].items()}

def bedroom_price_rejection(bedrooms,price=None):
    if isinstance(bedrooms,bool) or not isinstance(bedrooms,(int,float)) or bedrooms not in BEDROOM_PRICE_LIMITS:return 'bedrooms'
    if price is not None and price>BEDROOM_PRICE_LIMITS[bedrooms]:return 'price'
    return None

PRICE_MAX = 200001
TAX_BUFFER = Decimal(str(BEDROOM_PRICE_POLICY['annual_tax_safety_buffer']))
POLICY_VERSION = BEDROOM_PRICE_POLICY['version']
SCREENING_POLICY_VERSION = BEDROOM_PRICE_POLICY['tax_policy_version']

def annual_tax_limit(bedrooms):
    if isinstance(bedrooms,bool) or not isinstance(bedrooms,(int,float)) or not math.isfinite(bedrooms) or not float(bedrooms).is_integer():return None
    return BEDROOM_TAX_LIMITS.get(int(bedrooms))

def annual_tax_passes(amount,bedrooms):
    limit=annual_tax_limit(bedrooms)
    return limit is not None and Decimal(str(amount)) + TAX_BUFFER <= limit

def thresholds():
    return dict(policy_version=SCREENING_POLICY_VERSION, price_max=PRICE_MAX,maximum_price_by_bedrooms=BEDROOM_PRICE_LIMITS,
                tax_add=int(TAX_BUFFER), tax_max_by_bedrooms={k:int(v) for k,v in BEDROOM_TAX_LIMITS.items()},
                tax_comparison='<=', single_family_only=True)
