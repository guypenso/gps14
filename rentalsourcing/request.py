"""Request construction and offline diagnostics. Importing this module sends nothing."""
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

PARAMS = dict(city='Pittsburgh', state='PA', status='Active', bedrooms='2:4',
              bathrooms='1:*', squareFootage='1000:*', price='*:200001',
              limit=500, offset=0, includeTotalCount='true')


def listing_parameters(boundary):
    params=dict(PARAMS)
    if hasattr(boundary,'search_parameters'):
        params.pop('city',None);params.update(boundary.search_parameters())
    return params


def municipal_queries(boundary=None):
    queries=[]
    for slug,city,zipcode,name in [('pleasant-hills','Pleasant Hills','15236','Pleasant Hills Borough'),('bethel-park','Bethel Park','15102','Bethel Park Municipality')]:
        region=getattr(boundary,'additional_municipalities',{}).get(name)
        params=dict(PARAMS,city=city,zipCode=zipcode)
        if region is not None:
            from .geography import search_circle
            # Postal city names/ZIPs do not define either approved municipality.
            params.pop('city');params.pop('zipCode');params.update(search_circle(region.geometry))
        queries.append((slug,params))
    return queries

def build_request():
    key = os.environ.get('RENTCAST_API_KEY')
    if not key:
        raise RuntimeError('RENTCAST_API_KEY is not configured')
    from .config import Settings
    from .geography import load_boundary
    params=listing_parameters(load_boundary(Settings.from_env().boundary_file))
    return urllib.request.Request(
        'https://api.rentcast.io/v1/listings/sale?' + urllib.parse.urlencode(params),
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
