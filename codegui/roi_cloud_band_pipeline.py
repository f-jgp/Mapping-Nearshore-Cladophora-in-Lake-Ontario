"""
Orchestrate: STAC footprint download → homography ROI crop → HSV cloud rate → band download.

Calls existing run_* entry points only; does not reimplement their logic.
Run without arguments to open the GUI.
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from cloud_gui import run_cloud_job
from cv2homograph_ui import run_homography_job
from footprint_download_contained import DownloadedScene, run_contained_download
from footprint_ui import COLLECTION, STAC_CATALOG_URL, bbox_from_geojson, validate_bbox
from sentinel_band_download import download_product_jp2_files

LogFn = Callable[[str], None]

SUMMARY_FILENAME = "pipeline_summary.txt"


@dataclass
class ScenePipelineResult:
    item_id: str
    cloud_percent: Optional[float]
    bands_downloaded: bool
    scene_dir: Path
    error: Optional[str] = None


def crop_path_for_quicklook(quicklook_path: Path, scene_dir: Path) -> Path:
    """Match cv2homograph_ui.run_homography_job output naming."""
    stem = quicklook_path.stem.strip() or "result"
    return scene_dir / f"{stem}_crop.jpg"


def cloud_coverage_path_for_crop(crop_path: Path, scene_dir: Path) -> Path:
    """Match cloud_gui.run_cloud_job output naming."""
    stem = crop_path.stem.strip() or "output"
    return scene_dir / f"{stem}_cloud_coverage.txt"


def parse_cloud_coverage_txt(path: Path) -> float:
    if not path.is_file():
        raise FileNotFoundError(f"Cloud coverage file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("cloud_percent:"):
            return float(stripped.split(":", 1)[1].strip())
    raise ValueError(f"No cloud_percent line in {path}")


def organize_scene_files(scene: DownloadedScene, out_dir: Path) -> tuple[Path, Path, Path]:
    """Move footprint/quicklook into out_dir/<item_id>/; return scene_dir and paths."""
    scene_dir = out_dir / scene.item_id
    scene_dir.mkdir(parents=True, exist_ok=True)

    quicklook_dst = scene_dir / scene.quicklook_path.name
    footprint_dst = scene_dir / scene.footprint_path.name

    for src, dst in (
        (scene.quicklook_path, quicklook_dst),
        (scene.footprint_path, footprint_dst),
    ):
        if src.resolve() == dst.resolve():
            continue
        if dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))

    return scene_dir, quicklook_dst, footprint_dst


def process_scene(
    *,
    scene: DownloadedScene,
    out_dir: Path,
    roi_path: Path,
    max_cloud_percent: float,
    cdse_username: str,
    cdse_password: str,
    log: LogFn,
) -> ScenePipelineResult:
    item_id = scene.item_id
    scene_dir, quicklook_path, footprint_path = organize_scene_files(scene, out_dir)
    log(f"=== Scene {item_id} ===")

    try:
        log("--- Homography ---")
        run_homography_job(
            quicklook_path=quicklook_path,
            footprint_path=footprint_path,
            roi_path=roi_path,
            out_dir=scene_dir,
            log=log,
        )

        crop_path = crop_path_for_quicklook(quicklook_path, scene_dir)
        if not crop_path.is_file():
            raise FileNotFoundError(f"Homography crop not found: {crop_path}")

        log("--- Cloud coverage ---")
        run_cloud_job(
            image_path=crop_path,
            output_dir=scene_dir,
            log=log,
        )

        coverage_path = cloud_coverage_path_for_crop(crop_path, scene_dir)
        cloud_percent = parse_cloud_coverage_txt(coverage_path)
        log(f"Cloud percent (ROI crop): {cloud_percent:.4f}%")

        if cloud_percent > max_cloud_percent:
            log(
                f"Skip band download: {cloud_percent:.4f}% > max {max_cloud_percent:.4f}%"
            )
            return ScenePipelineResult(
                item_id=item_id,
                cloud_percent=cloud_percent,
                bands_downloaded=False,
                scene_dir=scene_dir,
            )

        log("--- Band download (all resolutions / bands) ---")
        download_product_jp2_files(
            item_id=item_id,
            out_dir=scene_dir,
            band_names=None,
            cdse_username=cdse_username,
            cdse_password=cdse_password,
            log=log,
        )
        return ScenePipelineResult(
            item_id=item_id,
            cloud_percent=cloud_percent,
            bands_downloaded=True,
            scene_dir=scene_dir,
        )
    except Exception as exc:
        log(f"Scene {item_id} failed: {exc}")
        return ScenePipelineResult(
            item_id=item_id,
            cloud_percent=None,
            bands_downloaded=False,
            scene_dir=scene_dir,
            error=str(exc),
        )


def write_pipeline_summary(out_dir: Path, results: List[ScenePipelineResult]) -> Path:
    lines = [
        "item_id\tcloud_percent\tbands_downloaded\terror",
    ]
    for r in results:
        cp = "" if r.cloud_percent is None else f"{r.cloud_percent:.4f}"
        err = r.error or ""
        lines.append(f"{r.item_id}\t{cp}\t{int(r.bands_downloaded)}\t{err}")
    path = out_dir / SUMMARY_FILENAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_roi_cloud_band_pipeline(
    *,
    roi_path: Path,
    search_bbox: list[float],
    start_date: str,
    end_date: str,
    limit: int,
    out_dir: Path,
    max_cloud_percent: float,
    cdse_username: str,
    cdse_password: str,
    catalog_url: str = STAC_CATALOG_URL,
    collection: str = COLLECTION,
    log: LogFn,
) -> List[ScenePipelineResult]:
    minx, miny, maxx, maxy = search_bbox
    validate_bbox(minx, miny, maxx, maxy)

    if not roi_path.is_file():
        raise FileNotFoundError(f"ROI file not found: {roi_path}")

    out_dir.mkdir(parents=True, exist_ok=True)

    log("--- STAC download (ROI fully inside footprint) ---")
    _n, scenes = run_contained_download(
        roi_path=roi_path,
        search_bbox=search_bbox,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        out_dir=out_dir,
        collection=collection,
        catalog_url=catalog_url,
        log=log,
    )

    if not scenes:
        log("No scenes to process.")
        write_pipeline_summary(out_dir, [])
        return []

    results: List[ScenePipelineResult] = []
    for scene in scenes:
        results.append(
            process_scene(
                scene=scene,
                out_dir=out_dir,
                roi_path=roi_path,
                max_cloud_percent=max_cloud_percent,
                cdse_username=cdse_username,
                cdse_password=cdse_password,
                log=log,
            )
        )

    summary_path = write_pipeline_summary(out_dir, results)
    downloaded = sum(1 for r in results if r.bands_downloaded)
    log(f"Pipeline finished. Bands downloaded for {downloaded}/{len(results)} scene(s).")
    log(f"Summary: {summary_path}")
    return results


def make_ui() -> tk.Tk:
    root = tk.Tk()
    root.title("Sentinel-2 ROI Cloud Band Pipeline")
    root.minsize(820, 560)

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(11, weight=1)

    geojson_var = tk.StringVar(value="")
    out_var = tk.StringVar(value="")

    minlon_var = tk.StringVar(value="")
    minlat_var = tk.StringVar(value="")
    maxlon_var = tk.StringVar(value="")
    maxlat_var = tk.StringVar(value="")

    start_var = tk.StringVar(value="2023-08-01")
    end_var = tk.StringVar(value="2023-08-10")
    limit_var = tk.StringVar(value="5")
    max_cloud_var = tk.StringVar(value="20")

    cdse_username_var = tk.StringVar(value=os.environ.get("CDSE_USERNAME", ""))
    cdse_password_var = tk.StringVar(value="")

    def add_labeled_entry(row: int, label: str, var: tk.StringVar) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)

    def add_labeled_path(row: int, label: str, var: tk.StringVar, choose_fn) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Browse...", command=choose_fn).grid(
            row=row, column=2, sticky="ew", padx=(8, 0), pady=4
        )

    log_box = tk.Text(frm, height=14, wrap="word")
    log_box.grid(row=11, column=0, columnspan=3, sticky="nsew", pady=(12, 0))

    def log(msg: str) -> None:
        log_box.insert("end", msg + "\n")
        log_box.see("end")

    def choose_geojson() -> None:
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
        except Exception as exc:
            messagebox.showerror("Failed to read GeoJSON", str(exc))

    def choose_out_dir() -> None:
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
        return ttk.Entry(parent, textvariable=var)

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
    add_labeled_entry(6, "Max cloud % (download bands if at or below)", max_cloud_var)

    ttk.Label(frm, text="CDSE username").grid(row=7, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=cdse_username_var).grid(row=7, column=1, sticky="ew", pady=4)

    ttk.Label(frm, text="CDSE password").grid(row=8, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=cdse_password_var, show="*").grid(row=8, column=1, sticky="ew", pady=4)

    btn_run = ttk.Button(frm, text="Run pipeline")
    btn_run.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    def on_run() -> None:
        try:
            roi_s = geojson_var.get().strip()
            if not roi_s:
                raise ValueError("ROI GeoJSON is required.")
            roi_path = Path(roi_s)

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

            max_cloud_percent = float(max_cloud_var.get().strip())
            if max_cloud_percent < 0:
                raise ValueError("Max cloud % must be >= 0.")

            out_dir_str = out_var.get().strip()
            if not out_dir_str:
                raise ValueError("Output folder is required.")
            out_dir = Path(out_dir_str)

            cdse_username = cdse_username_var.get().strip()
            cdse_password = cdse_password_var.get().strip()
        except Exception as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        btn_run.config(state="disabled")
        log("Starting pipeline...")

        params = dict(
            roi_path=roi_path,
            search_bbox=[minx, miny, maxx, maxy],
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            out_dir=out_dir,
            max_cloud_percent=max_cloud_percent,
            cdse_username=cdse_username,
            cdse_password=cdse_password,
        )

        def worker() -> None:
            try:
                run_roi_cloud_band_pipeline(log=log, **params)
                log("Done.")
            except Exception as exc:
                log(f"Failed: {exc}")
                messagebox.showerror("Run failed", str(exc))
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
