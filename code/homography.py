import cv2
import numpy as np
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon

quicklook_path = r"F:\yaogan\stac_output\S2A_MSIL2A_20230717T155911_N0510_R097_T17TQJ_20241015T210729_quicklook.jpg"
quicklook_geojson_path = r"F:\yaogan\stac_output\S2A_MSIL2A_20230717T155911_N0510_R097_T17TQJ_20241015T210729_footprint.geojson"
roi_path = r"F:\yaogan\oir\OIR.geojson"

out_crop_path = r"F:\yaogan\stac_output\roi_crop_cv2.jpg"
out_mark_path = r"F:\yaogan\stac_output\roi_marked_cv2.png"

def get_exterior_coords(geom):

    if geom.geom_type == "Polygon":
        coords = list(geom.exterior.coords)
    elif geom.geom_type == "MultiPolygon":
        largest = max(geom.geoms, key=lambda g: g.area)
        coords = list(largest.exterior.coords)
    else:
        raise ValueError(f"Unsupported geometry type: {geom.geom_type}")

    if coords[0] == coords[-1]:
        coords = coords[:-1]
    return coords


def order_footprint_corners(coords):

    pts = np.array(coords, dtype=np.float32)

    if len(pts) != 4:
        raise ValueError(f" {len(pts)} 。")

    idx = np.argsort(-pts[:, 1])
    top2 = pts[idx[:2]]
    bottom2 = pts[idx[2:]]

    top_left, top_right = top2[np.argsort(top2[:, 0])]

    bottom_left, bottom_right = bottom2[np.argsort(bottom2[:, 0])]

    return np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)


def clip_int(v, low, high):
    return max(low, min(high, int(round(v))))


quicklook_gdf = gpd.read_file(quicklook_geojson_path).to_crs("EPSG:4326")
quicklook_geom = quicklook_gdf.geometry.iloc[0]

footprint_coords = get_exterior_coords(quicklook_geom)

unique_coords = []
for c in footprint_coords:
    if c not in unique_coords:
        unique_coords.append(c)

if len(unique_coords) != 4:
    raise ValueError(
        f" {len(unique_coords)} ：{unique_coords}"
    )

footprint_pts = order_footprint_corners(unique_coords)


roi_gdf = gpd.read_file(roi_path).to_crs("EPSG:4326")
roi_geom = roi_gdf.unary_union

if isinstance(roi_geom, MultiPolygon):
    roi_geom = max(roi_geom.geoms, key=lambda g: g.area)


roi_coords = get_exterior_coords(roi_geom)
roi_pts_geo = np.array(roi_coords, dtype=np.float32)


img = cv2.imread(quicklook_path)
if img is None:
    raise FileNotFoundError(f"no pic: {quicklook_path}")

H_img, W_img = img.shape[:2]

img_pts = np.array([
    [0, 0],             # top-left
    [W_img, 0],         # top-right
    [W_img, H_img],     # bottom-right
    [0, H_img],         # bottom-left
], dtype=np.float32)


H_matrix, status = cv2.findHomography(footprint_pts, img_pts)

if H_matrix is None:
    raise RuntimeError("fail。")

print("Footprint ordered points (lon, lat):")
print(footprint_pts)

print("\nHomography matrix:")
print(H_matrix)

roi_pts_geo_cv = roi_pts_geo.reshape(-1, 1, 2)
roi_pts_img = cv2.perspectiveTransform(roi_pts_geo_cv, H_matrix)
roi_pts_img = roi_pts_img.reshape(-1, 2)

print("\nProjected ROI pixel points:")
for p in roi_pts_img:
    print(tuple(p))


xs = roi_pts_img[:, 0]
ys = roi_pts_img[:, 1]

left = clip_int(xs.min(), 0, W_img)
right = clip_int(xs.max(), 0, W_img)
top = clip_int(ys.min(), 0, H_img)
bottom = clip_int(ys.max(), 0, H_img)

print("\nImage size:", (W_img, H_img))
print("Pixel crop box:", [left, top, right, bottom])

if left >= right or top >= bottom:
    raise ValueError("footprint ROI not match。")


img_mark = img.copy()


roi_polygon_pixels = np.round(roi_pts_img).astype(np.int32).reshape((-1, 1, 2))

overlay = img_mark.copy()
cv2.fillPoly(overlay, [roi_polygon_pixels], color=(0, 255, 255)) 
alpha = 0.3
img_mark = cv2.addWeighted(overlay, alpha, img_mark, 1 - alpha, 0)

cv2.rectangle(img_mark, (left, top), (right, bottom), color=(0, 255, 255), thickness=0)

cv2.imwrite(out_mark_path, img_mark)

crop = img[top:bottom, left:right]
cv2.imwrite(out_crop_path, crop)

print("\nsave mark:", out_mark_path)
print("save crop:", out_crop_path)