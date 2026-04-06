import cv2
import numpy as np

gt_img_path = r"F:\yaogan\pic\ool\geoinfo.png"
homo_img_path = r"F:\yaogan\pic\ool\homography.png"
square_img_path = r"F:\yaogan\pic\ool\square.png"

gt_img = cv2.imread(gt_img_path)
homo_img = cv2.imread(homo_img_path)
square_img = cv2.imread(square_img_path)

if gt_img is None:
    raise FileNotFoundError(f"Cannot read: {gt_img_path}")
if homo_img is None:
    raise FileNotFoundError(f"Cannot read: {homo_img_path}")
if square_img is None:
    raise FileNotFoundError(f"Cannot read: {square_img_path}")

# 以 ground truth 为基准尺寸
H, W = gt_img.shape[:2]

# 如果尺寸不同，先 resize 到 GT 尺寸
if homo_img.shape[:2] != (H, W):
    homo_img = cv2.resize(homo_img, (W, H), interpolation=cv2.INTER_NEAREST)

if square_img.shape[:2] != (H, W):
    square_img = cv2.resize(square_img, (W, H), interpolation=cv2.INTER_NEAREST)

# OpenCV uses BGR
GREEN = np.array([0, 255, 0], dtype=np.uint8)
RED = np.array([0, 0, 255], dtype=np.uint8)
YELLOW = np.array([0, 255, 255], dtype=np.uint8)

def get_mask(img, color, tol=10):
    lower = np.clip(color.astype(np.int16) - tol, 0, 255).astype(np.uint8)
    upper = np.clip(color.astype(np.int16) + tol, 0, 255).astype(np.uint8)
    return cv2.inRange(img, lower, upper) > 0

gt_mask = get_mask(gt_img, GREEN)
homo_mask = get_mask(homo_img, RED)
square_mask = get_mask(square_img, YELLOW)

def coverage(gt, pred):
    gt_sum = gt.sum()
    if gt_sum == 0:
        return 0.0
    return np.logical_and(gt, pred).sum() / gt_sum

def precision(gt, pred):
    pred_sum = pred.sum()
    if pred_sum == 0:
        return 0.0
    return np.logical_and(gt, pred).sum() / pred_sum

def iou(gt, pred):
    inter = np.logical_and(gt, pred).sum()
    union = np.logical_or(gt, pred).sum()
    if union == 0:
        return 0.0
    return inter / union

print("=== Homography vs Ground Truth ===")
print(f"Coverage : {coverage(gt_mask, homo_mask):.6f}")
print(f"Precision: {precision(gt_mask, homo_mask):.6f}")
print(f"IoU      : {iou(gt_mask, homo_mask):.6f}")

print("\n=== Square vs Ground Truth ===")
print(f"Coverage : {coverage(gt_mask, square_mask):.6f}")
print(f"Precision: {precision(gt_mask, square_mask):.6f}")
print(f"IoU      : {iou(gt_mask, square_mask):.6f}")