"""
"""
from affine import Affine


class WindowFromSlice(object):
    def __getitem__(self, yx):
        """ Translate numpy-like slices to rasterio window tuples.
        """
        assert isinstance(yx, tuple) and len(yx) == 2
        y, x = yx
        return ((0 if y.start is None else y.start, y.stop),
                (0 if x.start is None else x.start, x.stop))


w_ = WindowFromSlice()


def polygon_path(x, y=None):
    """A little bit like numpy.meshgrid, except returns only boundary values and
    limited to 2d case only.

    Examples:
      [0,1], [3,4] =>
      array([[0, 1, 1, 0, 0],
             [3, 3, 4, 4, 3]])

      [0,1] =>
      array([[0, 1, 1, 0, 0],
             [0, 0, 1, 1, 0]])
    """
    import numpy as np

    if y is None:
        y = x

    return np.vstack([
        np.vstack([x, np.full_like(x, y[0])]).T,
        np.vstack([np.full_like(y, x[-1]), y]).T[1:],
        np.vstack([x, np.full_like(x, y[-1])]).T[::-1][1:],
        np.vstack([np.full_like(y, x[0]), y]).T[::-1][1:]]).T


def gbox_boundary(gbox, pts_per_side=16):
    """Return points in pixel space along the perimeter of a GeoBox, or a 2d array.

    """
    from numpy import linspace

    H, W = gbox.shape[:2]
    xx = linspace(0, W, pts_per_side, dtype='float32')
    yy = linspace(0, H, pts_per_side, dtype='float32')

    return polygon_path(xx, yy).T[:-1]


def decompose_rws(A):
    """Compute decomposition Affine matrix sans translation into Rotation Shear and Scale.

    Note: that there are ambiguities for negative scales.

    Example: R(90)*S(1,1) == R(-90)*S(-1,-1),
    (R*(-I))*((-I)*S) == R*S

    A = R W S

    Where:

    R [ca -sa]  W [1, w]  S [sx,  0]
      [sa  ca]    [0, 1]    [ 0, sy]

    """
    from numpy import diag, asarray
    from numpy.linalg import cholesky, det, inv

    if isinstance(A, Affine):
        def to_affine(m, t=(0, 0)):
            a, b, d, e = m.ravel()
            c, f = t
            return Affine(a, b, c,
                          d, e, f)

        (a, b, c,
         d, e, f,
         *_) = A
        R, W, S = decompose_rws(asarray([[a, b],
                                         [d, e]], dtype='float64'))

        return to_affine(R, (c, f)), to_affine(W), to_affine(S)

    assert A.shape == (2, 2)

    WS = cholesky(A.T @ A).T
    R = A @ inv(WS)

    if det(R) < 0:
        R[:, -1] *= -1
        WS[-1, :] *= -1

    ss = diag(WS)
    S = diag(ss)
    W = WS @ diag(1.0/ss)

    return R, W, S


def affine_from_pts(X, Y):
    """ Given points X,Y compute A, such that: Y = A*X.

        Needs at least 3 points.
    """
    from numpy import ones, vstack
    from numpy.linalg import lstsq

    assert len(X) == len(Y)
    assert len(X) >= 3

    n = len(X)

    XX = ones((n, 3), dtype='float64')
    YY = vstack(Y)
    for i, x in enumerate(X):
        XX[i, :2] = x

    mm, *_ = lstsq(XX, YY, rcond=None)
    a, d, b, e, c, f = mm.ravel()

    return Affine(a, b, c,
                  d, e, f)


def get_scale_at_point(pt, tr, r=None):
    """ Given an arbitrary locally linear transform estimate scale change around a point.

    1. Approximate Y = tr(X) as Y = A*X+t in the neighbourhood of pt, for X,Y in R2
    2. Extract scale components of A


    pt - estimate transform around this point
    r  - radius around the point (default 1)

    tr - List((x,y)) -> List((x,y))
         takes list of 2-d points on input and outputs same length list of 2d on output

    """
    pts0 = [(0, 0), (-1, 0), (0, -1), (1, 0), (0, 1)]
    x0, y0 = pt
    if r is None:
        XX = [(float(x+x0), float(y+y0)) for x, y in pts0]
    else:
        XX = [(float(x*r+x0), float(y*r+y0)) for x, y in pts0]
    YY = tr(XX)
    A = affine_from_pts(XX, YY)
    _, _, S = decompose_rws(A)
    return (S.a, S.e)


def native_pix_transform(src, dst):
    """

    direction: from src to dst
    .back: goes the other way
    """
    from types import SimpleNamespace
    from osgeo import osr

    # TODO: special case CRS_in == CRS_out
    #
    _in = SimpleNamespace(crs=src.crs._crs, A=src.transform)
    _out = SimpleNamespace(crs=dst.crs._crs, A=dst.transform)

    _fwd = osr.CoordinateTransformation(_in.crs, _out.crs)
    _bwd = osr.CoordinateTransformation(_out.crs, _in.crs)

    _fwd = (_in.A, _fwd, ~_out.A)
    _bwd = (_out.A, _bwd, ~_in.A)

    def transform(pts, params):
        A, f, B = params
        return [B*pt[:2] for pt in f.TransformPoints([A*pt[:2] for pt in pts])]

    def tr(pts):
        return transform(pts, _fwd)
    tr.back = lambda pts: transform(pts, _bwd)

    return tr


def scaled_down_geobox(src_geobox, scaler: int):
    """Given a source geobox and integer scaler compute geobox of a scaled down image.

        Output geobox will be padded when shape is not a multiple of scaler.
        Example: 5x4, scaler=2 -> 3x2

        NOTE: here we assume that pixel coordinates are 0,0 at the top-left
              corner of a top-left pixel.

    """
    from datacube.utils.geometry import GeoBox

    assert scaler > 1

    H, W = [X//scaler + (1 if X % scaler else 0)
            for X in src_geobox.shape]

    # Since 0,0 is at the corner of a pixel, not center, there is no
    # translation between pixel plane coords due to scaling
    A = src_geobox.transform * Affine.scale(scaler, scaler)

    return GeoBox(W, H, A, src_geobox.crs)


def align_down(x, align):
    return x - (x % align)


def align_up(x, align):
    return align_down(x+(align-1), align)


def compute_reproject_roi(src, dst, padding=1, align=None):
    """ Compute ROI of src to read.
    """
    import numpy as np
    pts_per_side = 5

    tr = native_pix_transform(src, dst)
    XY = np.vstack(tr.back(gbox_boundary(dst, pts_per_side)))

    _in = np.floor(XY.min(axis=0)).astype('int32') - padding
    _out = np.ceil(XY.max(axis=0)).astype('int32') + padding

    if align is not None:
        _in = align_down(_in, align)
        _out = align_up(_out, align)

    xx = np.asarray([_in[0], _out[0]])
    yy = np.asarray([_in[1], _out[1]])

    xx = np.clip(xx, 0, src.width, out=xx)
    yy = np.clip(yy, 0, src.height, out=yy)

    return (slice(yy[0], yy[1]), slice(xx[0], xx[1]))