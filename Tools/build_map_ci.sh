#!/bin/bash
# Country-scale topo map build (GitHub Actions, Ubuntu). Local pilot builds use build_map_region.sh.
#   Tools/build_map_ci.sh WEST,SOUTH,EAST,NORTH OUT_DIR
# Env: BASEMAP_MAXZOOM (default 14), PROTOMAPS_BUILD (default: latest daily build), BUILD_CONTOURS (default 1)
# Needs on PATH: pmtiles, gdalbuildvrt, gdal_contour, tippecanoe; python venv at .venv with Tools deps.
set -euo pipefail
BBOX=$1; OUT=$2
MAXZ=${BASEMAP_MAXZOOM:-14}
WORK=${WORK_DIR:-work}
mkdir -p "$OUT" "$WORK/dem"
BUILD=${PROTOMAPS_BUILD:-$(curl -sS https://build-metadata.protomaps.dev/builds.json | python3 -c "import json,sys; print(json.load(sys.stdin)[-1]['key'])")}

echo "::group::basemap ($BUILD, z0-$MAXZ)"
pmtiles extract "https://build.protomaps.com/$BUILD" "$OUT/basemap.pmtiles" --bbox="$BBOX" --maxzoom="$MAXZ"
echo "::endgroup::"

echo "::group::DEM (Copernicus GLO-30)"
IFS=, read -r W S E N <<< "$BBOX"
for lat in $(seq "${S%.*}" "${N%.*}"); do
  for lon in $(seq "${W%.*}" "${E%.*}"); do
    name=$(printf "Copernicus_DSM_COG_10_N%02d_00_E%03d_00_DEM" "$lat" "$lon")
    [ -f "$WORK/dem/$name.tif" ] && continue
    code=$(curl -sS -o "$WORK/dem/$name.tif" -w "%{http_code}" "https://copernicus-dem-30m.s3.amazonaws.com/$name/$name.tif")
    [ "$code" = 200 ] || rm -f "$WORK/dem/$name.tif"   # open sea has no tile
  done
done
ls "$WORK/dem" | wc -l
gdalbuildvrt -q "$WORK/dem.vrt" "$WORK"/dem/*.tif
echo "::endgroup::"

echo "::group::terrain (terrarium z8-12)"
.venv/bin/python -I Tools/build_terrain.py --terrain-only --bbox "$BBOX" --out "$WORK" "$WORK"/dem/*.tif
pmtiles convert "$WORK/terrain.mbtiles" "$OUT/terrain.pmtiles"
rm "$WORK/terrain.mbtiles"
echo "::endgroup::"

# 20 m contours (100 m index lines are styled in the app). BUILD_CONTOURS=0 skips them.
if [ "${BUILD_CONTOURS:-1}" = 1 ]; then
echo "::group::contours (20 m, z12-15)"
gdal_contour -q -a ele -i 20 -f GeoJSONSeq "$WORK/dem.vrt" "$WORK/contours.geojsons"
tippecanoe -q -o "$OUT/contours.pmtiles" -l contours -Z12 -z15 --force \
  -y ele --simplification=4 --drop-densest-as-needed --extend-zooms-if-still-dropping \
  --attribution="Contours from Copernicus DEM GLO-30 © DLR e.V. 2010-2014 and © Airbus 2014-2018" \
  "$WORK/contours.geojsons"
rm "$WORK/contours.geojsons"
echo "::endgroup::"
fi

ls -la "$OUT"
