import numpy as np

from mpi4py import MPI
from pmesh.domain import GridND

from .utils import _get_box


COMM_WORLD = MPI.COMM_WORLD
COMM_SELF = MPI.COMM_SELF


def gather_array(data, root=0, mpicomm=COMM_WORLD):
    """
    Taken from https://github.com/bccp/nbodykit/blob/master/nbodykit/utils.py
    Gather the input data array from all ranks to the specified ``root``.
    This uses `Gatherv`, which avoids mpi4py pickling, and also
    avoids the 2 GB mpi4py limit for bytes using a custom datatype

    Parameters
    ----------
    data : array_like
        The data on each rank to gather.

    root : int, Ellipsis, default=0
        The rank number to gather the data to. If root is Ellipsis or None,
        broadcast the result to all ranks.

    mpicomm : MPI communicator, default=MPI.COMM_WORLD
        The MPI communicator.

    Returns
    -------
    recvbuffer : array_like, None
        The gathered data on root, and `None` otherwise.
    """
    if root is None: root = Ellipsis

    if all(mpicomm.allgather(np.isscalar(data))):
        if root is Ellipsis:
            return np.array(mpicomm.allgather(data))
        gathered = mpicomm.gather(data, root=root)
        if mpicomm.rank == root:
            return np.array(gathered)
        return None

    data = np.asarray(data)

    # need C-contiguous order
    if not data.flags['C_CONTIGUOUS']:
        data = np.ascontiguousarray(data)
    local_length = data.shape[0]

    # check dtypes and shapes
    shapes = mpicomm.allgather(data.shape)
    dtypes = mpicomm.allgather(data.dtype)

    # check for structured data
    if dtypes[0].char == 'V':

        # check for structured data mismatch
        names = set(dtypes[0].names)
        if any(set(dt.names) != names for dt in dtypes[1:]):
            raise ValueError('mismatch between data type fields in structured data')

        # check for 'O' data types
        if any(dtypes[0][name] == 'O' for name in dtypes[0].names):
            raise ValueError('object data types ("O") not allowed in structured data in gather_array')

        # compute the new shape for each rank
        newlength = mpicomm.allreduce(local_length)
        newshape = list(data.shape)
        newshape[0] = newlength

        # the return array
        if root is Ellipsis or mpicomm.rank == root:
            recvbuffer = np.empty(newshape, dtype=dtypes[0], order='C')
        else:
            recvbuffer = None

        for name in dtypes[0].names:
            d = gather_array(data[name], root=root, mpicomm=mpicomm)
            if root is Ellipsis or mpicomm.rank == root:
                recvbuffer[name] = d

        return recvbuffer

    # check for 'O' data types
    if dtypes[0] == 'O':
        raise ValueError('object data types ("O") not allowed in structured data in gather_array')

    # check for bad dtypes and bad shapes
    if root is Ellipsis or mpicomm.rank == root:
        bad_shape = any(s[1:] != shapes[0][1:] for s in shapes[1:])
        bad_dtype = any(dt != dtypes[0] for dt in dtypes[1:])
    else:
        bad_shape = None; bad_dtype = None

    if root is not Ellipsis:
        bad_shape, bad_dtype = mpicomm.bcast((bad_shape, bad_dtype), root=root)

    if bad_shape:
        raise ValueError('mismatch between shape[1:] across ranks in gather_array')
    if bad_dtype:
        raise ValueError('mismatch between dtypes across ranks in gather_array')

    shape = data.shape
    dtype = data.dtype

    # setup the custom dtype
    duplicity = np.product(np.array(shape[1:], 'intp'))
    itemsize = duplicity * dtype.itemsize
    dt = MPI.BYTE.Create_contiguous(itemsize)
    dt.Commit()

    # compute the new shape for each rank
    newlength = mpicomm.allreduce(local_length)
    newshape = list(shape)
    newshape[0] = newlength

    # the return array
    if root is Ellipsis or mpicomm.rank == root:
        recvbuffer = np.empty(newshape, dtype=dtype, order='C')
    else:
        recvbuffer = None

    # the recv counts
    counts = mpicomm.allgather(local_length)
    counts = np.array(counts, order='C')

    # the recv offsets
    offsets = np.zeros_like(counts, order='C')
    offsets[1:] = counts.cumsum()[:-1]

    # gather to root
    if root is Ellipsis:
        mpicomm.Allgatherv([data, dt], [recvbuffer, (counts, offsets), dt])
    else:
        mpicomm.Gatherv([data, dt], [recvbuffer, (counts, offsets), dt], root=root)

    dt.Free()
    return recvbuffer


def local_size(size, mpicomm=COMM_WORLD):
    """
    Divide global ``size`` into local (process) size.

    Parameters
    ----------
    size : int
        Global size.

    mpicomm : MPI communicator, default=MPI.COMM_WORLD
        The MPI communicator.

    Returns
    -------
    localsize : int
        Local size. Sum of local sizes over all processes equals global size.
    """
    start = mpicomm.rank * size // mpicomm.size
    stop = (mpicomm.rank + 1) * size // mpicomm.size
    return stop - start


def scatter_array(data, counts=None, root=0, mpicomm=COMM_WORLD):
    """
    Taken from https://github.com/bccp/nbodykit/blob/master/nbodykit/utils.py
    Scatter the input data array across all ranks, assuming `data` is
    initially only on `root` (and `None` on other ranks).
    This uses ``Scatterv``, which avoids mpi4py pickling, and also
    avoids the 2 GB mpi4py limit for bytes using a custom datatype

    Parameters
    ----------
    data : array_like or None
        On `root`, this gives the data to split and scatter.

    counts : list of int
        List of the lengths of data to send to each rank.

    root : int, default=0
        The rank number that initially has the data.

    mpicomm : MPI communicator, default=MPI.COMM_WORLD
        The MPI communicator.

    Returns
    -------
    recvbuffer : array_like
        The chunk of `data` that each rank gets.
    """
    if counts is not None:
        counts = np.asarray(counts, order='C')
        if len(counts) != mpicomm.size:
            raise ValueError('counts array has wrong length!')

    # check for bad input
    if mpicomm.rank == root:
        bad_input = not isinstance(data, np.ndarray)
    else:
        bad_input = None
    bad_input = mpicomm.bcast(bad_input, root=root)
    if bad_input:
        raise ValueError('`data` must by numpy array on root in scatter_array')

    if mpicomm.rank == root:
        # need C-contiguous order
        if not data.flags['C_CONTIGUOUS']:
            data = np.ascontiguousarray(data)
        shape_and_dtype = (data.shape, data.dtype)
    else:
        shape_and_dtype = None

    # each rank needs shape/dtype of input data
    shape, dtype = mpicomm.bcast(shape_and_dtype, root=root)

    # object dtype is not supported
    fail = False
    if dtype.char == 'V':
        fail = any(dtype[name] == 'O' for name in dtype.names)
    else:
        fail = dtype == 'O'
    if fail:
        raise ValueError('"object" data type not supported in scatter_array; please specify specific data type')

    # initialize empty data on non-root ranks
    if mpicomm.rank != root:
        np_dtype = np.dtype((dtype, shape[1:]))
        data = np.empty(0, dtype=np_dtype)

    # setup the custom dtype
    duplicity = np.product(np.array(shape[1:], 'intp'))
    itemsize = duplicity * dtype.itemsize
    dt = MPI.BYTE.Create_contiguous(itemsize)
    dt.Commit()

    # compute the new shape for each rank
    newshape = list(shape)

    if counts is None:
        newshape[0] = newlength = local_size(shape[0], mpicomm=mpicomm)
    else:
        if counts.sum() != shape[0]:
            raise ValueError('the sum of the `counts` array needs to be equal to data length')
        newshape[0] = counts[mpicomm.rank]

    # the return array
    recvbuffer = np.empty(newshape, dtype=dtype, order='C')

    # the send counts, if not provided
    if counts is None:
        counts = mpicomm.allgather(newlength)
        counts = np.array(counts, order='C')

    # the send offsets
    offsets = np.zeros_like(counts, order='C')
    offsets[1:] = counts.cumsum()[:-1]

    # do the scatter
    mpicomm.Barrier()
    mpicomm.Scatterv([data, (counts, offsets), dt], [recvbuffer, dt], root=root)
    dt.Free()
    return recvbuffer


def domain_decompose(mpicomm, smoothing, positions1, weights1=None, positions2=None, weights2=None, boxsize=None, domain_factor=None):
    """
    Adapted from https://github.com/bccp/nbodykit/blob/master/nbodykit/algorithms/pair_counters/domain.py.
    Decompose positions and weights on a grid of MPI processes.
    Requires mpi4py and pmesh.

    Parameters
    ----------
    mpicomm : MPI communicator
        The MPI communicator.

    smoothing : float
        The maximum Cartesian separation implied by the user's binning.

    positions1 : array of shape (N, 3)
        Positions in the first catalog.

    positions2 : array of shape (N, 3), default=None
        Optionally, for cross-pair counts, positions in the second catalog. See ``positions1``.

    weights1 : list, array, default=None
        Optionally, weights of the first catalog.

    weights2 : list, array, default=None
        Optionally, weights in the second catalog.

    boxsize : array, default=None
        For periodic wrapping, the 3 side-lengths of the periodic cube.

    domain_factor : int, default=None
        Multiply the size of the MPI mesh by this factor.
        If ``None``, defaults to 2 in case ``boxsize`` is ``None``,
        else (periodic wrapping) 1.

    Returns
    -------
    (positions1, weights1), (positions2, weights2) : arrays
        The (decomposed) set of positions and weights.
    """
    autocorr = positions2 is None
    if autocorr:
        positions2 = positions1
        weights2 = weights1

    if mpicomm.size == 1 or mpicomm.allreduce(len(positions1)) == 0 or mpicomm.allreduce(len(positions2)) == 0:
        return (positions1, weights1), (positions2, weights2)

    def split_size_3d(s):
        """
        Split `s` into three integers, a, b, c, such
        that a * b * c == s and a <= b <= c.
        """
        a = int(s ** (1. / 3.)) + 1
        while a > 1:
            if s % a == 0:
                s = s // a
                break
            a = a - 1
        b = int(s ** 0.5) + 1
        while b > 1:
            if s % b == 0:
                s = s // b
                break
            b = b - 1
        c = s
        return a, b, c

    periodic = boxsize is not None
    ngrid = split_size_3d(mpicomm.size)
    if domain_factor is None:
        domain_factor = 1 if periodic else 2
    ngrid *= domain_factor

    size1 = mpicomm.allreduce(len(positions1))

    cpositions1 = positions1
    cpositions2 = positions2

    if periodic:
        cpositions1 = cpositions1 % boxsize
    if autocorr:
        cpositions2 = cpositions1
    else:
        if periodic:
            cpositions2 = cpositions2 % boxsize

    if periodic:
        posmin = np.zeros_like(boxsize)
        posmax = np.asarray(boxsize)
    else:
        posmin, posmax = _get_box(*([cpositions1] if autocorr else [cpositions1, cpositions2]))
        posmin, posmax = np.min(mpicomm.allgather(posmin), axis=0), np.max(mpicomm.allgather(posmax), axis=0)
        posmin -= 1e-9  # margin to make sure all positions will be included
        posmax += 1e-9

    # domain decomposition
    grid = [np.linspace(pmin, pmax, grid + 1, endpoint=True) for pmin, pmax, grid in zip(posmin, posmax, ngrid)]
    domain = GridND(grid, comm=mpicomm, periodic=periodic)  # raises VisibleDeprecationWarning: Creating an ndarray from ragged nested sequences

    if not periodic:
        # balance the load
        domain.loadbalance(domain.load(cpositions1))

    # exchange first particles
    layout = domain.decompose(cpositions1, smoothing=0)
    positions1 = layout.exchange(positions1)  # exchange takes a list of arrays
    if weights1 is not None and len(weights1):
        multiple_weights = len(weights1) > 1
        weights1 = layout.exchange(*weights1, pack=False)
        if multiple_weights: weights1 = list(weights1)
        else: weights1 = [weights1]

    boxsize = posmax - posmin

    # exchange second particles
    if smoothing > boxsize.max() * 0.25:
        positions2 = gather_array(positions2, root=Ellipsis, mpicomm=mpicomm)
        if weights2 is not None: weights2 = [gather_array(w, root=Ellipsis, mpicomm=mpicomm) for w in weights2]
    else:
        layout = domain.decompose(cpositions2, smoothing=smoothing)
        positions2 = layout.exchange(positions2)
        if weights2 is not None and len(weights2):
            multiple_weights = len(weights2) > 1
            weights2 = layout.exchange(*weights2, pack=False)
            if multiple_weights: weights2 = list(weights2)
            else: weights2 = [weights2]

    nsize1 = mpicomm.allreduce(len(positions1))
    assert nsize1 == size1, 'some particles1 disappeared (after: {:d} v.s. before: {:d})...'.format(nsize1, size1)

    return (positions1, weights1), (positions2, weights2)
