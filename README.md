# Mapping-Nearshore-Cladophora-in-Lake-Ontario
**Introduction and Study Area**

Cladophora is a filamentous green alga native to the North American Great Lakes. Its excessive proliferation not only causes foul odors and impairs public beach recreation but also triggers severe ecological issues, including avian botulism outbreaks. Since the 1990s, the filtering effect of invasive species such as dreissenid mussels has significantly increased water clarity, allowing sunlight to penetrate to greater depths. This has led to massive Cladophora blooms even under relatively low nutrient concentrations. The study area of this research focuses on the nearshore waters along the southern shore of Lake Ontario (the United States side). To achieve precise calibration of remote sensing observations, the spatial scope of the study is strictly defined as two independent 6 km × 6 km square regions, centered respectively around two key hydrological and biological monitoring stations established by the United States Geological Survey (USGS): the OIR station (Irondequoit, near Rochester) and the OOL station (Olcott).
These two core USGS stations provide substantial, highly valuable ground-truth data for this study. These comprehensive datasets encompass multi-depth water flow velocities, water turbidity, and various critical chemical constituents in the water column (such as nutrient concentrations). More importantly, the stations provide net weight data of Cladophora samples collected in situ across different depth gradients. These multi-dimensional, high-precision ground truth indicators not only serve as an irreplaceable validation foundation for evaluating and calibrating various spectral remote sensing indices within our open-source computational architecture, but also enable us to deeply investigate the complex mechanisms underlying the relationships between micro-environmental physicochemical variables and nearshore benthic algal outbreaks.

<p align="center">
  <img src="images/x.png" width="400">
</p>
<p align="center">
  <img src="images/y.png" width="400">
</p>

For each coordinate of the ROI, we can use the formula to calculate the relative position on the quicklook image. The $lon_r$ and $lat_r$ are the longitude and latitude of the point we are calculated. For $maxlon_f$ is east of the quicklook footprint, $min⁡lon_f$ is west of the quicklook footprint $max⁡lat_f$ is north of the quicklook footprint and $min⁡lat_f$ is south of the quicklook footprint. W and H are the width and height of the quicklook image

**ROI find by geoinfomation**

<p align="center">
  <img src="images/real.png" width="400">
</p>

**ROI find by calculate quicklook footprint**
<p align="center">
  <img src="images/calculate.png" width="400">
</p>


The footprint polygon and the region of interest (ROI) are first loaded from GeoJSON files. Program will transformed both into a consistent coordinate reference system The four corner points of the footprint are ordered consistently (top-left, top-right, bottom-right, bottom-left) to match the corresponding image corners. The quicklook image is loaded, and its four corner pixel coordinates are defined as:(0,0),(W,0),(W,H),(0,H) where W and H are the image width and height. A homography matrix H is computed using at least four pairs of corresponding points between the footprint (geographic coordinates) and the image (pixel coordinates). 

<p align="center">
  <img src="images/h1.png" width="150">
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="images/h2.png" width="250">
</p>


<p align="center">
  <img src="images/h3.png" width="800">
</p>


The calculatation process can be simplify by using python openCV library. The ROI vertices are transformed using the homography matrix and maps the ROI from geographic space to image pixel space. Because of the result is homogeneous coordinates, it need normalized by the scale factor that obtain from the final pixel coordinates. The projected ROI points are used to compute a bounding box in pixel coordinates, ensuring the values are clipped within image boundaries. Finally, the corresponding image region is cropped and calculate the region cloud density base on image brightness.


<p align="center">
  <img src="images/roucalculatation.png" width="400">
</p>

<p align="center">
  <img src="images/normalize.png" width="400">
</p>

**ROI find by homography**
<p align="center">
  <img src="images/roi_marked_cv2.png" width="400">
</p>

Green is the ground truth provide by the geoinformation. Yellow is the result using the rectangle projection. Red is the result using homography

**compair ori**
<p align="center">
  <img src="images/compair_ori.png" width="400">
</p>

**compair ool**
<p align="center">
  <img src="images/compair_ool.png" width="400">
</p>

| Dataset| Method     | Coverage | Precision| IoU      |
|--------|------------|----------|----------|----------|
| ori    | rectangle  | 0.375946 | 0.366582 | 0.227901 |
|        | Homography | 0.998136 | 0.836551 | 0.835246 |
| ool    | rectangle  | 0.649688 | 0.633553 | 0.472232 |
|        | Homography | 0.967785 | 0.896563 | 0.870581 |

Coverage: how much of the ground truth ROI is successfully captured by the method. <br>
Precision: how much of the predicted region actually belongs to the ground truth ROI indicating overestimation. <br>
IoU : evaluation of both under-coverage and over-coverage<br>
