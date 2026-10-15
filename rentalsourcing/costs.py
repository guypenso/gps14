"""Offline estimates from published API prices, reviewed 2026-10-15."""
import math

def monthly_requests(listings_per_day, tax_checks_per_day, days=30, page_size=500):
    if min(listings_per_day,tax_checks_per_day,days) < 0 or not 1 <= page_size <= 500:
        raise ValueError('Invalid estimate inputs')
    pages=max(1,math.ceil(listings_per_day/page_size))
    return days*(pages+tax_checks_per_day)

def api_costs(requests):
    plans=[('Developer',0,50,0.20),('Foundation',74,1000,0.06),
           ('Growth',199,5000,0.03),('Scale',449,25000,0.015)]
    return {name:round(base+max(0,requests-included)*extra,2)
            for name,base,included,extra in plans}
