"""Verify original, fully signed MIME bytes. No Authentication-Results trust path."""
from dataclasses import dataclass,field
import datetime as dt
from email import policy as mail_policy
from email.parser import BytesParser
from email.utils import getaddresses,parsedate_to_datetime
import hashlib
import ipaddress
import json
import logging
import re
import time
from urllib.parse import urlsplit,urlencode
import urllib.request
import urllib.error
import dkim
import dns.rdata
import dns.rdataclass
import dns.rdatatype
import dns.message
import dns.flags
import dns.rcode
import base64
from .request import NoRedirect

LIMIT=2_000_000
DNS_URL='https://cloudflare-dns.com/dns-query'
CRITICAL=('from','to','date','subject','message-id','content-type','mime-version')
OPTIONAL=('content-transfer-encoding','content-disposition')


class AuthFailure(RuntimeError):
    def __init__(self,category,retryable=False):
        super().__init__(category);self.category=category;self.retryable=retryable


def domain(value):
    if not isinstance(value,str) or len(value)>253:raise AuthFailure('invalid_dkim_domain')
    value=value.rstrip('.').lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',value) or '.' not in value or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',p) for p in value.split('.')):
        raise AuthFailure('invalid_dkim_domain')
    return value


def addresses(values):
    result=getaddresses([str(v) for v in values])
    if not result or any(not a or '@' not in a for _,a in result):raise AuthFailure('ambiguous_original_address')
    return [a.lower() for _,a in result]


def raw_url(value,hosts):
    try:
        p=urlsplit(value);host=p.hostname
        if p.scheme!='https' or not host or p.username or p.password or p.fragment or p.port not in (None,443):raise ValueError
        if host.lower() not in hosts or not p.path or host.lower().endswith(('.local','.internal')):raise ValueError
        try:ipaddress.ip_address(host)
        except ValueError:pass
        else:raise ValueError
        if any(c.isspace() for c in value):raise ValueError
    except (ValueError,TypeError):raise AuthFailure('raw_download_host_not_approved') from None
    return value


@dataclass(frozen=True)
class VerifiedEmail:
    email_id: str
    raw: bytes=field(repr=False)
    sender: str
    signing_domain: str
    selector: str
    verified_at: str
    raw_sha256: str


class DkimDNS:
    """Fixed HTTPS resolver, TTL-bound PG public-key cache, bounded CNAME chain.
    TLS authenticates the resolver. No sender-chosen HTTP endpoint/UDP resolver.
    This is DKIM DNS trust, not a claim that all signing domains deploy DNSSEC.
    """
    def __init__(self,store,*,opener=None,clock=None):
        self.store=store;self.opener=opener or urllib.request.build_opener(NoRedirect)
        self.clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))

    def __call__(self,name,timeout=5):
        name=name.decode('ascii').rstrip('.').lower()
        if not re.fullmatch(r'[a-z0-9_.-]{1,253}',name) or '._domainkey.' not in name:raise AuthFailure('invalid_dkim_selector')
        now=self.clock();cached=self.store.execute('SELECT payload,expires_at FROM realscout_dkim_keys WHERE name=?',(name,)).fetchone()
        if cached and dt.datetime.fromisoformat(cached[1])>now:return cached[0].encode() if cached[0] else None
        current=name;seen=set();ttl=3600;key=None
        for _ in range(4):
            if current in seen:raise AuthFailure('dkim_dns_alias_loop')
            seen.add(current)
            query=dns.message.make_query(current,dns.rdatatype.TXT,want_dnssec=True)
            encoded=base64.urlsafe_b64encode(query.to_wire()).rstrip(b'=').decode()
            req=urllib.request.Request(DNS_URL+'?'+urlencode(dict(dns=encoded)),
                headers={'Accept':'application/dns-message','User-Agent':'RentalSourcing/1.0'})
            try:
                with self.opener.open(req,timeout=min(5,timeout)) as response:
                    body=response.read(65537)
                    if response.status!=200 or len(body)>65536:raise AuthFailure('dkim_dns_unavailable',True)
                    answer=dns.message.from_wire(body)
            except AuthFailure:raise
            except Exception:raise AuthFailure('dkim_dns_unavailable',True) from None
            if not query.is_response(answer):raise AuthFailure('dkim_dns_question_mismatch')
            if answer.rcode() not in (dns.rcode.NOERROR,dns.rcode.NXDOMAIN) or answer.flags & dns.flags.TC:
                raise AuthFailure('dkim_dns_unavailable',True)
            records=[dict(name=rr.name.to_text(),type=rr.rdtype,TTL=rr.ttl,data=item.to_text())
                     for rr in answer.answer for item in rr]
            need_query=False
            # Follow only explicit CNAME owners; do not accept unrelated TXT records.
            for hop in range(4):
                entries=[r for r in records if r.get('name','').rstrip('.').lower()==current]
                txt=[r for r in entries if r.get('type')==16];alias=[r for r in entries if r.get('type')==5]
                if txt and not alias:
                    if len(txt)!=1:raise AuthFailure('ambiguous_dkim_dns_key')
                    try:
                        parsed=dns.rdata.from_text(dns.rdataclass.IN,dns.rdatatype.TXT,txt[0]['data'])
                        key=b''.join(parsed.strings).decode('ascii');ttl=min(ttl,int(txt[0]['TTL']))
                    except Exception:raise AuthFailure('invalid_dkim_dns_key') from None
                    break
                if not alias:break
                if len(alias)!=1:raise AuthFailure('ambiguous_dkim_dns_key')
                ttl=min(ttl,int(alias[0].get('TTL',0)));target=alias[0]['data'].rstrip('.').lower()
                if not re.fullmatch(r'[a-z0-9_.-]{1,253}',target):raise AuthFailure('invalid_dkim_dns_alias')
                if target in seen:raise AuthFailure('dkim_dns_alias_loop')
                current=target
                if not any(r.get('name','').rstrip('.').lower()==current for r in records):
                    need_query=True;break
                seen.add(current)
            if key is not None or not need_query:break
        else:raise AuthFailure('dkim_dns_alias_limit')
        if key is None:ttl=60
        expiry=now+dt.timedelta(seconds=max(0,min(3600,ttl)))
        self.store.execute('INSERT INTO realscout_dkim_keys VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at',
            (name,key,now.isoformat(),expiry.isoformat()))
        return key.encode() if key is not None else None


class RawDkimVerifier:
    def __init__(self,store,hosts,*,opener=None,dnsfunc=None,clock=None):
        self.hosts=tuple(h.lower() for h in hosts);self.opener=opener or urllib.request.build_opener(NoRedirect)
        self.clock=clock or (lambda:dt.datetime.now(dt.timezone.utc));self.dns=dnsfunc or DkimDNS(store,clock=self.clock)

    def verify(self,data,source_policy):
        info=data.get('raw')
        if not isinstance(info,dict) or not info.get('download_url'):raise AuthFailure('original_raw_message_unavailable',True)
        url=raw_url(info['download_url'],self.hosts)
        now=self.clock()
        try:
            expiry=dt.datetime.fromisoformat(info['expires_at'].replace('Z','+00:00'))
            if expiry.tzinfo is None:raise ValueError
            if expiry<=now:raise AuthFailure('raw_download_expired',True)
        except (KeyError,ValueError,TypeError):raise AuthFailure('invalid_raw_download_metadata') from None
        # Deliberately NO Resend Authorization, cookies, Referer or redirected requests.
        req=urllib.request.Request(url,headers={'Accept':'message/rfc822','User-Agent':'RentalSourcing/1.0'})
        try:
            with self.opener.open(req,timeout=20) as response:
                raw=response.read(LIMIT+1)
                if response.status!=200:raise AuthFailure('raw_download_unavailable',True)
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close();raise AuthFailure('raw_download_unavailable',code in (403,404,408,429,500,502,503,504)) from None
        except (urllib.error.URLError,TimeoutError,OSError):raise AuthFailure('raw_download_unavailable',True) from None
        return self.verify_bytes(raw,data,source_policy)

    def verify_bytes(self,raw,data,source_policy):
        now=self.clock()
        if not isinstance(raw,bytes) or len(raw)>LIMIT:raise AuthFailure('original_raw_message_too_large')
        msg=BytesParser(policy=mail_policy.default).parsebytes(raw)
        if msg.defects or any(len(msg.get_all(k,[]))!=1 for k in CRITICAL) or any(len(msg.get_all(k,[]))>1 for k in OPTIONAL):
            raise AuthFailure('ambiguous_signed_headers')
        sender=addresses(msg.get_all('From'))
        if len(sender)!=1 or sender[0] not in {s.lower() for s in source_policy.allowed_senders}:raise AuthFailure('original_sender_not_authorized')
        if addresses([data.get('from','')])!=sender:raise AuthFailure('original_sender_metadata_mismatch')
        recipients=addresses(msg.get_all('To'))
        if not source_policy.original_recipients or not set(recipients)&{a.lower() for a in source_policy.original_recipients}:
            raise AuthFailure('original_recipient_not_authorized')
        if str(msg['Message-ID']).strip()!=data.get('message_id'):raise AuthFailure('original_message_identity_mismatch')
        try:
            sent=parsedate_to_datetime(str(msg['Date']));received=dt.datetime.fromisoformat(data['created_at'].replace('Z','+00:00'))
            if sent.tzinfo is None or received.tzinfo is None:raise ValueError
            if sent>received+dt.timedelta(minutes=5) or received>now+dt.timedelta(minutes=5) or now-sent>dt.timedelta(days=7):raise ValueError
        except (ValueError,TypeError,KeyError,OverflowError):raise AuthFailure('signed_message_outside_time_window') from None
        signatures=msg.get_all('DKIM-Signature',[])
        if not 1<=len(signatures)<=5:raise AuthFailure('missing_or_excessive_dkim_signatures')
        quiet=logging.Logger('dkim-verification-private',level=logging.CRITICAL+1)
        verifier=dkim.DKIM(raw,minkey=2048,logger=quiet);deadline=time.monotonic()+25
        def lookup(name,timeout=5):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise AuthFailure('dkim_dns_unavailable',True)
            return self.dns(name,timeout=min(timeout,remaining,5))
        for idx,value in enumerate(signatures):
            try:
                tags=dkim.util.parse_tag_value(str(value).encode('ascii'))
                signer=domain(tags[b'd'].decode('ascii'));selector=tags[b's'].decode('ascii')
                if signer!=sender[0].rsplit('@',1)[1] or tags.get(b'a')!=b'rsa-sha256' or b'l' in tags:continue
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,63}',selector):continue
                signed={h.strip().lower() for h in tags[b'h'].decode('ascii').split(':')}
                if not set(CRITICAL).issubset(signed) or any(msg.get(k) is not None and k not in signed for k in OPTIONAL):continue
                stamp=int(tags[b't']);age=now.timestamp()-stamp
                if age < -300 or age>7*86400 or stamp>received.timestamp()+300:continue
                if b'x' in tags and (int(tags[b'x'])<=now.timestamp() or int(tags[b'x'])<stamp):continue
                if verifier.verify(idx=idx,dnsfunc=lookup):
                    return VerifiedEmail(data['id'],raw,sender[0],signer,selector,now.isoformat(),hashlib.sha256(raw).hexdigest())
            except AuthFailure:raise
            except (dkim.DKIMException,ValueError,KeyError,UnicodeError,TypeError,OverflowError):continue
        raise AuthFailure('aligned_full_message_dkim_not_verified')
