
import geopandas as gpd
from PIL import Image


quicklook_path = r"F:\yaogan\stac_output\S2A_MSIL2A_20230816T155911_N0510_R097_T17TQJ_20241022T034712_quicklook.jpg"
quicklook_geojson_path = r"F:\yaogan\stac_output\S2A_MSIL2A_20230717T155911_N0510_R097_T17TQJ_20241015T210729_footprint.geojson"
roi_path = r"F:\yaogan\oir\OIR.geojson"
out_path = r"F:\yaogan\stac_output\roi_crop.jpg"


quicklook_gdf = gpd.read_file(quicklook_geojson_path).to_crs("EPSG:4326")
west, south, east, north = quicklook_gdf.total_bounds

roi_gdf = gpd.read_file(roi_path).to_crs("EPSG:4326")
minx, miny, maxx, maxy = roi_gdf.total_bounds

img = Image.open(quicklook_path)
W, H = img.size


def lon_to_x(lon, west, east, width):
    return (lon - west) / (east - west) * width

def lat_to_y(lat, south, north, height):
    return (north - lat) / (north - south) * height

x1 = lon_to_x(minx, west, east, W)
x2 = lon_to_x(maxx, west, east, W)
y1 = lat_to_y(maxy, south, north, H)
y2 = lat_to_y(miny, south, north, H)


x1 = max(0, min(W, int(round(x1))))
x2 = max(0, min(W, int(round(x2))))
y1 = max(0, min(H, int(round(y1))))
y2 = max(0, min(H, int(round(y2))))

left, right = sorted([x1, x2])
top, bottom = sorted([y1, y2])

print("Quicklook bbox:", [west, south, east, north])
print("ROI bbox:", [minx, miny, maxx, maxy])
print("Image size:", (W, H))
print("Pixel crop box:", [left, top, right, bottom])



crop = img.crop((left, top, right, bottom))
crop.save(out_path)

print("save:", out_path)
def x_to_lon(x, west, east, width):
    return west + (x / width) * (east - west)

def y_to_lat(y, south, north, height):
    return north - (y / height) * (north - south)

top_left = (
    x_to_lon(left, west, east, W),
    y_to_lat(top, south, north, H)
)
top_right = (
    x_to_lon(right, west, east, W),
    y_to_lat(top, south, north, H)
)
bottom_left = (
    x_to_lon(left, west, east, W),
    y_to_lat(bottom, south, north, H)
)
bottom_right = (
    x_to_lon(right, west, east, W),
    y_to_lat(bottom, south, north, H)
)

print("Top-left approx:", top_left)
print("Top-right approx:", top_right)
print("Bottom-left approx:", bottom_left)
print("Bottom-right approx:", bottom_right)

from PIL import ImageDraw


crop = img.crop((left, top, right, bottom))
crop.save(out_path)


img_mark = img.copy()
draw = ImageDraw.Draw(img_mark)


draw.rectangle(
    [(left, top), (right, bottom)],
    outline="yellow",
    width=0
)


overlay = Image.new("RGBA", img_mark.size, (0,0,0,0))
overlay_draw = ImageDraw.Draw(overlay)

overlay_draw.rectangle(
    [(left, top), (right, bottom)],
    fill=(255,255,0,80)
)

img_mark = Image.alpha_composite(img_mark.convert("RGBA"), overlay)

mark_path = out_path.replace(".jpg", "_marked.png")
img_mark.save(mark_path)

print(out_path)
print(mark_path)