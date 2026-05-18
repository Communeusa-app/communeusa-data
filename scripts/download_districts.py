"""
Download Census TIGER/Line district shapefiles, convert to simplified GeoJSON.

Sources:
  Congressional districts (119th Congress, 2024):
    https://www2.census.gov/geo/tiger/TIGER2024/CD/tl_2024_us_cd119.zip
  WA State House (SLDL):
    https://www2.census.gov/geo/tiger/TIGER2024/SLDL/tl_2024_53_sldl.zip
  WA State Senate (SLDU):
    https://www2.census.gov/geo/tiger/TIGER2024/SLDU/tl_2024_53_sldu.zip

Output:
  output/districts/congressional.geojson
  output/districts/house_districts_wa.geojson
  output/districts/senate_districts_wa.geojson

Usage:
    pip install geopandas requests
    python3 scripts/download_districts.py
"""

import io
import zipfile
from pathlib import Path

import geopandas as gpd
import requests

# ── Config ─────────────────────────────────────────────────────────────────────

SOURCES = [
    {
        "url": "https://www2.census.gov/geo/tiger/TIGER2024/CD/tl_2024_53_cd119.zip",
        "shapefile": "tl_2024_53_cd119.shp",
        "output": "congressional.geojson",
        "filter": None,
        # Simplify tolerance in degrees (~1 km at mid-latitudes)
        "tolerance": 0.01,
    },
    {
        "url": "https://www2.census.gov/geo/tiger/TIGER2024/SLDL/tl_2024_53_sldl.zip",
        "shapefile": "tl_2024_53_sldl.shp",
        "output": "house_districts_wa.geojson",
        "filter": None,
        "tolerance": 0.005,
    },
    {
        "url": "https://www2.census.gov/geo/tiger/TIGER2024/SLDU/tl_2024_53_sldu.zip",
        "shapefile": "tl_2024_53_sldu.shp",
        "output": "senate_districts_wa.geojson",
        "filter": None,
        "tolerance": 0.005,
    },
]

OUT_DIR = Path(__file__).parent.parent / "output" / "districts"

# ── Helpers ────────────────────────────────────────────────────────────────────

def download_zip(url: str) -> bytes:
    print(f"  Downloading {url.split('/')[-1]} …", end=" ", flush=True)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    print(f"{len(resp.content) / 1_048_576:.1f} MB")
    return resp.content

def load_shapefile(zip_bytes: bytes, shapefile_name: str) -> gpd.GeoDataFrame:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Write all zip members to a temporary in-memory dir via a temp path
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)
            shp_path = Path(tmp) / shapefile_name
            gdf = gpd.read_file(shp_path)
    return gdf

def process(source: dict) -> None:
    print(f"\n[{source['output']}]")
    raw = download_zip(source["url"])
    gdf = load_shapefile(raw, source["shapefile"])
    print(f"  Loaded {len(gdf)} features, CRS: {gdf.crs}")

    # Filter to WA if this is the national congressional file
    if source["filter"]:
        col, val = source["filter"]
        gdf = gdf[gdf[col] == val].copy()
        print(f"  Filtered to {len(gdf)} WA features")

    # Reproject to WGS-84 for GeoJSON output
    gdf = gdf.to_crs(epsg=4326)

    # Simplify geometry (preserve_topology avoids sliver polygons)
    gdf["geometry"] = gdf["geometry"].simplify(
        source["tolerance"], preserve_topology=True
    )

    out_path = OUT_DIR / source["output"]
    gdf.to_file(out_path, driver="GeoJSON")
    size_kb = out_path.stat().st_size / 1024
    print(f"  Saved → {out_path.relative_to(Path(__file__).parent.parent)}  ({size_kb:.0f} KB)")

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for source in SOURCES:
        process(source)
    print("\nDone.")

if __name__ == "__main__":
    main()
