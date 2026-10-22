"""Explicit test double for non-cryptographic queue/report regression tests only.
Cryptographic security tests use real generated RSA signatures and RawDkimVerifier.
"""
from email.message import EmailMessage
import hashlib
from rentalsourcing.realscout_auth import VerifiedEmail,AuthFailure


class FixtureVerifier:
    def __init__(self,*args,**kwargs):pass
    def verify(self,data,policy):
        if data.get('authentication',{}).get('dmarc')!='pass':raise AuthFailure('synthetic_unverified_message')
        m=EmailMessage();m['From']=data['from'];m['Subject']=data['subject'];m['To']=policy.inbox
        m['Date']=data['headers']['date'];m['Message-ID']=data['message_id']
        m.set_content(data.get('text') or '')
        if data.get('html'):m.add_alternative(data['html'],subtype='html')
        raw=m.as_bytes()
        return VerifiedEmail(data['id'],raw,data['from'],'example.invalid','synthetic',data['created_at'],hashlib.sha256(raw).hexdigest())
