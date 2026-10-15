import datetime as dt
import json
import os
import socket
import tempfile
import unittest
from unittest.mock import patch
from rentalsourcing.schedule import scheduled_day
from rentalsourcing.cloud import send_email
from rentalsourcing.provenance import LiveRecord,LiveTax
from rentalsourcing.core import basic_rejection
from rentalsourcing.storage import Store,BudgetExceeded

class ReadinessTests(unittest.TestCase):
    def test_exactly_one_eight_am_slot_every_day_including_dst(self):
        start=dt.date(2026,1,1)
        for i in range(365):
            day=start+dt.timedelta(days=i)
            slots=[dt.datetime.combine(day,dt.time(h),tzinfo=dt.timezone.utc) for h in (12,13)]
            self.assertEqual(sum(scheduled_day(t) is not None for t in slots),1,str(day))
        self.assertIsNotNone(scheduled_day(dt.datetime(2026,1,1,13,tzinfo=dt.timezone.utc)))
        self.assertIsNotNone(scheduled_day(dt.datetime(2026,7,1,12,tzinfo=dt.timezone.utc)))
    def test_duplicate_scheduled_day_claim(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store('sqlite:///'+d+'/db')
            self.assertTrue(store.claim_scheduled_day('2026-10-15'))
            self.assertFalse(store.claim_scheduled_day('2026-10-15'))
            store.close()
    def test_total_budget_does_not_reset_at_month_boundary(self):
        store=Store('sqlite:///:memory:')
        store.reserve(10,10,dt.datetime(2026,10,31),total_limit=1)
        with self.assertRaises(BudgetExceeded):
            store.reserve(10,10,dt.datetime(2026,11,1),total_limit=1)
        store.close()
    def test_fixtures_and_demo_cannot_send_even_with_live_email_enabled(self):
        with patch.dict(os.environ,{'ENABLE_EMAIL':'true','APP_ENV':'production'}), patch('socket.socket',side_effect=AssertionError('No network')):
            for row,tax in [({'id':'test'},{'annual':100}),
                            (LiveRecord(id='test',demo=True),LiveTax(annual=100)),
                            (LiveRecord(id='test'),{'annual':100})]:
                with self.assertRaises(PermissionError):send_email(row,tax)
        self.assertEqual(basic_rejection({'demo':True}),'demo_data')
    def test_email_off_blocks_live_records(self):
        with patch.dict(os.environ,{'ENABLE_EMAIL':'false'}):
            with self.assertRaises(PermissionError):send_email(LiveRecord(),LiveTax())

if __name__=='__main__':unittest.main()

class CostTests(unittest.TestCase):
    def test_pagination_and_published_cost_arithmetic(self):
        from rentalsourcing.costs import monthly_requests,api_costs
        self.assertEqual(monthly_requests(500,10),330)
        self.assertEqual(monthly_requests(501,10),360)
        self.assertEqual(api_costs(330)['Developer'],56)
        self.assertEqual(api_costs(930)['Foundation'],74)
        self.assertEqual(api_costs(3030)['Foundation'],195.8)
        self.assertEqual(api_costs(9030)['Growth'],319.9)
