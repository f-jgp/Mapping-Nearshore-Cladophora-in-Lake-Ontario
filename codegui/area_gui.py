from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable, Iterable, List, Tuple
from urllib.parse import quote, urljoin

import cv2
import numpy as np
import requests
import tkinter as tk
from pyproj import CRS, Transformer
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import transform as shp_transform, unary_union
from tkinter import filedialog, messagebox, ttk

try:
    import rasterio
    from rasterio.features import geometry_mask
except ImportError:  # pragma: no cover - reported in the GUI at runtime
    rasterio = None
    geometry_mask = None


LogFn = Callable[[str], None]

DEFAULT_ODATA_CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1"
DEFAULT_ODATA_DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1"

# OData / product defaults (not configurable in the GUI)
FIXED_ODATA_CATALOGUE_URL = DEFAULT_ODATA_CATALOGUE_URL
FIXED_COLLECTION = "SENTINEL-2"
FIXED_TCI_ASSET_KEY = "TCI_60m"

GREEN = np.array([0, 255, 0], dtype=np.uint8)
RED = np.array([255, 0, 0], dtype=np.uint8)
YELLOW = np.array([255, 255, 0], dtype=np.uint8)

# BGR colors matching area.py (OpenCV default) for filled-vs-tci metrics
AREA_METRICS_BGR_GREEN = np.array([0, 255, 0], dtype=np.uint8)
AREA_METRICS_BGR_RED = np.array([0, 0, 255], dtype=np.uint8)
AREA_METRICS_BGR_YELLOW = np.array([0, 255, 255], dtype=np.uint8)

COMPOSITE_LAYER_ALPHA = 0.5
# Upscale quicklook RGB layers to TCI HxW (TCI itself is not shrunk)
UPSCALE_QUICKLOOK_OUTLINE_INTERP = cv2.INTER_LINEAR
UPSCALE_QUICKLOOK_FILLED_INTERP = cv2.INTER_NEAREST


def _crs_from_geojson_root(root: dict) -> CRS:
    crs_obj = root.get("crs")
    if not crs_obj:
        return CRS.from_epsg(4326)

    typ = crs_obj.get("type")
    props = crs_obj.get("properties") or {}
    if typ == "name":
        name = (props.get("name") or "").strip()
        return CRS.from_user_input(name) if name else CRS.from_epsg(4326)
    if typ == "link":
        raise ValueError(
            "GeoJSON uses linked CRS metadata, which is not supported. "
            "Re-export it with embedded CRS or as EPSG:4326."
        )
    return CRS.from_epsg(4326)


def _geometries_from_geojson_dict(root: dict) -> List:
    t = root.get("type")
    if t == "FeatureCollection":
        geoms = []
        for feature in root.get("features") or []:
            geom = feature.get("geometry") if isinstance(feature, dict) else None
            if geom:
                geoms.append(shape(geom))
        return geoms
    if t == "Feature":
        geom = root.get("geometry")
        return [shape(geom)] if geom else []
    if t and t != "GeometryCollection":
        return [shape(root)]
    raise ValueError(f"Unsupported GeoJSON type: {t!r}")


def _to_wgs84(geom, src: CRS):
    dst = CRS.from_epsg(4326)
    if src == dst:
        return geom
    transformer = Transformer.from_crs(src, dst, always_xy=True)
    return shp_transform(transformer.transform, geom)


def _transform_crs(geom, src: CRS, dst):
    dst_crs = CRS.from_user_input(dst)
    if src == dst_crs:
        return geom
    transformer = Transformer.from_crs(src, dst_crs, always_xy=True)
    return shp_transform(transformer.transform, geom)


def _read_geojson_geometries_wgs84(path: Path) -> List:
    with open(path, encoding="utf-8") as f:
        root = json.load(f)

    src_crs = _crs_from_geojson_root(root)
    geoms = _geometries_from_geojson_dict(root)
    if not geoms:
        raise ValueError(f"No geometries found in GeoJSON: {path}")
    return [_to_wgs84(geom, src_crs) for geom in geoms]


def read_geojson_first_geometry(path: Path):
    return _read_geojson_geometries_wgs84(path)[0]


def read_geojson_union(path: Path):
    geom = unary_union(_read_geojson_geometries_wgs84(path))
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def polygon_from_geometry(geom) -> Polygon:
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda g: g.area)
    raise ValueError(f"Expected Polygon or MultiPolygon, got {geom.geom_type}.")


def get_exterior_coords(geom) -> List[Tuple[float, float]]:
    polygon = polygon_from_geometry(geom)
    coords = list(polygon.exterior.coords)
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    return coords


def unique_coords(coords: Iterable[Tuple[float, float]]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for coord in coords:
        if coord not in out:
            out.append(coord)
    return out


def order_footprint_corners(coords: List[Tuple[float, float]]) -> np.ndarray:
    pts = np.array(coords, dtype=np.float32)
    if len(pts) != 4:
        raise ValueError(f"Footprint must have 4 unique corners, got {len(pts)}.")

    idx = np.argsort(-pts[:, 1])
    top2 = pts[idx[:2]]
    bottom2 = pts[idx[2:]]

    top_left, top_right = top2[np.argsort(top2[:, 0])]
    bottom_left, bottom_right = bottom2[np.argsort(bottom2[:, 0])]
    return np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)


def clip_int(v: float, low: int, high: int) -> int:
    return max(low, min(high, int(round(v))))


def lon_to_x(lon: float, west: float, east: float, width: int) -> float:
    return (lon - west) / (east - west) * width


def lat_to_y(lat: float, south: float, north: float, height: int) -> float:
    return (north - lat) / (north - south) * height


def x_to_lon(x: float, west: float, east: float, width: int) -> float:
    return west + (x / width) * (east - west)


def y_to_lat(y: float, south: float, north: float, height: int) -> float:
    return north - (y / height) * (north - south)


def extract_item_id_from_quicklook(path: Path) -> str:
    stem = path.stem.strip()
    for suffix in ("_quicklook", "_thumbnail", "_preview", "_overview"):
        idx = stem.lower().find(suffix)
        if idx > 0:
            return stem[:idx]
    return stem


def safe_filename_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def get_cdse_token(username: str, password: str) -> str:
    username = username.strip() or os.environ.get("CDSE_USERNAME", "").strip()
    password = password.strip() or os.environ.get("CDSE_PASSWORD", "").strip()
    env_token = os.environ.get("CDSE_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    if not username or not password:
        raise ValueError(
            "TCI_60m download requires Copernicus Data Space authentication. "
            "Enter CDSE username/password in the GUI, or set CDSE_USERNAME and CDSE_PASSWORD."
        )

    resp = requests.post(
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
        data={
            "client_id": "cdse-public",
            "grant_type": "password",
            "username": username,
            "password": password,
        },
        timeout=60,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise ValueError(f"Failed to get CDSE access token: HTTP {resp.status_code} {resp.text[:300]}") from exc

    token = resp.json().get("access_token")
    if not token:
        raise ValueError("CDSE token response did not contain access_token.")
    return token


def odata_collection_name(collection: str) -> str:
    value = collection.strip()
    if value.lower() in {"sentinel-2-l2a", "sentinel-2", "s2"}:
        return "SENTINEL-2"
    return value or "SENTINEL-2"


def tci_target_suffix(asset_key: str) -> str:
    key = (asset_key.strip() or "TCI_60m").strip("*")
    if key.lower().endswith(".jp2"):
        return key if key.startswith("_") else f"_{key}"
    return f"_{key}.jp2"


def product_name_from_item_id(item_id: str) -> str:
    return item_id if item_id.endswith(".SAFE") else f"{item_id}.SAFE"


def escape_odata_string(value: str) -> str:
    return value.replace("'", "''")


def get_product_by_name(*, product_name: str, catalogue_url: str, collection: str) -> dict:
    url = f"{catalogue_url.rstrip('/')}/Products"
    collection_name = odata_collection_name(collection)
    params = {
        "$filter": (
            f"Name eq '{escape_odata_string(product_name)}' "
            f"and Collection/Name eq '{escape_odata_string(collection_name)}'"
        ),
        "$select": "Id,Name,S3Path,Online,ContentLength",
        "$top": "1",
    }
    resp = requests.get(url, params=params, timeout=60)
    resp.raise_for_status()
    products = resp.json().get("value", [])
    if not products:
        raise ValueError(f"Product not found in OData catalogue: {product_name}")
    return products[0]


def node_url(download_url: str, product_id: str, node_path: List[str], *, value: bool = False) -> str:
    url = f"{download_url.rstrip('/')}/Products({product_id})"
    for name in node_path:
        url += f"/Nodes({quote(name, safe='')})"
    return f"{url}/$value" if value else f"{url}/Nodes"


def request_with_auth_redirects(
    session: requests.Session,
    url: str,
    *,
    stream: bool = False,
    timeout: int = 300,
) -> requests.Response:
    for _ in range(10):
        resp = session.get(url, allow_redirects=False, stream=stream, timeout=timeout)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                resp.raise_for_status()
            url = urljoin(url, location)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("Too many redirects while downloading from OData Nodes.")


def list_nodes(
    session: requests.Session,
    *,
    download_url: str,
    product_id: str,
    node_path: List[str],
) -> List[dict]:
    resp = request_with_auth_redirects(
        session,
        node_url(download_url, product_id, node_path, value=False),
    )
    data = resp.json()
    return data.get("result", data.get("value", []))


def find_files_recursive(
    session: requests.Session,
    *,
    download_url: str,
    product_id: str,
    node_path: List[str],
    target_suffix: str,
) -> List[List[str]]:
    found: List[List[str]] = []
    for node in list_nodes(session, download_url=download_url, product_id=product_id, node_path=node_path):
        name = node.get("Name")
        if not name:
            continue

        child_path = node_path + [name]
        if name.endswith(target_suffix):
            found.append(child_path)
            continue

        children_number = int(node.get("ChildrenNumber") or 0)
        if children_number > 0:
            found.extend(
                find_files_recursive(
                    session,
                    download_url=download_url,
                    product_id=product_id,
                    node_path=child_path,
                    target_suffix=target_suffix,
                )
            )
    return found


def download_node_file(
    session: requests.Session,
    *,
    download_url: str,
    product_id: str,
    file_node_path: List[str],
    out_path: Path,
) -> Path:
    resp = request_with_auth_redirects(
        session,
        node_url(download_url, product_id, file_node_path, value=True),
        stream=True,
    )
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    return out_path


def download_tci_60m(
    *,
    item_id: str,
    out_dir: Path,
    catalogue_url: str,
    collection: str,
    asset_key: str,
    cdse_username: str,
    cdse_password: str,
    log: LogFn,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    product_name = product_name_from_item_id(item_id)
    target_suffix = tci_target_suffix(asset_key)
    out_path = out_dir / f"{item_id}_{safe_filename_part(target_suffix.lstrip('_'))}"
    if out_path.exists() and out_path.stat().st_size > 0:
        log(f"Using existing TCI file: {out_path}")
        return out_path

    log(f"OData product name: {product_name}")
    product = get_product_by_name(
        product_name=product_name,
        catalogue_url=catalogue_url,
        collection=collection,
    )
    product_id = product["Id"]
    log(f"OData Product Id: {product_id}")
    log(f"S3Path: {product.get('S3Path', '')}")

    log("Requesting CDSE access token for OData Nodes download...")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {get_cdse_token(cdse_username, cdse_password)}"})

    root_nodes = list_nodes(
        session,
        download_url=DEFAULT_ODATA_DOWNLOAD_URL,
        product_id=product_id,
        node_path=[],
    )
    if not root_nodes:
        raise ValueError(f"No OData root nodes found for product: {product_name}")

    safe_node = next((n.get("Name") for n in root_nodes if n.get("Name") == product_name), None)
    safe_node = safe_node or root_nodes[0].get("Name")
    if not safe_node:
        raise ValueError(f"OData root node has no Name for product: {product_name}")

    log(f"Searching SAFE nodes for *{target_suffix} ...")
    matched_files = find_files_recursive(
        session,
        download_url=DEFAULT_ODATA_DOWNLOAD_URL,
        product_id=product_id,
        node_path=[safe_node],
        target_suffix=target_suffix,
    )
    if not matched_files:
        raise ValueError(f"No file ending with {target_suffix!r} found in product nodes.")

    selected_file = matched_files[0]
    log(f"Downloading OData node: {' / '.join(selected_file)}")
    download_node_file(
        session,
        download_url=DEFAULT_ODATA_DOWNLOAD_URL,
        product_id=product_id,
        file_node_path=selected_file,
        out_path=out_path,
    )
    log(f"Saved TCI: {out_path}")
    return out_path


def homography_prediction(
    *,
    quicklook_shape: Tuple[int, int],
    footprint_geom,
    roi_polygon: Polygon,
) -> Tuple[Polygon, Tuple[int, int, int, int]]:
    """Project ROI through homography, then use axis-aligned bounding box on the quicklook (外接矩形)."""
    h_img, w_img = quicklook_shape
    footprint_coords = unique_coords(get_exterior_coords(footprint_geom))
    footprint_pts = order_footprint_corners(footprint_coords)

    img_pts = np.array(
        [[0, 0], [w_img, 0], [w_img, h_img], [0, h_img]],
        dtype=np.float32,
    )
    matrix, _status = cv2.findHomography(footprint_pts, img_pts)
    if matrix is None:
        raise RuntimeError("Failed to compute homography matrix.")

    roi_pts_geo = np.array(get_exterior_coords(roi_polygon), dtype=np.float32)
    roi_pts_img = cv2.perspectiveTransform(roi_pts_geo.reshape(-1, 1, 2), matrix).reshape(-1, 2)

    xs = roi_pts_img[:, 0]
    ys = roi_pts_img[:, 1]
    left = clip_int(float(xs.min()), 0, w_img)
    right = clip_int(float(xs.max()), 0, w_img)
    top = clip_int(float(ys.min()), 0, h_img)
    bottom = clip_int(float(ys.max()), 0, h_img)
    if left >= right or top >= bottom:
        raise ValueError("Invalid homography AABB; footprint and ROI may not match the quicklook.")

    inv_matrix = np.linalg.inv(matrix)
    corners_px = np.array(
        [[left, top], [right, top], [right, bottom], [left, bottom]],
        dtype=np.float32,
    ).reshape(-1, 1, 2)
    corners_geo = cv2.perspectiveTransform(corners_px, inv_matrix).reshape(-1, 2)
    pred_geom = Polygon([(float(x), float(y)) for x, y in corners_geo])
    if not pred_geom.is_valid:
        pred_geom = pred_geom.buffer(0)

    bbox_pixels = (left, top, right, bottom)
    return pred_geom, bbox_pixels


def bbox_prediction(
    *,
    quicklook_shape: Tuple[int, int],
    footprint_geom,
    roi_geom,
) -> Tuple[Polygon, Tuple[int, int, int, int]]:
    """Linear lon/lat to pixel mapping using axis-aligned bounds (quicklookclip.py)."""
    h_img, w_img = quicklook_shape
    west, south, east, north = footprint_geom.bounds
    minx, miny, maxx, maxy = roi_geom.bounds

    x1 = clip_int(lon_to_x(minx, west, east, w_img), 0, w_img)
    x2 = clip_int(lon_to_x(maxx, west, east, w_img), 0, w_img)
    y1 = clip_int(lat_to_y(maxy, south, north, h_img), 0, h_img)
    y2 = clip_int(lat_to_y(miny, south, north, h_img), 0, h_img)

    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    if left >= right or top >= bottom:
        raise ValueError("Invalid bbox prediction; footprint and ROI may not overlap.")

    pred_west = x_to_lon(left, west, east, w_img)
    pred_east = x_to_lon(right, west, east, w_img)
    pred_north = y_to_lat(top, south, north, h_img)
    pred_south = y_to_lat(bottom, south, north, h_img)
    return box(pred_west, pred_south, pred_east, pred_north), (left, top, right, bottom)


def read_tci_rgb(path: Path):
    if rasterio is None:
        raise ImportError("rasterio is required to read the georeferenced TCI_60m image.")

    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError(f"TCI image has no CRS: {path}")
        bands = [1, 2, 3] if src.count >= 3 else [1]
        data = src.read(bands)
        profile = {
            "crs": src.crs,
            "transform": src.transform,
            "height": src.height,
            "width": src.width,
        }

    if data.shape[0] == 1:
        data = np.repeat(data, 3, axis=0)

    rgb = np.transpose(data[:3], (1, 2, 0)).astype(np.float32)
    if rgb.max() > 255:
        rgb = np.clip(rgb, 0, np.percentile(rgb, 99))
        max_value = float(rgb.max()) or 1.0
        rgb = rgb / max_value * 255.0
    return np.clip(rgb, 0, 255).astype(np.uint8), profile


def geometry_to_mask(geom_wgs84, raster_profile: dict) -> np.ndarray:
    if geometry_mask is None:
        raise ImportError("rasterio is required to rasterize ROI masks.")

    raster_crs = CRS.from_user_input(raster_profile["crs"])
    geom_projected = _transform_crs(geom_wgs84, CRS.from_epsg(4326), raster_crs)
    return geometry_mask(
        [mapping(geom_projected)],
        transform=raster_profile["transform"],
        invert=True,
        out_shape=(raster_profile["height"], raster_profile["width"]),
        all_touched=True,
    )


def quicklook_bbox_overlay_rgb(
    quicklook_bgr: np.ndarray,
    bbox_pixels: Tuple[int, int, int, int],
    color_rgb: np.ndarray,
    thickness: int,
) -> np.ndarray:
    """RGB image (quicklook background + rectangle). thickness=cv2.FILLED for solid fill."""
    img = cv2.cvtColor(quicklook_bgr, cv2.COLOR_BGR2RGB).copy()
    left, top, right, bottom = bbox_pixels
    cv2.rectangle(
        img,
        (left, top),
        (right, bottom),
        color=tuple(int(c) for c in color_rgb),
        thickness=thickness,
        lineType=cv2.LINE_8,
    )
    return img


def _resize_bgr_to_hw(bgr: np.ndarray, height: int, width: int) -> np.ndarray:
    h, w = bgr.shape[:2]
    if (h, w) == (height, width):
        return bgr
    return cv2.resize(bgr, (width, height), interpolation=cv2.INTER_NEAREST)


def get_mask_area_metrics(img_bgr: np.ndarray, color_bgr: np.ndarray, tol: int = 10) -> np.ndarray:
    """Same as area.py get_mask: inRange on BGR image."""
    lower = np.clip(color_bgr.astype(np.int16) - tol, 0, 255).astype(np.uint8)
    upper = np.clip(color_bgr.astype(np.int16) + tol, 0, 255).astype(np.uint8)
    return cv2.inRange(img_bgr, lower, upper) > 0


def coverage_area_metrics(gt: np.ndarray, pred: np.ndarray) -> float:
    gt_sum = int(gt.sum())
    if gt_sum == 0:
        return 0.0
    return float(np.logical_and(gt, pred).sum() / gt_sum)


def precision_area_metrics(gt: np.ndarray, pred: np.ndarray) -> float:
    pred_sum = int(pred.sum())
    if pred_sum == 0:
        return 0.0
    return float(np.logical_and(gt, pred).sum() / pred_sum)


def iou_area_metrics(gt: np.ndarray, pred: np.ndarray) -> float:
    inter = float(np.logical_and(gt, pred).sum())
    union = float(np.logical_or(gt, pred).sum())
    if union == 0:
        return 0.0
    return inter / union


def blend_rgb_layers(base_rgb: np.ndarray, over_rgb: np.ndarray, alpha: float) -> np.ndarray:
    b = base_rgb.astype(np.float64)
    o = over_rgb.astype(np.float64)
    return np.clip((1.0 - alpha) * b + alpha * o, 0, 255).astype(np.uint8)


def upscale_rgb_to_hw(
    img_rgb: np.ndarray, height: int, width: int, interpolation: int
) -> np.ndarray:
    """Resize RGB image to (height, width); no-op copy if already that size."""
    if img_rgb.shape[0] == height and img_rgb.shape[1] == width:
        return img_rgb.copy()
    return cv2.resize(img_rgb, (width, height), interpolation=interpolation)


def write_filled_roi_metrics_txt(
    *,
    out_path: Path,
    tci_roi_bgr: np.ndarray,
    clip_filled_bgr: np.ndarray,
    homo_filled_bgr: np.ndarray,
    height: int,
    width: int,
) -> None:
    """Coverage / precision / IoU on the TCI pixel grid (GT = full-res tci_roi BGR)."""
    tci_b = _resize_bgr_to_hw(tci_roi_bgr, height, width)
    clip_b = _resize_bgr_to_hw(clip_filled_bgr, height, width)
    homo_b = _resize_bgr_to_hw(homo_filled_bgr, height, width)
    gt_m = get_mask_area_metrics(tci_b, AREA_METRICS_BGR_GREEN)
    clip_m = get_mask_area_metrics(clip_b, AREA_METRICS_BGR_YELLOW)
    homo_m = get_mask_area_metrics(homo_b, AREA_METRICS_BGR_RED)

    cov_q = coverage_area_metrics(gt_m, clip_m)
    prec_q = precision_area_metrics(gt_m, clip_m)
    iou_q = iou_area_metrics(gt_m, clip_m)
    cov_h = coverage_area_metrics(gt_m, homo_m)
    prec_h = precision_area_metrics(gt_m, homo_m)
    iou_h = iou_area_metrics(gt_m, homo_m)

    lines = [

        "ground truth:green",
        "chomography:red ",
        "rectangle:yellow ",
        "",
        "=== homography vs ground truth ===",
        f"Coverage : {cov_h:.6f}",
        f"Precision: {prec_h:.6f}",
        f"IoU      : {iou_h:.6f}",
        "",
        "=== rectangle vs ground truth ===",
        f"Coverage : {cov_q:.6f}",
        f"Precision: {prec_q:.6f}",
        f"IoU      : {iou_q:.6f}",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def run_area_job(
    *,
    quicklook_path: Path,
    footprint_path: Path,
    roi_path: Path,
    out_dir: Path,
    out_basename: str,
    catalog_url: str,
    collection: str,
    tci_asset_key: str,
    cdse_username: str,
    cdse_password: str,
    log: LogFn,
) -> None:
    if not quicklook_path.is_file():
        raise FileNotFoundError(f"Quicklook image not found: {quicklook_path}")
    if not footprint_path.is_file():
        raise FileNotFoundError(f"Footprint GeoJSON not found: {footprint_path}")
    if not roi_path.is_file():
        raise FileNotFoundError(f"ROI GeoJSON not found: {roi_path}")
    if not out_basename.strip():
        raise ValueError("Output base filename is required.")

    if rasterio is None:
        raise ImportError("rasterio is not installed. Install rasterio before running this GUI.")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_prefix = out_dir / out_basename.strip()
    out_quicklookclip = out_prefix.with_name(out_prefix.name + "_quicklookclip.png")
    out_cv2homograph = out_prefix.with_name(out_prefix.name + "_cv2homograph.png")
    out_quicklookclip_filled = out_prefix.with_name(out_prefix.name + "_quicklookclip_filled.png")
    out_cv2homograph_filled = out_prefix.with_name(out_prefix.name + "_cv2homograph_filled.png")
    out_tci_roi = out_prefix.with_name(out_prefix.name + "_tci_roi.png")
    out_composite = out_prefix.with_name(out_prefix.name + "_composite.png")
    out_filled_roi_metrics = out_prefix.with_name(out_prefix.name + "_filled_roi_metrics.txt")

    log(f"Quicklook: {quicklook_path}")
    log(f"Footprint: {footprint_path}")
    log(f"ROI: {roi_path}")
    log(
        "After run, only <name>_composite.png and <name>_filled_roi_metrics.txt are kept "
        "(intermediate PNGs are removed)."
    )

    quicklook = cv2.imread(str(quicklook_path))
    if quicklook is None:
        raise FileNotFoundError(f"Failed to read quicklook image: {quicklook_path}")
    h_img, w_img = quicklook.shape[:2]

    footprint_union = read_geojson_union(footprint_path)
    roi_union = read_geojson_union(roi_path)
    footprint_for_homography = read_geojson_first_geometry(footprint_path)
    roi_polygon_homography = polygon_from_geometry(roi_union)

    _, homography_bbox_pixels = homography_prediction(
        quicklook_shape=(h_img, w_img),
        footprint_geom=footprint_for_homography,
        roi_polygon=roi_polygon_homography,
    )
    _, bbox_pixels = bbox_prediction(
        quicklook_shape=(h_img, w_img),
        footprint_geom=footprint_union,
        roi_geom=roi_union,
    )

    item_id = extract_item_id_from_quicklook(quicklook_path)
    log(f"Product id from quicklook filename: {item_id}")
    tci_path = download_tci_60m(
        item_id=item_id,
        out_dir=out_dir,
        catalogue_url=catalog_url,
        collection=collection,
        asset_key=tci_asset_key,
        cdse_username=cdse_username,
        cdse_password=cdse_password,
        log=log,
    )

    tci_rgb, raster_profile = read_tci_rgb(tci_path)
    true_mask = geometry_to_mask(roi_union, raster_profile)
    h_tci, w_tci = int(tci_rgb.shape[0]), int(tci_rgb.shape[1])

    clip_outline_rgb = quicklook_bbox_overlay_rgb(quicklook, bbox_pixels, YELLOW, 1)
    homo_outline_rgb = quicklook_bbox_overlay_rgb(quicklook, homography_bbox_pixels, RED, 1)
    cv2.imwrite(str(out_quicklookclip), cv2.cvtColor(clip_outline_rgb, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(out_cv2homograph), cv2.cvtColor(homo_outline_rgb, cv2.COLOR_RGB2BGR))

    clip_filled_rgb = quicklook_bbox_overlay_rgb(quicklook, bbox_pixels, YELLOW, cv2.FILLED)
    homo_filled_rgb = quicklook_bbox_overlay_rgb(quicklook, homography_bbox_pixels, RED, cv2.FILLED)
    cv2.imwrite(str(out_quicklookclip_filled), cv2.cvtColor(clip_filled_rgb, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(out_cv2homograph_filled), cv2.cvtColor(homo_filled_rgb, cv2.COLOR_RGB2BGR))

    tci_roi_rgb = tci_rgb.copy()
    tci_roi_rgb[true_mask] = GREEN
    cv2.imwrite(str(out_tci_roi), cv2.cvtColor(tci_roi_rgb, cv2.COLOR_RGB2BGR))

    clip_outline_up = upscale_rgb_to_hw(
        clip_outline_rgb, h_tci, w_tci, UPSCALE_QUICKLOOK_OUTLINE_INTERP
    )
    homo_outline_up = upscale_rgb_to_hw(
        homo_outline_rgb, h_tci, w_tci, UPSCALE_QUICKLOOK_OUTLINE_INTERP
    )
    a = COMPOSITE_LAYER_ALPHA
    composite_rgb = blend_rgb_layers(
        blend_rgb_layers(tci_roi_rgb, clip_outline_up, a),
        homo_outline_up,
        a,
    )
    cv2.imwrite(str(out_composite), cv2.cvtColor(composite_rgb, cv2.COLOR_RGB2BGR))

    clip_filled_up = upscale_rgb_to_hw(
        clip_filled_rgb, h_tci, w_tci, UPSCALE_QUICKLOOK_FILLED_INTERP
    )
    homo_filled_up = upscale_rgb_to_hw(
        homo_filled_rgb, h_tci, w_tci, UPSCALE_QUICKLOOK_FILLED_INTERP
    )
    tci_roi_bgr = cv2.cvtColor(tci_roi_rgb, cv2.COLOR_RGB2BGR)
    clip_filled_bgr = cv2.cvtColor(clip_filled_up, cv2.COLOR_RGB2BGR)
    homo_filled_bgr = cv2.cvtColor(homo_filled_up, cv2.COLOR_RGB2BGR)
    write_filled_roi_metrics_txt(
        out_path=out_filled_roi_metrics,
        tci_roi_bgr=tci_roi_bgr,
        clip_filled_bgr=clip_filled_bgr,
        homo_filled_bgr=homo_filled_bgr,
        height=h_tci,
        width=w_tci,
    )

    for _intermediate in (
        out_quicklookclip,
        out_cv2homograph,
        out_quicklookclip_filled,
        out_cv2homograph_filled,
        out_tci_roi,
    ):
        if _intermediate.is_file():
            _intermediate.unlink()

    if tci_path.is_file():
        tci_path.unlink()
    tci_aux = tci_path.parent / (tci_path.name + ".aux.xml")
    if tci_aux.is_file():
        tci_aux.unlink()

    log(f"TCI canvas {w_tci}x{h_tci} (native); quicklook {w_img}x{h_img} layers upscaled for composite/metrics.")
    log(f"Saved composite: {out_composite}")
    log(f"Saved filled ROI metrics: {out_filled_roi_metrics}")
    log("Removed intermediate PNGs (quicklookclip, cv2homograph, *_filled, tci_roi).")
    log(f"Removed downloaded TCI: {tci_path}")
    log("Done.")


def make_ui() -> tk.Tk:
    root = tk.Tk()
    root.title("Comparison Statistics")
    root.minsize(900, 620)

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(7, weight=1)

    quicklook_var = tk.StringVar(value="")
    footprint_var = tk.StringVar(value="")
    roi_var = tk.StringVar(value="")
    out_dir_var = tk.StringVar(value="")
    cdse_username_var = tk.StringVar(value=os.environ.get("CDSE_USERNAME", ""))
    cdse_password_var = tk.StringVar(value="")

    def add_labeled_path(row: int, label: str, var: tk.StringVar, choose_fn) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Browse...", command=choose_fn).grid(
            row=row,
            column=2,
            sticky="ew",
            padx=(8, 0),
            pady=4,
        )

    def add_labeled_entry(row: int, label: str, var: tk.StringVar, *, show: str = "") -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var, show=show).grid(row=row, column=1, sticky="ew", pady=4)

    def choose_quicklook() -> None:
        path = filedialog.askopenfilename(
            title="Select quicklook image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.tif *.tiff *.webp"), ("All files", "*.*")],
        )
        if path:
            quicklook_var.set(path)

    def choose_footprint() -> None:
        path = filedialog.askopenfilename(
            title="Select quicklook footprint GeoJSON",
            filetypes=[("GeoJSON", "*.geojson *.json"), ("All files", "*.*")],
        )
        if path:
            footprint_var.set(path)

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

    add_labeled_path(0, "Quicklook image", quicklook_var, choose_quicklook)
    add_labeled_path(1, "Quicklook footprint", footprint_var, choose_footprint)
    add_labeled_path(2, "ROI GeoJSON", roi_var, choose_roi)
    add_labeled_path(3, "Output folder", out_dir_var, choose_out_dir)
    add_labeled_entry(4, "CDSE username", cdse_username_var)
    add_labeled_entry(5, "CDSE password", cdse_password_var, show="*")

    btn_run = ttk.Button(frm, text="Run")
    btn_run.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    log_box = tk.Text(frm, height=16, wrap="word")
    log_box.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(12, 0))

    def log(msg: str) -> None:
        def append() -> None:
            log_box.insert("end", msg + "\n")
            log_box.see("end")

        root.after(0, append)

    def on_run() -> None:
        try:
            quicklook_path = Path(quicklook_var.get().strip())
            footprint_path = Path(footprint_var.get().strip())
            roi_path = Path(roi_var.get().strip())
            out_dir = Path(out_dir_var.get().strip())
            out_basename = (extract_item_id_from_quicklook(quicklook_path) or quicklook_path.stem).strip() or "area_compare"
            catalog_url = FIXED_ODATA_CATALOGUE_URL
            collection = FIXED_COLLECTION
            tci_asset_key = FIXED_TCI_ASSET_KEY
            cdse_username = cdse_username_var.get().strip()
            cdse_password = cdse_password_var.get().strip()
            if not out_dir_var.get().strip():
                raise ValueError("Output folder is required.")
        except Exception as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        btn_run.config(state="disabled")
        log("Starting...")

        def worker() -> None:
            try:
                run_area_job(
                    quicklook_path=quicklook_path,
                    footprint_path=footprint_path,
                    roi_path=roi_path,
                    out_dir=out_dir,
                    out_basename=out_basename,
                    catalog_url=catalog_url,
                    collection=collection,
                    tci_asset_key=tci_asset_key,
                    cdse_username=cdse_username,
                    cdse_password=cdse_password,
                    log=log,
                )
            except Exception as exc:
                err_msg = str(exc)
                log(f"Failed: {err_msg}")
                root.after(0, lambda msg=err_msg: messagebox.showerror("Run failed", msg))
            finally:
                root.after(0, lambda: btn_run.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    btn_run.config(command=on_run)
    return root


def main() -> None:
    app = make_ui()
    app.mainloop()


if __name__ == "__main__":
    main()
