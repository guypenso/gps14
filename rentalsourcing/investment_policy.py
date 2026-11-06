"""Mandatory investment thresholds, effective November 7, 2026.

Annual tax evidence is separate from assessment estimates. The ceiling is
inclusive; never round an over-limit amount down into eligibility.
"""
from decimal import Decimal

PRICE_MAX = 200001
TAX_BUFFER = Decimal('100')
TAX_CEILING = Decimal('2400')
POLICY_VERSION = 'investment-20261107-tax2400-inclusive'

def annual_tax_passes(amount):
    return Decimal(str(amount)) + TAX_BUFFER <= TAX_CEILING

def thresholds():
    return dict(policy_version=POLICY_VERSION, price_max=PRICE_MAX,
                tax_add=int(TAX_BUFFER), tax_max=int(TAX_CEILING),
                tax_comparison='<=', single_family_only=True)
