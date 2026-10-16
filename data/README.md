# Official city geometry

Run `python scripts/fetch_boundary.py` once during the Render build. This downloads the official
CityBoundary layer linked from https://data.wprdc.org/dataset/pittsburgh-city-boundary,
requests WGS84 longitude/latitude, validates polygon topology and downtown/Bellevue test points,
and writes a SHA256 provenance manifest. Production refuses an absent or mismatched manifest.
No simplified rectangle or ZIP boundary is used as a substitute. The generated files are ignored
by Git because the build retrieves the official source. Tests use clearly fictional polygons only.
The upstream public GIS service must be reachable during the deployment build; no RentCast calls
are performed by this script. Review the source's use terms before distributing the geometry.
