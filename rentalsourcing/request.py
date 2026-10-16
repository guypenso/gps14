"""Request construction and offline diagnostics. Importing this module sends nothing."""
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

PARAMS = dict(city='Pittsburgh', state='PA', status='Active', bedrooms='3:*',
              bathrooms='1:*', squareFootage='1000:*', price='*:200000',
              limit=20, offset=0, includeTotalCount='true')


def build_request():
    key = os.environ.get('RENTCAST_API_KEY')
    if not key:
        raise RuntimeError('RENTCAST_API_KEY is not configured')
    return urllib.request.Request(
        'https://api.rentcast.io/v1/listings/sale?' + urllib.parse.urlencode(PARAMS),
        headers={'X-Api-Key': key, 'Accept': 'application/json'}, method='GET')


def diagnose(error):
    """Classify locally; retain reason with runtime secret redacted."""
    reason = getattr(error, 'reason', error)
    message = str(reason)
    key = os.environ.get('RENTCAST_API_KEY')
    if key:
        message = message.replace(key, '[REDACTED]')
    if isinstance(error, urllib.error.HTTPError):
        stage = 'http_response_origin_unverified'
    elif 'Tunnel connection failed' in message:
        stage = 'proxy_connect'
    elif isinstance(reason, socket.gaierror):
        stage = 'dns'
    elif isinstance(reason, ssl.SSLError):
        stage = 'tls'
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        stage = 'timeout_stage_unknown'
    else:
        stage = 'unknown'
    return dict(errorType=type(error).__module__ + '.' + type(error).__name__,
                reasonType=type(reason).__module__ + '.' + type(reason).__name__,
                reason=message, stage=stage, errno=getattr(reason, 'errno', None),
                http_status=error.code if isinstance(error,urllib.error.HTTPError) else None)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send_once(*, authorized=False):
    """Requires explicit caller authorization; no retry or redirect following."""
    if not authorized:
        raise PermissionError('A new RentCast request requires user authorization')
    opener = urllib.request.build_opener(NoRedirect)
    return opener.open(build_request(), timeout=45)
