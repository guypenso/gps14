"""Authenticated read-only PENTEN decisions. Human review remains receiver-owned."""
import datetime as dt,hashlib,hmac,json,urllib.request,uuid
from .command_center import configuration,signed_request
from .request import NoRedirect


def refresh(store,now=None,opener=None,config=None):
    config=config or configuration()
    if not config:return {'state':'disabled','decisions':0}
    url,key,token=config;url=url.rsplit('/',1)[0]+('/review-classifications' if url.endswith('/review-sync') else '/classifications')
    opener=opener or urllib.request.build_opener(NoRedirect);now=now or dt.datetime.now(dt.timezone.utc)
    cursor=None;seen=set();records=[]
    for page in range(20):
        nonce=uuid.uuid4().hex;body=json.dumps({'nonce':nonce,'cursor':cursor},separators=(',',':')).encode()
        request=signed_request((url,key,token),body,now)
        with opener.open(request,timeout=20) as response:
            raw=response.read(1024*1024+1)
            if response.status!=200 or len(raw)>1024*1024:raise ValueError('Invalid review response')
            sig=response.headers.get('X-RentalSourcing-Response-Signature','')
            if not hmac.compare_digest(sig,'v1='+hmac.new(key.encode(),raw,hashlib.sha256).hexdigest()):raise ValueError('Invalid signed review response')
            result=json.loads(raw)
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
        for d in records:
            name='penten_review:'+d['source_id'];old=store.execute('SELECT payload FROM settings WHERE name=?',(name,)).fetchone()
            if old and json.loads(old[0]).get('status')=='rejected':continue
            store.save_setting(name,dict(d,fetched_at=now.isoformat()))
        store.save_setting('penten_review_feed',dict(fetched_at=now.isoformat(),decisions=len(records)))
    return {'state':'fresh','decisions':len(records),'criteria_counts':result.get('criteria_counts',{})}
