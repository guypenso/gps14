"""Authenticated read-only PENTEN decisions. Human review remains receiver-owned."""
import datetime as dt,hashlib,hmac,json,urllib.request,uuid
from .command_center import configuration,review_configuration,signed_request
from .request import NoRedirect


def _refresh(store,now=None,opener=None,config=None):
    config=config or review_configuration() or configuration()
    if not config:return {'state':'disabled','decisions':0}
    url,key,token=config;url=url.rsplit('/',1)[0]+('/review-classifications' if url.endswith(('/review-sync','/review-reports')) else '/classifications')
    opener=opener or urllib.request.build_opener(NoRedirect);now=now or dt.datetime.now(dt.timezone.utc)
    cursor=None;seen=set();records=[]
    for page in range(20):
        nonce=uuid.uuid4().hex;body=json.dumps({'nonce':nonce,'cursor':cursor},separators=(',',':')).encode()
        from .sync_runtime import request_json,SyncFailure
        def validate(result,raw,response):
            sig=response.headers.get('X-RentalSourcing-Response-Signature','')
            if not hmac.compare_digest(sig,'v1='+hmac.new(key.encode(),raw,hashlib.sha256).hexdigest()):raise SyncFailure('response_signature',None,False,url,'review-feedback')
            if result.get('nonce')!=nonce:raise ValueError('Review nonce mismatch')
            if not isinstance(result.get('decisions'),list) or len(result['decisions'])>500:raise ValueError('Review schema invalid')
            for d in result['decisions']:
                if not isinstance(d.get('source_id'),str) or not 1<=len(d['source_id'])<=300 or d.get('status') not in {'new','pursue','monitor','rejected'}:raise ValueError('Review decision invalid')
                for k in ('reviewed_at','workflow_at'):
                    if d.get(k) and dt.datetime.fromisoformat(d[k].replace('Z','+00:00')).tzinfo is None:raise ValueError('Unzoned review timestamp')
        result=request_json(store,'review_feedback','review-page:'+str(cursor),(url,key,token),body,opener=opener,now=now,validate=validate,poll=True)
        if page==0:revision=result.get('snapshot_version')
        elif revision and result.get('snapshot_version')!=revision:
            error=SyncFailure('feedback_snapshot_changed',409,True,url,'review-feedback')
            from .sync_runtime import component,incident
            component(store,'review_feedback','partial',now=now,pending=1,error=error.safe(),recovery='scheduled');incident(store,'review_feedback',error.safe(),now=now);raise error
        if result.get('nonce')!=nonce or not isinstance(result.get('decisions'),list) or len(result['decisions'])>500:raise ValueError('Invalid review response schema')
        for d in result['decisions']:
            if not isinstance(d.get('source_id'),str) or not 1<=len(d['source_id'])<=300 or d.get('status') not in {'new','pursue','monitor','rejected'}:raise ValueError('Invalid review decision')
            records.append(d)
        cursor=result.get('next_cursor')
        if cursor is None:break
        if not isinstance(cursor,str) or cursor in seen:raise ValueError('Repeated review cursor')
        seen.add(cursor)
    else:raise ValueError('Review feed page cap reached')
    with store.transaction():
        seen_ids={}
        applied=unchanged=stale=status_updates_after_previous_fetch=older_status_mismatches=new_source_identities=0
        for d in records:
            previous=seen_ids.get(d['source_id'])
            if previous and previous!=d:raise ValueError('Conflicting source aliases')
            seen_ids[d['source_id']]=d
            name='penten_review:'+d['source_id'];old=store.execute('SELECT payload FROM settings WHERE name=?',(name,)).fetchone()
            prior=json.loads(old[0]) if old else {}
            if not prior:new_source_identities+=1
            elif prior.get('status')!=d.get('status'):
                if d.get('reviewed_at') and prior.get('fetched_at') and dt.datetime.fromisoformat(d['reviewed_at'].replace('Z','+00:00'))>dt.datetime.fromisoformat(prior['fetched_at'].replace('Z','+00:00')):status_updates_after_previous_fetch+=1
                else:older_status_mismatches+=1
            fields=('status','reviewed_at','reviewed_by','contract_state','workflow_at','workflow_by')
            if prior.get('status')=='rejected' and d['status']!='rejected':stale+=1;continue
            def parsed(value):
                if not value:return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
                date=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
                if date.tzinfo is None:raise ValueError('Review timestamps require timezone')
                return date
            if d['status']!='rejected' and parsed(prior.get('reviewed_at'))>parsed(d.get('reviewed_at')):stale+=1;continue
            if parsed(prior.get('workflow_at'))>parsed(d.get('workflow_at')):
                d={**d,**{k:prior[k] for k in ('contract_state','workflow_at','workflow_by') if k in prior}}
            if all(prior.get(k)==d.get(k) for k in fields):unchanged+=1
            else:
                if prior:
                    history=hashlib.sha256(json.dumps({k:prior.get(k) for k in fields},sort_keys=True).encode()).hexdigest()
                    store.save_setting('penten_review_history:'+hashlib.sha256(d['source_id'].encode()).hexdigest()+':'+history,prior)
                applied+=1
            store.save_setting(name,dict(d,fetched_at=now.isoformat()))
        actual=[json.loads(store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:'+identity,)).fetchone()[0]) for identity in seen_ids]
        store.save_setting('penten_review_feed',dict(fetched_at=now.isoformat(),decisions=len(seen_ids),snapshot_version=revision,decision_digest=hashlib.sha256(json.dumps([[r.get(k) for k in ('source_id','status','reviewed_at','reviewed_by','contract_state','workflow_at','workflow_by')] for r in sorted(actual,key=lambda r:r['source_id'])],ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),applied=applied,unchanged=unchanged,stale_ignored=stale,status_updates_after_previous_fetch=status_updates_after_previous_fetch,older_status_mismatches=older_status_mismatches,new_source_identities=new_source_identities))
    from .sync_runtime import component
    component(store,'review_feedback','success',now=now,processed=len(seen_ids),recovery='reconciled')
    return {'state':'fresh','decisions':len(seen_ids),'applied':applied,'unchanged':unchanged,'stale_ignored':stale,'status_updates_after_previous_fetch':status_updates_after_previous_fetch,'older_status_mismatches':older_status_mismatches,'new_source_identities':new_source_identities,'snapshot_version':revision,'criteria_counts':result.get('criteria_counts',{})}


def refresh(store,now=None,opener=None,config=None):
    try:return _refresh(store,now,opener,config)
    except Exception as exc:
        from .sync_runtime import classify,component,incident
        error=classify(exc,operation='review-feedback');component(store,'review_feedback','partial' if error.retryable else 'failed',now=now,pending=1,error=error.safe(),recovery='scheduled' if error.retryable else 'manual_action_required');incident(store,'review_feedback',error.safe(),now=now);raise

def review_current(store,now):
    import os
    if not (os.getenv('ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK','false')=='true' or os.getenv('ENABLE_REVIEW_INVENTORY_SYNC','false')=='true'):return True
    from .sync_runtime import get
    feed=get(store,'penten_review_feed',{});health=get(store,'health_component:review_feedback',{})
    try:
        date=dt.datetime.fromisoformat(feed['fetched_at'].replace('Z','+00:00'))
        return health.get('state')=='success' and date.tzinfo is not None and dt.timedelta(0)<=now-date<=dt.timedelta(minutes=15)
    except (KeyError,ValueError,TypeError):return False
