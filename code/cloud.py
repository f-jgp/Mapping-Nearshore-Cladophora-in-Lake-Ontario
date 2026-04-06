import cv2
import numpy as np

img = cv2.imread(r"F:\yaogan\stac_output\roi_crop.jpg")

if img is None:
    raise ValueError("Image load failed")

hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)

H, W = hsv.shape[:2]

cloud_count = 0
total_pixels = H * W

cloud_mask = np.zeros((H, W), dtype=np.uint8)

for i in range(H):
    for j in range(W):
        pixel = hsv[i, j]

        S = pixel[1] / 255.0
        V = pixel[2] / 255.0

        is_cloud = (V > 0.75) and (S < 0.20)

        if is_cloud:
            cloud_count += 1
            cloud_mask[i, j] = 255


cloud_ratio = cloud_count / total_pixels
print("Cloud ratio:", cloud_ratio)


#cv2.imwrite("cloud_mask.png", cloud_mask)