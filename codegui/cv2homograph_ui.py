import json
import threading
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np
import tkinter as tk
from pyproj import CRS, Transformer
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import transform as shp_transform, unary_union
from tkinter import filedialog, messagebox, ttk

OVERLAY_ALPHA = 0.3


def _crs_from_geojson_root(root: dict) -> CRS:
    crs_obj = root.get("crs")
    if not crs_obj:
        return CRS.from_epsg(4326)
    typ = crs_obj.get("type")
    props = crs_obj.get("properties") or {}
    if typ == "name":
        name = (props.get("name") or "").strip()
        if not name:
            return CRS.from_epsg(4326)
        return CRS.from_user(name)
    if typ == "link":
        raise ValueError(
            "GeoJSON uses linked CRS metadata, which is not supported. "
            "Re-export the file with embedded CRS or as EPSG:4326."
        )
    return CRS.from_epsg(4326)


def _geometries_from_geojson_dict(root: dict) -> List:
    t = root.get("type")
    if t == "FeatureCollection":
        feats = root.get("features") or []
        out = []
        for f in feats:
            g = f.get("geometry")
            if g:
                out.append(shape(g))
        return out
    if t == "Feature":
        return [shape(root["geometry"])]
    if t and t != "GeometryCollection":
        return [shape(root)]
    raise ValueError(f"Unsupported GeoJSON type: {t!r}")


def _to_wgs84(geom, src: CRS):
    dst = CRS.from_epsg(4326)
    tr = Transformer.from_crs(src, dst, always_xy=True)
    return shp_transform(tr.transform, geom)


def _read_geojson_first_geometry(path: Path):
    with open(path, encoding="utf-8") as f:
        root = json.load(f)
    src_crs = _crs_from_geojson_root(root)
    geoms = _geometries_from_geojson_dict(root)
    if not geoms:
        raise ValueError(f"No geometries in GeoJSON: {path}")
    return _to_wgs84(geoms[0], src_crs)


def _read_geojson_union(path: Path):
    with open(path, encoding="utf-8") as f:
        root = json.load(f)
    src_crs = _crs_from_geojson_root(root)
    geoms = _geometries_from_geojson_dict(root)
    if not geoms:
        raise ValueError(f"No geometries in GeoJSON: {path}")
    wgs = [_to_wgs84(g, src_crs) for g in geoms]
    return unary_union(wgs)


def get_exterior_coords(geom) -> List[Tuple[float, float]]:
    if geom.geom_type == "Polygon":
        coords = list(geom.exterior.coords)
    elif geom.geom_type == "MultiPolygon":
        largest = max(geom.geoms, key=lambda g: g.area)
        coords = list(largest.exterior.coords)
    else:
        raise ValueError(f"Unsupported geometry type: {geom.geom_type}")

    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    return coords


def order_footprint_corners(coords: List[Tuple[float, float]]) -> np.ndarray:
    pts = np.array(coords, dtype=np.float32)
    if len(pts) != 4:
        raise ValueError(f"Footprint must have 4 unique corners, got {len(pts)}.")

    idx = np.argsort(-pts[:, 1])  # lat desc
    top2 = pts[idx[:2]]
    bottom2 = pts[idx[2:]]

    top_left, top_right = top2[np.argsort(top2[:, 0])]
    bottom_left, bottom_right = bottom2[np.argsort(bottom2[:, 0])]

    return np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)


def clip_int(v: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(v))))


def run_homography_job(
    *,
    quicklook_path: Path,
    footprint_path: Path,
    roi_path: Path,
    out_dir: Path,
    log,
) -> None:
    if not quicklook_path.exists():
        raise FileNotFoundError(f"Quicklook image not found: {quicklook_path}")
    if not footprint_path.exists():
        raise FileNotFoundError(f"Footprint GeoJSON not found: {footprint_path}")
    if not roi_path.exists():
        raise FileNotFoundError(f"ROI GeoJSON not found: {roi_path}")

    out_basename = quicklook_path.stem.strip() or "result"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = out_dir / out_basename
    out_mark_path = out_prefix.with_name(out_prefix.name + "_marked.png")
    out_crop_path = out_prefix.with_name(out_prefix.name + "_crop.jpg")

    log(f"Quicklook: {quicklook_path}")
    log(f"Footprint: {footprint_path}")
    log(f"ROI: {roi_path}")
    log(f"Output marked: {out_mark_path}")
    log(f"Output crop: {out_crop_path}")

    quicklook_geom = _read_geojson_first_geometry(footprint_path)
    footprint_coords = get_exterior_coords(quicklook_geom)

    unique_coords: List[Tuple[float, float]] = []
    for c in footprint_coords:
        if c not in unique_coords:
            unique_coords.append(c)
    if len(unique_coords) != 4:
        raise ValueError(f"Footprint is not a 4-corner polygon, got {len(unique_coords)}: {unique_coords}")

    footprint_pts = order_footprint_corners(unique_coords)

    roi_geom = _read_geojson_union(roi_path)
    if isinstance(roi_geom, MultiPolygon):
        roi_geom = max(roi_geom.geoms, key=lambda g: g.area)
    if not isinstance(roi_geom, Polygon):
        raise ValueError("ROI must be Polygon or MultiPolygon.")

    roi_coords = get_exterior_coords(roi_geom)
    roi_pts_geo = np.array(roi_coords, dtype=np.float32)

    img = cv2.imread(str(quicklook_path))
    if img is None:
        raise FileNotFoundError(f"Failed to read image: {quicklook_path}")

    h_img, w_img = img.shape[:2]
    img_pts = np.array(
        [
            [0, 0],
            [w_img, 0],
            [w_img, h_img],
            [0, h_img],
        ],
        dtype=np.float32,
    )

    h_matrix, _status = cv2.findHomography(footprint_pts, img_pts)
    if h_matrix is None:
        raise RuntimeError("Failed to compute homography matrix.")

    roi_pts_geo_cv = roi_pts_geo.reshape(-1, 1, 2)
    roi_pts_img = cv2.perspectiveTransform(roi_pts_geo_cv, h_matrix).reshape(-1, 2)

    xs = roi_pts_img[:, 0]
    ys = roi_pts_img[:, 1]
    left = clip_int(float(xs.min()), 0, w_img)
    right = clip_int(float(xs.max()), 0, w_img)
    top = clip_int(float(ys.min()), 0, h_img)
    bottom = clip_int(float(ys.max()), 0, h_img)

    if left >= right or top >= bottom:
        raise ValueError("Invalid crop box; footprint and ROI may not match.")

    img_mark = img.copy()
    roi_polygon_pixels = np.round(roi_pts_img).astype(np.int32).reshape((-1, 1, 2))

    overlay = img_mark.copy()
    cv2.fillPoly(overlay, [roi_polygon_pixels], color=(0, 255, 255))
    a = OVERLAY_ALPHA
    a = 0.0 if a < 0.0 else (1.0 if a > 1.0 else a)
    img_mark = cv2.addWeighted(overlay, a, img_mark, 1 - a, 0)

    cv2.rectangle(img_mark, (left, top), (right, bottom), color=(0, 255, 255), thickness=1)

    cv2.imwrite(str(out_mark_path), img_mark)
    crop = img[top:bottom, left:right]
    cv2.imwrite(str(out_crop_path), crop)

    log("Done.")


def make_ui() -> tk.Tk:
    root = tk.Tk()
    root.title("Homography")
    root.minsize(860, 540)

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(6, weight=1)

    quicklook_var = tk.StringVar(value="")
    footprint_var = tk.StringVar(value="")
    roi_var = tk.StringVar(value="")
    out_dir_var = tk.StringVar(value="")

    def add_labeled_path(row: int, label: str, var: tk.StringVar, choose_fn) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Browse...", command=choose_fn).grid(row=row, column=2, padx=(8, 0), pady=4)

    def choose_quicklook():
        p = filedialog.askopenfilename(
            title="Select quicklook image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.tif *.tiff *.webp"), ("All files", "*.*")],
        )
        if p:
            quicklook_var.set(p)

    def choose_footprint():
        p = filedialog.askopenfilename(
            title="Select quicklook footprint GeoJSON",
            filetypes=[("GeoJSON", "*.geojson *.json"), ("All files", "*.*")],
        )
        if p:
            footprint_var.set(p)

    def choose_roi():
        p = filedialog.askopenfilename(
            title="Select ROI GeoJSON",
            filetypes=[("GeoJSON", "*.geojson *.json"), ("All files", "*.*")],
        )
        if p:
            roi_var.set(p)

    def choose_out_dir():
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            out_dir_var.set(d)

    add_labeled_path(0, "Quicklook image", quicklook_var, choose_quicklook)
    add_labeled_path(1, "Quicklook footprint", footprint_var, choose_footprint)
    add_labeled_path(2, "ROI (GeoJSON)", roi_var, choose_roi)
    add_labeled_path(3, "Output folder", out_dir_var, choose_out_dir)

    log_box = tk.Text(frm, height=14, wrap="word")
    log_box.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(12, 0))

    def log(msg: str) -> None:
        log_box.insert("end", msg + "\n")
        log_box.see("end")

    btn_run = ttk.Button(frm, text="Run")
    btn_run.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    def on_run():
        try:
            q = Path(quicklook_var.get().strip())
            f = Path(footprint_var.get().strip())
            r = Path(roi_var.get().strip())
            out_dir_str = out_dir_var.get().strip()
            if not out_dir_str:
                raise ValueError("Output folder is required.")
            out_dir = Path(out_dir_str)
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))
            return

        btn_run.config(state="disabled")
        log("Starting...")

        def worker():
            try:
                run_homography_job(
                    quicklook_path=q,
                    footprint_path=f,
                    roi_path=r,
                    out_dir=out_dir,
                    log=log,
                )
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

