"""Resend request contract and allowlisted, secret-free failure evidence."""
import json
import os
import re
import urllib.request

ENDPOINT='https://api.resend.com/emails'


def request(body,key,identity):
    return urllib.request.Request(ENDPOINT,method='POST',data=body.encode('utf-8'),headers={
        'Authorization':'Bearer '+key,'Content-Type':'application/json',
        'Accept':'application/json','User-Agent':'RentalSourcing/1.0','Idempotency-Key':identity})


def failure(error):
    # Never persist/log raw responses: even provider errors can contain addresses
    # or echoed credentials. Only known error names and categories escape here.
    try:
        raw=error.read(8192).decode('utf-8',errors='replace')
        payload=json.loads(raw)
    except (ValueError,OSError,AttributeError):payload=None
    name=payload.get('name') if isinstance(payload,dict) else None
    message=str(payload.get('message','')).lower() if isinstance(payload,dict) else ''
    known={'validation_error','missing_api_key','restricted_api_key','suspended_api_key','invalid_permission','invalid_parameter','missing_required_field'}
    category='unclassified_http_error'
    if error.code==403 and name=='validation_error':
        if 'only send testing emails to your own' in message:category='onboarding_recipient_restriction'
        elif 'domain' in message and ('not verified' in message or 'verify your domain' in message):category='sender_domain_not_verified'
    elif name in {'missing_api_key','restricted_api_key','suspended_api_key','invalid_permission'}:category=name
    elif name in known:category='request_validation'
    headers=error.headers or {}
    request_id=headers.get('resend-request-id') or headers.get('x-request-id') or ''
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',request_id):request_id=''
    if any(os.getenv(key) and os.environ[key] in request_id for key in ('RESEND_API_KEY','RENTCAST_API_KEY')):request_id=''
    return dict(http_status=error.code,category=category,error_type=name if name in known else 'unknown',
        request_id=request_id,response_kind='json' if isinstance(payload,dict) else 'non_json',
        confirmed_rejection=bool(name in known and category!='unclassified_http_error' and error.code in {400,401,403,404,405,413,422}))
