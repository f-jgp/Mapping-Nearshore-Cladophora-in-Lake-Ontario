"""GUI for HSV cloud detection (same rules as cloud.py: V > 0.75 and S < 0.20 by default)."""

from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional, Tuple

import cv2
import numpy as np

# HSV cloud rules (same as cloud.py default); edit constants here to tune.
V_MIN = 0.75
S_MAX = 0.20


def compute_cloud_mask(
    img_bgr: np.ndarray,
    *,
    v_min: float = V_MIN,
    s_max: float = S_MAX,
) -> Tuple[np.ndarray, float]:
    """HSV (OpenCV): S,V scaled to 0..1. Cloud: high V and low S (vectorized)."""
    if img_bgr.ndim != 3 or img_bgr.shape[2] != 3:
        raise ValueError("Image must be BGR with 3 channels.")

    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1].astype(np.float32) / 255.0
    v = hsv[:, :, 2].astype(np.float32) / 255.0

    cloud_mask = ((v > v_min) & (s < s_max)).astype(np.uint8) * 255
    h, w = cloud_mask.shape[:2]
    ratio = float(np.count_nonzero(cloud_mask)) / float(h * w)
    return cloud_mask, ratio


def run_cloud_job(
    *,
    image_path: Path,
    output_dir: Optional[Path],
    log,
) -> None:
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"Failed to read image: {image_path}")

    log(f"Image: {image_path} ({img.shape[1]}×{img.shape[0]})")
    log(f"Rules: V > {V_MIN} and S < {S_MAX} (normalized 0~1)")

    mask, ratio = compute_cloud_mask(img)
    log(f"Cloud ratio: {ratio:.6f} ({ratio * 100:.4f} %)")

    if output_dir is not None:
        stem = image_path.stem.strip() or "output"
        mask_path = output_dir / f"{stem}_cloud_mask.png"
        coverage_path = output_dir / f"{stem}_cloud_coverage.txt"
        output_dir.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(mask_path), mask):
            raise RuntimeError(f"Failed to write mask: {mask_path}")
        log(f"Mask saved: {mask_path}")
        lines = [
            f"cloud_percent: {ratio * 100:.4f}",
        ]
        coverage_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"Coverage saved: {coverage_path}")

    log("Done.")


def make_ui() -> tk.Tk:
    root = tk.Tk()
    root.title("Cloud Rate Calculator")
    root.minsize(720, 480)

    frm = ttk.Frame(root, padding=12)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    frm.rowconfigure(3, weight=1)

    image_var = tk.StringVar(value="")
    out_dir_var = tk.StringVar(value="")

    def add_labeled_path(row: int, label: str, var: tk.StringVar, choose_fn) -> None:
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=var).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(frm, text="Browse...", command=choose_fn).grid(row=row, column=2, padx=(8, 0), pady=4)

    def choose_image():
        p = filedialog.askopenfilename(
            title="Select image",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.tif *.tiff *.webp"), ("All files", "*.*")],
        )
        if p:
            image_var.set(p)
            out_dir_var.set(str(Path(p).parent))

    def choose_out_dir():
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            out_dir_var.set(d)

    add_labeled_path(0, "Input image", image_var, choose_image)
    add_labeled_path(1, "Output folder", out_dir_var, choose_out_dir)

    log_box = tk.Text(frm, height=14, wrap="word")
    log_box.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(12, 0))

    def log(msg: str) -> None:
        log_box.insert("end", msg + "\n")
        log_box.see("end")

    btn_run = ttk.Button(frm, text="Run")
    btn_run.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))

    def on_run():
        try:
            img_s = image_var.get().strip()
            if not img_s:
                raise ValueError("Input image is required.")
            img_path = Path(img_s)
            out_s = out_dir_var.get().strip()
            output_dir = Path(out_s) if out_s else None
        except Exception as e:
            messagebox.showerror("Invalid input", str(e))
            return

        btn_run.config(state="disabled")
        log("Starting...")

        def worker():
            try:
                run_cloud_job(
                    image_path=img_path,
                    output_dir=output_dir,
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
