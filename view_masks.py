"""
Visualize segmentation masks with napari
"""
import numpy as np
import napari
import imageio.v3 as iio

# Load the masks
print("Loading masks...")
masks = np.load('masks_2.npy')
print(f"Masks shape: {masks.shape}")
print(f"Number of cells detected: {len(np.unique(masks)) - 1}")
print(f"Cell IDs range: {masks.min()} to {masks.max()}")

# Create napari viewer
viewer = napari.Viewer(title="Cellpose Segmentation Results")

# Add the segmentation masks as labels
viewer.add_labels(masks, name='Cell Masks')

print("\nNapari viewer opened!")
print("- Scroll to zoom, click and drag to pan")
print("- Hover over cells to see their IDs")
print("- Each cell has a unique color and ID number")

napari.run()
