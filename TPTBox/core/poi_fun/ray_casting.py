import numpy as np
from numpy.linalg import norm
from scipy.interpolate import RegularGridInterpolator

from TPTBox import NII
from TPTBox.core.vert_constants import COORDINATE


def max_distance_ray_cast_convex(
    region: NII,
    start_coord: COORDINATE | np.ndarray,
    direction_vector: np.ndarray,
    acc_delta: float = 0.00005,
):
    start_point_np = np.asarray(start_coord)
    if start_point_np is None:
        return None

    """Convex assumption!"""
    # Compute a normal vector, that defines the plane direction
    normal_vector = np.asarray(direction_vector)
    normal_vector = normal_vector / norm(normal_vector)
    # Create a function to interpolate within the mask array
    interpolator = RegularGridInterpolator([np.arange(region.shape[i]) for i in range(3)], region.get_array())

    def is_inside(distance):
        coords = [start_point_np[i] + normal_vector[i] * distance for i in [0, 1, 2]]
        if any(i < 0 for i in coords):
            return 0
        if any(coords[i] > region.shape[i] - 1 for i in range(len(coords))):
            return 0
        # Evaluate the mask value at the interpolated coordinates
        mask_value = interpolator(coords)
        return mask_value > 0.5

    if not is_inside(0):
        return start_point_np
    count = 0
    min_v = 0
    max_v = sum(region.shape)
    delta = max_v * 2
    while acc_delta < delta:
        bisection = (max_v - min_v) / 2 + min_v
        if is_inside(bisection):
            min_v = bisection
        else:
            max_v = bisection
        delta = max_v - min_v
        count += 1
    return start_point_np + normal_vector * ((min_v + max_v) / 2)


def ray_cast_pixel_lvl(
    start_point_np: np.ndarray,
    normal_vector: np.ndarray,
    shape: np.ndarray | tuple[int, ...],
    two_sided=False,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    def _calc_pixels(normal_vector, start_point_np):
        # Make a plane through start_point with the norm of "normal_vector", which is shifted by "shift" along the norm
        start_point_np = start_point_np.copy()
        num_pixel = np.abs(np.floor(np.max((np.array(shape) - start_point_np) / normal_vector))).item()
        arange = np.arange(0, min(num_pixel, 1000), step=1, dtype=float)
        coords = [start_point_np[i] + normal_vector[i] * arange for i in [0, 1, 2]]

        # Clip coordinates to region bounds
        for i in [0, 1, 2]:
            cut_off = (shape[i] <= np.floor(coords[i])).sum()
            if cut_off == 0:
                cut_off = (np.floor(coords[i]) <= 0).sum()
            if cut_off != 0:
                coords = [c[:-cut_off] for c in coords]
                arange = arange[:-cut_off]
        # Convert coordinates to integers for indexing
        int_coords = [c.astype(int) for c in coords]

        return np.stack(int_coords, -1), arange

    plane_coords, arange = _calc_pixels(normal_vector, start_point_np)
    if two_sided:
        plane_coords2, arange2 = _calc_pixels(-normal_vector, start_point_np)
        arange2 = -arange2
        plane_coords = np.concatenate([plane_coords, plane_coords2])
        arange = np.concatenate([arange, arange2]) - np.min(arange2)

    return plane_coords, arange


def add_ray_to_img(start_point: np.ndarray | COORDINATE, normal_vector: np.ndarray, region: NII, add_to_img=True, inplace=False, value=0):
    start_point = np.array(start_point)
    plane_coords, arange = ray_cast_pixel_lvl(start_point, normal_vector, shape=region.shape)
    if plane_coords is None:
        return None
    selected_arr = np.zeros(region.shape, dtype=region.dtype)
    selected_arr[plane_coords[..., 0], plane_coords[..., 1], plane_coords[..., 2]] = arange if value == 0 else value
    ray = region.set_array(selected_arr)
    if add_to_img:
        if not inplace:
            region = region.copy()
        region[ray != 0] = ray[ray != 0]
        return region
    return ray
