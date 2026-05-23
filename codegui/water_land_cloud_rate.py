"""Water vs land cloud rates from Sentinel-2 SCL and global water occurrence.

Run without arguments to open the GUI.
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from pystac_client import Client
from shapely.geometry import shape

from footprint_download_contained import load_roi_geometry
from footprint_ui import COLLECTION, STAC_CATALOG_URL, bbox_from_geojson, validate_bbox
from sentinel_band_download import download_resolution_jp2_files

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject
except ImportError:  # pragma: no cover
    rasterio = None
    Resampling = None
    reproject = None

LogFn = Callable[[str], None]

_PACKAGE_DIR = Path(__file__).resolve().parent
WATER_OCCURRENCE_PATH = _PACKAGE_DIR / "waterlandcloud" / "occurrence_80W_50Nv1_4_2021.tif"
WATER_THRESHOLD = 50
CLOUD_CLASSES = (8, 9, 10)
SCL_RESOLUTION = "R60m"
SCL_BAND = "SCL"
SUMMARY_FILENAME = "water_land_cloud_rate_summary.txt"


@dataclass(frozen=True)
class WaterLandCloudMetrics:
    scl_path: Path
    occurrence_path: Path
    water_threshold: int
    cloud_classes: tuple[int, ...]
    valid_pixels: int
    water_pixels: int
    land_pixels: int
    cloud_water_pixels: int
    cloud_land_pixels: int
    water_cloud_rate: float
    land_cloud_rate: float

    def format_report(self) -> str:
        lines = [
            f"scl_file: {self.scl_path}",
            f"occurrence_file: {self.occurrence_path}",
            f"water_threshold: {self.water_threshold}",
            f"cloud_classes: {', '.join(str(c) for c in self.cloud_classes)}",
            f"valid_pixels: {self.valid_pixels}",
            f"water_pixels: {self.water_pixels}",
            f"cloud_water_pixels: {self.cloud_water_pixels}",
            f"water_cloud_rate: {self._fmt_rate(self.water_cloud_rate)}",
            f"land_pixels: {self.land_pixels}",
            f"cloud_land_pixels: {self.cloud_land_pixels}",
            f"land_cloud_rate: {self._fmt_rate(self.land_cloud_rate)}",
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _fmt_rate(value: float) -> str:
        if math.isnan(value):
            return "nan"
        return f"{value:.6f}"


@dataclass
class AggregateWaterLandCloudCounts:
    valid_pixels: int = 0
    water_pixels: int = 0
    land_pixels: int = 0
    cloud_water_pixels: int = 0
    cloud_land_pixels: int = 0
    scenes_processed: int = 0

    def add(self, metrics: WaterLandCloudMetrics) -> None:
        self.valid_pixels += metrics.valid_pixels
        self.water_pixels += metrics.water_pixels
        self.land_pixels += metrics.land_pixels
        self.cloud_water_pixels += metrics.cloud_water_pixels
        self.cloud_land_pixels += metrics.cloud_land_pixels
        self.scenes_processed += 1

    @property
    def water_cloud_rate(self) -> float:
        if self.water_pixels <= 0:
            return float("nan")
        return float(self.cloud_water_pixels) / float(self.water_pixels)

    @property
    def land_cloud_rate(self) -> float:
        if self.land_pixels <= 0:
            return float("nan")
        return float(self.cloud_land_pixels) / float(self.land_pixels)

    @staticmethod
    def format_rate(value: float) -> str:
        if math.isnan(value):
            return "nan"
        return f"{value:.6f}"


def _require_rasterio() -> None:
    if rasterio is None or reproject is None or Resampling is None:
        raise ImportError("rasterio is required. Install with: pip install rasterio")


def _resolve_occurrence_path(occurrence_path: Optional[Path] = None) -> Path:
    path = occurrence_path or WATER_OCCURRENCE_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Water occurrence GeoTIFF not found: {path}")
    return path


def align_occurrence_to_scl(
    *,
    occurrence_path: Path,
    scl_crs,
    scl_transform,
    scl_height: int,
    scl_width: int,
) -> np.ndarray:
    """Reproject occurrence raster onto the SCL grid (nearest neighbour)."""
    _require_rasterio()
    aligned = np.empty((scl_height, scl_width), dtype=np.float32)
    with rasterio.open(occurrence_path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=aligned,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=scl_transform,
            dst_crs=scl_crs,
            resampling=Resampling.nearest,
            src_nodata=src.nodata,
            dst_nodata=np.nan,
        )
    return aligned


def compute_water_land_cloud_metrics(
    *,
    scl_path: Path,
    occurrence_path: Optional[Path] = None,
    water_threshold: int = WATER_THRESHOLD,
    cloud_classes: tuple[int, ...] = CLOUD_CLASSES,
) -> WaterLandCloudMetrics:
    _require_rasterio()
    if not scl_path.is_file():
        raise FileNotFoundError(f"SCL file not found: {scl_path}")

    occ_path = _resolve_occurrence_path(occurrence_path)

    with rasterio.open(scl_path) as scl_ds:
        scl = scl_ds.read(1)
        scl_nodata = scl_ds.nodata
        scl_crs = scl_ds.crs
        scl_transform = scl_ds.transform
        height, width = scl_ds.height, scl_ds.width

    if scl_crs is None:
        raise ValueError(f"SCL has no CRS: {scl_path}")

    occurrence = align_occurrence_to_scl(
        occurrence_path=occ_path,
        scl_crs=scl_crs,
        scl_transform=scl_transform,
        scl_height=height,
        scl_width=width,
    )

    scl_valid = np.ones(scl.shape, dtype=bool)
    if scl_nodata is not None:
        scl_valid &= scl != scl_nodata

    occ_valid = np.isfinite(occurrence)
    valid_mask = scl_valid & occ_valid

    water_mask = valid_mask & (occurrence > float(water_threshold))
    land_mask = valid_mask & ~water_mask
    cloud_mask = valid_mask & np.isin(scl, list(cloud_classes))

    water_pixels = int(water_mask.sum())
    land_pixels = int(land_mask.sum())
    cloud_water_pixels = int((cloud_mask & water_mask).sum())
    cloud_land_pixels = int((cloud_mask & land_mask).sum())
    valid_pixels = int(valid_mask.sum())

    water_cloud_rate = (
        float(cloud_water_pixels) / float(water_pixels) if water_pixels > 0 else float("nan")
    )
    land_cloud_rate = (
        float(cloud_land_pixels) / float(land_pixels) if land_pixels > 0 else float("nan")
    )

    return WaterLandCloudMetrics(
        scl_path=scl_path.resolve(),
        occurrence_path=occ_path.resolve(),
        water_threshold=water_threshold,
        cloud_classes=cloud_classes,
        valid_pixels=valid_pixels,
        water_pixels=water_pixels,
        land_pixels=land_pixels,
        cloud_water_pixels=cloud_water_pixels,
        cloud_land_pixels=cloud_land_pixels,
        water_cloud_rate=water_cloud_rate,
        land_cloud_rate=land_cloud_rate,
    )


def search_stac_items_covering_roi(
    *,
    roi_path: Path,
    start_date: str,
    end_date: str,
    limit: int,
    catalog_url: str = STAC_CATALOG_URL,
    collection: str = COLLECTION,
    log: LogFn,
) -> tuple[list, int]:
    if not roi_path.is_file():
        raise FileNotFoundError(f"ROI file not found: {roi_path}")

    bbox = bbox_from_geojson(roi_path)
    minx, miny, maxx, maxy = bbox
    validate_bbox(minx, miny, maxx, maxy)
    roi_geom = load_roi_geometry(roi_path)

    log(f"ROI: {roi_path}")
    log(f"BBOX (STAC search): {bbox}")
    log(f"Date range: {start_date} / {end_date}")
    log(f"STAC limit: {limit}")

    catalog = Client.open(catalog_url.strip())
    search = catalog.search(
        collections=[collection.strip()],
        bbox=bbox,
        datetime=f"{start_date.strip()}/{end_date.strip()}",
        limit=limit,
    )
    items = list(search.items())
    log(f"STAC candidates: {len(items)}")

    covered = []
    for item in items:
        geom = item.geometry
        if not geom:
            continue
        scene = shape(geom)
        if not scene.is_valid:
            scene = scene.buffer(0)
        if scene.covers(roi_geom):
            covered.append(item)

    log(f"Scenes fully covering ROI: {len(covered)}")
    return covered, len(items)


def _snapshot_dir_files(directory: Path) -> dict[Path, float]:
    if not directory.is_dir():
        return {}
    return {p.resolve(): p.stat().st_mtime for p in directory.iterdir() if p.is_file()}


def _paths_written_by_download(saved_paths: List[Path], before: dict[Path, float]) -> List[Path]:
    written: List[Path] = []
    for path in saved_paths:
        key = path.resolve()
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        if key not in before:
            written.append(path)
        elif path.stat().st_mtime > before[key] + 1e-6:
            written.append(path)
    return written


def _delete_downloaded_files(paths: List[Path], log: LogFn) -> int:
    deleted = 0
    seen: set[Path] = set()
    for path in paths:
        for candidate in (path.resolve(), Path(str(path.resolve()) + ".aux.xml")):
            if candidate in seen or not candidate.is_file():
                continue
            seen.add(candidate)
            candidate.unlink()
            deleted += 1
            log(f"Deleted: {candidate}")
    return deleted


def format_batch_summary(
    *,
    roi_path: Path,
    start_date: str,
    end_date: str,
    stac_candidates: int,
    aggregate: AggregateWaterLandCloudCounts,
    occurrence_path: Path,
    deleted_scl_count: int,
) -> str:
    lines = [
        f"roi_file: {roi_path.resolve()}",
        f"start_date: {start_date}",
        f"end_date: {end_date}",
        f"stac_candidates: {stac_candidates}",
        f"scenes_processed: {aggregate.scenes_processed}",
        f"occurrence_file: {occurrence_path.resolve()}",
        f"water_threshold: {WATER_THRESHOLD}",
        f"cloud_classes: {', '.join(str(c) for c in CLOUD_CLASSES)}",
        f"scl_resolution: {SCL_RESOLUTION}",
        f"scl_band: {SCL_BAND}",
        f"total_valid_pixels: {aggregate.valid_pixels}",
        f"total_water_pixels: {aggregate.water_pixels}",
        f"total_cloud_water_pixels: {aggregate.cloud_water_pixels}",
        f"aggregate_water_cloud_rate: {AggregateWaterLandCloudCounts.format_rate(aggregate.water_cloud_rate)}",
        f"total_land_pixels: {aggregate.land_pixels}",
        f"total_cloud_land_pixels: {aggregate.cloud_land_pixels}",
        f"aggregate_land_cloud_rate: {AggregateWaterLandCloudCounts.format_rate(aggregate.land_cloud_rate)}",
        f"deleted_downloaded_scl_count: {deleted_scl_count}",
    ]
    return "\n".join(lines) + "\n"


def run_batch_water_land_cloud_job(
    *,
    roi_path: Path,
    start_date: str,
    end_date: str,
    limit: int,
    out_dir: Path,
    cdse_username: str,
    cdse_password: str,
    occurrence_path: Optional[Path] = None,
    log: LogFn,
) -> Path:
    _require_rasterio()
    if limit <= 0:
        raise ValueError("Limit must be a positive integer.")

    out_dir.mkdir(parents=True, exist_ok=True)
    occ_path = _resolve_occurrence_path(occurrence_path)
    download_dir = out_dir / "_scl_download"
    download_dir.mkdir(parents=True, exist_ok=True)

    items, stac_candidates = search_stac_items_covering_roi(
        roi_path=roi_path,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        log=log,
    )

    aggregate = AggregateWaterLandCloudCounts()
    files_to_delete: List[Path] = []
    deleted_count = 0

    try:
        if not items:
            log("No scenes fully covering ROI.")
        else:
            for i, item in enumerate(items, 1):
                log(f"--- Scene {i}/{len(items)}: {item.id} ---")
                before = _snapshot_dir_files(download_dir)
                try:
                    saved = download_resolution_jp2_files(
                        item_id=item.id,
                        resolution=SCL_RESOLUTION,
                        out_dir=download_dir,
                        band_names=[SCL_BAND],
                        cdse_username=cdse_username,
                        cdse_password=cdse_password,
                        log=log,
                    )
                except Exception as exc:
                    log(f"Download failed for {item.id}: {exc}")
                    continue

                written = _paths_written_by_download(saved, before)
                files_to_delete.extend(written)

                if not saved:
                    log("No SCL file available; skip metrics.")
                    continue

                for scl_path in saved:
                    if not scl_path.is_file() or scl_path.stat().st_size <= 0:
                        continue
                    log(f"Computing metrics: {scl_path.name}")
                    metrics = compute_water_land_cloud_metrics(
                        scl_path=scl_path,
                        occurrence_path=occ_path,
                    )
                    aggregate.add(metrics)
                    log(
                        f"  water cloud rate: {metrics.water_cloud_rate:.6f}, "
                        f"land cloud rate: {metrics.land_cloud_rate:.6f}"
                    )
    finally:
        deleted_count = _delete_downloaded_files(files_to_delete, log)

    summary_path = out_dir / SUMMARY_FILENAME
    summary_path.write_text(
        format_batch_summary(
            roi_path=roi_path,
            start_date=start_date,
            end_date=end_date,
            stac_candidates=stac_candidates,
            aggregate=aggregate,
            occurrence_path=occ_path,
            deleted_scl_count=deleted_count,
        ),
        encoding="utf-8",
    )
    log(f"Aggregate water cloud rate: {aggregate.water_cloud_rate:.6f}")
    log(f"Aggregate land cloud rate: {aggregate.land_cloud_rate:.6f}")
    log(f"Saved summary: {summary_path}")
    return summary_path


def run_water_land_cloud_job(
    *,
    scl_path: Path,
    out_dir: Path,
    occurrence_path: Optional[Path] = None,
    log: LogFn,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = compute_water_land_cloud_metrics(
        scl_path=scl_path,
        occurrence_path=occurrence_path,
    )

    log(f"SCL: {metrics.scl_path}")
    log(f"Occurrence: {metrics.occurrence_path}")
    log(f"Valid pixels: {metrics.valid_pixels}")
    log(f"Water pixels: {metrics.water_pixels}")
    log(f"Land pixels: {metrics.land_pixels}")

    if metrics.water_pixels == 0:
        log("Warning: no water pixels; water_cloud_rate is nan.")
    else:
        log(
            f"Water cloud: {metrics.cloud_water_pixels} / {metrics.water_pixels} "
            f"= {metrics.water_cloud_rate:.6f}"
        )

    if metrics.land_pixels == 0:
        log("Warning: no land pixels; land_cloud_rate is nan.")
    else:
        log(
            f"Land cloud: {metrics.cloud_land_pixels} / {metrics.land_pixels} "
            f"= {metrics.land_cloud_rate:.6f}"
        )

    out_path = out_dir / f"{scl_path.stem}_water_land_cloud_rate.txt"
    out_path.write_text(metrics.format_report(), encoding="utf-8")
    log(f"Saved: {out_path}")
    return out_path


def make_ui() -> tk.Tk:
    root = tk.Tk()
    root.title("Water / Land Cloud Rate")
    root.minsize(820, 560)

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(9, weight=1)

    roi_var = tk.StringVar(value="")
    out_dir_var = tk.StringVar(value="")
    start_var = tk.StringVar(value="2023-08-01")
    end_var = tk.StringVar(value="2023-08-10")
    limit_var = tk.StringVar(value="50")
    cdse_username_var = tk.StringVar(value=os.environ.get("CDSE_USERNAME", ""))
    cdse_password_var = tk.StringVar(value="")

    def add_labeled_path(row: int, label: str, var: tk.StringVar, choose_fn) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Browse...", command=choose_fn).grid(
            row=row, column=2, padx=(8, 0), pady=4
        )

    def add_labeled_entry(row: int, label: str, var: tk.StringVar, *, show: str = "") -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var, show=show).grid(row=row, column=1, sticky="ew", pady=4)

    def choose_roi() -> None:
        path = filedialog.askopenfilename(
            title="Select ROI GeoJSON",
            filetypes=[("GeoJSON", "*.geojson *.json"), ("All files", "*.*")],
        )
        if path:
            roi_var.set(path)

    def choose_out_dir() -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            out_dir_var.set(path)

    add_labeled_path(0, "ROI GeoJSON", roi_var, choose_roi)
    add_labeled_entry(1, "Start date (YYYY-MM-DD)", start_var)
    add_labeled_entry(2, "End date (YYYY-MM-DD)", end_var)
    add_labeled_entry(3, "Limit", limit_var)
    add_labeled_path(4, "Output folder", out_dir_var, choose_out_dir)
    add_labeled_entry(5, "CDSE username", cdse_username_var)
    add_labeled_entry(6, "CDSE password", cdse_password_var, show="*")

    log_box = tk.Text(frm, height=14, wrap="word")
    log_box.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(12, 0))

    def log(msg: str) -> None:
        log_box.insert("end", msg + "\n")
        log_box.see("end")

    btn_run = ttk.Button(frm, text="Run")
    btn_run.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    def on_run() -> None:
        try:
            roi_s = roi_var.get().strip()
            if not roi_s:
                raise ValueError("ROI GeoJSON is required.")
            roi_path = Path(roi_s)

            out_s = out_dir_var.get().strip()
            if not out_s:
                raise ValueError("Output folder is required.")
            out_dir = Path(out_s)

            start_date = start_var.get().strip()
            end_date = end_var.get().strip()
            if not start_date or not end_date:
                raise ValueError("Start date and end date are required.")

            limit = int(limit_var.get().strip())
            if limit <= 0:
                raise ValueError("Limit must be a positive integer.")

            cdse_username = cdse_username_var.get().strip()
            cdse_password = cdse_password_var.get().strip()
        except Exception as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        btn_run.config(state="disabled")
        log("Starting...")

        def worker() -> None:
            try:
                run_batch_water_land_cloud_job(
                    roi_path=roi_path,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                    out_dir=out_dir,
                    cdse_username=cdse_username,
                    cdse_password=cdse_password,
                    log=log,
                )
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
