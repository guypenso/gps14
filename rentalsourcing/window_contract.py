"""One factual processing-window contract, independent of market completeness.

No external calls, spending, property decisions or emails. Evidence holds and
planned pacing remain visible without becoming fake infrastructure failures.
"""
VERSION='processing-window-v1'
LABELS={'fresh':'Fresh Discovery Completed','free_source':'Free-Source Processing Completed',
        'paced':'Budget-Paced Processing','partial':'Partially Completed','failed':'Failed'}
CRITICAL_COMPONENTS={'review_feedback','property_sync','inventory_sync','price_sync','consistency',
                     'daily_workflow','email_delivery','discovery','discovery_slot_morning','discovery_slot_evening','sync_backlog'}


def free_source_status(result):
    runs=(result.get('email') or {}).get('results') or []
    transport=bool(runs) and all(r.get('state') in ('success','skipped') and not r.get('failed') and not r.get('more') and not r.get('pending_reparse') for r in runs)
    holds=sum(r.get('unresolved_listing_blocks',0) for r in runs)
    return {'checked':bool(runs) and any(r.get('state')=='success' for r in runs),
            'completed':transport,'evidence_holds':holds,
            'messages_processed':sum(r.get('messages_processed',0) for r in runs),
            'requires_retry':not transport or not any(r.get('state')=='success' for r in runs),'state':result.get('state','not_checked')}


def outcome(stats,*,critical=0,source=None):
    source=source or stats.get('free_source') or {}
    fresh=bool(stats.get('discovery_state')=='fresh' and stats.get('fresh_request_made') and
               stats.get('requests_consumed',0)>0 and stats.get('pages'))
    issues=[]
    for field in ('future_discovery','rental_intelligence','property_quality','evening_inventory_receipt'):
        if stats.get(field,{}).get('state') in ('pending_recovery','failed'):issues.append(field)
    if stats.get('listing_intake_state') in ('failed','not_configured') or source.get('requires_retry'):issues.append('free_source')
    states={v.get('state') for v in stats.get('partitions',{}).values()}
    if states & {'source_blocked','retry_scheduled','request_deferred'}:issues.append('source_retrieval')
    failed=stats.get('discovery_state')=='failed' or bool(stats.get('error_type'))
    if failed:state='partial' if fresh or source.get('checked') else 'failed'
    elif critical or issues:state='partial'
    elif fresh:state='fresh'
    elif stats.get('discovery_state')=='scheduled_pacing_no_listing_request':state='paced'
    elif source.get('completed') and source.get('checked'):state='free_source'
    else:state='partial'
    return {'version':VERSION,'state':state,'label':LABELS[state],
            'fresh_rentcast_discovery':fresh,'free_source_checked':source.get('checked',False),
            'provider_query_complete':stats.get('discovery_complete',False),
            'market_coverage_verified':False,'critical_failures':critical,'recoverable_stages':issues,
            'evidence_holds':source.get('evidence_holds',0),'budget_reason':stats.get('budget_reason'),
            'exit_failure':bool(critical or state=='failed')}
