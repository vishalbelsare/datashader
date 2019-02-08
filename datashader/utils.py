from __future__ import absolute_import, division, print_function

import os

from inspect import getmro

import numba as nb
import numpy as np
import pandas as pd

from xarray import DataArray
import dask.dataframe as dd
import datashape

from datashader.datatypes import RaggedDtype

ngjit = nb.jit(nopython=True, nogil=True)


class Expr(object):
    """Base class for expression-like objects.

    Implements hashing and equality checks. Subclasses should implement an
    ``inputs`` attribute/property, containing a tuple of everything that fully
    defines that expression.
    """
    def __hash__(self):
        return hash((type(self), self._hashable_inputs()))

    def __eq__(self, other):
        return (type(self) is type(other) and
                self._hashable_inputs() == other._hashable_inputs())

    def __ne__(self, other):
        return not self == other

    def _hashable_inputs(self):
        """
        Return a version of the inputs tuple that is suitable for hashing and
        equality comparisons
        """
        result = []
        for ip in self.inputs:
            if isinstance(ip, (list, set)):
                result.append(tuple(ip))
            elif isinstance(ip, np.ndarray):
                result.append(ip.tobytes())
            else:
                result.append(ip)

        return tuple(result)


class Dispatcher(object):
    """Simple single dispatch."""
    def __init__(self):
        self._lookup = {}

    def register(self, typ, func=None):
        """Register dispatch of `func` on arguments of type `typ`"""
        if func is None:
            return lambda f: self.register(typ, f)
        if isinstance(typ, tuple):
            for t in typ:
                self.register(t, func)
        else:
            self._lookup[typ] = func
        return func

    def __call__(self, head, *rest, **kwargs):
        # We dispatch first on type(head), and fall back to iterating through
        # the mro. This is significantly faster in the common case where
        # type(head) is in the lookup, with only a small penalty on fall back.
        lk = self._lookup
        typ = type(head)
        if typ in lk:
            return lk[typ](head, *rest, **kwargs)
        for cls in getmro(typ)[1:]:
            if cls in lk:
                return lk[cls](head, *rest, **kwargs)
        raise TypeError("No dispatch for {0} type".format(typ))


def isreal(dt):
    """Check if a datashape is numeric and real.

    Example
    -------
    >>> isreal('int32')
    True
    >>> isreal('float64')
    True
    >>> isreal('string')
    False
    >>> isreal('complex64')
    False
    """
    dt = datashape.predicates.launder(dt)
    return isinstance(dt, datashape.Unit) and dt in datashape.typesets.real


def calc_res(raster):
    """Calculate the resolution of xarray.DataArray raster and return it as the
    two-tuple (xres, yres).
    """
    h, w = raster.shape[-2:]
    ydim, xdim = raster.dims[-2:]
    xcoords = raster[xdim].values
    ycoords = raster[ydim].values
    xres = (xcoords[-1] - xcoords[0]) / (w - 1)
    yres = (ycoords[0] - ycoords[-1]) / (h - 1)
    return xres, yres


def calc_bbox(xs, ys, res):
    """Calculate the bounding box of a raster, and return it in a four-element
    tuple: (xmin, ymin, xmax, ymax). This calculation assumes the raster is
    uniformly sampled (equivalent to a flat-earth assumption, for geographic
    data) so that an affine transform (using the "Augmented Matrix" approach)
    suffices:
    https://en.wikipedia.org/wiki/Affine_transformation#Augmented_matrix

    Parameters
    ----------
    xs : numpy.array
        1D NumPy array of floats representing the x-values of a raster. This
        likely originated from an xarray.DataArray or xarray.Dataset object
        (xr.open_rasterio).
    ys : numpy.array
        1D NumPy array of floats representing the y-values of a raster. This
        likely originated from an xarray.DataArray or xarray.Dataset object
        (xr.open_rasterio).
    res : tuple
        Two-tuple (int, int) which includes x and y resolutions (aka "grid/cell
        sizes"), respectively.
    """
    xbound = xs.max() if res[0] < 0 else xs.min()
    ybound = ys.min() if res[1] < 0 else ys.max()

    xmin = ymin = np.inf
    xmax = ymax = -np.inf
    Ab = np.array([[res[0],  0.,      xbound],
                   [0.,      -res[1], ybound],
                   [0.,      0.,      1.]])
    for x_, y_ in [(0, 0), (0, len(ys)), (len(xs), 0), (len(xs), len(ys))]:
        x, y, _ = np.dot(Ab, np.array([x_, y_, 1.]))
        if x < xmin: xmin = x
        if x > xmax: xmax = x
        if y < ymin: ymin = y
        if y > ymax: ymax = y
    xpad, ypad = res[0]/2., res[1]/2.
    return xmin-xpad, ymin+ypad, xmax-xpad, ymax+ypad


def get_indices(start, end, coords, res):
    """
    Transform continuous start and end coordinates into array indices.

    Parameters
    ----------
    start : float
        coordinate of the lower bound.
    end : float
        coordinate of the upper bound.
    coords : numpy.ndarray
        coordinate values along the axis.
    res : tuple
        Resolution along an axis (aka "grid/cell sizes")
    """
    size = len(coords)
    half = abs(res)/2.
    vmin, vmax = coords.min(), coords.max()
    span = vmax-vmin
    start, end = start+half-vmin, end-half-vmin
    sidx, eidx = int((start/span)*size), int((end/span)*size)
    if eidx < sidx:
        return sidx, sidx
    return sidx, eidx


def orient_array(raster, res=None, layer=None):
    """
    Reorients the array to a canonical orientation depending on
    whether the x and y-resolution values are positive or negative.

    Parameters
    ----------
    raster : DataArray
        xarray DataArray to be reoriented
    res : tuple
        Two-tuple (int, int) which includes x and y resolutions (aka "grid/cell
        sizes"), respectively.
    layer : int
        Index of the raster layer to be reoriented (optional)

    Returns
    -------
    array : numpy.ndarray
        Reoriented 2d NumPy ndarray
    """
    if res is None:
        res = calc_res(raster)
    array = raster.data
    if layer is not None: array = array[layer-1]
    if array.ndim == 2:
        if res[0] < 0: array = array[:, ::-1]
        if res[1] > 0: array = array[::-1]
    else:
        if res[0] < 0: array = array[:, :, ::-1]
        if res[1] > 0: array = array[:, ::-1]
    return array


def compute_coords(width, height, x_range, y_range, res):
    """
    Computes DataArray coordinates at bin centers

    Parameters
    ----------
    width : int
        Number of coordinates along the x-axis
    height : int
        Number of coordinates along the y-axis
    x_range : tuple
        Left and right edge of the coordinates
    y_range : tuple
        Bottom and top edges of the coordinates
    res : tuple
        Two-tuple (int, int) which includes x and y resolutions (aka "grid/cell
        sizes"), respectively. Used to determine coordinate orientation.

    Returns
    -------
    xs : numpy.ndarray
        1D array of x-coordinates
    ys : numpy.ndarray
        1D array of y-coordinates
    """
    (x0, x1), (y0, y1) = x_range, y_range
    xd = (x1-x0)/float(width)
    yd = (y1-y0)/float(height)
    xpad, ypad = abs(xd/2.), abs(yd/2.)
    x0, x1 = x0+xpad, x1-xpad
    y0, y1 = y0+ypad, y1-ypad
    xs = np.linspace(x0, x1, width)
    ys = np.linspace(y0, y1, height)
    if res[0] < 0: xs = xs[::-1]
    if res[1] > 0: ys = ys[::-1]
    return xs, ys


def downsample_aggregate(aggregate, factor, how='mean'):
    """Create downsampled aggregate factor in pixels units"""
    ys, xs = aggregate.shape[:2]
    crarr = aggregate[:ys-(ys % int(factor)), :xs-(xs % int(factor))]
    concat = np.concatenate([[crarr[i::factor, j::factor]
                            for i in range(factor)]
                            for j in range(factor)])

    if how == 'mean':
        return np.nanmean(concat, axis=0)
    elif how == 'sum':
        return np.nansum(concat, axis=0)
    elif how == 'max':
        return np.nanmax(concat, axis=0)
    elif how == 'min':
        return np.nanmin(concat, axis=0)
    elif how == 'median':
        return np.nanmedian(concat, axis=0)
    elif how == 'std':
        return np.nanstd(concat, axis=0)
    elif how == 'var':
        return np.nanvar(concat, axis=0)
    else:
        raise ValueError("Invalid 'how' downsample method. Options mean, sum, max, min, median, std, var")


def summarize_aggregate_values(aggregate, how='linear', num=180):
    """Helper function similar to np.linspace which return values from aggregate min value to aggregate max value in either linear or log space.
    """

    max_val = np.nanmax(aggregate.values)
    min_val = np.nanmin(aggregate.values)

    if min_val == 0:
        min_val = aggregate.data[aggregate.data > 0].min()

    if how == 'linear':
        vals = np.linspace(min_val, max_val, num)[None, :]
    else:
        vals = (np.logspace(0,
                            np.log1p(max_val - min_val),
                            base=np.e, num=num,
                            dtype=min_val.dtype) + min_val)[None, :]

    return DataArray(vals), min_val, max_val


def hold(f):
    '''
    simple arg caching decorator
    '''
    last = []

    def _(*args):
        if not last or last[0] != args:
            last[:] = args, f(*args)
        return last[1]
    return _


def export_image(img, filename, fmt=".png", _return=True, export_path=".", background=""):
    """Given a datashader Image object, saves it to a disk file in the requested format"""

    from datashader.transfer_functions import set_background

    if not os.path.exists(export_path):
        os.mkdir(export_path)

    if background:
        img = set_background(img, background)

    img.to_pil().save(os.path.join(export_path, filename + fmt))
    return img if _return else None


def lnglat_to_meters(longitude, latitude):
    """
    Projects the given (longitude, latitude) values into Web Mercator
    coordinates (meters East of Greenwich and meters North of the Equator).

    Longitude and latitude can be provided as scalars, Pandas columns,
    or Numpy arrays, and will be returned in the same form.  Lists
    or tuples will be converted to Numpy arrays.

    Examples:
       easting, northing = lnglat_to_meters(-40.71,74)

       easting, northing = lnglat_to_meters(np.array([-74]),np.array([40.71]))

       df=pandas.DataFrame(dict(longitude=np.array([-74]),latitude=np.array([40.71])))
       df.loc[:, 'longitude'], df.loc[:, 'latitude'] = lnglat_to_meters(df.longitude,df.latitude)
    """
    if isinstance(longitude, (list, tuple)):
        longitude = np.array(longitude)
    if isinstance(latitude, (list, tuple)):
        latitude = np.array(latitude)

    origin_shift = np.pi * 6378137
    easting = longitude * origin_shift / 180.0
    northing = np.log(np.tan((90 + latitude) * np.pi / 360.0)) * origin_shift / np.pi
    return (easting, northing)


# Heavily inspired by odo
def dshape_from_pandas_helper(col):
    """Return an object from datashape.coretypes given a column from a pandas
    dataframe.
    """
    if isinstance(col.dtype, type(pd.Categorical.dtype)) or isinstance(col.dtype, pd.api.types.CategoricalDtype):
        cat_dshape = datashape.dshape('{} * {}'.format(
            len(col.cat.categories),
            col.cat.categories.dtype,
        ))
        return datashape.Categorical(col.cat.categories.values,
                                     type=cat_dshape,
                                     ordered=col.cat.ordered)
    elif col.dtype.kind == 'M':
        tz = getattr(col.dtype, 'tz', None)
        if tz is not None:
            # Pandas stores this as a pytz.tzinfo, but DataShape wants a string
            tz = str(tz)
        return datashape.Option(datashape.DateTime(tz=tz))
    elif isinstance(col.dtype, RaggedDtype):
        return col.dtype
    dshape = datashape.CType.from_numpy_dtype(col.dtype)
    dshape = datashape.string if dshape == datashape.object_ else dshape
    if dshape in (datashape.string, datashape.datetime_):
        return datashape.Option(dshape)
    return dshape


def dshape_from_pandas(df):
    """Return a datashape.DataShape object given a pandas dataframe."""
    return len(df) * datashape.Record([(k, dshape_from_pandas_helper(df[k]))
                                       for k in df.columns])


def dshape_from_dask(df):
    """Return a datashape.DataShape object given a dask dataframe."""
    return datashape.var * dshape_from_pandas(df.head()).measure


def dataframe_from_multiple_sequences(x_values, y_values):
   """
   Converts a set of multiple sequences (eg: time series), stored as a 2 dimensional
   numpy array into a pandas dataframe that can be plotted by datashader.
   The pandas dataframe eventually contains two columns ('x' and 'y') with the data.
   Each time series is separated by a row of NaNs.
   Discussion at: https://github.com/bokeh/datashader/issues/286#issuecomment-334619499

   x_values: 1D numpy array with the values to be plotted on the x axis (eg: time)
   y_values: 2D numpy array with the sequences to be plotted of shape (num sequences X length of each sequence)

   """

   # Add a NaN at the end of the array of x values
   x = np.zeros(x_values.shape[0] + 1)
   x[-1] = np.nan
   x[:-1] = x_values

   # Tile this array of x values: number of repeats = number of sequences/time series in the data
   x = np.tile(x, y_values.shape[0])

   # Add a NaN at the end of every sequence in y_values
   y = np.zeros((y_values.shape[0], y_values.shape[1] + 1))
   y[:, -1] = np.nan
   y[:, :-1] = y_values

   # Return a dataframe with this new set of x and y values
   return pd.DataFrame({'x': x, 'y': y.flatten()})


def _pd_mesh(vertices, simplices):
    """Helper for ``datashader.utils.mesh()``. Both arguments are assumed to be
    Pandas DataFrame objects.
    """
    # Winding auto-detect
    winding = [0, 1, 2]
    first_tri = vertices.values[simplices.values[0, winding].astype(np.int64), :2]
    a, b, c = first_tri
    if np.cross(b-a, c-a).item() >= 0:
        winding = [0, 2, 1]

    # Construct mesh by indexing into vertices with simplex indices
    vertex_idxs = simplices.values[:, winding]
    if not vertex_idxs.dtype == 'int64':
        vertex_idxs = vertex_idxs.astype(np.int64)
    vals = np.take(vertices.values, vertex_idxs, axis=0)
    vals = vals.reshape(np.prod(vals.shape[:2]), vals.shape[2])
    res = pd.DataFrame(vals, columns=vertices.columns)

    # If vertices don't have weights, use simplex weights
    verts_have_weights = len(vertices.columns) > 2
    if not verts_have_weights:
        weight_col = simplices.columns[3]
        res[weight_col] = simplices.values[:, 3].repeat(3)

    return res


def _dd_mesh(vertices, simplices):
    """Helper for ``datashader.utils.mesh()``. Both arguments are assumed to be
    Dask DataFrame objects.
    """
    # Construct mesh by indexing into vertices with simplex indices
    # TODO: For dask: avoid .compute() calls, and add winding auto-detection
    vertex_idxs = simplices.values[:, :3].astype(np.int64)
    vals = vertices.values.compute()[vertex_idxs]
    vals = vals.reshape(np.prod(vals.shape[:2]), vals.shape[2])
    res = pd.DataFrame(vals, columns=vertices.columns)

    # If vertices don't have weights, use simplex weights
    verts_have_weights = len(vertices.columns) > 2
    if not verts_have_weights:
        weight_col = simplices.columns[3]
        res[weight_col] = simplices.values[:, 3].compute().repeat(3)

    return res


def mesh(vertices, simplices):
    """Merge vertices and simplices into a triangular mesh, suitable to be
    passed into the ``Canvas.trimesh()`` method via the ``mesh``
    keyword-argument. Both arguments are assumed to be Dask DataFrame
    objects.
    """
    # Verify the simplex data structure
    assert simplices.values.shape[1] >= 3, ('At least three vertex columns '
                                            'are required for the triangle '
                                            'definition')
    simplices_all_ints = simplices.dtypes.iloc[:3].map(
        lambda dt: np.issubdtype(dt, np.integer)
    ).all()
    assert simplices_all_ints, ('Simplices must be integral. You may '
                                'consider casting simplices to integers '
                                'with ".astype(int)"')

    assert len(vertices.columns) > 2 or simplices.values.shape[1] > 3, 'If no vertex weight column is provided, a triangle weight column is required.'


    if isinstance(vertices, dd.DataFrame) and isinstance(simplices, dd.DataFrame):
        return _dd_mesh(vertices, simplices)

    return _pd_mesh(vertices, simplices)
