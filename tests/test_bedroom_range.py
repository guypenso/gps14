import pytest
from rentalsourcing.core import verification,basic_rejection
from rentalsourcing.config import Settings
from rentalsourcing.geography import load_boundary
from rentalsourcing.request import PARAMS

@pytest.mark.parametrize('beds,expected',[(3,'Candidate'),(4,'Candidate'),(0,'Rejected'),(1,'Rejected'),(2,'Rejected'),(5,'Rejected'),(6,'Rejected'),(3.5,'Rejected'),(None,'Pending Verification'),('3','Pending Verification'),(True,'Pending Verification')])
def test_bedroom_range(beds,expected):
    row=dict(id='fixture',formattedAddress='100 Fixture St, Pittsburgh, PA 15206',state='PA',zipCode='15206',status='Active',propertyType='Single Family',price=100000,bedrooms=beds,bathrooms=1,squareFootage=1200,latitude=40.468,longitude=-79.925)
    assert verification(row,load_boundary(Settings.from_env().boundary_file))[0]==expected
    if expected=='Rejected':assert basic_rejection(row)=='bedrooms'

def test_provider_bedroom_range():assert PARAMS['bedrooms']=='3:4'
