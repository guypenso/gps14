import datetime as dt
import json
import logging
import os

def event(name, **data):
    text=json.dumps({'time':dt.datetime.now(dt.timezone.utc).isoformat(),'event':name,**data},ensure_ascii=False,default=str)
    for key in ('RENTCAST_API_KEY','RESEND_API_KEY','DATABASE_URL','SOURCE_DATABASE_URL','DASHBOARD_PASSWORD','RESEND_RECEIVING_API_KEY','RESEND_WEBHOOK_SECRET','REALSCOUT_LINK_KEY','TRESTLE_CLIENT_SECRET','TRESTLE_CLIENT_ID','MLS_BEARER_TOKEN'):
        value=os.getenv(key)
        if value: text=text.replace(value,'[REDACTED]')
    logging.getLogger('rentalsourcing').info(text)
