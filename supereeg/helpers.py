from __future__ import division
from __future__ import print_function

# from builtins import zip
from builtins import map
from builtins import range
from builtins import object
import multiprocessing
import copy
import os
import numpy.matlib as mat
import pandas as pd
import nibabel as nb
import numpy as np
import imageio
import glob

from nilearn.input_data import NiftiMasker
from nilearn import plotting as ni_plt
from nilearn import image
from scipy.stats import kurtosis, zscore, pearsonr
from scipy.spatial.distance import pdist
from scipy.spatial.distance import cdist
from scipy.spatial.distance import squareform
from scipy import linalg
from joblib import Parallel, delayed


def _apply_by_file_index(bo, xform, aggregator):
    """
    Session dependent function application and aggregation

    Parameters
    ----------
    bo : supereeg.Brain
        Contains data

    xform : function
        The function to apply to the data matrix from each filename

    aggregator: function
        Function for aggregating results across multiple iterations

    Returns
    ----------
    results : numpy ndarray
         Array of aggregated results

    """

    for idx, session in enumerate(bo.sessions.unique()):
        if idx is 0:
            results = xform(bo.get_data()[bo.sessions == session, :])
        else:
            results = aggregator(results, xform(bo.get_data()[bo.sessions == session, :]))

    return results


def _kurt_vals(bo):
    """
    Function that calculates maximum kurtosis values for each channel

    Parameters
    ----------
    bo : supereeg.Brain
        Contains data

    Returns
    ----------
    results: 1D ndarray
        Maximum kurtosis across sessions for each channel

    """

    def aggregate(prev, next):
        return np.max(np.vstack((prev, next)), axis=0)

    return _apply_by_file_index(bo, kurtosis, aggregate)


def _get_corrmat(bo):
    """
    Function that calculates the average subject level correlation matrix for brain object across session

    Parameters
    ----------
    bo : supereeg.Brain
        Contains data

    Returns
    ----------
    results: 2D np.ndarray
        The average correlation matrix across sessions

    """

    def aggregate(p, n):
        return p + n

    def zcorr(x):
        return _r2z(1 - squareform(pdist(x.T, 'correlation')))

    summed_zcorrs = _apply_by_file_index(bo, zcorr, aggregate)

    return _z2r(summed_zcorrs / len(bo.sessions.unique()))


def _z2r(z):
    """
    Function that calculates the inverse Fisher z-transformation

    Parameters
    ----------
    z : int or ndarray
        Fishers z transformed correlation value

    Returns
    ----------
    result : int or ndarray
        Correlation value

    """
    with np.errstate(invalid='ignore', divide='ignore'):
        return (np.exp(2 * z) - 1) / (np.exp(2 * z) + 1)


def _r2z(r):
    """
    Function that calculates the Fisher z-transformation

    Parameters
    ----------
    r : int or ndarray
        Correlation value

    Returns
    ----------
    result : int or ndarray
        Fishers z transformed correlation value

    """
    with np.errstate(invalid='ignore', divide='ignore'):
        return 0.5 * (np.log(1 + r) - np.log(1 - r))


def _rbf(x, center, width=20):
    """
    Radial basis function

    Parameters
    ----------
    x : ndarray
        Series of all coordinates (one per row) - R_full

    c : ndarray
        Series of subject's coordinates (one per row) - R_subj

    width : int
        Radius

    Returns
    ----------
    results : ndarray
        Matrix of _rbf weights for each subject coordinate for all coordinates

    """
    return np.exp(-cdist(x, center, metric='euclidean') ** 2 / float(width))


def tal2mni(r):
    """
    Convert coordinates (electrode locations) from Talairach to MNI space

    Parameters
    ----------
    r : ndarray
        Coordinate locations (Talairach space)

    Returns
    ----------
    results : ndarray
        Coordinate locations (MNI space)

    """

    rotmat = np.array([[1, 0, 0, 0], [0, 0.9988, 0.0500, 0], [0, -0.0500, 0.9988, 0], [0, 0, 0, 1.0000]])
    up = np.array([[0.9900, 0, 0, 0], [0, 0.9700, 0, 0], [0, 0, 0.9200, 0], [0, 0, 0, 1.0000]])
    down = np.array([[0.9900, 0, 0, 0], [0, 0.9700, 0, 0], [0, 0, 0.8400, 0], [0, 0, 0, 1.0000]])

    inpoints = np.c_[r, np.ones(r.shape[0], dtype=np.float)].T
    tmp = inpoints[2, :] < 0
    inpoints[:, tmp] = linalg.solve(np.dot(rotmat, down), inpoints[:, tmp])
    inpoints[:, ~tmp] = linalg.solve(np.dot(rotmat, up), inpoints[:, ~tmp])

    return _round_it(inpoints[0:3, :].T, 2)


def _uniquerows(x):
    """
    Finds unique rows

    Parameters
    ----------
    x : ndarray
        Coordinates

    Returns
    ----------
    results : ndarray
        unique rows

    """
    y = np.ascontiguousarray(x).view(np.dtype((np.void, x.dtype.itemsize * x.shape[1])))
    _, idx = np.unique(y, return_index=True)

    return x[idx]


def _expand_corrmat_fit(C, weights):
    """
    Gets full correlation matrix

    Parameters
    ----------
    C : Numpy array
        Subject's correlation matrix

    weights : Numpy array
        Weights matrix calculated using _rbf function matrix

    mode : str
        Specifies whether to compute over all elecs (fit mode) or just new elecs
        (predict mode)

    Returns
    ----------
    numerator : Numpy array
        Numerator for the expanded correlation matrix
    denominator : Numpy array
        Denominator for the expanded correlation matrix

    """
    C[np.eye(C.shape[0]) == 1] = 0
    C[np.where(np.isnan(C))] = 0

    n = weights.shape[0]
    K = np.zeros([n, n])
    W = np.zeros([n, n])
    Z = C

    s = 0

    vals = list(range(s, n))
    for x in vals:
        xweights = weights[x, :]

        vals = list(range(x))
        for y in vals:
            yweights = weights[y, :]

            next_weights = np.outer(xweights, yweights)
            next_weights = next_weights - np.triu(next_weights)

            W[x, y] = np.sum(next_weights)
            K[x, y] = np.sum(Z * next_weights)
    return (K + K.T), (W + W.T)


def _expand_corrmat_predict(C, weights):
    """
    Gets full correlation matrix

    Parameters
    ----------
    C : Numpy array
        Subject's correlation matrix

    weights : Numpy array
        Weights matrix calculated using _rbf function matrix

    mode : str
        Specifies whether to compute over all elecs (fit mode) or just new elecs
        (predict mode)

    Returns
    ----------
    numerator : Numpy array
        Numerator for the expanded correlation matrix
    denominator : Numpy array
        Denominator for the expanded correlation matrix

    """

    C[np.eye(C.shape[0]) == 1] = 0
    C[np.where(np.isnan(C))] = 0

    n = weights.shape[0]
    K = np.zeros([n, n])
    W = np.zeros([n, n])
    Z = C

    s = C.shape[0]
    sliced_up = [(x, y) for x in range(s, n) for y in range(x)]

    results = Parallel(n_jobs=multiprocessing.cpu_count())(
        delayed(_compute_coord)(coord, weights, Z) for coord in sliced_up)

    W[[x[0] for x in sliced_up], [x[1] for x in sliced_up]] = [x[0] for x in results]
    K[[x[0] for x in sliced_up], [x[1] for x in sliced_up]] = [x[1] for x in results]

    return (K + K.T), (W + W.T)


def _compute_coord(coord, weights, Z):
    next_weights = np.outer(weights[coord[0], :], weights[coord[1], :])
    next_weights = next_weights - np.triu(next_weights)
    return np.sum(next_weights), np.sum(Z * next_weights)


def _chunk_bo(bo, chunk):
    """
    Chunk brain object by session for reconstruction. Returns chunked indices

    Parameters
    ----------
    bo : supereeg.Brain
        Brain object used to reconstruct and data to chunk

    chunk : list
        Chunked indices

    Returns
    ----------
    nbo : brain object
        Chunked brain object with chunked zscored data in the data field

    """
    return bo.get_slice(times=[i for i in chunk if i is not None])


def _timeseries_recon(bo, K, chunk_size=1000):
    """
    Reconstruction done by chunking by session

    Parameters
    ----------
    bo : supereeg.Brain
        Copied brain object with zscored data

    K : correlation matrix
        Correlation matix including observed and predicted locations

    chunk_size : int
        Size to break data into

    Returns
    ----------
    results : ndarray
        Compiled reconstructed timeseries

    """
    zbo = copy.copy(bo)
    zbo.data = pd.DataFrame(bo.get_zscore_data())

    results = []
    for idx, session in enumerate(bo.sessions.unique()):
        block_results = []
        if idx is 0:
            for each in _chunker(zbo.sessions[bo.sessions == session].index.tolist(), chunk_size):
                z_bo = _chunk_bo(zbo, each)
                block = _reconstruct_activity(z_bo, K, zscored=True)
                if block_results == []:
                    block_results = block
                else:
                    block_results = np.vstack((block_results, block))
            results = block_results
        else:
            for each in _chunker(zbo.sessions[bo.sessions == session].index.tolist(), chunk_size):
                z_bo = _chunk_bo(zbo, each)
                block = _reconstruct_activity(z_bo, K, zscored=True)
                if block_results == []:
                    block_results = block
                else:
                    block_results = np.vstack((block_results, block))
            results = np.vstack((results, block_results))
    return results


def _chunker(iterable, chunksize, fillvalue=None):
    """
    Chunks longer sequence by regular interval

    Parameters
    ----------
    iterable : list or ndarray
        Use would be a long timeseries that needs to be broken down

    chunksize : int
        Size to break down

    Returns
    ----------
    results : ndarray
        Chunked timeseries

    """
    try:
        from itertools import zip_longest as zip_longest
    except:
        from itertools import izip_longest as zip_longest

    args = [iter(iterable)] * chunksize
    return list(zip_longest(*args, fillvalue=fillvalue))


def _reconstruct_activity(bo, K, zscored=False):
    """
    Reconstruct activity - need to add chunking option here

    Parameters
    ----------
    bo : supereeg.Brain
        brain object with zscored data

    K : correlation matrix
        Correlation matix including observed and predicted locations

    zscore = False

    Returns
    ----------
    results : ndarray
        Reconstructed timeseries

    """
    s = K.shape[0] - bo.locs.shape[0]
    Kba = K[:s, s:]
    Kaa = K[s:, s:]
    if zscored:
        Y = bo.get_data()
    else:
        Y = bo.get_zscore_data()
    return np.squeeze(np.dot(np.dot(Kba, np.linalg.pinv(Kaa)), Y.T).T)


def _round_it(locs, places):
    """
    Rounding function

    Parameters
    ----------
    locs : array or float
        Number be rounded

    places : int
        Number of places to round

    Returns
    ----------
    result : array or float
        Rounded number

    """
    return np.round(locs, decimals=places)


def filter_elecs(bo, measure='kurtosis', threshold=10):
    """
    Filter electrodes based on kurtosis value

    Parameters
    ----------
    bo : supereeg.Brain
        Brain object

    measure : 'kurtosis'
        Method to filter electrodes. Only kurtosis supported currently.

    threshold : int
        Threshold for filtering

    Returns
    ----------
    result : supereeg.Brain
        Brain object with electrodes and corresponding data that passes kurtosis thresholding

    """
    thresh_bool = bo.kurtosis > threshold
    nbo = copy.deepcopy(bo)
    nbo.data = bo.data.loc[:, ~thresh_bool]
    nbo.locs = bo.locs.loc[~thresh_bool]
    nbo.n_elecs = bo.data.shape[1]
    return nbo


def filter_subj(bo, measure='kurtosis', threshold=10):
    """
    Filter subjects if less than two electrodes pass kurtosis value

    Parameters
    ----------
    bo : supereeg.Brain
        Brain object

    measure : 'kurtosis'
        Method to filter electrodes. Only kurtosis supported currently.

    threshold : int
        Threshold for filtering.

    Returns
    ----------
    result : supereeg.Brain.meta or None
        Meta field from brain object if two or more electrodes pass kurtosis thresholding.

    """
    if not bo.meta is None:
        thresh_bool = bo.kurtosis > threshold
        if sum(~thresh_bool) < 2:
            print(bo.meta + ': not enough electrodes pass threshold')
        else:
            return bo.meta
    else:
        print('no meta data for brain object')


def _corr_column(X, Y):
    return np.array([pearsonr(x, y)[0] for x, y in zip(X.T, Y.T)])


def _normalize_Y(Y_matrix):
    """
    Normalizes timeseries

    Parameters
    ----------
    Y_matrix : ndarray
        Raw activity from each electrode channel

    Returns
    ----------
    results : ndarray
        Normalized activity from each electrode channel

    """
    Y = Y_matrix
    m = mat.repmat(np.min(Y, axis=0), Y.shape[0], 1)
    Y = Y - m
    m = mat.repmat(np.max(Y, axis=0), Y.shape[0], 1)
    Y = np.divide(Y, m)
    added = mat.repmat(0.5 + np.arange(Y.shape[1]), Y.shape[0], 1)
    Y = Y + added
    return pd.DataFrame(Y)


class BrainData(object):
    def __init__(self, fname, mask_strategy='background'):
        self.fname = fname
        if len(self.fname) == 0:
            self.Y = []
            self.R = []
            self.N = 0
            self.V = 0
            self._vox_size = (0, 0, 0)
            self.im_size = (0, 0, 0)
            self.mask = []
            self.img = []
        else:
            img = nb.load(self.fname)
            if not hasattr(img, 'dataobj'):
                print("Loading: " + self.fname + " [DISK READ]")
            else:
                print("Loading: " + self.fname + " [RAM CACHE]")

            self.mask = NiftiMasker(mask_strategy=mask_strategy)
            self.mask.fit(self.fname)

            hdr = img.get_header()
            S = img.get_sform()
            self._vox_size = hdr.get_zooms()
            self.im_size = img.shape

            if len(img.shape) > 3:
                self.N = img.shape[3]
            else:
                self.N = 1

            self.Y = self.mask.transform(self.fname)
            self.V = self.Y.shape[1]
            vmask = np.nonzero(np.array(
                np.reshape(self.mask.mask_img_.dataobj, (1, np.prod(self.mask.mask_img_.shape)), order='F')))[1]

            vox_coords = _fullfact(img.shape[0:3])[vmask, :]
            self.matrix_coordinates = vox_coords

            self.R = np.array(vox_coords * S[0:3, 0:3] + np.tile(S[0:3, 3].T, (self.V, 1)))


def _loadnii(fname, mask_strategy='background'):
    """
    Load nifti

    Parameters
    ----------
    fname : filepath
        Path to a template nifti file

    mask_strategy : str
        If mask_strategy is 'background', treat uniformly valued voxels at the outer parts
        of the images as background.

        If mask_strategy is 'epi', use nilearn's background detection strategy: find the least dense point
        of the histogram, between fractions lower_cutoff and upper_cutoff of the total image histogram.

    Returns
    ----------
    results : ndarray
        Normalized activity from each electrode channel

    """
    return BrainData(fname, mask_strategy)


def _fullfact(dims):
    """
    Replicates MATLAB's _fullfact function (behaves the same way)
    """
    vals = np.asmatrix(list(range(1, dims[0] + 1))).T
    if len(dims) == 1:
        return vals
    else:
        aftervals = np.asmatrix(_fullfact(dims[1:]))
        inds = np.asmatrix(np.zeros((np.prod(dims), len(dims))))
        row = 0
        for i in range(aftervals.shape[0]):
            inds[row:(row + len(vals)), 0] = vals
            inds[row:(row + len(vals)), 1:] = np.tile(aftervals[i, :], (len(vals), 1))
            row += len(vals)
        return inds


def model_compile(data):
    """
    Compile existing expanded correlation matrices.

    Parameters
    ----------
    data : list of model object file directories
        Compiles model objects

    Returns
    ----------
    model : supereeg.Model
        A new updated model object

    """
    from .load import load
    from .model import Model

    m = load(data[0])
    numerator = m.numerator
    denominator = m.denominator
    n_subs = 1

    for mo in data[1:]:
        m = load(mo)
        # numerator = np.nansum(np.dstack((numerator, m.numerator)), 2)
        numerator += m.numerator
        denominator += m.denominator
        n_subs += 1

    return Model(numerator=numerator, denominator=denominator,
                 locs=m.locs, n_subs=n_subs)
    ### this concatenation of locations doesn't work when updating an existing model (but would be necessary for a build)
    # return Model(numerator=numerator, denominator=denominator,
    #              locs=pd.concat([m.locs, bo.locs]), n_subs=n_subs)


def _near_neighbor(bo, mo, match_threshold='auto'):
    """
    Finds the nearest voxel for each subject's electrode location and uses
    that as revised electrodes location matrix in the prediction.

    Parameters
    ----------

    bo : supereeg.Brain
        Brain object to update

    mo : supereeg.Model
        Model object for the nearests locations used to predict

    match_threshold : 'auto', int, or None
        Threshold used to find nearest neighbor

        options:

        match_threshold = 'auto' : include only nearest neighbor if falls within one voxel distance

        match_threshold = None or 0 : set nearest_neighbor = False and proceed (only exact matches will be used)

        match_threshold > 0 : include only nearest neighbor that are within given distance

    Returns
    ----------
    bo : supereeg.Brain
        A new updated brain object

    """

    nbo = copy.deepcopy(bo)
    d = cdist(nbo.locs, mo.locs, metric='Euclidean')
    for i in range(len(nbo.locs)):
        min_ind = list(zip(*np.where(d == d.min())))[0]
        nbo.locs.iloc[min_ind[0], :] = mo.locs.iloc[min_ind[1], :]
        d[min_ind[0]] = np.inf
        d[:, min_ind[1]] = np.inf
    if not match_threshold is 0 or None:

        if match_threshold is 'auto':
            v_size = _vox_size(mo.locs)
            thresh_bool = abs(nbo.locs - bo.locs) > v_size
            thresh_bool = thresh_bool.any(1).ravel()
        else:
            thresh_bool = abs(nbo.locs - bo.locs) > match_threshold
            thresh_bool = thresh_bool.any(1).ravel()
            assert match_threshold > 0, 'Negative Euclidean distances are not allowed'
        nbo.data = nbo.data.loc[:, ~thresh_bool]
        nbo.locs = nbo.locs.loc[~thresh_bool]
        nbo.n_elecs = nbo.data.shape[1]
        nbo.kurtosis = nbo.kurtosis[~thresh_bool]
        return nbo
    else:
        return nbo


def _vox_size(locs):
    """
    Finds voxel size

    Parameters
    ----------
    locs : pandas DataFrame
        Locations in brain extracted from nifti

    Returns
    ----------
    results : ndarray
        1 x n_dims of voxel size

    """
    n_dims = locs.shape[1]
    v_size = np.zeros([1, n_dims])
    # make voxel function
    for i in np.arange(n_dims):
        a = np.unique(locs.iloc[:, i])
        dists = pdist(np.atleast_2d(a).T, 'euclidean')
        v_size[0][i] = np.min(dists[dists > 0])

    return v_size


def sort_unique_locs(locs):
    """
    Sorts unique locations

    Parameters
    ----------
    locs : pandas.DataFrame or numpy.ndarray
        Electrode locations

    Returns
    ----------
    results : numpy.ndarray
        Array of unique locations

    """
    if isinstance(locs, pd.DataFrame):
        unique_full_locs = np.vstack(set(map(tuple, locs.as_matrix())))
    elif isinstance(locs, np.ndarray):
        unique_full_locs = np.vstack(set(map(tuple, locs)))
    else:
        print('unknown location type')

    return unique_full_locs[unique_full_locs[:, 0].argsort(),]


def _count_overlapping(X, Y):
    """
    Finds overlapping locations (Y in X)

    Parameters
    ----------
    X : brain object or model object
        Electrode locations

    Y : brain object or model object
        Electrode locations

    Returns
    ----------
    results : ndarray
        Array of length(X.locs) with 0s and 1s, where 1s denote overlapping locations Y in X

    """

    return np.sum([(X.locs == y).all(1) for idy, y in Y.locs.iterrows()], 0).astype(bool)


def make_gif_pngs(nifti, gif_path, window_min=1000, window_max=1002, **kwargs):
    """
    Plots series of nifti timepoints as nilearn plot_glass_brain in .png format

    Parameters
    ----------
    nifti : nib.nifti1.Nifti1Image
        Nifti of reconconstruction

    result_dir : directory
        Directory to save .png files

    window_min : int
        Lower bound for time window.

    window_max : int
        Upper bound for time window.

    Returns
    ----------
    results : png
        Series of pngs

    """

    for i in range(window_min, window_max):
        nii_i = image.index_img(nifti, i)
        outfile = os.path.join(gif_path, str(i) + '.png')
        #ni_plt.plot_glass_brain(nii_i, output_file=outfile)
        ni_plt.plot_glass_brain(nii_i, output_file=outfile, **kwargs)

    images = []
    for file in os.listdir(gif_path):
        if file.endswith(".png"):
            images.append(imageio.imread(os.path.join(gif_path, file)))
    gif_outfile = os.path.join(gif_path, 'gif_' + str(window_min) + '_' + str(window_max) + '.gif')

    imageio.mimsave(gif_outfile, images)
