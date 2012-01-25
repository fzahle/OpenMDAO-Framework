"""OpenMDAO Command Line Interface stuff"""

import os
import sys
import webbrowser
from ConfigParser import SafeConfigParser
from argparse import ArgumentParser
from subprocess import call, check_call

from openmdao.main.plugin import plugin_docs, plugin_build_docs, print_sub_help
from openmdao.test.testing import read_config, filter_config, run_openmdao_suite

def list_testhosts(parser, options, args=None):
    if args:
        print_sub_help(parser, 'list_testhosts')
        return -1
    hostlist, config = read_config(options)
    for host in filter_config(hostlist, config, options):
        plat = config.get(host, 'platform')
        py = config.get(host, 'py')
        print host.ljust(30), plat.ljust(10), py

def test_openmdao(parser, options, args):
    # nosetests uses sys.argv for its usage message when the user passes a -h
    # arg, regardless of what args we pass to it, so if we see -h in sys.args,
    # change the first entry in sys.argv to 'openmdao test'.  Otherwise, usage message
    # will be 'openmdao [options]' instead of 'openmdao test [options]'
    if '-h' in sys.argv:
        sys.argv[0] = 'openmdao test'
    run_openmdao_suite(sys.argv[1:])
    
def openmdao_docs(parser, options, args=None):
    if args:
        print_sub_help(parser, 'docs')
        return -1
    plugin_docs(parser, options, args)

def _get_openmdao_parser():
    """Sets up the plugin arg parser and all of its subcommand parsers."""
    
    top_parser = ArgumentParser()
    subparsers = top_parser.add_subparsers(title='commands')
    
    parser = subparsers.add_parser('list_testhosts', help='list hosts in testhosts config file')
    parser.add_argument("-c", "--config", action='store', dest='cfg', metavar='CONFIG',
                        default='~/.openmdao/testhosts.cfg',
                        help="Path of config file where info for remote testing/building hosts is located")
    parser.add_argument("--filter", action='append', dest='filters', 
                        default=[],
                        help="boolean expression to filter hosts")
    parser.add_argument("--host", action='append', dest='hosts', metavar='HOST',
                        default=[],
                        help="Select host from config file to run on. "
                             "To run on multiple hosts, use multiple --host args")
    parser.add_argument("--all", action="store_true", dest='allhosts',
                        help="Use all hosts found in testhosts.cfg file")
    parser.set_defaults(func=list_testhosts)

    parser = subparsers.add_parser('docs', help='view the docs')
    parser.add_argument('plugin_dist_name', nargs='?',
                        help='name of plugin distribution or class')
    parser.add_argument("-b", "--browser", action="store", type=str, 
                        dest='browser', choices=webbrowser._browsers.keys(),
                        help="browser name")
    parser.set_defaults(func=openmdao_docs)
    
    parser = subparsers.add_parser('test', add_help=False,
                                   description="run the OpenMDAO test suite")
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='display test progress')
    parser.add_argument('packages', metavar='package', type=str, nargs='*',
                        help='package to be tested')
    parser.set_defaults(func=test_openmdao)
        
    # the following subcommands will only be available in a dev build, because
    # openmdao.devtools is not part of a normal OpenMDAO release
    try:
        # these commands will only be available on windows machines if pywin32 is available
        import fabric
        from openmdao.devtools.push_docs import push_docs
        from openmdao.devtools.remotetst import test_branch
        from openmdao.devtools.remote_cfg import add_config_options

        parser = subparsers.add_parser('test_branch', help='run tests on remote machines')
        parser.add_argument("-k","--keep", action="store_true", dest='keep',
                            help="Don't delete the temporary build directory. "
                                 "If testing on EC2 stop the instance instead of terminating it.")
        parser.add_argument("-f","--file", action="store", type=str, 
                            dest='fname',
                            help="Pathname of a tarfile or URL of a git repo. "
                                 "Defaults to the current repo.")
        parser.add_argument("-b","--branch", action="store", type=str, 
                            dest='branch',
                            help="If file is a git repo, supply branch name here")
        parser.add_argument("--testargs", action="store", type=str, dest='testargs',
                            default='',
                            help="args to be passed to openmdao test")
        parser = add_config_options(parser)
        parser.set_defaults(func=test_branch)
        
        parser = subparsers.add_parser('push_docs', help='push the dev docs up to the server')
        parser.add_argument('host', help='host to push docs to')
        parser.add_argument("-d", "--destination", action="store", type=str, 
                            dest="docdir", default='downloads',
                            help="directory where dev_docs directory will be placed")
        parser.add_argument("-n", "--nodocbuild", action="store_true", 
                            dest="nodocbuild",
                            help="used for testing. The docs will not be rebuilt if they already exist")
        parser.set_defaults(func=push_docs)

    except ImportError:
        pass

    try:
        from openmdao.devtools.build_docs import build_docs, test_docs
        parser = subparsers.add_parser('build_docs', help='build OpenMDAO docs')
        parser.add_argument("-v", "--version", action="store", type=str, 
                            dest="version",
                            help="the OpenMDAO version")
        parser.set_defaults(func=build_docs)
        
        parser = subparsers.add_parser('test_docs', help='run tests on the OpenMDAO docs')
        parser.set_defaults(func=test_docs)
        
    except ImportError:
        pass
    
    return top_parser

def openmdao():
    parser = _get_openmdao_parser()
    options, args = parser.parse_known_args()

    sys.exit(options.func(parser, options, args))
    
if __name__ == '__main__':
    openmdao()
    