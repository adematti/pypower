import os
import tempfile

import numpy as np
from matplotlib import pyplot as plt

from cosmoprimo.fiducial import DESI
from mockfactory import LagrangianLinearMock, Catalog
from mockfactory.make_survey import RandomBoxCatalog

from pypower import MeshFFTPower, CatalogFFTPower, CatalogMesh, PowerStatistic, utils, setup_logging


base_dir = '_catalog'
data_fn = os.path.join(base_dir, 'lognormal_data.fits')
randoms_fn = os.path.join(base_dir, 'lognormal_randoms.fits')


def save_lognormal():
    z = 1.
    boxsize = 600.
    boxcenter = 0.
    los = 'x'
    nbar = 1e-3
    bias = 2.0
    nmesh = 256
    seed = 42
    power = DESI().get_fourier().pk_interpolator().to_1d(z=z)
    f = 0.8
    mock = LagrangianLinearMock(power, nmesh=nmesh, boxsize=boxsize, boxcenter=boxcenter, seed=seed, unitary_amplitude=True)
    mock.set_real_delta_field(bias=bias-1.)
    mock.set_analytic_selection_function(nbar=nbar)
    mock.poisson_sample(seed=seed, resampler='cic', compensate=True)
    mock.set_rsd(f=f, los=los)
    #mock.set_rsd(f=f)
    data = mock.to_catalog()
    offset = mock.boxcenter - mock.boxsize / 2.
    data['Position'] = (data['Position'] - offset) % mock.boxsize + offset
    randoms = RandomBoxCatalog(nbar=4.*nbar, boxsize=boxsize, boxcenter=boxcenter, seed=44)

    for catalog in [data, randoms]:
        catalog['NZ'] = nbar*catalog.ones()
        catalog['WEIGHT_FKP'] = np.ones(catalog.size, dtype='f8')

    data.save_fits(data_fn)
    randoms.save_fits(randoms_fn)


def test_power_statistic():
    edges = np.linspace(0., 0.2, 11)
    modes = (edges[:-1] + edges[1:])/2.
    power = np.ones_like(modes)
    nmodes = np.ones_like(modes, dtype='i8')
    ells = (0, 2, 4)
    power = PowerStatistic(edges, modes, power, nmodes, ells, statistic='multipole')
    power.rebin(factor=2)
    assert np.allclose(power.k, (modes[::2] + modes[1::2])/2.)
    assert np.allclose(power.kedges, np.linspace(0., 0.2, 6))
    assert power.shape == (modes.size//2,)
    with tempfile.TemporaryDirectory() as tmp_dir:
        fn = os.path.join(tmp_dir, 'tmp.npy')
        power.save(fn)
        test = PowerStatistic.load(fn)
        assert np.all(test.power == power.power)


def test_mesh_power():
    boxsize = 600.
    boxcenter = 0.
    nmesh = 256
    kedges = np.linspace(0., 0.1, 6)
    muedges = np.linspace(-1., 1., 4)
    dk = kedges[1] - kedges[0]
    ells = (0, 2, 4)
    resampler = 'cic'
    interlacing = 2
    dtype = 'f8'
    data = Catalog.load_fits(data_fn)
    #randoms = Catalog.load_fits(randoms_fn)

    def get_ref_power(data):
        from nbodykit.lab import FFTPower
        mesh = data.to_nbodykit().to_mesh(position='Position', BoxSize=boxsize, Nmesh=nmesh, resampler=resampler, interlaced=bool(interlacing), compensated=True, dtype=dtype)
        return FFTPower(mesh, mode='2d', poles=ells, Nmu=len(muedges) - 1, los=[1,0,0], dk=dk, kmin=kedges[0], kmax=kedges[-1]+1e-9)

    def get_mesh_power(data, los):
        mesh = CatalogMesh(data_positions=data['Position'], boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos', dtype=dtype)
        return MeshFFTPower(mesh, ells=ells, los=los, edges=(kedges, muedges))

    def get_mesh_power_cross(data, los):
        mesh1 = CatalogMesh(data_positions=data['Position'].T, boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='xyz')
        mesh2 = CatalogMesh(data_positions=data['Position'], boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos')
        return MeshFFTPower(mesh1, mesh2=mesh2, ells=ells, los=los, edges=kedges)

    ref_power = get_ref_power(data)
    #ref_norm = ref_power.attrs['norm']

    for los in [[1,0,0], 'x']:
        result = get_mesh_power(data, los=los)

        with tempfile.TemporaryDirectory() as tmp_dir:
            fn = os.path.join(tmp_dir, 'tmp.npy')
            result.save(fn)
            result = MeshFFTPower.load(fn)

        power = result.wedges
        for imu, mu in enumerate(power.mu.T):
            mu = np.mean(mu, axis=-1) # average over k
            assert np.allclose(power(mu=mu) + power.shotnoise, ref_power.power['power'][:,imu], atol=1e-6, rtol=3e-3)

        power = result.poles
        #norm = power.wnorm
        for ell in ells:
            #print(power(ell=ell).real + (ell == 0)*power.shotnoise / ref_power.poles['power_{}'.format(ell)].real)
            #assert np.allclose(power(ell=ell).real + (ell == 0)*power.shotnoise, ref_power.poles['power_{}'.format(ell)].real, atol=1e-6, rtol=3e-3)
            assert np.allclose(power(ell=ell) + (ell == 0)*power.shotnoise, ref_power.poles['power_{}'.format(ell)], atol=1e-6, rtol=3e-3)

    power_cross = get_mesh_power_cross(data, los=los).poles
    for ell in ells:
        assert np.allclose(power_cross(ell=ell) - (ell == 0)*power.shotnoise, power(ell=ell))


def test_norm():
    boxsize = 1000.
    nmesh = 128
    resampler = 'tsc'
    interlacing = False
    boxcenter = np.array([3000.,0.,0.])[None,:]
    dtype = 'f8'
    los = None
    data = Catalog.load_fits(data_fn)
    randoms = Catalog.load_fits(randoms_fn)
    for catalog in [data, randoms]:
        catalog['Position'] += boxcenter
        catalog['Weight'] = catalog.ones()
    mesh = CatalogMesh(data_positions=data['Position'], data_weights=data['Weight'], randoms_positions=randoms['Position'], randoms_weights=randoms['Weight'],
                       boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos', dtype=dtype)

    from pypower.fft_power import normalization, normalization_from_nbar
    old = normalization_from_nbar(randoms['NZ'], randoms['Weight'], data_weights=data['Weight'], mpicomm=mesh.mpicomm)
    new = normalization(mesh)
    assert np.allclose(new, old, atol=0, rtol=1e-1)


def test_catalog_power():
    boxsize = 1000.
    nmesh = 128
    kedges = np.linspace(0., 0.1, 6)
    dk = kedges[1] - kedges[0]
    ells = (0, 1, 2, 3, 4)
    resampler = 'tsc'
    interlacing = 2
    boxcenter = np.array([3000.,0.,0.])[None,:]
    dtype = 'f8'
    cdtype = 'c16'
    los = None
    data = Catalog.load_fits(data_fn)
    randoms = Catalog.load_fits(randoms_fn)
    for catalog in [data, randoms]:
        catalog['Position'] += boxcenter
        catalog['Weight'] = catalog.ones()

    def get_ref_power(data, randoms):
        from nbodykit.lab import FKPCatalog, ConvolvedFFTPower
        fkp = FKPCatalog(data.to_nbodykit(), randoms.to_nbodykit(), nbar='NZ')
        mesh = fkp.to_mesh(position='Position', comp_weight='Weight', nbar='NZ', BoxSize=boxsize, Nmesh=nmesh, resampler=resampler, interlaced=bool(interlacing), compensated=True, dtype=cdtype)
        return ConvolvedFFTPower(mesh, poles=ells, dk=dk, kmin=kedges[0], kmax=kedges[-1]+1e-9)

    def get_catalog_power(data, randoms, position_type='pos'):
        data_positions, randoms_positions = data['Position'], randoms['Position']
        if position_type == 'xyz':
            data_positions, randoms_positions = data['Position'].T, randoms['Position'].T
        elif position_type == 'rdd':
            data_positions, randoms_positions = utils.cartesian_to_sky(data['Position'].T), utils.cartesian_to_sky(randoms['Position'].T)
        return CatalogFFTPower(data_positions1=data_positions, data_weights1=data['Weight'], randoms_positions1=randoms_positions, randoms_weights1=randoms['Weight'],
                               boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, ells=ells, los=los, edges=kedges, position_type=position_type, dtype=dtype)

    def get_catalog_mesh_power(data, randoms):
        mesh = CatalogMesh(data_positions=data['Position'], data_weights=data['Weight'], randoms_positions=randoms['Position'], randoms_weights=randoms['Weight'],
                            boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos', dtype=dtype)
        return MeshFFTPower(mesh, ells=ells, los=los, edges=kedges)

    ref_power = get_ref_power(data, randoms)
    ref_norm = ref_power.attrs['randoms.norm']

    for position_type in ['pos', 'xyz', 'rdd']:
        result = get_catalog_power(data, randoms, position_type=position_type)

        with tempfile.TemporaryDirectory() as tmp_dir:
            fn = os.path.join(tmp_dir, 'tmp.npy')
            result.save(fn)
            result = CatalogFFTPower.load(fn)

        power = result.poles
        norm = power.wnorm

        for ell in ells:
            #print((power(ell=ell).real + (ell == 0)*power.shotnoise)*norm/ref_norm / ref_power.poles['power_{}'.format(ell)].real)
            # precision is 1e-3 if offset = self.boxcenter - self.boxsize/2. + 0.5*self.boxsize
            #assert np.allclose((power(ell=ell).real + (ell == 0)*power.shotnoise)*norm/ref_norm, ref_power.poles['power_{}'.format(ell)].real, atol=1e-6, rtol=3e-2)
            assert np.allclose((power(ell=ell) + (ell == 0)*power.shotnoise)*norm/ref_norm, ref_power.poles['power_{}'.format(ell)], atol=1e-6, rtol=3e-2)

    power_mesh = get_catalog_mesh_power(data, randoms).poles
    for ell in ells:
        assert np.allclose(power_mesh(ell=ell), power(ell=ell))

    def get_catalog_power_cross(data, randoms):
        return CatalogFFTPower(data_positions1=data['Position'].T, data_weights1=data['Weight'], randoms_positions1=randoms['Position'].T, randoms_weights1=randoms['Weight'],
                               data_positions2=data['Position'].T, data_weights2=data['Weight'], randoms_positions2=randoms['Position'].T, randoms_weights2=randoms['Weight'],
                               boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, ells=ells, los=los, edges=kedges, position_type='xyz')

    power_cross = get_catalog_power_cross(data,randoms).poles
    for ell in ells:
        assert np.allclose(power_cross(ell=ell) - (ell == 0)*power.shotnoise, power(ell=ell))


if __name__ == '__main__':

    setup_logging()
    #save_lognormal()

    test_power_statistic()
    test_mesh_power()
    test_catalog_power()
    test_norm()
