import datetime as dt
import json
import logging
import os

def configure_cli():
    """Cron evidence must reach stdout even when the host preconfigures logging."""
    import sys
    logger=logging.getLogger('rentalsourcing')
    logger.setLevel(logging.INFO)
    logger.disabled=False
    if not any(getattr(h,'rentalsourcing_cli',False) for h in logger.handlers):
        handler=logging.StreamHandler(sys.stdout)
        handler.rentalsourcing_cli=True
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    logger.propagate=False

def event(name, **data):
    text=json.dumps({'time':dt.datetime.now(dt.timezone.utc).isoformat(),'event':name,**data},ensure_ascii=False,default=str)
    for key in ('RENTCAST_API_KEY','RESEND_API_KEY','DATABASE_URL','SOURCE_DATABASE_URL','DASHBOARD_PASSWORD'):
        value=os.getenv(key)
        if value: text=text.replace(value,'[REDACTED]')
    logging.getLogger('rentalsourcing').info(text)
