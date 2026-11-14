"""Bounded, fair listing discovery using independent durable query cursors.

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
from .request import listing_parameters, municipal_queries
from .storage import BudgetExceeded
from .telemetry import event

VERSION = 'multi-partition-v2'
PREFIX = 'coverage_partition:' + POLICY_VERSION + ':'
MAIN = 'pittsburgh-bellevue'
ORDER = (MAIN, 'bethel-park', 'pleasant-hills')


def get(store, key, default=None):
    row = store.execute('SELECT payload FROM settings WHERE name=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def scopes(boundary):
    return {MAIN: listing_parameters(boundary), **dict(municipal_queries())}


class ListingClient(Client):
    """No enrichment endpoint can spend the discovery client's allowance."""
    def get(self, path, params=None, cache=True):
        if path != '/listings/sale':
            raise PermissionError('Partition discovery authorizes listing requests only')
        rows, total = super().get(path, params, cache)
        self.last_rows = rows
        return rows, total


def _state(store, slug, query):
    digest = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
    state = get(store, PREFIX + slug)
    if state and state['query_hash'] == digest:
        # Engine checkpoints survive a crash between ingestion and audit update.
        row = store.execute('SELECT offset_value,collection_done,stats,updated_at FROM scan_jobs WHERE id=?', (state.get('job_id'),)).fetchone()
        if row:
            if row[0] > state['offset'] or bool(row[1]) != state['query_complete']:
                stats = json.loads(row[2])
                state['_checkpoint_recovery'] = {'partition': slug, 'job_id': state['job_id'],
                    'offset_before': state['offset'], 'offset_after': row[0], 'records': row[0]-state['offset'],
                    'requests': int(stats.get('discovery_state') == 'fresh'),
                    'fetched_at': stats.get('listing_observed_at'), 'checkpoint_recovered': True,
                    'query_complete': bool(row[1]), 'duplicates': None, 'changed': None}
                state['last_success_at'] = stats.get('listing_observed_at') or row[3]
                if row[1]: state['last_complete_at'] = row[3]
            state.update(offset=row[0], query_complete=bool(row[1]))
        return state
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
    store.save_setting(PREFIX + slug, state)
    return state


def _prepare(store, state, day, slot, now, special):
    if state.get('blocked') or (state.get('retry_after') and state['retry_after'] > now.isoformat()):
        return 'source_blocked' if state.get('blocked') else 'retry_cooldown'
    if state.get('query_complete'):
        if state.get('generation_run_key') == day + ':' + slot:
            return 'query_already_complete'
        # Completion belongs to one collection generation, not every future
        # scheduled slot. A later slot must check for new/changed inventory.
        # The previous scan job and its terminal offset remain untouched.
        state['previous_generation'] = {k: state.get(k) for k in
            ('job_id','offset','generation_run_key','last_complete_at')}
        state['job_id'] = None
    if not state.get('job_id'):
        state.update(job_id='coverage:' + POLICY_VERSION + ':' + state['partition'] + ':' + day + ':' + slot + ':' + state['query_hash'][:8],
                     offset=0, query_complete=False, generation_started_at=now.isoformat(),
                     generation_run_key=day + ':' + slot,
                     seen_ids={}, pages=0, observations=0, duplicate_observations=0, changed_observations=0)
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
    remaining = special_limit if special else store.scheduled_allowance(settings, now)
    scope = allocation_scope(day, slot) if special else (key, settings.per_run)
    effective = replace(settings, per_run=max(1, remaining), per_day=(6 if day=='2026-11-13' else 4) if special else settings.per_day,
                        max_pages=1, retries=0, email_enabled=False)
    client = ListingClient(store, effective, clock, opener, request_scope=scope)
    states = {slug: _state(store, slug, query) for slug, query in scopes(boundary).items()}
    report = prior or {'version': VERSION, 'run_key': key, 'started_at': now.isoformat(),
                      'pages': [], 'job_ids': [], 'new_source_ids': [], 'partitions': {}}
    report.update(request_limit=scope[1], discovery_state='skipped_budget_exhausted' if not remaining else 'verification_only')
    before = {r[0] for r in store.execute('SELECT id FROM properties').fetchall()}
    eligible = []
    unfinished = {slug for slug,state in states.items() if state.get('job_id') and not state['query_complete']}
    for slug in ORDER:
        recovered = states[slug].pop('_checkpoint_recovery', None)
        if recovered and recovered.get('fetched_at') and recovered['fetched_at'] >= report['started_at']:
            if not any(p['job_id'] == recovered['job_id'] and p['offset_after'] == recovered['offset_after'] for p in report['pages']):
                report['pages'].append(recovered)
            if recovered['job_id'] not in report['job_ids']: report['job_ids'].append(recovered['job_id'])
        reason = _prepare(store, states[slug], day, slot, now, special) if remaining else 'budget_blocked'
        report['partitions'][slug] = {'state': reason or ('budget_blocked' if not remaining else 'awaiting_turn'), 'query': states[slug]['query'],
            'offset_before': states[slug]['offset'], 'coverage_claim': 'configured_provider_query_only',
            'geographic_completeness': 'not_verified'}
        if not reason:
            eligible.append(slug)
    store.save_setting(key, report)
    # Under small budgets, least recently served areas go first. With enough
    # capacity every area gets a first turn, then Pittsburgh gets extra pages.
    if not special:
        eligible.sort(key=lambda slug: (states[slug].get('last_success_at') or '', ORDER.index(slug)))
    first_turns = list(eligible)
    extra = [MAIN, MAIN, 'bethel-park', 'pleasant-hills']
    attempts = 0
    while remaining > client.count and eligible and attempts < max(20, settings.max_pages):
        resumed_priority = next((slug for slug in ORDER if slug in unfinished and slug in eligible), None) if special and slot=='morning' else None
        if resumed_priority:
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
        job_id = state['job_id']
        if job_id not in report['job_ids']:
            report['job_ids'].append(job_id)
        request_before = client.count
        offset_before = state['offset']
        state['last_attempt_at'] = clock().isoformat()
        attempts += 1
        try:
            stats = Engine(store, boundary, client, effective, clock, allow_paid_tax=False,
                           query_parameters=state['query']).run(job_id, day)
            row = store.execute('SELECT offset_value,collection_done FROM scan_jobs WHERE id=?', (job_id,)).fetchone()
            received = stats.get('received_this_run', 0)
            if stats.get('discovery_state') not in {'fresh', 'cached'}:
                if 'deferred' in stats.get('stop_reason','').lower():
                    raise RequestDeferred('Page deferred by the existing request safety guard')
                raise BudgetExceeded('Page blocked by the existing budget guard')
            seen = state.setdefault('seen_ids', {})
            duplicates = changed = 0
            for listing in getattr(client, 'last_rows', []):
                identity = listing.get('id') if isinstance(listing, dict) else None
                if not identity:
                    continue
                fingerprint = hashlib.sha256(json.dumps(listing, sort_keys=True).encode()).hexdigest()
                if identity in seen:
                    duplicates += 1
                    changed += seen[identity] != fingerprint
                seen[identity] = fingerprint
            state.update(offset=row[0], query_complete=bool(row[1]), last_success_at=clock().isoformat(),
                         total=stats.get('total'), failures=0, retry_after=None)
            state['pages'] = state.get('pages', 0) + 1
            state['observations'] = state.get('observations', 0) + received
            state['duplicate_observations'] = state.get('duplicate_observations', 0) + duplicates
            state['changed_observations'] = state.get('changed_observations', 0) + changed
            if state['query_complete']:
                state['last_complete_at'] = clock().isoformat()
                eligible.remove(slug)
            page = {'partition': slug, 'job_id': job_id, 'offset_before': offset_before,
                    'offset_after': row[0], 'records': received, 'requests': client.count - request_before,
                    'fetched_at': client.last_fetched_at, 'duplicates': duplicates, 'changed': changed,
                    'query_complete': bool(row[1])}
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
            report['new_source_ids'] = sorted(set(report['new_source_ids']) | ({r[0] for r in store.execute('SELECT id FROM properties').fetchall()} - before))
            store.save_setting(key, report)
    report.update(finished_at=clock().isoformat(), requests_this_invocation=client.count,
                  received_this_run=sum(p['records'] for p in report['pages']),
                  requests_consumed=report.get('requests', 0),
                  discovery_complete=all(s['query_complete'] and (s.get('generation_run_key')==day+':'+slot or any(p['partition']==slug for p in report['pages'])) for slug,s in states.items()),
                  supplemental_jobs=report['job_ids'][1:],
                  remaining_budget=store.budget_health(settings, clock(), record_warning=False)['remaining'])
    report['discovery_partial'] = report['partial'] = not report['discovery_complete']
    report['fresh_request_made'] = any(p['requests'] for p in report['pages'])
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


def snapshot(store, now):
    """Read-only operational evidence for authenticated production verification."""
    from .discovery_allocation import snapshot as allocation_snapshot
    states = []
    for slug in ORDER:
        state = get(store, PREFIX + slug, {})
        states.append({k: v for k, v in state.items() if k not in {'seen_ids'}})
    last = get(store, 'multi-discovery:last', {})
    last = {k: v for k, v in last.items() if k in {'version','run_key','started_at','finished_at','request_limit',
        'requests','requests_consumed','received_this_run','remaining_budget','discovery_state',
        'discovery_complete','discovery_partial','pages','partitions','delivery_totals','penten_receipts',
        'fresh_request_made','latest_listing_observation_at'}}
    return {'version': VERSION, 'allocation': allocation_snapshot(store, now),
            'partitions': states, 'last_run': last,
            'provider_calls': 0, 'geographic_completeness': 'not_verified'}


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
