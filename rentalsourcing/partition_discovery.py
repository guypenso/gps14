"""Bounded all-area discovery with durable provider query cursors.

The existing Engine remains the only ingestion/eligibility implementation.
Completion means exhaustion of a configured provider query, never proof of
complete municipal, neighborhood, or market inventory.
"""
import datetime as dt
import hashlib
import json
from dataclasses import replace

from .collector import Client, RequestDeferred
from .engine import Engine
from .investment_policy import POLICY_VERSION
from .municipal_coverage import AREAS, UNIFIED, overlap_size, page_evidence, pages_for_total, traversal_expired, unified_query
from .request import listing_parameters, municipal_queries
from .storage import BudgetExceeded
from .telemetry import event

VERSION = 'all-area-hybrid-v1'
PREFIX = 'coverage_partition:' + POLICY_VERSION + ':'
MAIN = 'pittsburgh-bellevue'
ORDER = (MAIN, 'bethel-park', 'pleasant-hills')


def get(store, key, default=None):
    row = store.execute('SELECT payload FROM settings WHERE name=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def scopes(boundary):
    return {MAIN: listing_parameters(boundary), **dict(municipal_queries(boundary))}


class ListingClient(Client):
    """No enrichment endpoint can spend the discovery client's allowance."""
    def get(self, path, params=None, cache=True):
        if path != '/listings/sale':
            raise PermissionError('Partition discovery authorizes listing requests only')
        rows, total = super().get(path, params, cache)
        self.last_rows = rows
        return rows, total


def _fingerprints(rows):
    return [(row['id'], hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest())
            for row in rows if isinstance(row,dict) and isinstance(row.get('id'),str) and row['id']]


def _record_page(state,page,fingerprints,total):
    seen = state.setdefault('seen_ids', {})
    duplicates = changed = 0
    for identity,fingerprint in fingerprints:
        if identity in seen:
            duplicates += 1
            changed += seen[identity] != fingerprint
        seen[identity] = fingerprint
    page.update(duplicates=duplicates,changed=changed)
    state.update(offset=page['offset_after'], query_complete=page['query_complete'],
                 last_success_at=page['fetched_at'], total=total, failures=0, retry_after=None)
    state['pages'] = state.get('pages',0) + 1
    state['observations'] = state.get('observations',0) + page['records']
    state['duplicate_observations'] = state.get('duplicate_observations',0) + duplicates
    state['changed_observations'] = state.get('changed_observations',0) + changed
    if page['query_complete']:
        state['last_complete_at'] = page['fetched_at']
    if 'area_observations' in page:
        overlap = page['offset_before'] - page['offset_requested']
        warning = state.get('continuity_warning',False) or duplicates < min(overlap,page['records'])
        page.update(expected_overlap=overlap,continuity_warning=warning)
        state.update(continuity_warning=warning,last_page=page,
                     pages_for_reported_total=pages_for_total(total,state['query']['limit']))
        state['selection_pages'] = state.get('selection_pages',0) + page['requests']


def _state(store, slug, query):
    digest = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
    state = get(store, PREFIX + slug)
    if state and state['query_hash'] == digest:
        # Engine checkpoints survive a crash between ingestion and audit update.
        row = store.execute('SELECT offset_value,collection_done,stats,updated_at FROM scan_jobs WHERE id=?', (state.get('job_id'),)).fetchone()
        if row:
            if row[0] > state['offset'] or bool(row[1]) != state['query_complete']:
                stats = json.loads(row[2])
                evidence = stats.get('last_listing_page') or {}
                state['_checkpoint_recovery'] = {'partition': slug, 'job_id': state['job_id'],
                    'offset_before': state['offset'], 'offset_after': row[0],
                    'records': evidence.get('records', max(0, row[0]-state['offset'])),
                    'requests': evidence.get('requests', int(stats.get('discovery_state') == 'fresh')),
                    'fetched_at': stats.get('listing_observed_at'), 'checkpoint_recovered': True,
                    'query_complete': bool(row[1]), 'duplicates': None, 'changed': None}
                page = state['_checkpoint_recovery']
                if 'area_observations' in evidence:
                    page.update({k:evidence[k] for k in ('offset_requested','area_observations')})
                    _record_page(state,page,evidence['fingerprints'],stats.get('total'))
                else:
                    state['last_success_at'] = stats.get('listing_observed_at') or row[3]
                    if row[1]: state['last_complete_at'] = row[3]
            state.update(offset=row[0], query_complete=bool(row[1]))
        return state
    previous_query={}
    if state:
        # A corrected provider query cannot reuse the old query's offset.
        # Retain its complete state and terminal/unfinished job for audit and
        # rollback, and carry forward fairness accounting for this cycle.
        archive=PREFIX+slug+':query-history:'+state['query_hash']+':'+hashlib.sha256(str(state.get('job_id')).encode()).hexdigest()[:16]
        store.save_setting(archive,{k:v for k,v in state.items() if k!='_checkpoint_recovery'})
        previous_query={'query_history_key':archive,**{k:state.get(k) for k in ('query','query_hash','job_id','offset','query_complete','last_success_at','last_complete_at','pages','observations','total')}}
        fairness={k:state[k] for k in ('selection_cycle','selection_pages') if k in state}
    else:fairness={}
    # Adopt an unfinished legacy job at its exact existing offset; never reset it.
    pattern = 'coverage:' + POLICY_VERSION + ':' + slug + ':%'
    old = store.execute('SELECT id,offset_value,collection_done,updated_at FROM scan_jobs WHERE id LIKE ? ORDER BY collection_done,updated_at DESC LIMIT 1', (pattern,)).fetchone() if state is None else None
    state = {'partition': slug, 'query_hash': digest, 'query': query,
             'job_id': old[0] if old and not get(store, PREFIX + slug) else None,
             'offset': old[1] if old else 0, 'query_complete': bool(old[2]) if old else False,
             'last_success_at': old[3] if old and old[2] else None,
             'last_complete_at': old[3] if old and old[2] else None,
             'coverage_claim': 'configured_provider_query_only',
             'geographic_completeness': 'not_verified', 'failures': 0}
    if previous_query:state.update(previous_query=previous_query,**fairness)
    store.save_setting(PREFIX + slug, state)
    return state


def _prepare_reason(state, day, slot, now):
    if state.get('blocked') or (state.get('retry_after') and state['retry_after'] > now.isoformat()):
        return 'source_blocked' if state.get('blocked') else 'retry_cooldown'
    if state.get('query_complete') and state.get('generation_run_key') == day + ':' + slot:
        return 'query_already_complete'
    return None


def _prepare(store, state, day, slot, now):
    reason = _prepare_reason(state, day, slot, now)
    if reason:return reason
    expired = state['partition']==UNIFIED and traversal_expired(state,now)
    if state.get('query_complete') or expired:
        # Keep the old job and offset. A completed or week-old traversal
        # yields to a fresh head on the next run with a released listing call.
        state['previous_generation'] = {k: state.get(k) for k in
            ('job_id','offset','generation_run_key','last_complete_at','pages','observations','total')}
        state['previous_generation']['end_reason']='stale_traversal' if expired else 'query_exhausted'
        state['job_id'] = None
    if not state.get('job_id'):
        state.update(job_id='coverage:' + POLICY_VERSION + ':' + state['partition'] + ':' + day + ':' + slot + ':' + state['query_hash'][:8],
                     offset=0, query_complete=False, generation_started_at=now.isoformat(),
                     generation_run_key=day + ':' + slot,
                     seen_ids={}, pages=0, observations=0, duplicate_observations=0, changed_observations=0,
                     continuity_warning=False)
    store.save_setting(PREFIX + state['partition'], state)
    return None


def run(store, boundary, settings, day, slot, *, clock, opener=None):
    """Caller owns Store.lock(); wire reservations also use the atomic budget lock."""
    from .discovery_allocation import allowance, allocation_scope
    key = 'multi-discovery:' + day + ':' + slot
    prior = get(store, key, {})
    if prior.get('finished_at'):
        return {**prior, 'already_executed': True, 'requests_this_invocation': 0}, ListingClient(store, settings, clock, opener)
    now = clock()
    special_limit = allowance(store, settings, now, day, slot)
    special = special_limit is not None
    # Preserve separately authorized historical allocations and their exact
    # per-partition quotas. New ordinary scheduled windows share one query.
    weighted = not special and settings.approval_mode == 'monthly_budget' and not settings.supplemental_requests
    queries = {UNIFIED: dict(unified_query(boundary)[0],limit=settings.page_size)} if weighted else scopes(boundary)
    order = tuple(queries) if weighted else ORDER
    states = {slug: _state(store, slug, query) for slug, query in queries.items()}
    plan = None
    if weighted:
        from .monthly_allocation import listing_plan
        plan = listing_plan(store, settings, now, states)
        remaining = plan['available']
    else:
        remaining = special_limit if special else store.scheduled_allowance(settings, now)
    # An expired allocation can still return a zero allowance for a later
    # scheduled slot. That slot has no allocation scope; keep the run alive
    # for cached evidence, RealScout intake and delivery recovery.
    scope = (allocation_scope(day, slot) or (key, 0)) if special else (key, settings.per_run)
    from .approved_catchup import DAY as MANUAL_DAY,SLOT as MANUAL_SLOT,LIMITS as MANUAL_LIMITS
    manual=(day,slot)==(MANUAL_DAY,MANUAL_SLOT)
    effective = replace(settings, per_run=max(1, remaining), per_day=(6 if day=='2026-11-13' else 4) if special else settings.per_day,
                        max_pages=1, retries=0, email_enabled=False)
    client = ListingClient(store, effective, clock, opener, request_scope=scope)
    if weighted:
        from .billing import cycle_key
        selection_cycle = cycle_key(settings, now)
        for slug, state in states.items():
            if state.get('selection_cycle') != selection_cycle:
                state.update(selection_cycle=selection_cycle, selection_pages=0)
                store.save_setting(PREFIX + slug, state)
    report = prior or {'version': VERSION, 'run_key': key, 'started_at': now.isoformat(),
                      'pages': [], 'job_ids': [], 'new_source_ids': [], 'partitions': {}}
    health=store.budget_health(settings,now,record_warning=False)
    budget_reason = plan['reason'] if plan else health.get('block_reason')
    paced = budget_reason == 'monthly_listing_pacing'
    no_query_ready = budget_reason == 'no_query_ready'
    state = ('scheduled_pacing_no_listing_request' if paced else
             'source_unavailable' if no_query_ready else
             'skipped_budget_exhausted' if not remaining else 'verification_only')
    report.update(request_limit=remaining, discovery_state=state,
                  budget_reason=budget_reason or ('account_or_daily_allowance_exhausted' if not remaining else None))
    if plan:
        report['listing_plan'] = plan
        report['target_areas'] = list(AREAS)
    before = {r[0] for r in store.execute('SELECT id FROM properties').fetchall()}
    eligible = []
    unfinished = {slug for slug,state in states.items() if state.get('job_id') and not state['query_complete']}
    for slug in order:
        recovered = states[slug].pop('_checkpoint_recovery', None) or states[slug].get('last_page')
        if recovered and recovered.get('fetched_at') and recovered['fetched_at'] >= report['started_at']:
            if not any(p['job_id'] == recovered['job_id'] and p['offset_after'] == recovered['offset_after'] for p in report['pages']):
                report['pages'].append({**recovered, 'checkpoint_recovered':True})
            if recovered['job_id'] not in report['job_ids']: report['job_ids'].append(recovered['job_id'])
        # Checking eligibility must not reset an unselected completed query.
        # Open a new generation only when this partition receives an actual turn.
        reason = _prepare_reason(states[slug], day, slot, now)
        if not reason and not remaining:
            reason = 'monthly_pacing' if paced else 'budget_blocked'
        if not reason and plan and plan['advance'] and slug != plan['advance']['partition']:
            reason = 'reserved_for_overdue_partition'
        if manual and store.count_usage('request_scope:'+key+':'+slug)>=MANUAL_LIMITS[slug]:reason='partition_limit_reached'
        report['partitions'][slug] = {'state': reason or ('budget_blocked' if not remaining else 'awaiting_turn'), 'query': states[slug]['query'],
            'offset_before': states[slug]['offset'], 'coverage_claim': 'configured_provider_query_only',
            'geographic_completeness': 'not_verified'}
        if not reason:
            eligible.append(slug)
    store.save_setting(key, report)
    if not special and not weighted:
        eligible.sort(key=lambda slug: (states[slug].get('last_success_at') or '', order.index(slug)))
    first_turns = list(eligible)
    extra = [UNIFIED] if weighted else [MAIN, MAIN, 'bethel-park', 'pleasant-hills']
    def describe_page(rows):
        return {'area_observations':page_evidence(boundary,rows),'fingerprints':_fingerprints(rows)}
    attempts = 0
    while remaining > client.count and eligible and attempts < max(20, settings.max_pages):
        if manual:
            from .approved_catchup import EXPIRES
            if clock()>=EXPIRES:
                for slug in eligible:report['partitions'][slug]['state']='approval_expired'
                break
        resumed_priority = next((slug for slug in ORDER if slug in unfinished and slug in eligible), None) if special and slot=='morning' else None
        if manual:
            slug=next(slug for slug in ORDER if slug in eligible)
        elif resumed_priority:
            slug = resumed_priority
            if slug in first_turns:first_turns.remove(slug)
        elif first_turns:
            slug = first_turns.pop(0)
        else:
            for _ in range(len(extra)):
                slug = extra.pop(0)
                extra.append(slug)
                if slug in eligible:
                    break
        state = states[slug]
        reason = _prepare(store, state, day, slot, clock())
        if reason:
            report['partitions'][slug]['state'] = reason
            eligible.remove(slug)
            continue
        if not any(p['partition'] == slug for p in report['pages']):
            report['partitions'][slug]['offset_before'] = state['offset']
        if manual:client.request_scope=(key+':'+slug,MANUAL_LIMITS[slug])
        job_id = state['job_id']
        if job_id not in report['job_ids']:
            report['job_ids'].append(job_id)
        request_before = client.count
        before_page={r[0]:(r[1],r[2]) for r in store.execute('SELECT id,revision,state FROM properties').fetchall()}
        offset_before = state['offset']
        state['last_attempt_at'] = clock().isoformat()
        attempts += 1
        try:
            stats = Engine(store, boundary, client, effective, clock, allow_paid_tax=False,
                           query_parameters=state['query'], offset_overlap=overlap_size(effective.page_size) if weighted else 0,
                           describe_page=describe_page if weighted else None).run(job_id, day)
            row = store.execute('SELECT offset_value,collection_done FROM scan_jobs WHERE id=?', (job_id,)).fetchone()
            received = stats.get('received_this_run', 0)
            if stats.get('discovery_state') not in {'fresh', 'cached'}:
                if 'deferred' in stats.get('stop_reason','').lower():
                    raise RequestDeferred('Page deferred by the existing request safety guard')
                raise BudgetExceeded('Page blocked by the existing budget guard')
            page = {'partition': slug, 'job_id': job_id, 'offset_before': offset_before,
                    'offset_after': row[0], 'records': received, 'requests': client.count - request_before,
                    'fetched_at': client.last_fetched_at, 'query_complete': bool(row[1])}
            if weighted:
                checkpoint = stats['last_listing_page']
                page.update({k:checkpoint[k] for k in ('offset_requested','area_observations')})
                fingerprints = checkpoint['fingerprints']
            else:
                fingerprints = _fingerprints(client.last_rows)
            _record_page(state,page,fingerprints,stats.get('total'))
            after_page={r[0]:(r[1],r[2]) for r in store.execute('SELECT id,revision,state FROM properties').fetchall()}
            page_ids={identity for identity,_ in fingerprints}
            perf=state.setdefault('performance',{'requests':0,'records':0,'new_identities':0,'new_reviewable':0,'known_identities':0,'price_updates':0})
            perf['requests']+=client.count-request_before;perf['records']+=received
            perf['new_identities']+=len(page_ids-before_page.keys());perf['known_identities']+=len(page_ids&before_page.keys())
            perf['new_reviewable']+=sum(i not in before_page and after_page.get(i,(0,'Rejected'))[1]!='Rejected' for i in page_ids)
            perf['price_updates']+=sum(i in before_page and after_page.get(i,(0,None))[0]!=before_page[i][0] for i in page_ids)
            from .coverage_strategy import VERSION as SELECTION_POLICY
            state['selection_policy']=plan['policy'] if weighted else SELECTION_POLICY
            if state['query_complete']:
                eligible.remove(slug)
            report['pages'].append(page)
            report['discovery_state'] = 'fresh' if any(p['requests'] for p in report['pages']) else 'cached'
            report['partitions'][slug].update(state='query_complete' if row[1] else 'pagination_pending',
                offset=row[0], last_success_at=state['last_success_at'], total=state.get('total'))
            event('discovery_partition_page', **page)
        except BudgetExceeded as exc:
            report['partitions'][slug].update(state='request_deferred' if isinstance(exc, RequestDeferred) else 'budget_blocked', error_type=type(exc).__name__)
            state['retry_after'] = (clock()+dt.timedelta(minutes=15)).isoformat()
            eligible.remove(slug)
        except Exception as exc:
            state['failures'] = state.get('failures', 0) + 1
            status = getattr(exc, 'code', None)
            state['blocked'] = status in {401, 403, 404, 422}
            state['retry_after'] = None if state['blocked'] else (clock()+dt.timedelta(minutes=min(360, 15*2**min(state['failures']-1, 4)))).isoformat()
            state['error_type'] = type(exc).__name__
            report['partitions'][slug].update(state='source_blocked' if state['blocked'] else 'retry_scheduled',
                error_type=type(exc).__name__, http_status=status)
            eligible.remove(slug)
        finally:
            store.save_setting(PREFIX + slug, state)
            report['requests'] = store.count_usage('request_scope:' + scope[0])
            report['requests_this_invocation'] = client.count
            if manual and store.count_usage('request_scope:'+key+':'+slug)>=MANUAL_LIMITS[slug] and slug in eligible:
                eligible.remove(slug)
                report['partitions'][slug].update(state='partition_limit_reached')
            report['new_source_ids'] = sorted(set(report['new_source_ids']) | ({r[0] for r in store.execute('SELECT id FROM properties').fetchall()} - before))
            store.save_setting(key, report)
    report['requests'] = store.count_usage('request_scope:' + scope[0])
    report.update(finished_at=clock().isoformat(), requests_this_invocation=client.count,
                  received_this_run=sum(p['records'] for p in report['pages']),
                  requests_consumed=report.get('requests', 0),
                  discovery_complete=all(s['query_complete'] and (s.get('generation_run_key')==day+':'+slot or any(p['partition']==slug for p in report['pages'])) for slug,s in states.items()),
                  supplemental_jobs=report['job_ids'][1:],
                  remaining_budget=store.budget_health(settings, clock(), record_warning=False)['remaining'])
    report['discovery_partial'] = report['partial'] = not report['discovery_complete']
    report['fresh_request_made'] = any(p['requests'] for p in report['pages'])
    if report['pages']:
        report['discovery_state'] = 'fresh' if report['fresh_request_made'] else 'cached'
    report['latest_listing_observation_at'] = max((p['fetched_at'] for p in report['pages'] if p.get('fetched_at')), default=None)
    if not report['pages'] and any(p['state'] in {'source_blocked','retry_scheduled'} for p in report['partitions'].values()):
        report['discovery_state'] = 'failed'
    # This also recovers first-discovery identities persisted before an interruption.
    for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?', ('first_discovery:%',)).fetchall():
        first = json.loads(raw)
        if first.get('job_id') in report['job_ids'] and first.get('at', '') >= report['started_at']:
            report['new_source_ids'] = sorted(set(report['new_source_ids']) | {first['source_id']})
    if eligible:
        for slug in eligible:
            if report['partitions'][slug]['state'] == 'awaiting_turn':
                report['partitions'][slug]['state'] = 'budget_not_reached'
    store.save_setting(key, report)
    store.save_setting('multi-discovery:last', report)
    event('multi_partition_discovery', **{k: v for k, v in report.items() if k not in {'new_source_ids', 'job_ids'}})
    return report, client


def snapshot(store, now, boundary=None):
    """Read-only operational evidence for authenticated production verification."""
    from .discovery_allocation import snapshot as allocation_snapshot
    states = []
    for slug in (UNIFIED, *ORDER):
        state = get(store, PREFIX + slug, {})
        if not state:
            continue
        entry={k: v for k, v in state.items() if k not in {'seen_ids'}}
        try:
            observed=state.get('last_success_at')
            if not isinstance(observed,str):raise ValueError('Partition has no successful retrieval yet')
            at=dt.datetime.fromisoformat(observed.replace('Z','+00:00'))
            entry['coverage_age_seconds']=max(0,int((now-at).total_seconds()))
        except (KeyError,TypeError,ValueError):entry['coverage_age_seconds']=None
        entry['remaining_records_in_reported_total']=max(0,state['total']-state.get('offset',0)) if isinstance(state.get('total'),int) else None
        states.append(entry)
    last = get(store, 'multi-discovery:last', {})
    last = {k: v for k, v in last.items() if k in {'version','run_key','started_at','finished_at','request_limit',
        'requests','requests_consumed','received_this_run','remaining_budget','discovery_state',
        'discovery_complete','discovery_partial','pages','partitions','delivery_totals','penten_receipts',
        'fresh_request_made','latest_listing_observation_at','budget_reason','listing_plan','free_source','target_areas','municipalities'}}
    result = {'version': VERSION, 'allocation': allocation_snapshot(store, now),
              'partitions': states, 'last_run': last,
              'provider_calls': 0, 'geographic_completeness': 'not_verified'}
    if boundary is not None:
        from .municipal_coverage import comparison, geometries
        if geometries(boundary).keys()==AREAS.keys():
            result['unified_query_evidence']=comparison(store,boundary,now,scopes(boundary))
            active=next((state for state in states if state.get('partition')==UNIFIED),{})
            result['unified_query_evidence'].update(provider_total=active.get('total'),
                pages_for_reported_total=active.get('pages_for_reported_total'),total_observed_at=active.get('last_success_at'))
    return result


def preflight(store, boundary, settings, now):
    """Adopt saved cursors and verify future allowances without issuing requests."""
    from .discovery_allocation import allowance
    from .schedule import ISRAEL
    for slug, query in scopes(boundary).items():
        _state(store, slug, query)
    return {'version': VERSION, 'provider_calls': 0,
            'plans': [{'day': day, 'slot': slot, 'limit': allowance(store, settings,
                dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hour), tzinfo=ISRAEL), day, slot),
                'priority': list(ORDER)} for day, slot, hour in
                [('2026-11-13','evening',20),('2026-11-14','morning',8)]]}
