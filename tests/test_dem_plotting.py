import matplotlib.pyplot as plt
import numpy as np

from riftprep.config import GridSpec
from riftprep.plotting import plot_dem_domain


def test_dem_comparison_uses_equal_colours_for_equal_elevations():
    grid = GridSpec(0, 0, 60, 60, 30, "EPSG:5070", 2, 2)
    original = np.arange(9, dtype=float).reshape(3, 3) * 10 + 100
    active = np.ones((3, 3), dtype=bool)
    active[0, 0] = False
    final = original.copy()
    final[0, 0] = 9999
    final[2, 2] = 80  # An explicitly carved elevation must remain on the legend.
    figure = plot_dem_domain(original, final, active, grid)
    images = [axis.images[0] for axis in figure.axes if axis.images]
    assert len(images) == 3
    colours = [image.cmap(image.norm(150)) for image in images]
    assert colours[0] == colours[1] == colours[2]
    assert all(image.norm.vmin == 80 for image in images)
    assert all(image.norm.vmax < 9999 for image in images)
    plt.close(figure)
