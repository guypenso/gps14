import pytest
from conftest import listing
from rentalsourcing.turnkey import evidence
from rentalsourcing.core import verification,report_eligible,basic_rejection
from rentalsourcing.review_inventory import select
from rentalsourcing.collector import LIVE
@pytest.mark.parametrize('text,excluded',[
 ('Turnkey',True),('TURN-KEY investment',True),('Turn key rental',True),('Fully turnkey investment properties',True),('Tenant occupied turnkey investment package',True),('turn–key',True),
 ('not turnkey',False),('Not a fully turnkey investment',False),("This isn’t turnkey",False),('Non-turnkey',False),('No longer turnkey',False),('Renovated and move-in ready',False),('Tenant occupied',False),('Not only turnkey, but profitable',True),('Not turnkey. Another turnkey unit is included',True),('Turkey kitchen',False)])
def test_marketing_language(text,excluded):assert evidence({'description':text})['classified']==excluded

def test_structured_fields_and_unrelated_metadata():
 assert evidence({'metadata':{'investmentType':'Turnkey'}})['classified']
 assert evidence({'isTurnkey':True})['classified']
 assert not evidence({'formattedAddress':'100 Turnkey Lane','listingAgent':{'name':'Turnkey Realty'}})['classified']

def test_existing_backlog_rejects_turnkey_without_losing_source_or_qualifying_other_homes(store,boundary,clock):
 for identity,description in [('marketed','Turn-key investment'),('renovated','Renovated'),('negated','Not turnkey')]:
  p=listing(identity,description=description);store.save_property(identity,p,'Pending Verification',[],LIVE,1,clock().isoformat())
  assert (verification(p,boundary)[0]=='Rejected')==(identity=='marketed')
  assert report_eligible(p,boundary)==(identity!='marketed')
  if identity=='marketed':assert basic_rejection(p)=='turnkey'
 cards,counts=select(store,boundary,clock());assert {c['source_id'] for c,p in cards}=={'renovated','negated'}
 assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==3
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
