import numpy as np
import matplotlib.pyplot as plt
import napari, numpy as np 
from cellpose import models, io, plot
from cellpose.io import imread

io.logger_setup()

# In v4.0.8, use 'pretrained_model' instead of 'model'
model = models.CellposeModel(gpu=True, pretrained_model='cyto3')

# Define the file path and load image
file_path = 'media/carsen/DATA1/TIFFS/004_img.png'
img = imread(file_path)

# channels=[0,0] for grayscale
channels = [0,0]

# Run evaluation
masks, flows, styles = model.eval(img, diameter=None, channels=channels)

print(f">>>> Found {masks.max()} cells")

# --- VISUALIZATION ---
# Normalize image for display (0-255 uint8) to avoid Matplotlib warnings/errors
img_display = img.copy()
if img_display.dtype == np.uint16:
    img_display = (img_display / 256).astype(np.uint8)

fig, ax = plt.subplots(1, 2, figsize=(12, 5))

# Show original image (normalized)
ax[0].imshow(img_display)
ax[0].set_title('Original Image')
ax[0].axis('off')

# Show masks with random colors
# Create a colorful mask image
mask_RGB = plot.mask_overlay(img_display, masks)
ax[1].imshow(mask_RGB)
ax[1].set_title(f'Detected Cells: {masks.max()}')
ax[1].axis('off')

plt.tight_layout()
plt.show()

# --- SAVING RESULTS ---
# The standard _masks.png is black because it contains label IDs (1, 2, 3...)
# which are too dark to see. Let's save the colorful overlay you saw in the plot:
save_path = file_path.replace('_img.png', '_overlay.png')
plt.imsave(save_path, mask_RGB)
print(f">>>> Saved visible overlay to: {save_path}")

# This saves the raw data (label masks for analysis)
io.save_masks([img], [masks], [flows], [file_path], channels=channels, png=True, tif=False)
#For  lossless format using Api designe 
np.save("masks.npy", masks.astype(np.int32))
#For showing Label image 
masks = np.load("masks.npy")
napari.view_labels(masks)
napari.run()