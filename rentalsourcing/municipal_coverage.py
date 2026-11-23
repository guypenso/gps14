"""Coverage evidence for the four approved municipalities, independent of eligibility."""
import datetime as dt
import json
import math

from shapely import minimum_bounding_circle
from shapely.affinity import scale
from shapely.geometry import Point
from shapely.prepared import prep

from .collector import LIVE
from .core import EXCLUDED_ZIPS, number

AREAS = {
    'pittsburgh': 'Pittsburgh',
    'bellevue': 'Bellevue Borough',
    'bethel-park': 'Bethel Park Municipality',
    'pleasant-hills': 'Pleasant Hills Borough',
}
VERSION = 'all-approved-municipalities-v1'
UNIFIED = 'all-approved-municipalities'
PACING_POLICY = 'unified-geography-paced-v1'
MAX_TRAVERSAL_AGE = dt.timedelta(days=7)


def overlap_size(page_size):
    return page_size // 20


def traversal_expired(state, now):
    if not state.get('offset') or state.get('query_complete'):
        return False
    try:
        started = dt.datetime.fromisoformat(state.get('generation_started_at', '').replace('Z', '+00:00'))
        return now - started > MAX_TRAVERSAL_AGE
    except (ValueError, TypeError, AttributeError):
        return False


def pages_for_total(total, page_size):
    if not isinstance(total, int) or total < 0:
        return None
    return 1 + math.ceil(max(0, total - page_size) / (page_size - overlap_size(page_size)))



def geometries(boundary):
    if not hasattr(boundary, 'city'):
        return {}
    regions = {'pittsburgh': boundary.city, 'bellevue': boundary.bellevue}
    regions.update({slug: boundary.additional_municipalities[name]
                    for slug, name in AREAS.items() if name in boundary.additional_municipalities})
    return {slug: region.geometry for slug, region in regions.items()}


def miles(longitude, latitude, other_longitude, other_latitude):
    first, second = math.radians(latitude), math.radians(other_latitude)
    dx = math.radians(other_longitude - longitude)
    arc = math.sin((second - first) / 2) ** 2 + math.cos(first) * math.cos(second) * math.sin(dx / 2) ** 2
    return 2 * 3958.7613 * math.asin(min(1, math.sqrt(arc)))


def unified_query(boundary):
    """Build a compact bounding circle from the official, checksummed polygons."""
    from .request import PARAMS
    shapes = geometries(boundary)
    if shapes.keys() != AREAS.keys() or any(g.is_empty for g in shapes.values()):
        raise ValueError('All four official municipal boundaries are required')
    hull = boundary.geometry.convex_hull
    origin = (hull.centroid.x, hull.centroid.y)
    longitude_scale = math.cos(math.radians(origin[1]))
    envelope = minimum_bounding_circle(scale(hull, xfact=longitude_scale, origin=origin))
    center = scale(envelope.centroid, xfact=1 / longitude_scale, origin=origin)
    lat, lon = round(center.y, 7), round(center.x, 7)
    # GEOS finds the center in a local planar projection. Size it from actual
    # great-circle distances to original boundary vertices with a safety margin.
    extents = {slug: max(miles(lon, lat, x, y) for x, y in shape.convex_hull.exterior.coords)
               for slug, shape in shapes.items()}
    radius = math.ceil((max(extents.values()) + 0.05) * 10) / 10
    if not 0 < radius <= 100:
        raise ValueError('All-area circle exceeds the provider radius limit')
    params = {key: value for key, value in PARAMS.items() if key != 'city'}
    return dict(params, latitude=lat, longitude=lon, radius=radius), extents


def _area_index(boundary):
    return {slug: prep(geometry) for slug, geometry in geometries(boundary).items()}


def _area(index, row):
    x, y = row.get('longitude'), row.get('latitude')
    if not number(x) or not number(y) or not -180 <= x <= 180 or not -90 <= y <= 90:
        return 'unattributed'
    point = Point(x, y)
    matches = [slug for slug, geometry in index.items() if geometry.contains(point)]
    if len(matches) == 1:
        return matches[0]
    if matches or any(geometry.covers(point) for geometry in index.values()):
        return 'unattributed'
    return 'outside_approved_municipalities' if index.keys() == AREAS.keys() else 'unattributed'


def page_evidence(boundary, rows):
    """Count actual rows by municipal geometry, regardless of eligibility."""
    index = _area_index(boundary)
    counts = dict.fromkeys((*AREAS, 'outside_approved_municipalities', 'unattributed'), 0)
    for row in rows:
        counts[_area(index, row) if isinstance(row, dict) else 'unattributed'] += 1
    return counts


def stored_inventory(store, boundary, now, queries=None):
    """Inspect actual stored evidence by area without contacting listing sources."""
    index = _area_index(boundary)
    inventory = {
        slug: {source: {'records': 0, 'geography_eligible': 0, 'most_recent_observation': None}
               for source in ('rentcast', 'realscout')}
        for slug in AREAS
    }
    matches = dict.fromkeys(queries or {}, 0)
    if not index:
        return inventory, matches

    def observe(row, observed_at, source):
        slug = _area(index, row)
        if slug not in inventory:
            return
        area = inventory[slug][source]
        area['records'] += 1
        if (str(row.get('zipCode') or '').split('-')[0] not in EXCLUDED_ZIPS
                and boundary.evaluate(row)[0] == 'Candidate'):
            area['geography_eligible'] += 1
        try:
            timestamp = dt.datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
            if timestamp.tzinfo is None or timestamp > now:
                return
        except (ValueError, AttributeError, TypeError):
            return
        stamp = timestamp.astimezone(dt.timezone.utc).isoformat()
        if area['most_recent_observation'] is None or stamp > area['most_recent_observation']:
            area['most_recent_observation'] = stamp

    # Read only the handful of fields this audit uses, not every listing's
    # description, history or contact payload.
    fields = ('latitude', 'longitude', 'zipCode', 'status', 'price', 'bedrooms', 'bathrooms', 'squareFootage')
    columns = fields if queries else fields[:4]
    selected = ', '.join(f"payload::jsonb->'{key}'" if store.pg else f"json_extract(payload,'$.{key}')"
                         for key in columns)
    for observed_at, *values in store.execute(f'SELECT observed_at,{selected} FROM properties WHERE source=?', (LIVE,)):
        row = dict(zip(fields, values))
        observe(row, observed_at, 'rentcast')
        if not queries or str(row['status']).lower() != 'active':
            continue
        lat, lon, price, beds, baths, sqft = (row[k] for k in ('latitude', 'longitude', 'price', 'bedrooms', 'bathrooms', 'squareFootage'))
        if (all(number(v) for v in (lat, lon, price, beds, baths, sqft))
                and -90 <= lat <= 90 and -180 <= lon <= 180
                and 0 < price <= 200001 and 2 <= beds <= 4 and baths >= 1 and sqft >= 1000):
            for scope, query in queries.items():
                if miles(query['longitude'], query['latitude'], lon, lat) <= query['radius']:
                    matches[scope] += 1
    for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?', ('listing_identity:%',)):
        saved = json.loads(raw)
        if not saved.get('canonical_source_id') and isinstance(saved.get('property'), dict):
            observe(saved['property'], saved.get('observed_at'), 'realscout')
    return inventory, matches


def comparison(store, boundary, now, partition_queries):
    query, distances = unified_query(boundary)
    inventory, sample = stored_inventory(store, boundary, now, {UNIFIED: query, **partition_queries})
    return {
        'version': VERSION,
        'query': query,
        'target_areas': list(AREAS),
        'boundary_max_miles': {area: round(distance, 4) for area, distance in distances.items()},
        'stored_inventory': inventory,
        'matching_saved_active_listings': sample,
        'sample_scope': 'Stored RentCast snapshots matching the search fields, including older records; not live totals.',
        'provider_total': None,
        'market_completeness': 'not_verified',
        'provider_calls': 0,
    }


def window_evidence(store, boundary, stats, now):
    """Record what each municipality actually received in this scheduled slot."""
    index = geometries(boundary)
    inventory, _ = stored_inventory(store, boundary, now)
    source = stats.get('free_source') or {}
    shared = stats.get('partitions', {}).get(UNIFIED, {})
    result = {}
    for area, label in AREAS.items():
        pages = [p for p in stats.get('pages', []) if area in (p.get('area_observations') or {})]
        fresh = [p for p in pages if p.get('requests')]
        count = sum(p['area_observations'][area] for p in fresh)
        # Search geometry includes all municipalities. Offset paging only
        # confirms positive sightings for places represented on returned pages.
        state = (('fresh_observations' if count else 'queried_no_observations_on_returned_pages') if fresh
                 else 'cached_page_only' if pages else
                 shared.get('state') or stats.get('budget_reason') or stats.get('discovery_state', 'not_queried'))
        result[area] = {
            'name': label,
            'boundary_verified': area in index,
            'stored_inventory_checked_at': now.isoformat(),
            'rentcast': {
                'state': state,
                'fresh_rows_returned': count,
                'cached_page_rows': sum(p['area_observations'][area] for p in pages if not p.get('requests')),
                'latest_fresh_observation': max((p.get('fetched_at') for p in fresh
                                                 if p['area_observations'][area] and p.get('fetched_at')), default=None),
                **inventory[area]['rentcast'],
            },
            'realscout': {
                'state': ('mailbox_checked_search_scope_unverified' if source.get('mailbox_checked') else
                          'mailbox_blocked' if source.get('blocked') else 'mailbox_check_unverified'),
                'blocked': source.get('blocked', []),
                **inventory[area]['realscout'],
            },
            'public_listings': {
                'source': 'Pittsburgh Land Bank' if area == 'pittsburgh' else None,
                'state': (source.get('public_inventory_state') or 'check_unverified') if area == 'pittsburgh'
                          else 'no_verified_public_feed_for_area',
                'last_checked_at': source.get('public_checked_at') if area == 'pittsburgh' else None,
            },
            'market_completeness': 'not_verified',
        }
    return result
