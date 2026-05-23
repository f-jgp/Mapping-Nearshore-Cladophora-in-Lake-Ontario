"""CDSE OData download of Sentinel-2 JP2 bands by resolution (R10m / R20m / R60m).

Run without arguments to open the GUI.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Callable, Iterable, List, Optional
from urllib.parse import quote, urljoin

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

LogFn = Callable[[str], None]

DEFAULT_ODATA_CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1"
DEFAULT_ODATA_DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1"
FIXED_COLLECTION = "SENTINEL-2"

VALID_RESOLUTIONS = ("R10m", "R20m", "R60m")
VALID_BANDS = (
    "ALL",
    "B01",
    "B02",
    "B03",
    "B04",
    "B05",
    "B06",
    "B07",
    "B08",
    "B8A",
    "B09",
    "B10",
    "B11",
    "B12",
    "TCI",
    "SCL",
    "AOT",
    "WVP",
)

# Sentinel-2 L1C/L2A product id (e.g. S2B_MSIL2A_20230804T160829_N0510_R140_T17TPH_20241026T132733)
SENTINEL2_PRODUCT_ID_RE = re.compile(
    r"(S2[AB]_MSIL[12][AC]_\d{8}T\d{6}_N\d{4}_R\d{3}_T\w+_\d{8}T\d{6})",
    re.IGNORECASE,
)


def extract_sentinel2_product_id(path: Path | str) -> str:
    """Extract Sentinel-2 product id from any filename or path string."""
    text = Path(path).name if isinstance(path, (str, Path)) else str(path)
    stem = Path(text).stem
    match = SENTINEL2_PRODUCT_ID_RE.search(stem) or SENTINEL2_PRODUCT_ID_RE.search(text)
    if not match:
        raise ValueError(
            f"No Sentinel-2 product id found in filename: {text!r}. "
            "Expected pattern like S2B_MSIL2A_..._T17TPH_..."
        )
    return match.group(1).upper()


def normalize_resolution(resolution: str) -> str:
    value = resolution.strip()
    aliases = {
        "r10": "R10m",
        "r10m": "R10m",
        "10m": "R10m",
        "r20": "R20m",
        "r20m": "R20m",
        "20m": "R20m",
        "r60": "R60m",
        "r60m": "R60m",
        "60m": "R60m",
    }
    key = value.lower()
    if key in aliases:
        return aliases[key]
    if value in VALID_RESOLUTIONS:
        return value
    raise ValueError(f"Unsupported resolution {resolution!r}. Use one of: {', '.join(VALID_RESOLUTIONS)}")


def normalize_band_name(band_name: str) -> str:
    value = band_name.strip().upper()
    if value == "B8A":
        return value
    if value == "ALL":
        return value
    if value in VALID_BANDS:
        return value
    raise ValueError(f"Unsupported band {band_name!r}. Use ALL or one of: {', '.join(VALID_BANDS[1:])}")


def normalize_band_names(band_names: Optional[Iterable[str]]) -> Optional[List[str]]:
    if band_names is None:
        return None

    normalized = [normalize_band_name(band_name) for band_name in band_names if band_name.strip()]
    if not normalized or "ALL" in normalized:
        return None

    out: List[str] = []
    for band_name in normalized:
        if band_name not in out:
            out.append(band_name)
    return out


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
            "CDSE download requires authentication. "
            "Enter username/password or set CDSE_USERNAME and CDSE_PASSWORD."
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
        raise ValueError(
            f"Failed to get CDSE access token: HTTP {resp.status_code} {resp.text[:300]}"
        ) from exc

    token = resp.json().get("access_token")
    if not token:
        raise ValueError("CDSE token response did not contain access_token.")
    return token


def odata_collection_name(collection: str) -> str:
    value = collection.strip()
    if value.lower() in {"sentinel-2-l2a", "sentinel-2", "s2"}:
        return "SENTINEL-2"
    return value or "SENTINEL-2"


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


def find_jp2_files_recursive(
    session: requests.Session,
    *,
    download_url: str,
    product_id: str,
    node_path: List[str],
) -> List[List[str]]:
    """Return node paths for all .jp2 files under node_path."""
    found: List[List[str]] = []
    for node in list_nodes(session, download_url=download_url, product_id=product_id, node_path=node_path):
        name = node.get("Name")
        if not name:
            continue

        child_path = node_path + [name]
        if name.lower().endswith(".jp2"):
            found.append(child_path)
            continue

        children_number = int(node.get("ChildrenNumber") or 0)
        if children_number > 0:
            found.extend(
                find_jp2_files_recursive(
                    session,
                    download_url=download_url,
                    product_id=product_id,
                    node_path=child_path,
                )
            )
    return found


def find_jp2_files_by_resolution(
    session: requests.Session,
    *,
    download_url: str,
    product_id: str,
    safe_root: str,
    resolution: str,
    band_names: Optional[Iterable[str]] = None,
) -> List[List[str]]:
    """JP2 files whose OData node path contains a folder named R10m / R20m / R60m."""
    resolution = normalize_resolution(resolution)
    selected_bands = normalize_band_names(band_names)
    all_jp2 = find_jp2_files_recursive(
        session,
        download_url=download_url,
        product_id=product_id,
        node_path=[safe_root],
    )
    matched: List[List[str]] = []
    for node_path in all_jp2:
        segments = [p for p in node_path]
        if resolution in segments and jp2_node_matches_bands(node_path, selected_bands):
            matched.append(node_path)
    return matched


def jp2_node_matches_bands(file_node_path: List[str], band_names: Optional[Iterable[str]]) -> bool:
    selected_bands = normalize_band_names(band_names)
    if selected_bands is None:
        return True

    filename = file_node_path[-1].upper()
    return any(re.search(rf"(^|_){re.escape(band_name)}(_|\.)", filename) for band_name in selected_bands)


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


def resolution_out_dir(out_dir: Path, resolution: str) -> Path:
    """Return (and create) the resolution subfolder under the download root."""
    path = out_dir / normalize_resolution(resolution)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_resolution_dirs(out_dir: Path) -> None:
    """Create R10m, R20m, and R60m folders under the download root."""
    for resolution in VALID_RESOLUTIONS:
        resolution_out_dir(out_dir, resolution)


def output_path_for_node(
    *,
    out_dir: Path,
    product_id: str,
    file_node_path: List[str],
) -> Path:
    original_name = file_node_path[-1]
    prefix = safe_filename_part(product_id)
    out_name = f"{prefix}_{original_name}"
    return out_dir / out_name


def download_resolution_jp2_files(
    *,
    item_id: str,
    resolution: str,
    out_dir: Path,
    catalogue_url: str = DEFAULT_ODATA_CATALOGUE_URL,
    download_url: str = DEFAULT_ODATA_DOWNLOAD_URL,
    collection: str = FIXED_COLLECTION,
    cdse_username: str = "",
    cdse_password: str = "",
    band_names: Optional[Iterable[str]] = None,
    log: LogFn,
) -> List[Path]:
    """Download selected JP2 bands into out_dir/<resolution>/ for one Sentinel-2 product."""
    resolution = normalize_resolution(resolution)
    selected_bands = normalize_band_names(band_names)
    resolution_dir = resolution_out_dir(out_dir, resolution)
    product_name = product_name_from_item_id(item_id.strip())

    log(f"Product id: {item_id}")
    log(f"Resolution: {resolution}")
    log(f"Bands: {'ALL' if selected_bands is None else ', '.join(selected_bands)}")
    log(f"OData product name: {product_name}")

    product = get_product_by_name(
        product_name=product_name,
        catalogue_url=catalogue_url,
        collection=collection,
    )
    product_id = product["Id"]
    log(f"OData Product Id: {product_id}")
    log(f"S3Path: {product.get('S3Path', '')}")

    log("Requesting CDSE access token...")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {get_cdse_token(cdse_username, cdse_password)}"})

    root_nodes = list_nodes(
        session,
        download_url=download_url,
        product_id=product_id,
        node_path=[],
    )
    if not root_nodes:
        raise ValueError(f"No OData root nodes found for product: {product_name}")

    safe_node = next((n.get("Name") for n in root_nodes if n.get("Name") == product_name), None)
    safe_node = safe_node or root_nodes[0].get("Name")
    if not safe_node:
        raise ValueError(f"OData root node has no Name for product: {product_name}")

    log(f"Searching {resolution} for .jp2 files...")
    matched_files = find_jp2_files_by_resolution(
        session,
        download_url=download_url,
        product_id=product_id,
        safe_root=safe_node,
        resolution=resolution,
        band_names=selected_bands,
    )
    if not matched_files:
        band_desc = "all bands" if selected_bands is None else ", ".join(selected_bands)
        raise ValueError(f"No .jp2 files found under {resolution!r} for {band_desc} in product: {product_name}")

    log(f"Found {len(matched_files)} file(s).")
    saved: List[Path] = []
    for file_node_path in matched_files:
        out_path = output_path_for_node(
            out_dir=resolution_dir,
            product_id=item_id.strip(),
            file_node_path=file_node_path,
        )
        if out_path.exists() and out_path.stat().st_size > 0:
            log(f"Using existing: {out_path.name}")
            saved.append(out_path)
            continue

        log(f"Downloading: {' / '.join(file_node_path)}")
        download_node_file(
            session,
            download_url=download_url,
            product_id=product_id,
            file_node_path=file_node_path,
            out_path=out_path,
        )
        log(f"Saved: {out_path}")
        saved.append(out_path)

    return saved


def download_product_jp2_files(
    *,
    item_id: str,
    out_dir: Path,
    resolutions: Optional[Iterable[str]] = None,
    catalogue_url: str = DEFAULT_ODATA_CATALOGUE_URL,
    download_url: str = DEFAULT_ODATA_DOWNLOAD_URL,
    collection: str = FIXED_COLLECTION,
    cdse_username: str = "",
    cdse_password: str = "",
    band_names: Optional[Iterable[str]] = None,
    log: LogFn,
) -> dict[str, List[Path]]:
    """Download JP2 bands into R10m / R20m / R60m subfolders (default: all resolutions, all bands)."""
    if resolutions is None:
        resolution_list = list(VALID_RESOLUTIONS)
    else:
        resolution_list = []
        for resolution in resolutions:
            normalized = normalize_resolution(resolution)
            if normalized not in resolution_list:
                resolution_list.append(normalized)

    out_dir.mkdir(parents=True, exist_ok=True)
    ensure_resolution_dirs(out_dir)

    saved_by_resolution: dict[str, List[Path]] = {}
    for resolution in resolution_list:
        log(f"=== Resolution {resolution} ===")
        saved_by_resolution[resolution] = download_resolution_jp2_files(
            item_id=item_id,
            resolution=resolution,
            out_dir=out_dir,
            catalogue_url=catalogue_url,
            download_url=download_url,
            collection=collection,
            cdse_username=cdse_username,
            cdse_password=cdse_password,
            band_names=band_names,
            log=log,
        )
    return saved_by_resolution


def run_download_job(
    *,
    input_path: Path,
    out_dir: Path,
    all_resolutions: bool,
    resolution: str,
    band_names,
    cdse_username: str,
    cdse_password: str,
    log: LogFn,
) -> None:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    product_id = extract_sentinel2_product_id(input_path)
    log(f"Extracted product id from {input_path.name}: {product_id}")

    if all_resolutions:
        saved_by_resolution = download_product_jp2_files(
            item_id=product_id,
            out_dir=out_dir,
            band_names=band_names,
            cdse_username=cdse_username,
            cdse_password=cdse_password,
            log=log,
        )
        total = sum(len(paths) for paths in saved_by_resolution.values())
        log(f"Done. {total} file(s) under {out_dir} (R10m / R20m / R60m)")
    else:
        saved = download_resolution_jp2_files(
            item_id=product_id,
            resolution=resolution,
            out_dir=out_dir,
            band_names=band_names,
            cdse_username=cdse_username,
            cdse_password=cdse_password,
            log=log,
        )
        log(f"Done. {len(saved)} file(s) in {out_dir / resolution}")


def make_ui() -> tk.Tk:
    root = tk.Tk()
    root.title("Sentinel Band Downloader")
    root.minsize(820, 520)

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(9, weight=1)

    input_var = tk.StringVar(value="")
    out_dir_var = tk.StringVar(value="")
    resolution_var = tk.StringVar(value=VALID_RESOLUTIONS[0])
    all_resolutions_var = tk.BooleanVar(value=True)
    all_bands_var = tk.BooleanVar(value=True)
    cdse_username_var = tk.StringVar(value=os.environ.get("CDSE_USERNAME", ""))
    cdse_password_var = tk.StringVar(value="")

    def add_labeled_path(row: int, label: str, var: tk.StringVar, choose_fn) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Browse...", command=choose_fn).grid(
            row=row, column=2, padx=(8, 0), pady=4
        )

    def choose_input() -> None:
        path = filedialog.askopenfilename(
            title="Select input file",
            filetypes=[("All files", "*.*")],
        )
        if path:
            input_var.set(path)

    def choose_out_dir() -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            out_dir_var.set(path)

    add_labeled_path(0, "Input file", input_var, choose_input)
    add_labeled_path(1, "Output folder", out_dir_var, choose_out_dir)

    ttk.Label(frm, text="Resolution").grid(row=2, column=0, sticky="nw", pady=4)
    res_frame = ttk.Frame(frm)
    res_frame.grid(row=2, column=1, sticky="w", pady=4)

    all_resolutions_check = ttk.Checkbutton(
        res_frame,
        text="Download all resolutions (R10m / R20m / R60m)",
        variable=all_resolutions_var,
    )
    all_resolutions_check.grid(row=0, column=0, sticky="w")

    res_combo = ttk.Combobox(
        res_frame,
        textvariable=resolution_var,
        values=list(VALID_RESOLUTIONS),
        state="readonly",
        width=12,
    )
    res_combo.grid(row=1, column=0, sticky="w", pady=(4, 0))

    def sync_resolution_controls(*_args) -> None:
        if all_resolutions_var.get():
            res_combo.config(state="disabled")
        else:
            res_combo.config(state="readonly")

    all_resolutions_var.trace_add("write", sync_resolution_controls)
    sync_resolution_controls()

    ttk.Label(frm, text="Bands").grid(row=3, column=0, sticky="nw", pady=4)
    band_frame = ttk.Frame(frm)
    band_frame.grid(row=3, column=1, sticky="ew", pady=4)
    band_frame.columnconfigure(0, weight=1)

    all_bands_check = ttk.Checkbutton(
        band_frame,
        text="Download all bands",
        variable=all_bands_var,
    )
    all_bands_check.grid(row=0, column=0, sticky="w")

    band_listbox = tk.Listbox(
        band_frame,
        height=6,
        selectmode="extended",
        exportselection=False,
    )
    for band_name in VALID_BANDS[1:]:
        band_listbox.insert("end", band_name)
    band_listbox.grid(row=1, column=0, sticky="ew", pady=(4, 0))

    ttk.Label(frm, text="CDSE username").grid(row=4, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=cdse_username_var).grid(row=4, column=1, sticky="ew", pady=4)

    ttk.Label(frm, text="CDSE password").grid(row=5, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=cdse_password_var, show="*").grid(row=5, column=1, sticky="ew", pady=4)

    log_box = tk.Text(frm, height=14, wrap="word")
    log_box.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(12, 0))

    def log(msg: str) -> None:
        log_box.insert("end", msg + "\n")
        log_box.see("end")

    btn_run = ttk.Button(frm, text="Run")
    btn_run.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    def on_run() -> None:
        try:
            input_s = input_var.get().strip()
            if not input_s:
                raise ValueError("Input file is required.")
            input_path = Path(input_s)

            out_s = out_dir_var.get().strip()
            if not out_s:
                raise ValueError("Output folder is required.")
            out_dir = Path(out_s)

            all_resolutions = all_resolutions_var.get()
            resolution = resolution_var.get().strip()
            if not all_resolutions and resolution not in VALID_RESOLUTIONS:
                raise ValueError(f"Resolution must be one of: {', '.join(VALID_RESOLUTIONS)}")

            if all_bands_var.get():
                band_names = None
            else:
                selected = band_listbox.curselection()
                if not selected:
                    raise ValueError("Select at least one band, or enable Download all bands.")
                band_names = [band_listbox.get(i) for i in selected]

            cdse_username = cdse_username_var.get().strip()
            cdse_password = cdse_password_var.get().strip()
        except Exception as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        btn_run.config(state="disabled")
        log("Starting...")

        def worker() -> None:
            try:
                run_download_job(
                    input_path=input_path,
                    out_dir=out_dir,
                    all_resolutions=all_resolutions,
                    resolution=resolution,
                    band_names=band_names,
                    cdse_username=cdse_username,
                    cdse_password=cdse_password,
                    log=log,
                )
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
