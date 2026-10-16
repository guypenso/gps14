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
Downtown/Oakland inclusion and Bellevue/Green Tree/Wilkinsburg exclusion are
checked against the bundled geometry in `tests/test_official_boundary.py`.

To deliberately refresh from the official source, run `python scripts/fetch_boundary.py`
and review the resulting geometry and manifest before committing. Never replace
this file with a ZIP, county, neighborhood, approximate box, or test polygon.
