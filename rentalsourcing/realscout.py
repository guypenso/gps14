"""Offline MIME/card parser. Never fetches URLs, reads mailboxes or calls providers.
Saved-search adapter is provisional; Exclusives template validated against one authorized sample.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

SEARCH = 'RentalSourcing - Pittsburgh Under $200K'
PARSER_VERSION = 'realscout-email-v2-profiles'
EXCLUSIVES_SUBJECT = 'Exclusives: Be the first to see these homes'
MAX_BYTES = 2_000_000
ADDRESS = re.compile(
    r'(?<![\w,.$])(?P<street>\d{1,6}(?:-\d+)?\s+'
    r'(?!(?:sqft|sq\.?|beds?|bedrooms?|baths?|bathrooms?|days?)\b)[A-Za-z0-9 .\'/-]+?\s+'
    r'(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct|Boulevard|Blvd|Way|Place|Pl|Circle|Cir|Terrace|Ter|Pike|Trail|Trl)\.?'
    r'(?:\s*(?:,?\s*(?:Apt\.?|Unit|Suite|Ste|\#)\s*[A-Za-z0-9-]+))?)'
    r'\s*,?\s+(?P<city>[A-Za-z][A-Za-z .\'-]*?)\s*,?\s+(?P<state>Pennsylvania|[A-Z]{2})\s*,?\s+(?P<zip>\d{5}(?:-\d{4})?)\b', re.I)
PRICE = re.compile(r'\$\s*(\d[\d,]*(?:\.\d{1,2})?)\s*([kK]?)\b')
ALIASES = {'STREET':'ST','AVENUE':'AVE','ROAD':'RD','DRIVE':'DR','LANE':'LN',
           'COURT':'CT','BOULEVARD':'BLVD','PLACE':'PL','CIRCLE':'CIR','TERRACE':'TER','TRAIL':'TRL',
           'APT':'UNIT','SUITE':'UNIT','STE':'UNIT'}


def flatten(text):
    return ' '.join(unicodedata.normalize('NFKC', text).split())


def addresses(text):
    return list(ADDRESS.finditer(flatten(text)))


def address_key(street, city, state, zipcode):
    def words(value):
        value=unicodedata.normalize('NFKC', value).upper().replace('#', ' UNIT ')
        return ' '.join(ALIASES.get(w,w) for w in re.findall(r'[A-Z0-9-]+', value))
    return '|'.join((words(street),words(city),state.upper(),zipcode[:5]))


def identity(key):
    return 'rs:' + hashlib.sha256(key.encode()).hexdigest()


def safe_url(url):
    if not isinstance(url,str) or any(c.isspace() for c in url): return None
    try:
        p=urlsplit(url)
        if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):return None
        return url
    except ValueError:return None


def url_summary(url):
    # Entire path/query can be a bearer login link. Do not print either.
    return ('https://'+urlsplit(url).hostname+'/[private-detail-link]') if safe_url(url) else None


@dataclass
class Card:
    street: str
    city: str
    state: str
    zipcode: str
    price: float | None
    bedrooms: float | None
    bathrooms: float | None
    square_footage: float | None
    detail_url: str | None = field(repr=False)
    alert_kind: str = 'unspecified'
    issues: list[str] = field(default_factory=list)
    days_on_site: int | None = None
    listing_id: str | None = None
    listing_status: str | None = None
    previous_price: float | None = None

    @property
    def key(self):return address_key(self.street,self.city,self.state,self.zipcode)

    @property
    def candidate_id(self):return identity(self.key)

    def public(self):
        result=asdict(self);result['detail_url']=url_summary(self.detail_url)
        result['candidate_id']=self.candidate_id
        return result


@dataclass
class ParsedEmail:
    message_key: str
    sent_at: str | None
    sender: str = field(repr=False)
    search_name: str | None
    cards: list[Card]
    issues: list[str]
    parser_version: str = PARSER_VERSION
    alert_profile: str = 'saved_search'

    def public(self):
        return dict(message_key=self.message_key,sent_at=self.sent_at,search_name=self.search_name,
                    cards=[c.public() for c in self.cards],issues=self.issues,parser_version=self.parser_version,
                    alert_profile=self.alert_profile)


class Node:
    def __init__(self,tag='',attrs=(),parent=None):
        self.tag=tag;self.attrs=dict(attrs);self.parent=parent;self.parts=[]
        self.depth=parent.depth+1 if parent else 0
        if self.depth>128:raise ValueError('HTML nesting limit exceeded')
    def text(self):
        if self.tag in {'script','style','head','noscript'}:return ''
        style=self.attrs.get('style','').replace(' ','').lower()
        if 'hidden' in self.attrs or self.attrs.get('aria-hidden')=='true' or 'display:none' in style:return ''
        return flatten(' '.join(p.text() if isinstance(p,Node) else p for p in self.parts))


class Tree(HTMLParser):
    VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    def __init__(self):
        super().__init__(convert_charrefs=True);self.root=Node();self.current=self.root;self.links=[];self.count=0
    def handle_starttag(self,tag,attrs):
        self.count+=1
        if self.count>20000:raise ValueError('HTML node limit exceeded')
        n=Node(tag,attrs,self.current);self.current.parts.append(n)
        if tag in {'a','mj-button'}:self.links.append(n)
        if tag=='img' and n.attrs.get('alt'):n.parts.append(n.attrs['alt'])
        if tag not in self.VOID:self.current=n
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        n=self.current
        while n.parent:
            if n.tag==tag:self.current=n.parent;return
            n=n.parent
    def handle_data(self,data):self.current.parts.append(data)


def metric(text, units):
    values=[]
    for pattern in (rf'(\d[\d,]*(?:\.\d+)?)\s*(?:{units})\b',
                    rf'(?:{units})\s*:\s*(\d[\d,]*(?:\.\d+)?)\b'):
        values.extend(float(m.replace(',','')) for m in re.findall(pattern,text,re.I))
    unique=set(values)
    return next(iter(unique)) if len(unique)==1 else None


def make_card(text,match,url):
    # The saved-search title itself contains "$200K". It is never a listing price.
    text=re.sub(re.escape(SEARCH),'',text,flags=re.I)
    prices={float(Decimal(n.replace(',',''))*(1000 if suffix else 1)) for n,suffix in PRICE.findall(text)}
    beds=metric(text,r'bedrooms?|beds?|bd|br')
    baths=metric(text,r'bathrooms?|baths?|ba')
    area=metric(text,r'sq\.?\s*ft\.?|sqft|square\s+feet')
    price=next(iter(prices)) if len(prices)==1 else None
    current=re.search(r'(?:current|list)\s+price\s*:\s*\$([\d,]+(?:\.\d{1,2})?)',text,re.I)
    previous=re.search(r'(?:previous|original)\s+price\s*:\s*\$([\d,]+(?:\.\d{1,2})?)',text,re.I)
    if current:price=float(current.group(1).replace(',',''))
    previous_price=float(previous.group(1).replace(',','')) if previous else None
    issues=[]
    for name,value in [('price',price),('bedrooms',beds),('bathrooms',baths),('square_footage',area)]:
        if value is None:issues.append('missing_or_ambiguous_'+name)
    url=safe_url(url)
    if not url:issues.append('missing_or_unsafe_detail_link')
    lower=text.lower()
    kind=('exclusive' if re.search(r'\bexclusive\b',lower) else
          'price_reduction' if re.search(r'price (?:reduced|reduction|drop)',lower) else
          'new_listing' if re.search(r'new listing|just listed',lower) else 'unspecified')
    days={int(v) for v in re.findall(r'\b(\d+)\s+days?\s+on\s+site\b',text,re.I)}
    state='PA' if match['state'].casefold()=='pennsylvania' else match['state'].upper()
    identifier=re.search(r'\b(?:MLS\s*(?:ID|#|Number)|Listing\s*(?:ID|#))\s*:?\s*([A-Za-z0-9-]{1,64})\b',text,re.I)
    status=re.search(r'\b(?:listing\s+)?status\s*:\s*(Active|Pending|Sold|Off Market|Coming Soon|For Sale)\b',text,re.I)
    return Card(match['street'].strip(' ,'),match['city'].strip(' ,'),state,match['zip'],
                price,beds,baths,area,url,kind,issues,next(iter(days)) if len(days)==1 else None,
                identifier.group(1) if identifier else None,status.group(1).title() if status else None,previous_price)


def html_cards(html):
    tree=Tree();tree.feed(html);tree.close();cards=[];issues=[]
    for link in tree.links:
        if not re.search(r'\bview\s+(?:details|property)\b',link.text(),re.I):continue
        n=link
        while n:
            text=n.text();matches=addresses(text)
            if len(matches)>1:
                issues.append('ambiguous_card_boundary');break
            if len(matches)==1:
                # Prefer an enclosing card containing its metrics. Stop before crossing into another property.
                has_metrics=any(metric(text,units) is not None for units in
                    (r'bedrooms?|beds?|bd|br',r'bathrooms?|baths?|ba',r'sq\.?\s*ft\.?|sqft|square\s+feet'))
                if has_metrics or PRICE.search(text) or n.parent is None:
                    cards.append(make_card(text,matches[0],link.attrs.get('href')));break
                parent=n.parent
                if parent and len(addresses(parent.text()))==1:n=parent;continue
                cards.append(make_card(text,matches[0],link.attrs.get('href')));break
            n=n.parent
        else:issues.append('detail_link_without_parseable_address')
    return cards,issues,tree.root.text()


def text_cards(text):
    compact=flatten(text);cards=[];start=0
    for link in re.finditer(r'View\s+(?:Details|Property)\s*:?\s*(https://[^\s<>]+)',compact,re.I):
        block=compact[start:link.end()];matches=addresses(block)
        if len(matches)==1:cards.append(make_card(block,matches[0],link.group(1)))
        start=link.end()
    return cards


def parse_email(raw,expected_search=SEARCH,*,profile='saved_search'):
    if len(raw)>MAX_BYTES:raise ValueError('Email exceeds offline parser size limit')
    message=BytesParser(policy=policy.default).parsebytes(raw)
    if message.defects:raise ValueError('Malformed MIME message')
    html=[];plain=[]
    def body_parts(part):
        if part.get_content_disposition()=='attachment' or part.get_content_type()=='message/rfc822':return
        if part.is_multipart():
            for child in part.iter_parts():yield from body_parts(child)
        elif part.get_content_type() in {'text/html','text/plain'}:yield part
    for part in body_parts(message):
        if part.defects:raise ValueError('Malformed MIME part')
        try:text=part.get_content()
        except (LookupError,UnicodeError):raise ValueError('Unsupported MIME text encoding') from None
        if not isinstance(text,str):raise ValueError('Invalid MIME text')
        (html if part.get_content_type()=='text/html' else plain).append(text)
    issues=[]
    if html:
        cards,issues,visible=html_cards(' '.join(html))
    else:
        visible=' '.join(plain)
        # The authorized Exclusives sample has MJML markup in its text/plain part.
        if re.search(r'<mj-(?:section|wrapper|column)\b',visible,re.I):
            cards,issues,visible=html_cards(visible)
        else:cards=text_cards(visible)
    subject=str(message.get('Subject',''))
    if profile=='saved_search':
        if not expected_search or flatten(expected_search).casefold() not in flatten(subject+' '+visible).casefold():
            raise ValueError('Saved search does not match the authorized integration')
    elif profile=='exclusive_alert':
        if (flatten(subject).casefold()!=EXCLUSIVES_SUBJECT.casefold() or
                "you've got exclusive access" not in visible.casefold() or
                any(card.alert_kind!='exclusive' for card in cards)):
            raise ValueError('Message does not match the explicitly selected Exclusives profile')
        expected_search=None  # Do not invent a saved-search association for a broadcast.
    else:raise ValueError('Unknown alert profile')
    if not cards:
        # Unknown templates are never mistaken for a successful empty alert.
        issues.append('no_parseable_cards_template_review_required')
    by_key={};conflicts=set()
    for card in cards:
        old=by_key.get(card.key)
        if old:
            fields=('price','bedrooms','bathrooms','square_footage')
            if any(getattr(old,k)!=getattr(card,k) for k in fields):conflicts.add(card.key)
        else:by_key[card.key]=card
    for key in conflicts:by_key.pop(key);issues.append('conflicting_duplicate_cards')
    try:
        sent=parsedate_to_datetime(str(message.get('Date','')))
        sent_at=sent.isoformat() if sent.tzinfo else None
    except (ValueError,TypeError,OverflowError):sent_at=None
    if not sent_at:issues.append('missing_or_invalid_sent_timestamp')
    message_id=str(message.get('Message-ID','')).strip()
    material=message_id.encode() if message_id else raw
    key='email:'+hashlib.sha256(material).hexdigest()
    return ParsedEmail(key,sent_at,parseaddr(str(message.get('From','')))[1].lower(),expected_search,
                       list(by_key.values()),sorted(set(issues)),alert_profile=profile)


def main():
    p=argparse.ArgumentParser(description='Offline .eml parser; prints redacted preview; no DB or network')
    p.add_argument('eml',type=Path)
    p.add_argument('--profile',choices=['saved_search','exclusive_alert'],default='saved_search')
    args=p.parse_args()
    try:result=parse_email(args.eml.read_bytes(),profile=args.profile)
    except (ValueError,OSError):raise SystemExit('Unable to parse sample; template or MIME review needed') from None
    print(json.dumps(result.public(),indent=2,ensure_ascii=False))

if __name__=='__main__':main()
