import numpy as np
import rasterio

b2Path = path = r"F:\yaogan\oir\0816_clip\clip\T17TQJ_20230816T155911_B02_20m_clip.tif"
b3Path = path = r"F:\yaogan\oir\0816_clip\clip\T17TQJ_20230816T155911_B03_20m_clip.tif"
b4Path = path = r"F:\yaogan\oir\0816_clip\clip\T17TQJ_20230816T155911_B04_20m_clip.tif"
b8Path = path = r"F:\yaogan\oir\0816_clip\clip\T17TQJ_20230816T155911_B8A_20m_clip.tif"
b11Path = path = r"F:\yaogan\oir\0816_clip\clip\T17TQJ_20230816T155911_B11_20m_clip.tif"

out_ndvi = r"F:\yaogan\oir\0816_clip\index\ndvi.tif"
out_fai = r"F:\yaogan\oir\0816_clip\index\fai.tif"
out_ndavi = r"F:\yaogan\oir\0816_clip\index\ndavi.tif"
out_sabi = r"F:\yaogan\oir\0816_clip\index\sabi.tif"
with rasterio.open(b2Path) as b2_src,\
    rasterio.open(b3Path) as b3_src,\
    rasterio.open(b4Path) as b4_src,\
    rasterio.open(b8Path) as b8_src,\
    rasterio.open(b11Path) as b11_src:

    b2 = b2_src.read(1).astype("float32")/10000
    b3 = b3_src.read(1).astype("float32")/10000
    b4 = b4_src.read(1).astype("float32")/10000
    b8 = b8_src.read(1).astype("float32")/10000
    b11 = b11_src.read(1).astype("float32")/10000

    ndvi = (b8 - b4) / (b8 + b4)
    fai = b8 - (b4 + (b11 - b4) * ((842 - 665) / (1610 - 665)))
    ndavi = (b8 - b2) / (b8 + b2)
    sabi = (b8 - b2) / (b2 + b3)

    def print_stats(name, arr):
        print(f"\n{name}")
        print("Mean:", np.nanmean(arr))
        print("Std Dev:", np.nanstd(arr))
        print("Median:", np.nanmedian(arr))

    print_stats("NDVI", ndvi)
    print_stats("FAI", fai)
    print_stats("NDAVI", ndavi)
    print_stats("SABI", sabi)
    profile = b2_src.profile.copy()
    profile.update(
        driver="GTiff",
        dtype="float32",
        count=1,
        nodata=np.nan
    )

    with rasterio.open(out_ndvi, "w", **profile) as dst:
        dst.write(ndvi.astype("float32"), 1)

    with rasterio.open(out_fai, "w", **profile) as dst:
        dst.write(fai.astype("float32"), 1)

    with rasterio.open(out_ndavi, "w", **profile) as dst:
        dst.write(ndavi.astype("float32"), 1)

    with rasterio.open(out_sabi, "w", **profile) as dst:
        dst.write(sabi.astype("float32"), 1)

print("save:")
print(out_ndvi)
print(out_fai)
print(out_ndavi)
print(out_sabi)

