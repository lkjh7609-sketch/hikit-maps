# hikit-maps

Builds the topo map files used by the Hikit app (South Korea) and uploads them to Cloudflare R2.

- `basemap.pmtiles` — vector basemap extracted from the Protomaps daily build (OpenStreetMap)
- `terrain.pmtiles` — hillshade source (terrarium raster-dem, z8–12) from Copernicus DEM GLO-30
- `contours.pmtiles` — 20 m contours (z12–15) from Copernicus DEM GLO-30

Runs monthly (GitHub Actions) or on demand: Actions → Build topo maps → Run workflow.

Required secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ACCOUNT_ID`. Optional variable: `R2_BUCKET` (default `hikit-maps`).

## Attribution

- © OpenStreetMap contributors (ODbL), Protomaps
- Copernicus DEM GLO-30 © DLR e.V. 2010–2014 and © Airbus 2014–2018, provided under COPERNICUS by the European Union and ESA
