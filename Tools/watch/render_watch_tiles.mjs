// Renders the Hikit topo style into raster XYZ tiles for the Apple Watch (512 px JPEG, z13–z15),
// land only. The watch downloads these directly, so the phone never has to render maps.
//
// usage: node render_watch_tiles.mjs <style.json> <basemap> <terrain> <contours> <outDir>
//          [--bbox w,s,e,n] [--shard i/n] [--quality 70]
// Each archive is a local .pmtiles path or an https URL (read with range requests).
// Output: <outDir>/<z>/<x>/<y>.jpg and <outDir>/tiles-<shard>.txt (list of written tiles).
//
// Work is grouped by z13 tile: one render per zoom covers that z13 tile plus a margin (so labels
// at the edges are not clipped), then the 512 px child tiles are cut out of it.
import mbgl from "@maplibre/maplibre-gl-native";
import { PMTiles, FetchSource } from "pmtiles";
import { VectorTile } from "@mapbox/vector-tile";
import Pbf from "pbf";
import sharp from "sharp";
import fs from "fs";
import path from "path";

const args = process.argv.slice(2);
const [styleFile, basemapSrc, terrainSrc, contoursSrc, outDir] = args;
const opt = (name, def) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : def; };
const bbox = opt("bbox", "124.5,33.0,131.0,38.7").split(",").map(Number);
const [shard, shards] = opt("shard", "0/1").split("/").map(Number);
const quality = Number(opt("quality", "70"));

class FileSource {
  constructor(p) { this.p = p; this.fd = fs.openSync(p, "r"); }
  getKey() { return this.p; }
  async getBytes(offset, length) {
    const buf = Buffer.alloc(length);
    fs.readSync(this.fd, buf, 0, length, offset);
    return { data: buf.buffer.slice(buf.byteOffset, buf.byteOffset + length) };
  }
}
const open = src => new PMTiles(/^https?:/.test(src) ? new FetchSource(src) : new FileSource(src));
const archives = { basemap: open(basemapSrc), terrain: open(terrainSrc), contours: open(contoursSrc) };

// ---- Tile math (XYZ, Web Mercator)
const lon2x = (lon, z) => ((lon + 180) / 360) * 2 ** z;
const lat2y = (lat, z) => { const r = (lat * Math.PI) / 180; return ((1 - Math.asinh(Math.tan(r)) / Math.PI) / 2) * 2 ** z; };
const x2lon = (x, z) => (x / 2 ** z) * 360 - 180;
const y2lat = (y, z) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / 2 ** z))) * 180) / Math.PI;

// ---- Land mask from the basemap's "earth" layer at z13
function inside(rings, px, py) {
  let c = false;
  for (const ring of rings) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const a = ring[i], b = ring[j];
      if ((a.y > py) !== (b.y > py) && px < ((b.x - a.x) * (py - a.y)) / (b.y - a.y) + a.x) c = !c;
    }
  }
  return c;
}

/// Which 4x4 sub-cells (z15 children) of a z13 tile contain land. null = no land at all.
async function landCells(x, y) {
  const t = await archives.basemap.getZxy(13, x, y);
  if (!t) return null;
  const vt = new VectorTile(new Pbf(new Uint8Array(t.data)));
  const earth = vt.layers.earth;
  if (!earth || earth.length === 0) return null;
  const extent = earth.extent;
  const polys = [];
  for (let i = 0; i < earth.length; i++) {
    const f = earth.feature(i);
    if (f.type === 3) polys.push(f.loadGeometry());
  }
  const cells = new Set();
  const step = extent / 4;
  for (let cy = 0; cy < 4; cy++) {
    for (let cx = 0; cx < 4; cx++) {
      const x0 = cx * step, y0 = cy * step;
      let land = false;
      // Any polygon vertex in the cell (small islands), or any sample point on land.
      for (const rings of polys) {
        if (rings.some(r => r.some(p => p.x >= x0 && p.x < x0 + step && p.y >= y0 && p.y < y0 + step))) { land = true; break; }
      }
      for (let sy = 0; sy < 8 && !land; sy++) {
        for (let sx = 0; sx < 8 && !land; sx++) {
          const px = x0 + ((sx + 0.5) * step) / 8, py = y0 + ((sy + 0.5) * step) / 8;
          if (polys.some(rings => inside(rings, px, py))) land = true;
        }
      }
      if (land) cells.add(cy * 4 + cx);
    }
  }
  return cells.size ? cells : null;
}

// ---- Rendering
const style = JSON.parse(fs.readFileSync(styleFile, "utf8"));
style.sources.basemap = { type: "vector", tiles: ["hikit://basemap/{z}/{x}/{y}"], minzoom: 0, maxzoom: 15 };
style.sources.terrain = { type: "raster-dem", tiles: ["hikit://terrain/{z}/{x}/{y}"], encoding: "terrarium", tileSize: 256, minzoom: 8, maxzoom: 12 };
style.sources.contours = { type: "vector", tiles: ["hikit://contours/{z}/{x}/{y}"], minzoom: 12, maxzoom: 15 };

// The style's glyph server has no Hangul (the iPhone draws Korean with a system font instead),
// so labels come from OpenFreeMap's Noto Sans build, which includes CJK. Cached per run.
const FONT_ALIAS = { "Noto Sans Medium": "Noto Sans Bold" };
const glyphCache = new Map();
async function glyphs(url) {
  const m = url.match(/\/fonts\/([^/]+)\/(\d+-\d+)\.pbf/);
  if (!m) return null;
  const stack = decodeURIComponent(m[1]).split(",")[0];
  const key = `${stack}/${m[2]}`;
  if (glyphCache.has(key)) return glyphCache.get(key);
  let data = null;
  for (const u of [`https://tiles.openfreemap.org/fonts/${encodeURIComponent(FONT_ALIAS[stack] || stack)}/${m[2]}.pbf`, url]) {
    const r = await fetch(u).catch(() => null);
    if (r && r.ok) { data = Buffer.from(await r.arrayBuffer()); break; }
  }
  glyphCache.set(key, data);
  return data;
}

async function request(req, cb) {
  try {
    const m = req.url.match(/^hikit:\/\/(\w+)\/(\d+)\/(\d+)\/(\d+)/);
    if (m) {
      const t = await archives[m[1]].getZxy(+m[2], +m[3], +m[4]);
      return cb(null, t ? { data: Buffer.from(t.data) } : {});
    }
    if (req.url.includes("/fonts/")) {
      const g = await glyphs(req.url);
      return cb(null, g ? { data: g } : {});
    }
    const r = await fetch(req.url);
    if (!r.ok) return cb(null, {});
    cb(null, { data: Buffer.from(await r.arrayBuffer()) });
  } catch (e) { cb(e); }
}

const map = new mbgl.Map({ request, ratio: 2 });
map.load(style);
const render = o => new Promise((res, rej) => map.render(o, (e, b) => (e ? rej(e) : res(b))));

const MARGIN = 128; // points around each render, cropped away
async function renderZ13Block(x, y, cells) {
  const written = [];
  // z = target zoom; n = children per side inside the z13 tile.
  for (const z of [13, 14, 15]) {
    const n = 2 ** (z - 13);
    const side = 256 * n; // points covering the z13 tile at this zoom
    const W = side + 2 * MARGIN;
    const cxTile = x * n + n / 2, cyTile = y * n + n / 2;
    const center = [x2lon(cxTile, z), y2lat(cyTile, z)];
    const buf = await render({ zoom: z - 1, center, width: W, height: W });
    const img = sharp(buf, { raw: { width: W * 2, height: W * 2, channels: 4 } });
    for (let j = 0; j < n; j++) {
      for (let i = 0; i < n; i++) {
        // Land test in 4x4 z15 cells: a child at this zoom covers 4/n of them per side.
        const per = 4 / n;
        let land = false;
        for (let cy = j * per; cy < (j + 1) * per && !land; cy++)
          for (let cx = i * per; cx < (i + 1) * per && !land; cx++) land = cells.has(cy * 4 + cx);
        if (!land) continue;
        const tx = x * n + i, ty = y * n + j;
        const dir = path.join(outDir, `${z}`, `${tx}`);
        fs.mkdirSync(dir, { recursive: true });
        await img.clone()
          .extract({ left: (MARGIN + i * 256) * 2, top: (MARGIN + j * 256) * 2, width: 512, height: 512 })
          .removeAlpha()
          .jpeg({ quality, mozjpeg: true })
          .toFile(path.join(dir, `${ty}.jpg`));
        written.push(`${z}/${tx}/${ty}`);
      }
    }
  }
  return written;
}

// ---- Main
const [w, s, e, n] = bbox;
const x0 = Math.floor(lon2x(w, 13)), x1 = Math.floor(lon2x(e, 13));
const y0 = Math.floor(lat2y(n, 13)), y1 = Math.floor(lat2y(s, 13));
const jobs = [];
for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) jobs.push([x, y]);
const mine = jobs.filter((_, i) => i % shards === shard);
fs.mkdirSync(outDir, { recursive: true });
const list = fs.createWriteStream(path.join(outDir, `tiles-${shard}.txt`));
let done = 0, tiles = 0, land = 0;
const t0 = Date.now();
for (const [x, y] of mine) {
  const cells = await landCells(x, y);
  if (cells) {
    land++;
    const written = await renderZ13Block(x, y, cells);
    tiles += written.length;
    for (const t of written) list.write(t + "\n");
  }
  done++;
  if (done % 50 === 0 || done === mine.length) {
    const sec = (Date.now() - t0) / 1000;
    console.log(`shard ${shard}/${shards}: ${done}/${mine.length} z13 blocks (${land} land), ${tiles} tiles, ${sec.toFixed(0)} s`);
  }
}
list.end();
map.release();
console.log(`done: ${tiles} tiles`);
