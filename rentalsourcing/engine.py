"""Resumable daily pipeline, durable price events and verified tax enrichment."""
import datetime as dt
import hashlib
import json
import urllib.parse
import urllib.error
import uuid
from .core import verification,tax_verification,historical_reduction,number
from .collector import LIVE, RequestDeferred, tax_expiry
from .provenance import demo_marked
from .request import PARAMS
from .storage import BudgetExceeded
from .telemetry import event

class Engine:
    def __init__(self,store,boundary,client,settings,clock=None):
        self.store=store;self.boundary=boundary;self.client=client;self.settings=settings
        self.clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))

    def ingest(self,job_id,row):
        if not isinstance(row,dict):return
        source=getattr(self.client,'source','fixture')
        state,reasons=verification(row,self.boundary)
        if source!=LIVE and state!='Rejected':state,reasons='Pending Verification',['non_live_source']
        identity=row.get('id')
        if not isinstance(identity,str) or not identity:
            identity='unidentified:'+hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
        old=self.store.get_property(identity);revision=old['revision'] if old else 0
        previous=old['payload'].get('price') if old else None
        price=row.get('price');changed=number(price) and price!=previous
        if changed:revision+=1
        reduced=changed and number(previous) and price<previous
        historical=historical_reduction(row) if not old else None
        when=getattr(self.client,'last_fetched_at',None) or self.clock().isoformat()
        self.store.save_property(identity,row,'Pending Verification' if state=='Candidate' else state,['tax_not_verified'] if state=='Candidate' else reasons,source,revision,when)
        if changed:
            self.store.execute('INSERT INTO price_history VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
               (f'{identity}:{revision}',identity,price,previous,int(reduced),when))
            # A queued event is frozen in its outbox. New prices create separate events.
            self.store.execute("UPDATE deal_events SET state='superseded' WHERE property_id=? AND state='ready'",(identity,))
        current_event=self.store.execute('SELECT kind,previous_price,state FROM deal_events WHERE id=?',(f'{identity}:price:{revision}',)).fetchone()
        kind=current_event[0] if current_event and current_event[2]=='ready' else ('price_reduction' if reduced or historical else 'new')
        if state=='Candidate':
            # An unsent property may become verifiable at any later observed price.
            prior_sent=self.store.execute("SELECT 1 FROM deal_events WHERE property_id=? AND state IN ('sent','queued') LIMIT 1",(identity,)).fetchone()
            legacy_sent=self.store.claimed(identity)
            eligible=reduced or bool(current_event and current_event[2]=='ready') or (not prior_sent and not legacy_sent)
            event_id=f'{identity}:price:{revision}' if eligible else None
            if eligible:
                self.store.execute('INSERT INTO deal_events VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                    (event_id,identity,kind,price,previous if reduced else historical,revision,'ready','{}',when))
            self.store.execute('INSERT INTO scan_tasks VALUES (?,?,0,?,?) ON CONFLICT(job_id,property_id) DO UPDATE SET priority=excluded.priority,event_id=excluded.event_id',
                (job_id,identity,0 if reduced or (not old and historical) else (1 if not old else 2),event_id))
            # A daily listing refresh must not erase a valid cached decision
            # merely because the paid-request budget ends before this task.
            cached=self.store.get_tax(identity,self.clock().isoformat())
            try:
                age=self.clock()-dt.datetime.fromisoformat(when)
                fresh=dt.timedelta(0)<=age<=dt.timedelta(hours=24)
            except (ValueError,TypeError):fresh=False
            if (fresh and cached and cached[1]==LIVE and isinstance(cached[0],dict)
                    and cached[0].get('id')==identity and not demo_marked(cached[0])):
                tax_state,tax,reason=tax_verification(cached[0],self.clock().date())
                if tax_state!='Pending Verification':
                    self.store.set_verification(identity,tax_state,[reason] if reason else [])
                    if tax_state=='Qualified' and event_id:
                        payload=dict(listing=row,tax=tax,source=LIVE,tax_source=LIVE,
                            verified_at=self.clock().isoformat(),tax_fetched_at=cached[2])
                        self.store.execute("UPDATE deal_events SET payload=? WHERE id=? AND state='ready' AND revision=? AND price=?",
                            (json.dumps(payload),event_id,revision,price))
                    self.store.execute('UPDATE scan_tasks SET done=1 WHERE job_id=? AND property_id=?',(job_id,identity))
                    if event_id:self.store.execute('UPDATE scan_tasks SET done=1 WHERE property_id=? AND event_id=?',(identity,event_id))
                    return True

    def run(self,job_id,day,*,candidate_limit=None,first_page_only=False):
        store=self.store;job=store.job(job_id,day);stats=job['stats']
        if job['state']=='complete':return dict(stats,already_complete=True,discovery_state='previous_scan')
        stats['discovery_state']='verification_only'
        if candidate_limit is None:
            # Preserve unfinished tax work across daily jobs. Only current revisions
            # can produce a digest; stale observations remain pending below.
            backlog=store.execute('SELECT DISTINCT t.property_id FROM scan_tasks t JOIN properties p ON p.id=t.property_id WHERE t.done=0 AND p.state=?',('Pending Verification',)).fetchall()
            for (identity,) in backlog:
                p=store.get_property(identity)
                event_id=f"{identity}:price:{p['revision']}"
                ready=store.execute("SELECT kind FROM deal_events WHERE id=? AND state='ready'",(event_id,)).fetchone()
                if ready:
                    store.execute('INSERT INTO scan_tasks VALUES (?,?,0,?,?) ON CONFLICT(job_id,property_id) DO NOTHING',
                                  (job_id,identity,2,event_id))
        stats.pop('stop_reason',None)
        stats.pop('error_type',None)
        stats.setdefault('received',0);stats.setdefault('tax_checked',0);stats.setdefault('tax_cache_hits',0)
        offset=job['offset'];collection_done=job['collection_done'];attempt=str(uuid.uuid4())
        started_at=self.clock().isoformat()
        store.run(attempt,dict(job_id=job_id,status='running',started_at=started_at))
        try:
            pages=0
            while not collection_done and pages<self.settings.max_pages:
                before_requests=getattr(self.client,'count',0)
                rows,total=self.client.get('/listings/sale',dict(PARAMS,limit=self.settings.page_size,offset=offset))
                stats['discovery_state']='fresh' if getattr(self.client,'count',0)>before_requests else 'cached'
                stats['listing_observed_at']=getattr(self.client,'last_fetched_at',None)
                if not isinstance(rows,list):raise ValueError('Listings response must be a list')
                total_count=int(total) if total is not None else None
                if len(rows)>self.settings.page_size:raise ValueError('Oversized listings response')
                if total_count is not None:stats['total']=total_count
                new_offset=offset+len(rows)
                collection_done=len(rows)<self.settings.page_size or (total_count is not None and new_offset>=total_count)
                if first_page_only:
                    stats['sample_only']=True
                    stats['sample_truncated']=not collection_done
                    collection_done=True
                with store.transaction():
                    for row in rows:
                        if self.ingest(job_id,row):stats['tax_cache_hits']+=1
                    stats['received']+=len(rows)
                    store.checkpoint(job_id,new_offset,collection_done,stats)
                offset=new_offset;pages+=1
                # Leave room for enrichment before another page can exhaust the
                # run. The persisted cursor resumes collection on a later run.
                if candidate_limit is None and not collection_done and self.client.count>=max(1,self.settings.per_run-1):
                    if store.execute('SELECT 1 FROM scan_tasks WHERE job_id=? AND done=0 LIMIT 1',(job_id,)).fetchone():break
            if not collection_done:
                stats['partial']=True;store.operation('pagination:'+job_id,'Listing collection paused at the run or page allowance; the next scheduled run resumes this cursor.')
            tasks=store.execute('''SELECT t.property_id,t.event_id FROM scan_tasks t WHERE t.job_id=? AND t.done=0
              ORDER BY t.priority,(SELECT MIN(j.day) FROM scan_tasks older JOIN scan_jobs j ON j.id=older.job_id
                        WHERE older.property_id=t.property_id AND older.done=0),t.property_id''',(job_id,)).fetchall()
            if candidate_limit is not None:
                if not 0<=candidate_limit<=3:raise ValueError('POC supports at most three tax candidates')
                key='poc_selection:'+job_id
                selected=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
                identities=json.loads(selected[0]) if selected else [r[0] for r in tasks[:candidate_limit]]
                if not selected:store.save_setting(key,identities)
                tasks=[r for r in tasks if r[0] in identities]
            needed=sum(1 for identity,event_id in tasks if event_id and not store.get_tax(identity,self.clock().isoformat()))
            health=store.budget_health(self.settings,self.clock())
            if needed+health['days_remaining']>health['remaining']:
                store.operation('forecast:'+job_id,'Current tax-verification backlog may exceed the allowance after reserving one listing request per remaining day.')
                event('budget_forecast',uncached_tax_checks=needed,**health)
            for identity,event_id in tasks:
                p=store.get_property(identity)
                # Older scan resumes must never overwrite a newer observation.
                state,reasons=verification(p['payload'],self.boundary)
                if state!='Candidate' or p['source']!=LIVE:
                    store.execute('UPDATE scan_tasks SET done=1 WHERE job_id=? AND property_id=?',(job_id,identity));continue
                if self.clock()-dt.datetime.fromisoformat(p['observed_at'])>dt.timedelta(hours=24):
                    store.set_verification(identity,'Pending Verification',['stale_listing']);
                    continue
                saved=store.get_tax(identity,self.clock().isoformat())
                if saved and saved[1]==LIVE:
                    record,source,fetched=saved;stats['tax_cache_hits']+=1
                elif not event_id:
                    store.set_verification(identity,'Pending Verification',['tax_refresh_not_needed_no_new_event'])
                    store.execute('UPDATE scan_tasks SET done=1 WHERE job_id=? AND property_id=?',(job_id,identity));continue
                else:
                    try:record,_=self.client.get('/properties/'+urllib.parse.quote(identity,safe=''))
                    except RequestDeferred:
                        store.set_verification(identity,'Pending Verification',['request_deferred'])
                        continue
                    except (urllib.error.URLError,TimeoutError) as exc:
                        if isinstance(exc,urllib.error.HTTPError) and exc.code in {401,403}:raise
                        store.set_verification(identity,'Pending Verification',['tax_provider_unavailable'])
                        continue
                    stats['tax_checked']+=1;source=getattr(self.client,'source','fixture');fetched=getattr(self.client,'last_fetched_at',None) or self.clock().isoformat()
                    if not isinstance(record,dict) or record.get('id')!=identity or demo_marked(record):
                        store.set_verification(identity,'Pending Verification',['tax_identity_or_source_mismatch'])
                        continue
                    expires=tax_expiry(record,dt.datetime.fromisoformat(fetched),self.settings)
                    store.save_tax(identity,record,source,fetched,expires.isoformat())
                # Cached records need the same identity/demo checks as transport
                # records; a mismatched cache must never approve a property.
                if not isinstance(record,dict) or record.get('id')!=identity or demo_marked(record):
                    store.set_verification(identity,'Pending Verification',['tax_identity_or_source_mismatch'])
                    continue
                state,tax,reason=tax_verification(record,self.clock().date())
                if source!=LIVE:state,tax,reason='Pending Verification',None,'non_live_tax_source'
                with store.transaction():
                    store.set_verification(identity,state,[reason] if reason else [])
                    if state=='Qualified' and event_id:
                        payload=dict(listing=p['payload'],tax=tax,source=LIVE,tax_source=source,verified_at=self.clock().isoformat(),tax_fetched_at=fetched)
                        store.execute("UPDATE deal_events SET payload=? WHERE id=? AND state='ready' AND revision=? AND price=?",(json.dumps(payload),event_id,p['revision'],p['payload']['price']))
                    if state!='Pending Verification':
                        # Resolve matching work in older scans too; a pending
                        # negative cache is never mistaken for a completed check.
                        store.execute('UPDATE scan_tasks SET done=1 WHERE job_id=? AND property_id=?',(job_id,identity))
                        if event_id:
                            store.execute('UPDATE scan_tasks SET done=1 WHERE property_id=? AND event_id=?',(identity,event_id))
                    store.checkpoint(job_id,offset,collection_done,stats)
            pending=store.execute('SELECT COUNT(*) FROM scan_tasks WHERE job_id=? AND done=0',(job_id,)).fetchone()[0]
            stats['partial']=not collection_done or pending>0
            status='partial' if stats['partial'] else 'complete'
            store.checkpoint(job_id,offset,collection_done,stats,status)
            store.run(attempt,dict(job_id=job_id,status=status,started_at=started_at,finished_at=self.clock().isoformat(),**stats))
            return stats
        except BudgetExceeded as exc:
            stats['partial']=True;stats['stop_reason']=str(exc)
            store.operation('budget-stop:'+job_id,'Scan paused by API budget; pending properties have not been approved. '+str(exc))
            store.checkpoint(job_id,offset,collection_done,stats,'partial')
            store.run(attempt,dict(job_id=job_id,status='partial',started_at=started_at,finished_at=self.clock().isoformat(),**stats));event('scan_paused',**stats)
            return stats
        except Exception as exc:
            stats['partial']=True
            stats['error_type']=type(exc).__name__
            store.checkpoint(job_id,offset,collection_done,stats,'failed')
            store.run(attempt,dict(job_id=job_id,status='failed',started_at=started_at,finished_at=self.clock().isoformat(),**stats))
            store.operation('failure:'+job_id,'Scan failed; use --resume '+job_id+' after fixing the connection.')
            raise
