"""
This module got merged with the pscmp module and is now part of the pyrocko
library and may be removed from the beat repository in the future.
"""

import logging
import os

import math

import signal

from subprocess import Popen, PIPE
from os.path import join as pjoin

from pyrocko.guts import Float, Int, Tuple, Object, String
from pyrocko import cake
from pyrocko import gf


km = 1000.

guts_prefix = 'pf'

Timing = gf.meta.Timing

logger = logging.getLogger('PsGrn')

# how to call the programs
program_bins = {
    'psgrn.2008a': 'fomosto_psgrn2008a'
}

psgrn_components = 'ep ss ds cl'.split()

psgrn_displ_names = ('uz', 'ur', 'ut')
psgrn_stress_names = ('szz', 'srr', 'stt', 'szr', 'srt', 'stz')
psgrn_tilt_names = ('tr', 'tt', 'rot')
psgrn_gravity_names = ('gd', 'gr')


def nextpow2(i):
    return 2 ** int(math.ceil(math.log(i) / math.log(2.)))


def str_float_vals(vals):
    return ' '.join('%e' % val for val in vals)


def str_int_vals(vals):
    return ' '.join('%i' % val for val in vals)


def str_str_vals(vals):
    return ' '.join("'%s'" % val for val in vals)


def cake_model_to_config(mod):
    # fix elasticity params here !!! todo future include properly into cake ...
    eta1 = 0.
    eta2 = 0.
    alpha = 1.

    k = 1000.
    srows = []
    for i, row in enumerate(mod.to_scanlines()):
        depth, vp, vs, rho, qp, qs = row
        # replace qs with etas = 0.
        row = [depth / k, vp / k, vs / k, rho, eta1, eta2, alpha]
        srows.append('%i %15s' % (i + 1, str_float_vals(row)))

    return '\n'.join(srows), len(srows)


class PsGrnSpatialSampling(Object):
    n_steps = Int.T(default=10)
    start_distance = Float.T(default=0.)    # start sampling [km] from source
    end_distance = Float.T(default=100.)    # end

    def string_for_config(self):
        return '%i %15e %15e' % (self.n_steps, self.start_distance,
                                                        self.end_distance)


class PsGrnConfig(Object):

    psgrn_version = String.T(default='2008a')

    n_snapshots = Int.T(default=1)
    max_time = Float.T(default=1.)

    observation_depth = Float.T(default=0.)
    distance_grid = PsGrnSpatialSampling(n_steps=100,
                                         start_distance=0., end_distance=50.)
    depth_grid = PsGrnSpatialSampling(n_steps=100,
                                         start_distance=0., end_distance=40.)

    def items(self):
        return dict(self.T.inamevals(self))


class PsGrnConfigFull(PsGrnConfig):

    earthmodel_1d = gf.meta.Earthmodel1D.T(optional=True)
    psgrn_outdir = String.T(default='psgrn_green/')

    sampling_interval = Float.T(default=1.0)    # 1.0 for equidistant

    sw_source_regime = Int.T(default=1)         # 1-continental, 0-ocean
    sw_gravity = Int.T(default=0)

    accuracy_wavenumber_integration = Float.T(default=0.025)

    displ_filenames = Tuple.T(3, String.T(), default=psgrn_displ_names)
    stress_filenames = Tuple.T(6, String.T(), default=psgrn_stress_names)
    tilt_filenames = Tuple.T(3, String.T(), psgrn_tilt_names)
    gravity_filenames = Tuple.T(2, String.T(), psgrn_gravity_names)

    @staticmethod
    def example():
        conf = PsGrnConfigFull()
        conf.earthmodel_1d = cake.load_model().extract(depth_max=100 * km)
        conf.psgrn_outdir = 'TEST_psgrn_functions/'
        return conf

    def string_for_config(self):

        assert self.earthmodel_1d is not None

        d = self.__dict__.copy()

        model_str, nlines = cake_model_to_config(self.earthmodel_1d)
        d['n_model_lines'] = nlines
        d['model_lines'] = model_str

        d['str_psgrn_outdir'] = "'%s'" % './'

        d['str_displ_filenames'] = str_str_vals(self.displ_filenames)
        d['str_stress_filenames'] = str_str_vals(self.stress_filenames)
        d['str_tilt_filenames'] = str_str_vals(self.tilt_filenames)
        d['str_gravity_filenames'] = str_str_vals(self.gravity_filenames)

        d['str_distance_grid'] = self.distance_grid.string_for_config()
        d['str_depth_grid'] = self.depth_grid.string_for_config()

        template = '''# autogenerated PSGRN input by psgrn.py
#=============================================================================
# This is input file of FORTRAN77 program "psgrn08a" for computing responses
# (Green's functions) of a multi-layered viscoelastic halfspace to point
# dislocation sources buried at different depths. All results will be stored in
# the given directory and provide the necessary data base for the program
# "pscmp07a" for computing time-dependent deformation, geoid and gravity changes
# induced by an earthquake with extended fault planes via linear superposition.
# For more details, please read the accompanying READ.ME file.
#
# written by Rongjiang Wang
# GeoForschungsZentrum Potsdam
# e-mail: wang@gfz-potsdam.de
# phone +49 331 2881209
# fax +49 331 2881204
#
# Last modified: Potsdam, Jan, 2008
#
#################################################################
##                                                             ##
## Cylindrical coordinates (Z positive downwards!) are used.   ##
##                                                             ##
## If not specified otherwise, SI Unit System is used overall! ##
##                                                             ##
#################################################################
#
#------------------------------------------------------------------------------
#
#	PARAMETERS FOR SOURCE-OBSERVATION CONFIGURATIONS
#	================================================
# 1. the uniform depth of the observation points [km], switch for oceanic (0)
#    or continental(1) earthquakes;
# 2. number of (horizontal) observation distances (> 1 and <= nrmax defined in
#    psgglob.h), start and end distances [km], ratio (>= 1.0) between max. and
#    min. sampling interval (1.0 for equidistant sampling);
# 3. number of equidistant source depths (>= 1 and <= nzsmax defined in
#    psgglob.h), start and end source depths [km];
#
#    r1,r2 = minimum and maximum horizontal source-observation
#            distances (r2 > r1).
#    zs1,zs2 = minimum and maximum source depths (zs2 >= zs1 > 0).
#
#    Note that the same sampling rates dr_min and dzs will be used later by the
#    program "pscmp07a" for discretizing the finite source planes to a 2D grid
#    of point sources.
#------------------------------------------------------------------------------
        %(observation_depth)e  %(sw_source_regime)i
 %(str_distance_grid)s  %(sampling_interval)e
 %(str_depth_grid)s
#------------------------------------------------------------------------------
#
#	PARAMETERS FOR TIME SAMPLING
#	============================
# 1. number of time samples (<= ntmax def. in psgglob.h) and time window [days].
#
#    Note that nt (> 0) should be power of 2 (the fft-rule). If nt = 1, the
#    coseismic (t = 0) changes will be computed; If nt = 2, the coseismic
#    (t = 0) and steady-state (t -> infinity) changes will be computed;
#    Otherwise, time series for the given time samples will be computed.
#
#------------------------------------------------------------------------------
 %(n_snapshots)i    %(max_time)f
#------------------------------------------------------------------------------
#
#	PARAMETERS FOR WAVENUMBER INTEGRATION
#	=====================================
# 1. relative accuracy of the wave-number integration (suggested: 0.1 - 0.01)
# 2. factor (> 0 and < 1) for including influence of earth's gravity on the
#    deformation field (e.g. 0/1 = without / with 100percent gravity effect).
#------------------------------------------------------------------------------
 %(accuracy_wavenumber_integration)e
 %(sw_gravity)i
#------------------------------------------------------------------------------
#
#	PARAMETERS FOR OUTPUT FILES
#	===========================
#
# 1. output directory
# 2. file names for 3 displacement components (uz, ur, ut)
# 3. file names for 6 stress components (szz, srr, stt, szr, srt, stz)
# 4. file names for radial and tangential tilt components (as measured by a
#    borehole tiltmeter), rigid rotation of horizontal plane, geoid and gravity
#    changes (tr, tt, rot, gd, gr)
#
#    Note that all file or directory names should not be longer than 80
#    characters. Directory and subdirectoy names must be separated and ended
#    by / (unix) or \ (dos)! All file names should be given without extensions
#    that will be appended automatically by ".ep" for the explosion (inflation)
#    source, ".ss" for the strike-slip source, ".ds" for the dip-slip source,
#    and ".cl" for the compensated linear vector dipole source)
#
#------------------------------------------------------------------------------
 %(str_psgrn_outdir)s
 %(str_displ_filenames)s
 %(str_stress_filenames)s
 %(str_tilt_filenames)s %(str_gravity_filenames)s
#------------------------------------------------------------------------------
#
#	GLOBAL MODEL PARAMETERS
#	=======================
# 1. number of data lines of the layered model (<= lmax as defined in psgglob.h)
#
#    The surface and the upper boundary of the half-space as well as the
#    interfaces at which the viscoelastic parameters are continuous, are all
#    defined by a single data line; All other interfaces, at which the
#    viscoelastic parameters are discontinuous, are all defined by two data
#    lines (upper-side and lower-side values). This input format could also be
#    used for a graphic plot of the layered model. Layers which have different
#    parameter values at top and bottom, will be treated as layers with a
#    constant gradient, and will be discretised to a number of homogeneous
#    sublayers. Errors due to the discretisation are limited within about
#    5percent (changeable, see psgglob.h).
#
# 2....	parameters of the multilayered model
#
#    Burgers rheology (a Kelvin-Voigt body and a Maxwell body in series
#    connection) for relaxation of shear modulus is implemented. No relaxation
#    of compressional modulus is considered.
#
#    eta1  = transient viscosity (dashpot of the Kelvin-Voigt body; <= 0 means
#            infinity value)
#    eta2  = steady-state viscosity (dashpot of the Maxwell body; <= 0 means
#            infinity value)
#    alpha = ratio between the effective and the unrelaxed shear modulus
#            = mu1/(mu1+mu2) (> 0 and <= 1)
#
#    Special cases:
#        (1) Elastic: eta1 and eta2 <= 0 (i.e. infinity); alpha meaningless
#        (2) Maxwell body: eta1 <= 0 (i.e. eta1 = infinity)
#                          or alpha = 1 (i.e. mu1 = infinity)
#        (3) Standard-Linear-Solid: eta2 <= 0 (i.e. infinity)
#------------------------------------------------------------------------------
 %(n_model_lines)i                               |int: no_model_lines;
#------------------------------------------------------------------------------
# no  depth[km]  vp[km/s]  vs[km/s]  rho[kg/m^3] eta1[Pa*s] eta2[Pa*s] alpha
#------------------------------------------------------------------------------
%(model_lines)s
#=======================end of input===========================================
'''  # noqa
        return template % d


class PsGrnError(gf.store.StoreError):
    pass


class Interrupted(gf.store.StoreError):
    def __str__(self):
        return 'Interrupted.'


def remove_if_exists(fn, force=False):
    if os.path.exists(fn):
        if force:
            os.remove(fn)
        else:
            raise gf.CannotCreate('file %s already exists' % fn)


class PsGrnRunner:

    def __init__(self, outdir):
        if not os.path.exists(outdir):
            os.mkdir(outdir)
        self.outdir = outdir
        self.config = None

    def run(self, config, force=False):
        self.config = config

        input_fn = pjoin(self.outdir, 'input')

        remove_if_exists(input_fn, force=force)

        f = open(input_fn, 'w')
        input_str = config.string_for_config()

        logger.debug('===== begin psgrn input =====\n'
                     '%s===== end psgrn input =====' % input_str)

        f.write(input_str)
        f.close()
        program = program_bins['psgrn.%s' % config.psgrn_version]

        old_wd = os.getcwd()

        os.chdir(self.outdir)

        interrupted = []

        def signal_handler(signum, frame):
            os.kill(proc.pid, signal.SIGTERM)
            interrupted.append(True)

        original = signal.signal(signal.SIGINT, signal_handler)
        try:
            try:
                proc = Popen(program, stdin=PIPE, stdout=PIPE, stderr=PIPE)

            except OSError:
                os.chdir(old_wd)
                raise PsGrnError('could not start psgrn: "%s"' % program)

            (output_str, error_str) = proc.communicate('input\n')

        finally:
            signal.signal(signal.SIGINT, original)

        if interrupted:
            raise KeyboardInterrupt()

        logger.debug('===== begin psgrn output =====\n'
                     '%s===== end psgrn output =====' % output_str)

        errmess = []
        if proc.returncode != 0:
            errmess.append(
                'psgrn had a non-zero exit state: %i' % proc.returncode)

        if error_str:
            errmess.append('psgrn emitted something via stderr')

        if output_str.lower().find('error') != -1:
            errmess.append("the string 'error' appeared in psgrn output")

        if errmess:
            os.chdir(old_wd)
            raise PsGrnError('''
===== begin psgrn input =====
%s===== end psgrn input =====
===== begin psgrn output =====
%s===== end psgrn output =====
===== begin psgrn error =====
%s===== end psgrn error =====
%s
psgrn has been invoked as "%s"
in the directory %s'''.lstrip() % (
                input_str, output_str, error_str, '\n'.join(errmess), program,
                self.outdir))

        self.psgrn_output = output_str
        self.psgrn_error = error_str

        os.chdir(old_wd)
