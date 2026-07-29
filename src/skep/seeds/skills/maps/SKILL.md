---
name: maps
description: geocoding, routing, and place lookup via OpenStreetMap public APIs
---

# Maps — OSM geocoding and routing

Tools: allow_fetch_domain, read_url, search_web

All reads, all public, no keys. One-time grants the seed names:
`allow_fetch_domain nominatim.openstreetmap.org` (geocoding) and
`allow_fetch_domain router.project-osrm.org` (routing).

1. Geocode: `read_url` on
   `https://nominatim.openstreetmap.org/search?q=<place>&format=json&limit=3`
   — take lat/lon from the top hit, tell the user which match you
   used when the name was ambiguous.
2. Reverse: `/reverse?lat=..&lon=..&format=json`.
3. Route: OSRM
   `https://router.project-osrm.org/route/v1/driving/<lon1>,<lat1>;<lon2>,<lat2>?overview=false`
   — note OSRM wants lon,lat order (the classic bug); report distance
   and duration from the JSON.
4. Be polite: these are volunteer-run public instances — one request
   per question, never a bulk scrape loop. Bulk geocoding is a job for
   a local instance, honestly out of scope here.
