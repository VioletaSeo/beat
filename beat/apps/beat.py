#!/usr/bin/env python
import os
from os.path import join as pjoin

# disable internal blas parallelisation as we parallelise over chains
os.environ["OMP_NUM_THREADS"] = "1"

import logging
import sys
import copy
import shutil
from collections import OrderedDict

from optparse import OptionParser

from beat import heart, utility, inputf, plotting, config as bconfig
from beat.info import version
from beat.config import ffi_mode_str, geometry_mode_str
from beat.models import load_model, Stage, estimate_hypers, sample
from beat.backend import backend_catalog, extract_bounds_from_summary, \
                         thin_buffer
from beat.sampler import SamplingHistory
from beat.sampler.smc import sample_factor_final_stage

from beat.sources import MTSourceWithMagnitude
from beat.utility import list2string
from numpy import savez, atleast_2d, floor

from pyrocko import model, util
from pyrocko.trace import snuffle
from pyrocko.gf import LocalEngine

from pyrocko.guts import load, dump, Dict


logger = logging.getLogger('beat')


km = 1000.


def d2u(d):
    return dict((k.replace('-', '_'), v) for (k, v) in d.items())


subcommand_descriptions = {
    'init':           'create a new EQ model project, use only event'
                      ' name to skip catalog search',
    'import':         'import data or results, from external format or '
                      'modeling results, respectively',
    'update':         'update configuration file',
    'sample':         'sample the solution space of the problem',
    'build_gfs':      'build GF stores',
    'clone':          'clone EQ model project into new directory',
    'plot':           'plot specified setups or results',
    'check':          'check setup specific requirements',
    'summarize':      'collect results and create statistics',
    'export':         'export waveforms and displacement maps of'
                      ' specific solution(s)',
}

subcommand_usages = {
    'init':          'init <event_name> <event_date "YYYY-MM-DD"> '
                     '[options]',
    'import':        'import <event_name> [options]',
    'update':        'update <event_name> [options]',
    'sample':        'sample <event_name> [options]',
    'build_gfs':     'build_gfs <event_name> [options]',
    'clone':         'clone <event_name> <cloned_event_name> [options]',
    'plot':          'plot <event_name> <plot_type> [options]',
    'check':         'check <event_name> [options]',
    'summarize':     'summarize <event_name> [options]',
    'export':        'export <event_name> [options]',
}

subcommands = list(subcommand_descriptions.keys())
subcommand_descriptions['version'] = version

program_name = 'beat'

usage = program_name + ''' <subcommand> <arguments> ... [options]
BEAT: Bayesian earthquake analysis tool
 Version %(version)s
author: Hannes Vasyura-Bathke
email: hannes.vasyura-bathke@kaust.edu.sa

Subcommands:

    init            %(init)s
    clone           %(clone)s
    import          %(import)s
    update          %(update)s
    build_gfs       %(build_gfs)s
    sample          %(sample)s
    summarize       %(summarize)s
    export          %(export)s
    plot            %(plot)s
    check           %(check)s

To get further help and a list of available options for any subcommand run:

    beat <subcommand> --help

''' % d2u(subcommand_descriptions)


nargs_dict = {
    'init': 2,
    'clone': 2,
    'plot': 2,
    'import': 1,
    'update': 1,
    'build_gfs': 1,
    'sample': 1,
    'check': 1,
    'summarize': 1,
    'export': 1,
}

mode_choices = [geometry_mode_str, ffi_mode_str]

supported_geodetic_formats = ['matlab', 'ascii', 'kite']
supported_geodetic_types = ['SAR', 'GNSS']
supported_samplers = ['SMC', 'Metropolis', 'PT']

def add_common_options(parser):
    parser.add_option(
        '--loglevel',
        action='store',
        dest='loglevel',
        type='choice',
        choices=('critical', 'error', 'warning', 'info', 'debug'),
        default='info',
        help='set logger level to '
             '"critical", "error", "warning", "info", or "debug". '
             'Default is "%default".')


def get_project_directory(args, options, nargs=1, popflag=False):

    larg = len(args)

    if larg == nargs - 1:
        project_dir = os.getcwd()
    elif larg == nargs:
        if popflag:
            name = args.pop(0)
        else:
            name = args[0]
        project_dir = pjoin(os.path.abspath(options.main_path), name)
    else:
        project_dir = os.getcwd()

    return project_dir


def process_common_options(options, project_dir):
    util.ensuredir(project_dir)
    utility.setup_logging(
        project_dir, options.loglevel, logfilename='BEAT_log.txt')


def die(message, err=''):
    sys.exit('%s: error: %s \n %s' % (program_name, message, err))


def cl_parse(command, args, setup=None, details=None):
    usage = subcommand_usages[command]
    descr = subcommand_descriptions[command]

    if isinstance(usage, str):
        usage = [usage]

    susage = '%s %s' % (program_name, usage[0])
    for s in usage[1:]:
        susage += '\n%s%s %s' % (' ' * 7, program_name, s)

    description = descr[0].upper() + descr[1:] + '.'

    if details:
        description = description + ' %s' % details

    parser = OptionParser(usage=susage, description=description)

    if setup:
        setup(parser)

    add_common_options(parser)
    (options, args) = parser.parse_args(args)
    project_dir = get_project_directory(args, options, nargs_dict[command])
    process_common_options(options, project_dir)
    return parser, options, args


def list_callback(option, opt, value, parser):
    out = [ival.lstrip() for ival in value.split(',')]
    setattr(parser.values, option.dest, out)


def command_init(args):

    def setup(parser):

        parser.add_option(
            '--min_mag', dest='min_mag', type=float,
            default=6.,
            help='Minimum Mw for event, for catalog search.'
                 ' Default: "6.0"')

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) for creating directory structure.'
                 '  Default: current directory ./')

        parser.add_option(
            '--datatypes',
            default=['geodetic'], type='string',
            action='callback', callback=list_callback,
            help='Datatypes to include in the setup; "geodetic, seismic".')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--source_type', dest='source_type',
            choices=bconfig.source_names,
            default='RectangularSource',
            help='Source type to solve for; %s'
                 '. Default: "RectangularSource"' % (
                     '", "'.join(name for name in bconfig.source_names)))

        parser.add_option(
            '--n_sources', dest='n_sources', type='int',
            default=1,
            help='Integer Number of sources to invert for. Default: 1')

        parser.add_option(
            '--waveforms', type='string',
            action='callback', callback=list_callback,
            default=['any_P', 'any_S'],
            help='Waveforms to include in the setup; "any_P, any_S, slowest".')

        parser.add_option(
            '--sampler', dest='sampler',
            choices=supported_samplers,
            default='SMC',
            help='Sampling algorithm to sample the solution space of the'
                 ' general problem; %s. '
                 'Default: "SMC"' % list2string(supported_samplers))

        parser.add_option(
            '--hyper_sampler', dest='hyper_sampler',
            type='string', default='Metropolis',
            help='Sampling algorithm to sample the solution space of the'
                 ' hyperparameters only; So far only "Metropolis" supported.'
                 'Default: "Metropolis"')

        parser.add_option(
            '--use_custom', dest='use_custom',
            action='store_true',
            help='If set, a slot for a custom velocity model is being created'
                 ' in the configuration file.')

        parser.add_option(
            '--individual_gfs', dest='individual_gfs',
            action='store_true',
            help="If set, Green's Function stores will be created individually"
                 " for each station!")

    parser, options, args = cl_parse('init', args, setup=setup)

    la = len(args)

    if la > 2 or la < 1:
        logger.error('Wrong number of input arguments!')
        parser.print_help()
        sys.exit(1)

    if la == 2:
        name, date = args
    elif la == 1:
        logger.info(
            'Doing no catalog search for event information!')
        name = args[0]
        date = None

    return bconfig.init_config(name, date,
                              main_path=options.main_path,
                              min_magnitude=options.min_mag,
                              datatypes=options.datatypes,
                              mode=options.mode,
                              source_type=options.source_type,
                              n_sources=options.n_sources,
                              waveforms=options.waveforms,
                              sampler=options.sampler,
                              hyper_sampler=options.hyper_sampler,
                              use_custom=options.use_custom,
                              individual_gfs=options.individual_gfs)


def command_import(args):

    from pyrocko import io

    command_str = 'import'

    data_formats = io.allowed_formats('load')[2::]

    def setup(parser):

        parser.add_option(
            '--main_path',
            dest='main_path',
            type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--results', dest='results', type='string', default='',
            help='Import results from previous modeling step'
                 ' of given project path')

        parser.add_option(
            '--datatypes',
            default=['geodetic'], type='string',
            action='callback', callback=list_callback,
            help='Datatypes to import; "geodetic, seismic".')

        parser.add_option(
            '--geodetic_format', dest='geodetic_format',
            type='string', default=['kite'],
            action='callback', callback=list_callback,
            help='Data format to be imported; %s Default: "kite"' %
                 list2string(supported_geodetic_formats))

        parser.add_option(
            '--seismic_format', dest='seismic_format',
            default='mseed',
            choices=data_formats,
            help='Data format to be imported;'
                 'Default: "mseed"; Available: %s' % list2string(data_formats))

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem results to import; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--force', dest='force', action='store_true',
            help='Overwrite existing files')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])


    if not options.results:
        c = bconfig.load_config(project_dir, options.mode)

        if 'seismic' in options.datatypes:
            sc = c.seismic_config
            logger.info('Attempting to import seismic data from %s' %
                        sc.datadir)

            seismic_outpath = pjoin(c.project_dir, bconfig.seismic_data_name)
            if not os.path.exists(seismic_outpath) or options.force:
                stations = model.load_stations(
                    pjoin(sc.datadir, 'stations.txt'))

                if options.seismic_format == 'autokiwi':

                    data_traces = inputf.load_data_traces(
                        datadir=sc.datadir,
                        stations=stations,
                        divider='-')

                elif options.seismic_format in data_formats:
                    data_traces = inputf.load_data_traces(
                        datadir=sc.datadir,
                        stations=stations,
                        divider='.',
                        data_format=options.seismic_format)

                else:
                    raise TypeError(
                        'Format: %s not implemented yet.' %
                        options.seismic_format)

                logger.info('Rotating traces to RTZ wrt. event!')
                data_traces = inputf.rotate_traces_and_stations(
                    data_traces, stations, c.event)

                logger.info('Pickle seismic data to %s' % seismic_outpath)
                utility.dump_objects(seismic_outpath,
                                     outlist=[stations, data_traces])
                logger.info(
                    'Successfully imported traces for'
                    ' %i stations!' % len(stations))
            else:
                logger.info('%s exists! Use --force to overwrite!' %
                            seismic_outpath)

        if 'geodetic' in options.datatypes:
            gc = c.geodetic_config
            logger.info('Attempting to import geodetic data from %s' %
                        gc.datadir)

            geodetic_outpath = pjoin(c.project_dir, bconfig.geodetic_data_name)
            if not os.path.exists(geodetic_outpath) or options.force:

                gtargets = []
                for typ in gc.types:
                    if typ == 'SAR':
                        if 'matlab' in options.geodetic_format:
                            gtargets.extend(
                                inputf.load_SAR_data(gc.datadir, gc.names))
                        elif 'kite' in options.geodetic_format:
                            gtargets.extend(
                                inputf.load_kite_scenes(gc.datadir, gc.names))
                        else:
                            raise ImportError(
                                'Format %s not implemented yet for SAR data.' %
                                options.geodetic_format)

                    elif typ == 'GNSS':
                        if 'ascii' in options.geodetic_format:
                            for name in gc.names:
                                gtargets.extend(
                                    inputf.load_and_blacklist_gnss(
                                        gc.datadir, name, gc.blacklist))
                        else:
                            raise ImportError(
                                'Format %s not implemented yet for GNSS data.' %
                                options.geodetic_format)

                    else:
                        raise TypeError(
                            'Geodetic datatype "%s" is not supported! '
                            'Supported types are: %s ' % (
                                typ, list2string(supported_geodetic_types)))

                logger.info('Pickleing geodetic data to %s' % geodetic_outpath)
                utility.dump_objects(geodetic_outpath, outlist=gtargets)
            else:
                logger.info('%s exists! Use --force to overwrite!' %
                            geodetic_outpath)

    else:
        from pandas import read_csv
        logger.info(
            'Attempting to load results with mode %s from directory:'
            ' %s' % (options.mode, options.results))
        c = bconfig.load_config(project_dir, 'ffi')

        problem = load_model(
            options.results, options.mode, hypers=False)
        source_params = list(problem.config.problem_config.priors.keys())

        stage = Stage(homepath=problem.outfolder, backend=problem.config.sampler_config.backend)
        stage.load_results(
            varnames=problem.varnames,
            model=problem.model, stage_number=-1,
            load='trace', chains=[-1])

        point = plotting.get_result_point(stage, problem.config, 'max')

        if 'geodetic' in options.datatypes:
            if c.geodetic_config.fit_plane:

                logger.info('Importing ramp parameters ...')
                new_bounds = OrderedDict()

                for var in c.geodetic_config.get_hierarchical_names():
                    if var in point:
                        new_bounds[var] = (point[var], point[var])
                    else:
                        logger.warn(
                            'Ramps were fixed in previous run!'
                            ' Importing fixed values!')
                        tpoint = c.problem_config.get_test_point()
                        new_bounds[var] = (tpoint[var], tpoint[var])

                c.problem_config.set_vars(
                    new_bounds, attribute='hierarchicals')

        if options.mode == geometry_mode_str:
            n_sources = problem.config.problem_config.n_sources
            logger.info('Importing non-linear source geometry results!')

            for param in list(point.keys()):
                if param not in source_params:
                    point.pop(param)

            point = utility.adjust_point_units(point)
            source_points = utility.split_point(point)

            reference_sources = bconfig.init_reference_sources(
                source_points, n_sources, c.problem_config.source_type,
                c.problem_config.stf_type, ref_time=c.event.time)

            if 'geodetic' in options.datatypes:
                c.geodetic_config.gf_config.reference_sources = \
                    reference_sources
            if 'seismic' in options.datatypes:
                c.seismic_config.gf_config.reference_sources = \
                    reference_sources

            if 'seismic' in problem.config.problem_config.datatypes:
                summarydf = read_csv(
                    pjoin(problem.outfolder, 'summary.txt'), sep='\s+')

                new_bounds = {}
                for param in ['time']:
                    new_bounds[param] = extract_bounds_from_summary(
                        summarydf, varname=param,
                        shape=(n_sources,), roundto=0)

                c.problem_config.set_vars(
                    new_bounds, attribute='priors')

        elif options.mode == ffi_mode_str:
            npatches = problem.config.problem_config.mode_config.npatches
            logger.info('Importing linear static distributed slip results!')

            summarydf = read_csv(
                pjoin(problem.outfolder, 'summary.txt'), sep='\s+')

            new_bounds = {}
            for param in source_params:
                new_bounds[param] = extract_bounds_from_summary(
                    summarydf, varname=param, shape=(npatches,), roundto=1)

            c.problem_config.set_vars(
                new_bounds, attribute='priors')

        bconfig.dump_config(c)
        logger.info('Successfully updated config file!')


def command_update(args):

    command_str = 'update'

    def setup(parser):

        parser.add_option(
            '--main_path',
            dest='main_path',
            type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--parameters',
            default=['structure'], type='string',
            action='callback', callback=list_callback,
            help='Parameters to update; "structure, hypers, hierarchicals". '
                 'Default: ["structure"] (config file-structure only)')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--diff', dest='diff', action='store_true',
            help='create diff between normalized old and new versions')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    config_file_name = 'config_' + options.mode + '.yaml'

    config_fn = os.path.join(project_dir, config_file_name)

    from beat import upgrade
    upgrade.upgrade_config_file(
        config_fn, diff=options.diff, update=options.parameters)


def command_clone(args):

    command_str = 'clone'

    from beat.config import _datatype_choices as datatype_choices

    def setup(parser):

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--datatypes',
            default=['geodetic', 'seismic'], type='string',
            action='callback', callback=list_callback,
            help='Datatypes to clone; "geodetic, seismic".')

        parser.add_option(
            '--source_type', dest='source_type',
            choices=bconfig.source_names,
            default=None,
            help='Source type to replace in config; %s'
                 '. Default: "dont change"' % (
                     '", "'.join(name for name in bconfig.source_names)))

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--copy_data', dest='copy_data',
            action='store_true',
            help='If set, the imported data will be copied into the cloned'
                 ' directory.')

        parser.add_option(
            '--sampler', dest='sampler',
            choices=supported_samplers,
            default=None,
            help='Replace sampling algorithm in config to sample '
                 'the solution space of the general problem; %s.'
                 ' Default: "dont change"' % list2string(supported_samplers))

    parser, options, args = cl_parse(command_str, args, setup=setup)

    if not len(args) == 2:
        parser.print_help()
        sys.exit(1)

    name, cloned_name = args

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    cloned_dir = pjoin(os.path.dirname(project_dir), cloned_name)

    util.ensuredir(cloned_dir)

    for mode in [options.mode]:
        config_fn = pjoin(project_dir, 'config_' + mode + '.yaml')

        if os.path.exists(config_fn):
            logger.info('Cloning %s problem config.' % mode)
            c = bconfig.load_config(project_dir, mode)

            c.name = cloned_name
            c.project_dir = cloned_dir

            new_datatypes = []
            for datatype in datatype_choices:
                if datatype in options.datatypes:
                    if datatype not in c.problem_config.datatypes:
                        logger.warn('Datatype %s to be cloned is not'
                                    ' in config! Adding to new config!' % datatype)
                        c[datatype + '_config'] = \
                            bconfig.datatype_catalog[datatype](mode=options.mode)
                        re_init = True
                    else:
                        re_init = False

                    new_datatypes.append(datatype)

                    data_path = pjoin(project_dir, datatype + '_data.pkl')

                    if os.path.exists(data_path) and options.copy_data:
                        logger.info('Cloning %s data.' % datatype)
                        cloned_data_path = pjoin(
                            cloned_dir, datatype + '_data.pkl')
                        shutil.copyfile(data_path, cloned_data_path)
                else:
                    if datatype in c.problem_config.datatypes:
                        logger.warning(
                            'Removing datatype "%s" '
                            'from cloned config!' % datatype)
                        c[datatype + '_config'] = None

            c.problem_config.datatypes = new_datatypes

            if options.source_type is None:
                old_priors = copy.deepcopy(c.problem_config.priors)

                new_priors = c.problem_config.select_variables()
                for prior in new_priors:
                    if prior in list(old_priors.keys()):
                        c.problem_config.priors[prior] = old_priors[prior]

            else:
                logger.info('Replacing source with "%s"' % options.source_type)
                c.problem_config.source_type = options.source_type
                c.problem_config.init_vars()
                c.problem_config.set_decimation_factor()
                re_init = False

            if re_init:
                logger.info(
                    'Re-initialized priors because of new datatype!'
                    ' Please check prior bounds!')
                c.problem_config.init_vars()

            old_hypers = copy.deepcopy(c.problem_config.hyperparameters)

            c.update_hypers()
            for hyper in old_hypers.keys():
                c.problem_config.hyperparameters[hyper] = old_hypers[hyper]

            if options.sampler:
                c.sampler_config.name = options.sampler
                c.sampler_config.set_parameters()

            c.regularize()
            c.validate()
            bconfig.dump_config(c)

        else:
            raise IOError('Config file: %s does not exist!' % config_fn)


def command_sample(args):

    command_str = 'sample'

    def setup(parser):
        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--hypers', dest='hypers',
            action='store_true', help='Sample hyperparameters only.')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    problem = load_model(
        project_dir, options.mode, options.hypers)

    step = problem.init_sampler(hypers=options.hypers)

    if options.hypers:
        estimate_hypers(step, problem)
    else:
        sample(step, problem)


def result_check(mtrace, min_length):
    if len(mtrace.chains) < min_length:
        raise IOError(
            'Result traces do not exist. Previously deleted?')


def command_summarize(args):

    from pymc3 import summary

    command_str = 'summarize'

    def setup(parser):

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--force', dest='force', action='store_true',
            help='Overwrite existing files')

        parser.add_option(
            '--stage_number',
            dest='stage_number',
            type='int',
            default=None,
            help='Int of the stage number "n" of the stage to be summarized.'
                 ' Default: all stages up to last complete stage')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    logger.info('Loading problem ...')
    problem = load_model(project_dir, options.mode)
    problem.plant_lijection()

    stage = Stage(homepath=problem.outfolder, backend=problem.config.sampler_config.backend)
    stage_numbers = stage.handler.get_stage_indexes(options.stage_number)
    logger.info('Summarizing stage(s): %s' % list2string(stage_numbers))
    if len(stage_numbers) == 0:
        raise ValueError('No stage result found where sampling completed!')

    sc = problem.config.sampler_config
    sc_params = problem.config.sampler_config.parameters
    sampler_name = problem.config.sampler_config.name
    if hasattr(sc_params, 'rm_flag'):
        if sc_params.rm_flag:
            logger.info('Removing sampled chains!!!')
            input('Sure? Press enter! Otherwise Ctrl + C')
            rm_flag = True
        else:
            rm_flag = False
    else:
        rm_flag = False

    for stage_number in stage_numbers:

        stage_path = stage.handler.stage_path(stage_number)
        logger.info('Summarizing stage under: %s' % stage_path)

        result_trace_path = pjoin(stage_path, 'chain--1.csv')
        if not os.path.exists(result_trace_path) or options.force:
            # trace may exist by forceing
            if os.path.exists(result_trace_path):
                os.remove(result_trace_path)

            stage.load_results(
                model=problem.model, stage_number=stage_number, load='trace')

            if sampler_name == 'SMC':
                result_check(stage.mtrace, min_length=2)
                if stage_number == -1:
                    # final stage factor
                    n_steps = sc_params.n_steps * sample_factor_final_stage
                    idxs = range(len(thin_buffer(
                        list(range(n_steps)),
                        sc.buffer_thinning,
                        ensure_last=True)))
                else:
                    idxs = [-1]

                draws = sc_params.n_chains * len(idxs)
                chains = stage.mtrace.chains
            elif sampler_name == 'PT':
                result_check(stage.mtrace, min_length=1)
                idxs = range(
                    int(floor(sc_params.n_samples * sc_params.burn)),
                    sc_params.n_samples,
                    sc_params.thin)
                chains = [0]
                draws = len(idxs)
            else:
                raise NotImplementedError(
                    'Summarize function still needs to be implemented '
                    'for %s sampler' % problem.config.sampler_config.name)

            rtrace = backend_catalog[sc.backend](
                stage_path, model=problem.model, buffer_size=sc.buffer_size,
                progressbar=False)
            rtrace.setup(
                draws=draws, chain=-1, overwrite=True)

            if hasattr(problem, 'sources'):
                source = problem.sources[0]
            else:
                source = None

            if 'seismic' in problem.config.problem_config.datatypes:
                composite = problem.composites['seismic']
            else:
                composite = problem.composites['geodetic']

            for chain in chains:
                for idx in idxs:
                    point = stage.mtrace.point(idx=idx, chain=chain)
                    if isinstance(source, MTSourceWithMagnitude):
                        composite.point2sources(point)
                        ldicts = []
                        for source in composite.sources:
                            ldicts.append(source.scaled_m6_dict)

                        jpoint = utility.join_points(ldicts)
                        point.update(jpoint)
                        del jpoint, ldicts

                    lpoint = problem.model.lijection.d2l(point)
                    # TODO: in PT with large buffer sizes somehow memory leak
                    rtrace.write(lpoint, draw=chain)
                    del lpoint, point

                if rm_flag:
                    # remove chain
                    os.remove(stage.mtrace._straces[chain].filename)

            rtrace.record_buffer()
        else:
            logger.info(
                'Summarized trace exists! Use force=True to overwrite!')

    final_stage = -1
    if final_stage in stage_numbers:
        stage.load_results(
            model=problem.model, stage_number=final_stage, chains=[-1])
        rtrace = stage.mtrace

        if len(rtrace) == 0:
            raise ValueError(
                'Trace collection previously failed. Please rerun'
                ' "beat summarize <project_dir> --force!"')

        summary_file = pjoin(problem.outfolder, bconfig.summary_name)

        if os.path.exists(summary_file) and options.force:
            os.remove(summary_file)

        if not os.path.exists(summary_file) or options.force:
            logger.info('Writing summary to %s' % summary_file)
            df = summary(rtrace)
            with open(summary_file, 'w') as outfile:
                df.to_string(outfile)
        else:
            logger.info('Summary exists! Use force=True to overwrite!')


def command_build_gfs(args):

    command_str = 'build_gfs'

    def setup(parser):

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--datatypes',
            default=['geodetic'], type='string',
            action='callback', callback=list_callback,
            help='Datatypes to calculate the GFs for; "geodetic, seismic".'
                 ' Default: "geodetic"')

        parser.add_option(
            '--force', dest='force', action='store_true',
            help='Overwrite existing files')

        parser.add_option(
            '--execute', dest='execute', action='store_true',
            help='Start actual GF calculations. If not set only'
                 ' configuration files are being created')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    c = bconfig.load_config(project_dir, options.mode)

    if options.mode in [geometry_mode_str, 'interseismic']:
        for datatype in options.datatypes:
            if datatype == 'geodetic':
                gc = c.geodetic_config
                gf = c.geodetic_config.gf_config

                for crust_ind in range(*gf.n_variations):
                    heart.geo_construct_gf(
                        event=c.event,
                        geodetic_config=gc,
                        crust_ind=crust_ind,
                        execute=options.execute,
                        force=options.force)

            elif datatype == 'seismic':
                sc = c.seismic_config
                sf = sc.gf_config

                if sf.reference_location is None:
                    logger.info("Creating Green's Function stores individually"
                                " for each station!")
                    seismic_data_path = pjoin(
                        c.project_dir, bconfig.seismic_data_name)

                    stations, _ = utility.load_objects(seismic_data_path)
                else:
                    logger.info(
                        "Creating one global Green's Function store, which is "
                        "being used by all stations!")
                    stations = [sf.reference_location]
                    logger.info(
                        'Store name: %s' % sf.reference_location.station)

                for crust_ind in range(*sf.n_variations):
                    heart.seis_construct_gf(
                        stations=stations,
                        event=c.event,
                        seismic_config=sc,
                        crust_ind=crust_ind,
                        execute=options.execute,
                        force=options.force)
            else:
                raise ValueError('Datatype %s not supported!' % datatype)

            if not options.execute:
                logger.info('%s GF store configs successfully created! '
                            'To start calculations set --execute!' % datatype)

            if options.execute:
                logger.info('%s GF calculations successful!' % datatype)

    elif options.mode == ffi_mode_str:
        from beat import ffi
        import numpy as num

        slip_varnames = c.problem_config.get_slip_variables()
        varnames = c.problem_config.select_variables()
        outdir = pjoin(c.project_dir, options.mode, bconfig.linear_gf_dir_name)
        util.ensuredir(outdir)

        faultpath = pjoin(outdir, bconfig.fault_geometry_name)
        if not os.path.exists(faultpath) or options.force:
            for datatype in options.datatypes:
                try:
                    gf = c[datatype + '_config'].gf_config
                except AttributeError:
                    raise AttributeError(
                        'Datatype "%s" not existing in config!' % datatype)

                for source in gf.reference_sources:
                    source.update(lat=c.event.lat, lon=c.event.lon)

                logger.info('Discretizing reference sources ...')
                fault = ffi.discretize_sources(
                    varnames=slip_varnames,
                    sources=gf.reference_sources,
                    extension_widths=gf.extension_widths,
                    extension_lengths=gf.extension_lengths,
                    patch_widths=gf.patch_widths,
                    patch_lengths=gf.patch_lengths,
                    datatypes=options.datatypes)

            logger.info(
                'Storing discretized fault geometry to: %s' % faultpath)
            utility.dump_objects(faultpath, [fault])

            logger.info(
                'Fault discretization done! Updating problem_config...')
            logger.info('%s' % fault.__str__())
            c.problem_config.n_sources = fault.nsubfaults
            c.problem_config.mode_config.npatches = fault.npatches

            nucleation_strikes = []
            nucleation_dips = []
            for i in range(fault.nsubfaults):
                ext_source = fault.get_subfault(
                    i, datatype=options.datatypes[0], component='uparr')

                nucleation_dips.append(ext_source.width / km)
                nucleation_strikes.append(ext_source.length / km)

            nucl_start = num.zeros(fault.nsubfaults)
            new_bounds = {
                'nucleation_strike': (
                    nucl_start, num.array(nucleation_strikes)),
                'nucleation_dip': (nucl_start, num.array(nucleation_dips))
            }

            c.problem_config.set_vars(new_bounds)
            bconfig.dump_config(c)

        elif os.path.exists(faultpath):
            logger.info("Discretized fault geometry exists! Use --force to"
                        " overwrite!")
            logger.info('Loading existing discretized fault')
            fault = utility.load_objects(faultpath)[0]

        if options.execute:
            logger.info("Calculating linear Green's Functions")

            for datatype in options.datatypes:
                logger.info('for %s data ...' % datatype)

                if datatype == 'geodetic':
                    gf = c.geodetic_config.gf_config

                    geodetic_data_path = pjoin(
                        c.project_dir, bconfig.geodetic_data_name)

                    datasets = utility.load_objects(geodetic_data_path)

                    engine = LocalEngine(store_superdirs=[gf.store_superdir])

                    for crust_ind in range(*gf.n_variations):
                        logger.info('crust_ind %i' % crust_ind)

                        targets = heart.init_geodetic_targets(
                            datasets,
                            earth_model_name=gf.earth_model_name,
                            interpolation=c.geodetic_config.interpolation,
                            crust_inds=[crust_ind],
                            sample_rate=gf.sample_rate)

                        ffi.geo_construct_gf_linear(
                            engine=engine,
                            outdirectory=outdir,
                            event=c.event,
                            crust_ind=crust_ind,
                            datasets=datasets,
                            targets=targets,
                            nworkers=gf.nworkers,
                            fault=fault,
                            varnames=slip_varnames,
                            force=options.force)

                elif datatype == 'seismic':
                    seismic_data_path = pjoin(
                        c.project_dir, bconfig.seismic_data_name)
                    sc = c.seismic_config
                    gf = sc.gf_config
                    pc = c.problem_config

                    engine = LocalEngine(store_superdirs=[gf.store_superdir])

                    for crust_ind in range(*gf.n_variations):
                        logger.info('crust_ind %i' % crust_ind)
                        sc.gf_config.reference_model_idx = crust_ind
                        datahandler = heart.init_datahandler(
                            seismic_config=sc,
                            seismic_data_path=seismic_data_path)

                        for wc in sc.waveforms:
                            wmap = heart.init_wavemap(
                                waveformfit_config=wc,
                                datahandler=datahandler,
                                event=c.event)

                            ffi.seis_construct_gf_linear(
                                engine=engine,
                                fault=fault,
                                durations_prior=pc.priors['durations'],
                                velocities_prior=pc.priors['velocities'],
                                nucleation_time_prior=pc.priors['time'],
                                varnames=slip_varnames,
                                wavemap=wmap,
                                event=c.event,
                                nworkers=gf.nworkers,
                                starttime_sampling=gf.starttime_sampling,
                                duration_sampling=gf.duration_sampling,
                                sample_rate=gf.sample_rate,
                                outdirectory=outdir,
                                force=options.force)
        else:
            logger.info('Did not run GF calculation. Use --execute!')


def command_plot(args):

    command_str = 'plot'

    def setup(parser):

        parser.add_option(
            '--main_path',
            dest='main_path',
            type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--post_llk',
            dest='post_llk',
            choices=['max', 'min', 'mean', 'all', 'None'],
            default='max',
            help='Plot model with specified likelihood; "max", "min", "mean"'
                 ' "None" or "all"; Default: "max"')

        parser.add_option(
            '--stage_number',
            dest='stage_number',
            type='int',
            default=None,
            help='Int of the stage number "n" of the stage to be plotted.'
                 ' Default: all stages up to last complete stage')

        parser.add_option(
            '--varnames',
            default='',
            type='string',
            action='callback', callback=list_callback,
            help='Variable names to plot in figures. Example: "strike,dip"'
                 ' Default: empty string --> all')

        parser.add_option(
            '--nensemble',
            dest='nensemble',
            type='int',
            default=1,
            help='Int of the number of solutions that'
                 ' are used for fuzzy plots')

        parser.add_option(
            '--source_idxs',
            action='callback', callback=list_callback,
            type='string',
            default='',
            help='Indexes to patches'
                 ' of slip distribution to draw marginals for')

        parser.add_option(
            '--format',
            dest='format',
            choices=['display', 'pdf', 'png', 'svg', 'eps'],
            default='pdf',
            help='Output format of the plot; "display", "pdf", "png", "svg",'
                 '"eps" Default: "pdf"')

        parser.add_option(
            '--plot_projection',
            dest='plot_projection',
            choices=['latlon', 'local'],
            default='local',
            help='Output projection of the plot; "latlon" or "local"'
                 'Default: "local"')

        parser.add_option(
            '--dpi',
            dest='dpi',
            type='int',
            default=300,
            help='Output resolution of the plots in dpi (dots per inch);'
                 ' Default: "300"')

        parser.add_option(
            '--force',
            dest='force',
            action='store_true',
            help='Overwrite existing files')

        parser.add_option(
            '--reference',
            dest='reference',
            action='store_true',
            help='Plot reference (test_point) into stage posteriors.')

        parser.add_option(
            '--hypers',
            dest='hypers',
            action='store_true',
            help='Plot hyperparameter results only.')

        parser.add_option(
            '--build',
            dest='build',
            action='store_true',
            help='Build models during problem loading.')

    plots_avail = plotting.available_plots()

    details = '''Available <plot types> are: %s or "all". Multiple plots can be
selected giving a comma seperated list.''' % list2string(plots_avail)

    parser, options, args = cl_parse(command_str, args, setup, details)

    if len(args) < 1:
        parser.error('plot needs at least one argument!')
        parser.help()

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str], popflag=True)

    if args[0] == 'all':
        c = bconfig.load_config(project_dir, options.mode)
        plotnames = plotting.available_plots(
            options.mode, datatypes=c.problem_config.datatypes)
        logger.info('Plotting "all" plots: %s' % list2string(plotnames))
    else:
        plotnames = args[0].split(',')

    for plot in plotnames:
        if plot not in plots_avail:
            raise TypeError('Plot type %s not available! Available plots are:'
                            ' %s' % (plot, plots_avail))

    logger.info('Loading problem ...')

    problem = load_model(
        project_dir, options.mode, options.hypers, options.build)

    if len(options.source_idxs) < 1:
        source_idxs = None
    else:
        source_idxs = [int(idx) for idx in options.source_idxs]

    po = plotting.PlotOptions(
        plot_projection=options.plot_projection,
        post_llk=options.post_llk,
        load_stage=options.stage_number,
        outformat=options.format,
        force=options.force,
        dpi=options.dpi,
        varnames=options.varnames,
        nensemble=options.nensemble,
        source_idxs=source_idxs)

    if options.reference:
        try:
            po.reference = problem.model.test_point
            step = problem.init_sampler()
            po.reference['like'] = step.step(problem.model.test_point)[1][-1]
        except AttributeError:
            po.reference = problem.config.problem_config.get_test_point()
    else:
        po.reference = {}

    figure_path = pjoin(problem.outfolder, po.figure_dir)
    util.ensuredir(figure_path)

    for plot in plotnames:
        plotting.plots_catalog[plot](problem, po)


def command_check(args):

    command_str = 'check'
    whats = ['stores', 'traces', 'library', 'geometry']

    def setup(parser):
        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--main_path',
            dest='main_path',
            type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--datatypes',
            default=['seismic'],
            type='string',
            action='callback',
            callback=list_callback,
            help='Datatypes to check; "geodetic, seismic".')

        parser.add_option(
            '--what',
            dest='what',
            choices=whats,
            default='stores',
            help='Setup item to check; '
                 '"%s", Default: "stores"' % list2string(whats))

        parser.add_option(
            '--targets',
            default=[0],
            type='string',
            action='callback',
            callback=list_callback,
            help='Indexes to targets to display.')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    problem = load_model(
        project_dir, options.mode, hypers=False, build=False)

    tpoint = problem.config.problem_config.get_test_point()
    if options.mode == geometry_mode_str:
        problem.point2sources(tpoint)

    if options.what == 'stores':
        corrupted_stores = heart.check_problem_stores(
            problem, options.datatypes)

        for datatype in options.datatypes:
            store_ids = corrupted_stores[datatype]
            logger.warn('Store(s) with empty traces! : %s ' % store_ids)

    elif options.what == 'traces':
        sc = problem.composites['seismic']
        for wmap in sc.wavemaps:
            wmap.prepare_data(
                source=sc.event,
                engine=sc.engine,
                outmode='stacked_traces')
            snuffle(
                wmap.datasets + wmap._prepared_data,
                stations=wmap.stations, events=[sc.event])

    elif options.what == 'library':
        if options.mode != ffi_mode_str:
            logger.warning(
                'GF library exists only for "%s" '
                'optimization mode.' % ffi_mode_str)
        else:
            from beat import ffi

            for datatype in options.datatypes:
                for var in problem.config.problem_config.get_slip_variables():
                    outdir = pjoin(
                        problem.config.project_dir, options.mode,
                        bconfig.linear_gf_dir_name)
                    if datatype == 'seismic':
                        sc = problem.config.seismic_config
                        scomp = problem.composites['seismic']

                        for wmap in scomp.wavemaps:
                            filename = ffi.get_gf_prefix(
                                datatype, component=var,
                                wavename=wmap.config.name,
                                crust_ind=sc.gf_config.reference_model_idx)

                            logger.info(
                                'Loading Greens Functions'
                                ' Library %s for %s target' % (
                                    filename,
                                    list2string(options.targets)))
                            gfs = ffi.load_gf_library(
                                directory=outdir, filename=filename)

                            targets = [
                                int(target) for target in options.targets]
                            trs = gfs.get_traces(
                                targetidxs=targets,
                                patchidxs=list(range(gfs.npatches)),
                                durationidxs=list(range(gfs.ndurations)),
                                starttimeidxs=list(range(gfs.nstarttimes)))
                            snuffle(trs)
    elif options.what == 'geometry':
        from beat.plotting import source_geometry
        datatype = problem.config.problem_config.datatypes[0]
        if options.mode == ffi_mode_str:
            fault = problem.composites[datatype].load_fault_geometry()
            reference_sources = problem.config[
                datatype + '_config'].gf_config.reference_sources
            source_geometry(fault, reference_sources)
        else:
            logger.warning(
                'Checking geometry is only for'
                ' "%s" mode available' % ffi_mode_str)
    else:
        raise ValueError('Subject what: %s is not available!' % options.what)


def command_export(args):

    command_str = 'export'

    def setup(parser):

        parser.add_option(
            '--main_path', dest='main_path', type='string',
            default='./',
            help='Main path (absolute) leading to folders of events that'
                 ' have been created by "init".'
                 ' Default: current directory: ./')

        parser.add_option(
            '--mode', dest='mode',
            choices=mode_choices,
            default=geometry_mode_str,
            help='Inversion problem to solve; %s Default: "%s"' %
                 (list2string(mode_choices), geometry_mode_str))

        parser.add_option(
            '--stage_number',
            dest='stage_number',
            type='int',
            default=-1,
            help='Int of the stage number "n" of the stage to be summarized.'
                 ' Default: all stages up to last complete stage')

        parser.add_option(
            '--post_llk',
            dest='post_llk',
            choices=['max', 'min', 'mean', 'all'],
            default='max',
            help='Export model with specified likelihood; "max", "min", "mean"'
                 ' or "all"; Default: "max"')

        parser.add_option(
            '--reference',
            dest='reference',
            action='store_true',
            help='Export data for test point instead of result point '
                 '(post_llk)')

        parser.add_option(
            '--fix_output',
            dest='fix_output', action='store_true',
            help='Fix Network Station Location Codes in case '
                 'they are not compliant with mseed')

        parser.add_option(
            '--force', dest='force', action='store_true',
            help='Overwrite existing files')

    parser, options, args = cl_parse(command_str, args, setup=setup)

    project_dir = get_project_directory(
        args, options, nargs_dict[command_str])

    logger.info('Loading problem ...')
    problem = load_model(project_dir, options.mode)

    sc = problem.config.sampler_config

    stage = Stage(homepath=problem.outfolder,
                  backend=problem.config.sampler_config.backend)

    stage.load_results(
        varnames=problem.varnames,
        model=problem.model, stage_number=options.stage_number,
        load='trace', chains=[-1])

    trace_name = 'chain--1.{}'.format(problem.config.sampler_config.backend)
    results_path = pjoin(problem.outfolder, bconfig.results_dir_name)
    logger.info('Saving results to %s' % results_path)
    util.ensuredir(results_path)

    results_trace = pjoin(stage.handler.stage_path(-1), trace_name)
    shutil.copy(results_trace, pjoin(results_path, trace_name))

    if options.reference:
        point = problem.config.problem_config.get_test_point()
        options.post_llk = 'ref'
    else:
        point = plotting.get_result_point(
            stage, problem.config, point_llk=options.post_llk)

    rpoint = heart.ResultPoint(point=point, post_llk=options.post_llk)
    dump(rpoint,
         filename=pjoin(
             results_path, 'solution_{}.yaml'.format(options.post_llk)))

    for datatype, composite in problem.composites.items():
        logger.info(
            'Exporting "%s" synthetics for "%s" likelihood parameters:' % (
                datatype, options.post_llk))
        for varname, value in point.items():
            logger.info('%s: %s' % (
                varname, list2string(value.ravel().tolist())))

        results = composite.assemble_results(point, chop_bounds=['b', 'c'])
        if datatype == 'seismic':
            from pyrocko import io


            for traces, attribute in heart.results_for_export(
                    results, datatype=datatype):

                filename = '%s_%i.mseed' % (attribute, options.stage_number)
                outpath = pjoin(results_path, filename)
                try:
                    io.save(traces, outpath, overwrite=options.force)
                except io.mseed.CodeTooLong:
                    if options.fix_output:
                        for tr in traces:
                            tr.set_station(tr.station[-4::])
                            tr.set_location(
                                str(problem.config.seismic_config.gf_config.reference_model_idx))

                        io.save(traces, outpath, overwrite=options.force)
                    else:
                        raise ValueError(
                            'Some station codes are too long! '
                            '(the --fix_output option will truncate to '
                            'last 4 characters!)')

            # export stdz residuals


            if hasattr(sc.parameters, 'update_covariances'):
                if sc.parameters.update_covariances:
                    logger.info('Saving velocity model covariance matrixes...')
                    composite.update_weights(point)
                    for wmap in composite.wavemaps:
                        pcovs = {
                            list2string(dataset.nslc_id):
                                dataset.covariance.pred_v
                            for dataset in wmap.datasets}

                        outname = pjoin(
                            results_path, '%s_C_vm_%s' % (
                                datatype, wmap._mapid))
                        logger.info('"%s" to: %s' % (wmap._mapid, outname))
                        savez(outname, **pcovs)

                logger.info('Saving data covariance matrixes...')
                for wmap in composite.wavemaps:
                    dcovs = {
                        list2string(dataset.nslc_id):
                            dataset.covariance.data
                        for dataset in wmap.datasets}

                    outname = pjoin(
                        results_path, '%s_C_d_%s' % (
                            datatype, wmap._mapid))
                    logger.info('"%s" to: %s' % (wmap._mapid, outname))
                    savez(outname, **dcovs)

        elif datatype == 'geodetic':
            for ifgs, attribute in heart.results_for_export(
                    results, datatype=datatype):
                pass

            raise NotImplementedError(
                'Geodetic export not yet implemented!')
        else:
            raise NotImplementedError('Datatype %s not supported!' % datatype)

        stdzd_res_path = pjoin(
            results_path, '{}_stdzd_residuals.pkl'.format(datatype))
        logger.info('Exporting standardized residuals to %s' % stdzd_res_path)
        stdzd_res = composite.get_standardized_residuals(point)
        if stdzd_res:
            utility.dump_objects(stdzd_res_path, outlist=stdzd_res)


def main():

    if len(sys.argv) < 2:
        sys.exit('Usage: %s' % usage)

    args = list(sys.argv)
    args.pop(0)
    command = args.pop(0)

    if command in subcommands:
        globals()['command_' + command](args)

    elif command in ('--help', '-h', 'help'):
        if command == 'help' and args:
            acommand = args[0]
            if acommand in subcommands:
                globals()['command_' + acommand](['--help'])

        sys.exit('Usage: %s' % usage)

    else:
        sys.exit('BEAT: error: no such subcommand: %s' % command)


if __name__ == '__main__':
    main()
