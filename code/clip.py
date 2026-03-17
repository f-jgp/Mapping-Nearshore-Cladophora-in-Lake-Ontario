import os, glob
import rasterio
import geopandas as gpd
from rasterio.windows import from_bounds

roi_path = r"F:\yaogan\oir\OIR.geojson" 
jp2_dir  = r"F:\yaogan\oir\0816_clip\R20m"                                        
out_dir  = r"F:\yaogan\oir\0816_clip\clip"                               
os.makedirs(out_dir, exist_ok=True)

roi = gpd.read_file(roi_path)

jp2_files = sorted(glob.glob(os.path.join(jp2_dir, "*.jp2")))


for jp2 in jp2_files:
    with rasterio.open(jp2) as src:
        roi_proj = roi.to_crs(src.crs)

        minx, miny, maxx, maxy = roi_proj.total_bounds

        win = from_bounds(minx, miny, maxx, maxy, transform=src.transform)
        win = win.round_offsets().round_lengths()

        data = src.read(window=win) 
        out_transform = src.window_transform(win)

        meta = src.meta.copy()
        meta.update(
            driver="GTiff",
            height=data.shape[1],
            width=data.shape[2],
            transform=out_transform,
            compress="deflate"
        )

        base = os.path.splitext(os.path.basename(jp2))[0]
        out_path = os.path.join(out_dir, f"{base}_clip.tif")

        with rasterio.open(out_path, "w", **meta) as dst:
            dst.write(data)

        print("Saved:", out_path)