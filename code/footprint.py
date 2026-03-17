import geopandas as gpd

gdf = gpd.read_file(r"F:\yaogan\oir\OIR.geojson")

minx, miny, maxx, maxy = gdf.total_bounds
bbox = [minx, miny, maxx, maxy]

print(bbox)

from pystac_client import Client
import requests
import json
from pathlib import Path


out_dir = Path("stac_output")
out_dir.mkdir(exist_ok=True)


catalog = Client.open("https://stac.dataspace.copernicus.eu/v1/")

search = catalog.search(
    collections=["sentinel-2-l2a"],
    bbox=[-77.47444736990289, 43.23884076728718, -77.4005126300971, 43.29284716889311],  # 你的 ROI bbox
    datetime="2023-08-01/2023-08-30",
    limit=5
)

items = list(search.items())

if not items:
    print("no result")
    raise SystemExit

for i, item in enumerate(items, 1):
    print(f"\nScene {i}: {item.id}")


    footprint = item.geometry
    footprint_path = out_dir / f"{item.id}_footprint.geojson"
    with open(footprint_path, "w", encoding="utf-8") as f:
        json.dump(footprint, f, ensure_ascii=False, indent=2)
    print("save footprint:", footprint_path)


    asset_keys = list(item.assets.keys())
    print("assets keys:", asset_keys)


    quicklook_asset = None
    preferred_keys = ["thumbnail", "quicklook", "overview", "preview"]

    for key in preferred_keys:
        if key in item.assets:
            quicklook_asset = item.assets[key]
            break

    if quicklook_asset is None:
        for key, asset in item.assets.items():
            key_lower = key.lower()
            href_lower = (asset.href or "").lower()
            if any(x in key_lower for x in ["thumb", "quick", "preview", "overview"]) or \
               any(x in href_lower for x in ["thumb", "quick", "preview", "overview"]):
                quicklook_asset = asset
                break

    if quicklook_asset is None:
        print("no quicklook / thumbnail")
        continue

    quicklook_url = quicklook_asset.href
    print("quicklook url:", quicklook_url)


    ext = Path(quicklook_url).suffix or ".jpg"
    quicklook_path = out_dir / f"{item.id}_quicklook{ext}"

    resp = requests.get(quicklook_url, timeout=60)
    resp.raise_for_status()

    with open(quicklook_path, "wb") as f:
        f.write(resp.content)

    print("save quicklook:", quicklook_path)