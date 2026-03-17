import os
import numpy as np
import rasterio
import matplotlib.pyplot as plt
from matplotlib import cm

input_files = [
    r"F:\yaogan\oir\0602_clip\index\ndvi.tif",
    r"F:\yaogan\oir\0602_clip\index\fai.tif",
    r"F:\yaogan\oir\0602_clip\index\ndavi.tif",
    r"F:\yaogan\oir\0602_clip\index\sabi.tif"
]

out_dir = r"F:\yaogan\oir\0602_clip\colored_png"
os.makedirs(out_dir, exist_ok=True)


def choose_cmap(filename):
    name = filename.lower()

    if "ndvi" in name:
        return cm.get_cmap("turbo")
    elif "ndavi" in name:
        return cm.get_cmap("turbo")
    elif "fai" in name:
        return cm.get_cmap("turbo")
    elif "sabi" in name:
        return cm.get_cmap("turbo")
    else:
        return cm.get_cmap("viridis")


def stretch_array(arr, mask, lower=5, upper=99, gamma=0.7):

    valid = arr[~mask]
    if valid.size == 0:
        return None, None, None

    vmin = np.percentile(valid, lower)
    vmax = np.percentile(valid, upper)

    if vmax <= vmin:
        vmax = vmin + 1e-6

    norm = (arr - vmin) / (vmax - vmin)
    norm = np.clip(norm, 0, 1)

    norm = norm ** gamma

    return norm, vmin, vmax


for tif_path in input_files:
    base = os.path.splitext(os.path.basename(tif_path))[0]
    cmap = choose_cmap(base)

    with rasterio.open(tif_path) as src:
        arr = src.read(1).astype("float32")
        nodata = src.nodata

        mask = np.isnan(arr)
        if nodata is not None:
            mask |= (arr == nodata)

        mask |= ~np.isfinite(arr)

        norm, vmin, vmax = stretch_array(
            arr,
            mask,
            lower=5,
            upper=99,
            gamma=0.7
        )


        rgba = cmap(norm)

        rgba[mask] = [1, 1, 1, 1]

        out_png = os.path.join(out_dir, f"{base}_color.png")

        plt.figure(figsize=(8, 8))
        plt.imshow(rgba)
        plt.axis("off")
        plt.tight_layout(pad=0)
        plt.savefig(out_png, dpi=300, bbox_inches="tight", pad_inches=0)
        plt.close()

        print(f"Saved: {out_png}")
        print(f"{base} stretch range: vmin={vmin:.6f}, vmax={vmax:.6f}")