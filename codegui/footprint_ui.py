import json
import threading
from pathlib import Path
from typing import Iterable, Tuple, Optional

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from pystac_client import Client

# STAC source; edit constants here to tune.
STAC_CATALOG_URL = "https://stac.dataspace.copernicus.eu/v1/"
COLLECTION = "sentinel-2-l2a"


def iter_positions(geom: dict) -> Iterable[Tuple[float, float]]:
    geom_type = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")

    if geom_type == "Point":
        x, y = coords
        yield float(x), float(y)
        return

    if geom_type in ("MultiPoint", "LineString"):
        for x, y in coords:
            yield float(x), float(y)
        return

    if geom_type in ("MultiLineString", "Polygon"):
        for ring in coords:
            for x, y in ring:
                yield float(x), float(y)
        return

    if geom_type == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for x, y in ring:
                    yield float(x), float(y)
        return

    if geom_type == "GeometryCollection":
        for g in (geom or {}).get("geometries", []) or []:
            yield from iter_positions(g)
        return

    raise ValueError(f"Unsupported geometry type: {geom_type}")


def bbox_from_geojson(path: Path) -> list[float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    t = data.get("type")

    if t == "FeatureCollection":
        geoms = [
            f.get("geometry")
            for f in (data.get("features") or [])
            if isinstance(f, dict) and f.get("geometry")
        ]
    elif t == "Feature":
        geoms = [data.get("geometry")]
    else:
        geoms = [data]

    xs: list[float] = []
    ys: list[float] = []
    for g in geoms:
        if not g:
            continue
        for x, y in iter_positions(g):
            xs.append(x)
            ys.append(y)

    if not xs:
        raise ValueError("No coordinates found in the selected GeoJSON.")

    return [min(xs), min(ys), max(xs), max(ys)]


def validate_bbox(minx: float, miny: float, maxx: float, maxy: float) -> None:
    if not (minx < maxx and miny < maxy):
        raise ValueError("Invalid bbox. You must have minLon < maxLon and minLat < maxLat.")

    # Heuristic sanity check for lon/lat in WGS84.
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180):
        raise ValueError(
            "Longitude is out of range [-180, 180]. "
            "Your ROI may not be in WGS84 (EPSG:4326)."
        )
    if not (-90 <= miny <= 90 and -90 <= maxy <= 90):
        raise ValueError(
            "Latitude is out of range [-90, 90]. "
            "Your ROI may not be in WGS84 (EPSG:4326)."
        )


def find_quicklook_asset(item) -> Optional[object]:
    preferred_keys = ["thumbnail", "quicklook", "overview", "preview"]
    for k in preferred_keys:
        if k in item.assets:
            return item.assets[k]

    for k, asset in item.assets.items():
        key_lower = (k or "").lower()
        href_lower = ((getattr(asset, "href", None) or "")).lower()
        if any(x in key_lower for x in ["thumb", "quick", "preview", "overview"]) or any(
            x in href_lower for x in ["thumb", "quick", "preview", "overview"]
        ):
            return asset
    return None


def run_job(
    *,
    bbox: list[float],
    start_date: str,
    end_date: str,
    limit: int,
    out_dir: Path,
    log,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog = Client.open(STAC_CATALOG_URL)
    search = catalog.search(
        collections=[COLLECTION],
        bbox=bbox,
        datetime=f"{start_date}/{end_date}",
        limit=limit,
    )
    items = list(search.items())

    if not items:
        log("No results found.")
        return

    for i, item in enumerate(items, 1):
        log(f"Scene {i}: {item.id}")

        # Save footprint (geometry only, same as your original script)
        footprint = item.geometry
        footprint_path = out_dir / f"{item.id}_footprint.geojson"
        footprint_path.write_text(
            json.dumps(footprint, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(f"Saved footprint: {footprint_path}")

        asset_keys = list(item.assets.keys())
        log(f"Asset keys: {asset_keys}")

        quicklook_asset = find_quicklook_asset(item)
        if quicklook_asset is None:
            log("No quicklook/thumbnail asset found. Skipped.")
            continue

        quicklook_url = (quicklook_asset.href or "").strip()
        log(f"Quicklook URL: {quicklook_url}")

        if quicklook_url.lower().startswith("s3://"):
            log("Skip quicklook: s3:// URLs are not supported by requests.")
            continue

        ext = Path(quicklook_url).suffix or ".jpg"
        quicklook_path = out_dir / f"{item.id}_quicklook{ext}"

        resp = requests.get(quicklook_url, timeout=60)
        resp.raise_for_status()
        quicklook_path.write_bytes(resp.content)
        log(f"Saved quicklook: {quicklook_path}")


def make_ui() -> tk.Tk:
    root = tk.Tk()
    root.title("Sentinel-2 Quicklook Downloader")
    root.minsize(820, 520)

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    # Layout: 3 columns (label, input, button)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(9, weight=1)

    geojson_var = tk.StringVar(value="")
    out_var = tk.StringVar(value="")

    minlon_var = tk.StringVar(value="")
    minlat_var = tk.StringVar(value="")
    maxlon_var = tk.StringVar(value="")
    maxlat_var = tk.StringVar(value="")

    start_var = tk.StringVar(value="2023-08-01")
    end_var = tk.StringVar(value="2023-08-10")
    limit_var = tk.StringVar(value="5")

    def add_labeled_entry(row: int, label: str, var: tk.StringVar) -> ttk.Entry:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        e = ttk.Entry(frm, textvariable=var)
        e.grid(row=row, column=1, sticky="ew", pady=4)
        return e

    def add_labeled_path(row: int, label: str, var: tk.StringVar, choose_fn) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Browse...", command=choose_fn).grid(
            row=row, column=2, sticky="ew", padx=(8, 0), pady=4
        )

    def choose_geojson():
        p = filedialog.askopenfilename(
            title="Select ROI GeoJSON",
            filetypes=[("GeoJSON", "*.geojson *.json"), ("All files", "*.*")],
        )
        if not p:
            return
        geojson_var.set(p)

        try:
            bbox = bbox_from_geojson(Path(p))
            minlon_var.set(f"{bbox[0]:.8f}")
            minlat_var.set(f"{bbox[1]:.8f}")
            maxlon_var.set(f"{bbox[2]:.8f}")
            maxlat_var.set(f"{bbox[3]:.8f}")
            log(f"Loaded ROI and computed bbox: {bbox}")
        except Exception as e:
            messagebox.showerror("Failed to read GeoJSON", str(e))

    def choose_out_dir():
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            out_var.set(d)

    add_labeled_path(0, "ROI GeoJSON", geojson_var, choose_geojson)
    add_labeled_path(1, "Output folder", out_var, choose_out_dir)

    ttk.Label(frm, text="BBOX (WGS84 lon/lat)").grid(row=2, column=0, sticky="w", pady=(12, 4))

    bbox_grid = ttk.Frame(frm)
    bbox_grid.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(12, 4))
    for c in range(4):
        bbox_grid.columnconfigure(c, weight=1)

    def small_entry(parent, var: tk.StringVar) -> ttk.Entry:
        e = ttk.Entry(parent, textvariable=var)
        return e

    ttk.Label(bbox_grid, text="minLon").grid(row=0, column=0, sticky="w")
    small_entry(bbox_grid, minlon_var).grid(row=1, column=0, sticky="ew", padx=(0, 8))
    ttk.Label(bbox_grid, text="minLat").grid(row=0, column=1, sticky="w")
    small_entry(bbox_grid, minlat_var).grid(row=1, column=1, sticky="ew", padx=(0, 8))
    ttk.Label(bbox_grid, text="maxLon").grid(row=0, column=2, sticky="w")
    small_entry(bbox_grid, maxlon_var).grid(row=1, column=2, sticky="ew", padx=(0, 8))
    ttk.Label(bbox_grid, text="maxLat").grid(row=0, column=3, sticky="w")
    small_entry(bbox_grid, maxlat_var).grid(row=1, column=3, sticky="ew")

    add_labeled_entry(3, "Start date (YYYY-MM-DD)", start_var)
    add_labeled_entry(4, "End date (YYYY-MM-DD)", end_var)
    add_labeled_entry(5, "Limit", limit_var)

    log_box = tk.Text(frm, height=14, wrap="word")
    log_box.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(12, 0))

    def log(msg: str) -> None:
        log_box.insert("end", msg + "\n")
        log_box.see("end")

    btn_run = ttk.Button(frm, text="Run")
    btn_run.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    def on_run():
        try:
            minx = float(minlon_var.get().strip())
            miny = float(minlat_var.get().strip())
            maxx = float(maxlon_var.get().strip())
            maxy = float(maxlat_var.get().strip())
            validate_bbox(minx, miny, maxx, maxy)

            start_date = start_var.get().strip()
            end_date = end_var.get().strip()
            limit = int(limit_var.get().strip())
            if limit <= 0:
                raise ValueError("Limit must be a positive integer.")

            out_dir_str = out_var.get().strip()
            if not out_dir_str:
                raise ValueError("Output folder is required.")
            out_dir = Path(out_dir_str)
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))
            return

        btn_run.config(state="disabled")
        log("Starting...")

        params = dict(
            bbox=[minx, miny, maxx, maxy],
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            out_dir=out_dir,
        )

        def worker():
            try:
                run_job(log=log, **params)
                log("Done.")
            except Exception as e:
                log(f"Failed: {e}")
                messagebox.showerror("Run failed", str(e))
            finally:
                btn_run.config(state="normal")

        threading.Thread(target=worker, daemon=True).start()

    btn_run.config(command=on_run)

    return root


def main() -> None:
    app = make_ui()
    app.mainloop()


if __name__ == "__main__":
    main()

