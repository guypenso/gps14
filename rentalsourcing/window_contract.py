"""One factual processing-window contract, independent of market completeness.

No external calls, spending, property decisions or emails. Evidence holds and
planned pacing remain visible without becoming fake infrastructure failures.
"""
VERSION='processing-window-v2'
LABELS={'fresh':'Fresh Discovery Completed','free_source':'Free-Source Processing Completed',
        'paced':'Budget-Paced Processing','partial':'Partially Completed','failed':'Failed'}
CRITICAL_COMPONENTS={'review_feedback','property_sync','inventory_sync','price_sync','consistency',
                     'daily_workflow','email_delivery','discovery','discovery_slot_morning','discovery_slot_evening','sync_backlog'}


def free_source_status(result):
    if isinstance(result.get('source_processing'), dict):
        return result['source_processing']
    email = result.get('email') or {}
    runs = email.get('results') or []
    public = result.get('public_inventory') or {}
    checked_mailbox = any(r.get('mailbox_checked', r.get('state') == 'success'
                          and ('messages_returned' in r or 'messages_processed' in r)) for r in runs)
    checked_public = public.get('state') in {'fetched', 'recent_verified'}
    blocked = [r.get('error_type') or 'gmail_permission_unavailable' for r in runs if r.get('state') == 'blocked']
    needs_retry = any(r.get('retryable', r.get('state') in {'failed', 'skipped'})
                      or r.get('failed') or r.get('more') or r.get('pending_reparse')
                      or r.get('historical_listing_pages_remaining') for r in runs)
    needs_retry |= (public.get('state') in {'busy', 'failed'} or
                    bool(result.get('delivery_retryable')) or
                    not runs and result.get('state') in {'partial', 'failed'})
    checked = checked_mailbox or checked_public
    return {
        'state': result.get('state', 'not_checked'),
        'checked': checked, 'mailbox_checked': checked_mailbox, 'public_checked': checked_public,
        'public_inventory_state': public.get('state'), 'public_checked_at': public.get('checked_at'),
        'completed': checked and not needs_retry and not blocked,
        'requires_retry': needs_retry, 'blocked': blocked,
        'mailbox_checks': [
            {k: r[k] for k in ('state', 'error_type', 'phase', 'completed_at', 'messages_returned',
                               'messages_processed', 'mailbox_checked', 'retryable', 'reused') if k in r}
            for r in runs[:2]
        ],
        'evidence_holds': sum(r.get('unresolved_listing_blocks', 0) for r in runs),
        'messages_processed': sum(r.get('messages_processed', 0) for r in runs if not r.get('reused')),
        'penten_acknowledgments': {
            key: sum((item.get('delivery_counts') or {}).get(key, 0) for r in runs if not r.get('reused')
                     for item in r.get('listing_results', []))
            for key in ('inserted', 'updated', 'rejected', 'excluded', 'duplicate_cards')
        },
    }


def outcome(stats,*,critical=0,source=None):
    source=source or stats.get('free_source') or {}
    fresh=bool(stats.get('discovery_state')=='fresh' and stats.get('fresh_request_made') and
               stats.get('requests_consumed',0)>0 and stats.get('pages'))
    issues=[]
    for field in ('future_discovery','rental_intelligence','property_quality','evening_inventory_receipt'):
        if stats.get(field,{}).get('state') in ('pending_recovery','failed'):issues.append(field)
    if stats.get('listing_intake_state') in ('failed','not_configured') or source.get('requires_retry') or source.get('blocked'):issues.append('free_source')
    if stats.get('digest_identity_refresh')=='pending_recovery':issues.append('digest_identity_refresh')
    states={v.get('state') for v in stats.get('partitions',{}).values()}
    if states & {'source_blocked','retry_scheduled','request_deferred','retry_cooldown'}:issues.append('source_retrieval')
    failed=stats.get('discovery_state')=='failed' or bool(stats.get('error_type'))
    if failed:state='partial' if fresh or source.get('checked') else 'failed'
    elif critical or issues:state='partial'
    elif fresh:state='fresh'
    elif stats.get('discovery_state')=='scheduled_pacing_no_listing_request' and source.get('checked'):state='paced'
    elif source.get('completed') and source.get('checked'):state='free_source'
    else:state='partial'
    source_expected = 'listing_intake_state' in stats or bool(source)
    ineffective = source_expected and not fresh and not source.get('checked')
    if ineffective and 'free_source' not in issues:issues.append('free_source')
    return {'version':VERSION,'state':state,'label':LABELS[state],
            'fresh_rentcast_discovery':fresh,'free_source_checked':source.get('checked',False),
            'provider_query_complete':stats.get('discovery_complete',False),
            'market_coverage_verified':False,'critical_failures':critical,'recoverable_stages':issues,
            'evidence_holds':source.get('evidence_holds',0),'budget_reason':stats.get('budget_reason'),
            'exit_failure':bool(critical or state=='failed' or ineffective)}
