# Verified Pittsburgh municipal boundary

`pittsburgh.geojson` is the complete eight-feature City of Pittsburgh boundary,
downloaded from the official city ArcGIS layer linked by WPRDC on 2026-10-16.
It is WGS84 (EPSG:4326); adjacent features are unioned before point membership.
Source and exact query URL, retrieval time, and SHA256 are recorded in
`pittsburgh.source.json`. The production loader verifies this manifest and digest.

Catalog: https://data.wprdc.org/dataset/pittsburgh-city-boundary
Layer: https://services1.arcgis.com/YZCmUqbcsUpOKfj7/arcgis/rest/services/CityBoundary/FeatureServer/0

The verified files are bundled so deployment and scans do not depend on GIS
network availability. Render validates them offline at build time. A missing or
modified file fails closed. Border points and absent coordinates are excluded.
Downtown/Oakland inclusion and neighboring municipality exclusion are
checked against the original city geometry in `tests/test_official_boundary.py`.

To deliberately refresh from the official source, run `python scripts/fetch_boundary.py`
and review the resulting geometry and manifest before committing. Never replace
this file with a ZIP, county, neighborhood, approximate box, or test polygon.


## Approved geographic extension (2026-11-01)

The eligibility loader now combines the original city polygon with Bellevue Borough
from Allegheny County GIS, and restricts city properties mailed to ZIP 15212 to
the official Brighton Heights neighborhood polygon. Bellevue membership takes
precedence over the postal 15212 restriction, but all four excluded ZIPs still fail.
No ZIP or mailing city establishes geographic inclusion. Strict interior membership
is required; missing coordinates never enter future emails or sync snapshots.

`bellevue.geojson` and `brighton-heights.geojson` are complete, unsimplified WGS84
responses from the official county/city endpoints. Their `.source.json` manifests
record source, exact query, retrieval time and raw SHA256. They were acquired
through the existing GitHub CI because this workspace cannot contact GIS hosts.
CI artifact 13083776242 / run 41306877446 supplied these four files; artifact
SHA256: 2bbc550a37f494418869e9943813b514d94b559c615933fa7967376e46497119.
Public GIS labels may be uppercase; identity comparison is case-insensitive,
without accepting other municipality/neighborhood names. Missing or modified
polygons fail closed at build/startup. Production performs no GIS fetch.

The single daily listing request uses a documented RentCast geographic-circle
query encompassing the complete city and Bellevue polygons, then clips exactly
to eligible geography. Current center: 40.4349024, -79.9805549; radius: 7.9 miles.
This transport envelope can return neighboring municipalities, which are rejected
before reporting. No additional listing request, pagination, retry, quota, or
schedule change is introduced. Existing first-page limits can truncate coverage;
no new real-data coverage claim is made.

Refresh deliberately with `python scripts/fetch_regions.py`, review source/checksum
and geometry changes, and commit only the verified public assets.
