import json
import pytest
from rentalsourcing.storage import Store
from rentalsourcing.transfer import transfer
from conftest import listing


def test_transfer_preserves_allowance_outbox_and_pending_without_providers(store,tmp_path):
    target=Store('sqlite:///'+str(tmp_path/'target.sqlite'))
    try:
        store.execute('INSERT INTO usage VALUES (?,?)',('approval:exhausted',4))
        store.save_property('pending',listing('pending'),'Pending Verification',['missing_tax_history'],'rentcast-live',1,store.now())
        store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',('message','daily:day','uncertain','{}','[]',None,store.now(),store.now()))
        counts=transfer(store,target)
        assert counts['usage']==1 and counts['properties']==1 and counts['outbox']==1
        assert target.count_usage('approval:exhausted')==4
        assert target.get_property('pending')['state']=='Pending Verification'
        assert target.execute('SELECT state FROM outbox').fetchone()[0]=='uncertain'
        with pytest.raises(ValueError,match='empty'):transfer(store,target)
        assert target.count_usage('approval:exhausted')==4
    finally:target.close()


def test_transfer_is_atomic_if_late_table_has_incompatible_schema(store,tmp_path):
    target=Store('sqlite:///'+str(tmp_path/'target.sqlite'));target.migrate()
    try:
        store.execute('INSERT INTO usage VALUES (?,?)',('approval:exhausted',4))
        target.execute('ALTER TABLE operations ADD COLUMN incompatible TEXT')
        with pytest.raises(ValueError,match='Schema differs'):transfer(store,target)
        assert target.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    finally:target.close()
