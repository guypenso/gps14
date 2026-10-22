"""Additive candidate inbox in existing Store; no migrations run on import.
No provider calls. RealScout observations NEVER acquire rentcast-live provenance.
"""
from dataclasses import asdict
import datetime as dt
import hashlib
import json
from .collector import LIVE
from .core import verification, tax_verification
from .digest import recent_timestamp
from .provenance import demo_marked
from .realscout import addresses, address_key, identity

SOURCE='realscout-email'


def migrate(store):
    statements=[
        '''CREATE TABLE IF NOT EXISTS realscout_messages (
          id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, sent_at TEXT,
          received_at TEXT NOT NULL, parser_version TEXT NOT NULL, issues TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS realscout_candidates (
          id TEXT PRIMARY KEY, address_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
          detail_ciphertext TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          effective_at TEXT NOT NULL, version INTEGER NOT NULL, reviewed_at TEXT,
          reviewed_version INTEGER, first_message TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS realscout_observations (
          message_id TEXT NOT NULL, candidate_id TEXT NOT NULL, payload TEXT NOT NULL,
          sent_at TEXT, discovery TEXT NOT NULL, PRIMARY KEY(message_id,candidate_id))''',
        '''CREATE TABLE IF NOT EXISTS realscout_links (
          candidate_id TEXT PRIMARY KEY, property_id TEXT NOT NULL, linked_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS realscout_inbound_jobs (
          email_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, received_at TEXT NOT NULL,
          state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)''',
    ]
    with store.transaction():
        for sql in statements:store.execute(sql)
        from .realscout_migrations import upgrade
        upgrade(store)


def instant(value):
    if not isinstance(value,str):raise ValueError('Timezone-aware source timestamp required')
    value=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if value.tzinfo is None:raise ValueError('Timezone-aware source timestamp required')
    return value.astimezone(dt.timezone.utc)


class LinkVault:
    """Authenticated encryption; key stays outside DB/Git. No cleartext fallback."""
    def __init__(self,key):
        from cryptography.fernet import Fernet
        self.fernet=Fernet(key)
    def encrypt(self,url):return self.fernet.encrypt(url.encode()).decode()
    def decrypt(self,value):return self.fernet.decrypt(value.encode()).decode()


def ingest(store, parsed, *, received_at, authorized=False, vault=None, processed_at=None):
    if not authorized:raise PermissionError('Explicit authorization/trusted ingress is required')
    received=instant(received_at)
    processed=instant(processed_at) if processed_at else received
    if not parsed.sent_at:raise ValueError('Original source time missing; quarantine for review')
    sent=instant(parsed.sent_at)
    if sent>received:raise ValueError('Future message timestamp; quarantine for review')
    if parsed.issues or not parsed.cards:raise ValueError('Parser issues require review before ingestion')
    if any(card.detail_url for card in parsed.cards) and vault is None:
        raise ValueError('LinkVault required to retain private detail links safely')
    # Body identity is independent of tracking links, forwarding wrappers and encryption randomness.
    records=[]
    for card in parsed.cards:
        record=asdict(card);record.pop('detail_url');record['source']=SOURCE
        record['alert_profile']=parsed.alert_profile;record['search_name']=parsed.search_name
        records.append((card,record))
    material=json.dumps(sorted((r for _,r in records),key=lambda r:json.dumps(r,sort_keys=True)),sort_keys=True)
    fingerprint=hashlib.sha256(material.encode()).hexdigest()
    results=[]
    with store.transaction():
        if store.pg:store.execute('SELECT pg_advisory_xact_lock(73541209)')
        old_message=store.execute('SELECT content_hash FROM realscout_messages WHERE id=?',(parsed.message_key,)).fetchone()
        if old_message:
            if old_message[0]!=fingerprint:raise ValueError('Message identity reused with different card content')
            return {'duplicate_message':True,'candidates':[]}
        store.execute('INSERT INTO realscout_messages (id,content_hash,sent_at,received_at,parser_version,issues,processed_at) VALUES (?,?,?,?,?,?,?)',
            (parsed.message_key,fingerprint,sent.isoformat(),received.isoformat(),parsed.parser_version,'[]',processed.isoformat()))
        for card,record in records:
            row=store.execute('SELECT payload,effective_at,version FROM realscout_candidates WHERE id=?',(card.candidate_id,)).fetchone()
            discovery='new_to_system' if not row else 'previously_seen'
            if not row and card.price is not None and card.previous_price is not None and card.price!=card.previous_price:
                discovery='price_reduction' if card.price<card.previous_price else 'price_increase'
            if row:
                previous=json.loads(row[0])
                if sent<instant(row[1]):discovery='historical_observation'
                elif sent==instant(row[1]) and record!=previous:
                    raise ValueError('Conflicting observations have the same source timestamp')
                elif card.price is not None and previous.get('price') is not None and card.price<previous['price']:
                    discovery='price_reduction'
                elif card.price is not None and previous.get('price') is not None and card.price>previous['price']:
                    discovery='price_increase'
            encoded=json.dumps(record,sort_keys=True)
            cipher=vault.encrypt(card.detail_url) if card.detail_url and vault else None
            if not row:
                store.execute('INSERT INTO realscout_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (card.candidate_id,card.key,encoded,cipher,processed.isoformat(),processed.isoformat(),sent.isoformat(),1,None,None,parsed.message_key))
            elif discovery!='historical_observation':
                version=row[2]+(json.loads(row[0])!=record)
                store.execute('UPDATE realscout_candidates SET payload=?,detail_ciphertext=COALESCE(?,detail_ciphertext),last_seen=?,effective_at=?,version=? WHERE id=?',
                    (encoded,cipher,processed.isoformat(),sent.isoformat(),version,card.candidate_id))
            store.execute('INSERT INTO realscout_observations VALUES (?,?,?,?,?)',
                (parsed.message_key,card.candidate_id,encoded,sent.isoformat(),discovery))
            results.append({'id':card.candidate_id,'discovery':discovery})
    return {'duplicate_message':False,'candidates':results}


def mark_reviewed(store,candidate_id,now):
    instant(now)
    if not store.execute('UPDATE realscout_candidates SET reviewed_at=?,reviewed_version=version WHERE id=? RETURNING id',(now,candidate_id)).fetchone():
        raise ValueError('Unknown candidate')


def cached_identity_matches(store,candidate_id):
    candidate=store.execute('SELECT address_key FROM realscout_candidates WHERE id=?',(candidate_id,)).fetchone()
    if not candidate:raise ValueError('Unknown candidate')
    result=[]
    for pid,payload,source in store.execute('SELECT id,payload,source FROM properties').fetchall():
        if source!=LIVE:continue
        matches=addresses(json.loads(payload).get('formattedAddress',''))
        if len(matches)!=1:continue
        m=matches[0]
        if address_key(m['street'],m['city'],m['state'],m['zip'])==candidate[0]:result.append(pid)
    return result


def link_cached_identity(store,candidate_id,property_id,now):
    # Only a unique exact normalized address/unit match may link automatically.
    instant(now)
    if cached_identity_matches(store,candidate_id)!=[property_id]:
        raise ValueError('Missing or ambiguous cached property identity; manual resolution needed')
    store.execute('INSERT INTO realscout_links VALUES (?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET property_id=excluded.property_id,linked_at=excluded.linked_at',
                  (candidate_id,property_id,now))


def decision(store,candidate_id,boundary,now):
    """Original hard filters plus explicitly sourced, fresh listing and tax evidence."""
    item=store.execute('SELECT payload,effective_at,address_key FROM realscout_candidates WHERE id=?',(candidate_id,)).fetchone()
    if not item:raise ValueError('Unknown candidate')
    payload=json.loads(item[0]);linked=store.execute('SELECT property_id FROM realscout_links WHERE candidate_id=?',(candidate_id,)).fetchone()
    prop=store.get_property(linked[0]) if linked else None
    row=dict(id=candidate_id,formattedAddress=f"{payload['street']}, {payload['city']}, {payload['state']} {payload['zipcode']}",
             state=payload['state'],zipCode=payload['zipcode'],price=payload['price'],bedrooms=payload['bedrooms'],
             bathrooms=payload['bathrooms'],squareFootage=payload['square_footage'])
    # Only an explicit status field in an authorized regular alert counts as status evidence.
    # The words 'New Listing' or 'Exclusive' never become Active implicitly.
    status=payload.get('listing_status')
    if payload.get('alert_profile')=='saved_search' and recent_timestamp(item[1],now):
        if status in {'Active','Pending','Sold','Off Market','Coming Soon'}:row['status']=status
    if prop and prop['source']==LIVE:
        if demo_marked(prop['payload']):
            return dict(state='Pending Verification',reasons=['non_live_listing_source'],needs_tax=False)
        if cached_identity_matches(store,candidate_id)!=[linked[0]]:
            return dict(state='Pending Verification',reasons=['identity_match_changed'],needs_tax=False)
        row.update(latitude=prop['payload'].get('latitude'),longitude=prop['payload'].get('longitude'))
        if recent_timestamp(prop['observed_at'],now) and ('status' not in row):
            # An Exclusive observation must not be overridden by older cached Active evidence.
            if payload.get('alert_profile')!='exclusive_alert' or instant(prop['observed_at'])>=instant(item[1]):
                row['status']=prop['payload'].get('status')
    evidence=store.execute('SELECT property_id,payload,source,fetched_at,expires_at FROM realscout_evidence WHERE candidate_id=?',(candidate_id,)).fetchone()
    tax=None
    if evidence and evidence[2]==LIVE and instant(evidence[3])<=now<instant(evidence[4]):
        record=json.loads(evidence[1])
        from .realscout_enrichment import record_matches
        if not record_matches(record,item[2]) or record.get('id')!=evidence[0]:
            return dict(state='Pending Verification',reasons=['tax_identity_or_source_mismatch'],needs_tax=False)
        row.update(latitude=record.get('latitude'),longitude=record.get('longitude'))
        tax=(record,LIVE,evidence[3])
    if tax is None and linked:tax=store.get_tax(linked[0],now.isoformat())
    status_proof=store.execute('SELECT payload,source,fetched_at,expires_at FROM realscout_status_evidence WHERE candidate_id=?',(candidate_id,)).fetchone()
    status_fresh=False
    status_issue=None
    if status_proof and status_proof[1]==LIVE and instant(status_proof[2])<=now<instant(status_proof[3]):
        from .realscout_enrichment import record_matches
        observed_status=json.loads(status_proof[0])
        if record_matches(observed_status,item[2]) and instant(status_proof[2])>=instant(item[1]):
            from .core import number
            verified=dict(row,status=observed_status.get('status'))
            fields={'price':'price','bedrooms':'bedrooms','bathrooms':'bathrooms','squareFootage':'square_footage'}
            if all(number(observed_status.get(k)) for k in fields):
                for k in fields:verified[k]=observed_status[k]
                vstate,vreasons=verification(verified,boundary)
                if vstate=='Rejected':return dict(state='Rejected',reasons=['current_listing_'+r for r in vreasons],needs_tax=False)
                if any(observed_status[k]!=payload[v] for k,v in fields.items()):status_issue='current_listing_facts_differ'
            else:status_issue='current_listing_attributes_missing'
            row['status']=observed_status.get('status');status_fresh=True
    state,reasons=verification(row,boundary)
    if state=='Rejected':return dict(state=state,reasons=reasons,needs_tax=False)
    if not recent_timestamp(item[1],now) and not status_fresh:reasons.append('stale_email_observation')
    if status_issue:reasons.append(status_issue)
    amount=None
    if tax:
        expected=evidence[0] if evidence and tax[2]==evidence[3] else linked[0] if linked else None
        if tax[1]!=LIVE or not isinstance(tax[0],dict) or tax[0].get('id')!=expected:
            return dict(state='Pending Verification',reasons=['tax_identity_or_source_mismatch'],needs_tax=False)
        if demo_marked(tax[0]):return dict(state='Pending Verification',reasons=['non_live_tax_source'],needs_tax=False)
        tax_state,amount,reason=tax_verification(tax[0],now.date())
        if tax_state=='Rejected':return dict(state='Rejected',reasons=[reason],tax=amount,needs_tax=False)
        if reason:reasons.append(reason)
    else:reasons.append('financial_verification_required')
    if reasons:
        return dict(state='Pending Verification',reasons=sorted(set(reasons)),tax=amount,
                    needs_tax=not tax and state=='Candidate')
    return dict(state='Qualified',reasons=[],tax=amount,needs_tax=False)


def enrichment_plan(store,boundary,settings,now):
    """Planning only. Existing Client/reserve_api must gate any later authorized execution."""
    health=store.budget_health(settings,now,record_warning=False)
    remaining_today=max(0,settings.per_day-store.count_usage(now.strftime('day:%Y-%m-%d')))
    cap=min(settings.per_run,remaining_today,health['remaining']) if settings.enabled else 0
    planned=[];blocked=[]
    rows=store.execute('SELECT id,first_seen FROM realscout_candidates ORDER BY first_seen,id').fetchall()
    def priority(item):
        cid,first=item
        latest=store.execute('SELECT discovery FROM realscout_observations WHERE candidate_id=? ORDER BY sent_at DESC,message_id DESC LIMIT 1',(cid,)).fetchone()
        return (0 if latest and latest[0]=='price_reduction' else
                1 if instant(first).date()==now.date() else 2,first,cid)
    rows.sort(key=priority)
    for cid,_ in rows:
        result=decision(store,cid,boundary,now)
        if result['needs_tax']:
            link=store.execute('SELECT property_id FROM realscout_links WHERE candidate_id=?',(cid,)).fetchone()
            if len(planned)<cap:
                planned.append(dict(candidate_id=cid,property_id=link[0] if link else None,
                    endpoint='/properties/'+link[0] if link else '/properties',limit=1))
            else:blocked.append(dict(candidate_id=cid,reason='existing_allowance_unavailable'))
        elif result['state']=='Pending Verification':
            blocked.append(dict(candidate_id=cid,reasons=result['reasons']))
    return dict(planned=planned,blocked=blocked,max_attempts=cap,executes_requests=False,
                note='Existing cumulative/day/cycle/tax-reserve checks still apply at execution; no authority is granted here.')
