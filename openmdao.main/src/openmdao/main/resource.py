"""
.. _`resource.py`:

Support for allocation of servers from one or more resources
(i.e., the local host, a cluster of remote hosts, etc.).
"""

import ConfigParser
import datetime
import logging
import multiprocessing
import os.path
import pkg_resources
import Queue
import re
import socket
import sys
import threading
import time
import traceback

from openmdao.main import mp_distributing
from openmdao.main.mp_support import register
from openmdao.main.objserverfactory import ObjServerFactory
from openmdao.main.rbac import get_credentials, set_credentials, rbac

from openmdao.util.eggloader import check_requirements
from openmdao.util.wrkpool import WorkerPool

# DRMAA JobTemplate derived keys.
QUEUING_SYSTEM_KEYS = set((
    'remote_command',
    'args',
    'submit_as_hold',
    'rerunnable',
    'job_environment',
    'working_directory',
    'job_category',
    'email',
    'email_on_started',
    'email_on_terminated',
    'job_name',
    'input_path',
    'output_path',
    'error_path',
    'join_files',
    'reservation_id',
    'queue_name',
    'priority',
    'start_time',
    'deadline_time',
    'resource_limits',
    'accounting_id',

    # 'escape' mechanism kept from earlier version.
    'native_specification',
))

# DRMAA derived job categories.
JOB_CATEGORIES = set((
    'MPI',
    'GridMPI',
    'LAM-MPI',
    'MPICH1',
    'MPICH2',
    'OpenMPI',
    'PVM',
    'OpenMP',
    'OpenCL',
    'Java',
))

# DRMAA derived resource limits.
RESOURCE_LIMITS = set((
    'core_file_size',
    'data_seg_size',
    'file_size',
    'open_files',
    'stack_size',
    'virtual_memory',
    'cpu_time',
    'wallclock_time',
))

# DRMAA derived constants.
HOME_DIRECTORY = '$drmaa_hd_ph$'
WORKING_DIRECTORY = '$drmaa_wd_ph$'

# Legal allocator name pattern.
_LEGAL_NAME = re.compile(r'^[a-zA-Z][_a-zA-Z0-9]*$')

# Checks for IPv4 address.
_IPV4_HOST = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$')


class ResourceAllocationManager(object):
    """
    The allocation manager maintains a list of :class:`ResourceAllocator`
    which are used to select the "best fit" for a particular resource request.
    The manager is initialized with a :class:`LocalAllocator` for the local
    host, using `authkey` of 'PublicKey', and allowing 'shell' access.

    By default ``~/.openmdao/resources.cfg`` will be used for additional
    configuration information. To avoid this, call :meth:`configure` before
    any other allocation routines.
    """

    _lock = threading.Lock()
    _RAM = None  # Singleton.

    def __init__(self, config_filename=None):
        self._logger = logging.getLogger('RAM')
        self._pid = os.getpid()  # For detecting copy from fork.
        self._allocations = 0
        self._allocators = []
        self._deployed_servers = {}
        self._allocators.append(LocalAllocator('LocalHost',
                                               authkey='PublicKey',
                                               allow_shell=True))
        if config_filename is None:
            config_filename = os.path.join('~', '.openmdao', 'resources.cfg')
            config_filename = os.path.expanduser(config_filename)
            if not os.path.exists(config_filename):
                return

        if config_filename:
            self._configure(config_filename)

    @staticmethod
    def configure(config_filename):
        """
        Configure allocators. This *must* be called before any other accesses
        if you want to avoid getting the default configuration as specified
        by ``~/.openmdao/resources.cfg``.

        config_filename: string
            Name of configuration file.
            If null, no additional configuration is performed.
        """
        with ResourceAllocationManager._lock:
            if ResourceAllocationManager._RAM is None:
                ResourceAllocationManager._RAM = \
                    ResourceAllocationManager(config_filename)
            elif config_filename:
                ram = ResourceAllocationManager._RAM
                ram._configure(config_filename)

    def _configure(self, config_filename):
        """ Configure manager instance. """
        self._logger.debug('Configuring from %r', config_filename)
        with open(config_filename, 'r') as inp:
            cfg = ConfigParser.ConfigParser()
            cfg.readfp(inp)
            for name in cfg.sections():
                self._logger.debug('  name: %s', name)
                for allocator in self._allocators:
                    if allocator.name == name:
                        self._logger.debug('        existing allocator')
                        allocator.configure(cfg)
                        break
                else:
                    classname = cfg.get(name, 'classname')
                    self._logger.debug('    classname: %s', classname)
                    mod_name, dot, cls_name = classname.rpartition('.')
                    try:
                        __import__(mod_name)
                    except ImportError as exc:
                        raise RuntimeError("RAM configure %s: can't import %r: %s"
                                           % (name, mod_name, exc))
                    module = sys.modules[mod_name]
                    if not hasattr(module, cls_name):
                        raise RuntimeError('RAM configure %s: no class %r in %s'
                                           % (name, cls_name, mod_name))
                    cls = getattr(module, cls_name)
                    allocator = cls(name)
                    allocator.configure(cfg)
                    self._allocators.append(allocator)

    @staticmethod
    def _get_instance():
        """ Return singleton instance. """
        with ResourceAllocationManager._lock:
            ram = ResourceAllocationManager._RAM
            if ram is None:
                ResourceAllocationManager._RAM = ResourceAllocationManager()
            elif ram._pid != os.getpid():  # pragma no cover
                # We're a copy from a fork.
                for allocator in ram._allocators:
                    allocator.invalidate()
                ResourceAllocationManager._RAM = ResourceAllocationManager()
            return ResourceAllocationManager._RAM

    @staticmethod
    def add_allocator(allocator):
        """
        Add an allocator to the list of resource allocators.

        allocator: ResourceAllocator
            The allocator to be added.
        """
        ram = ResourceAllocationManager._get_instance()
        with ResourceAllocationManager._lock:
            ram._allocators.append(allocator)

    @staticmethod
    def insert_allocator(index, allocator):
        """
        Insert an allocator into the list of resource allocators.

        index: int
            List index for the insertion point.

        allocator: ResourceAllocator
            The allocator to be inserted.
        """
        ram = ResourceAllocationManager._get_instance()
        with ResourceAllocationManager._lock:
            ram._allocators.insert(index, allocator)

    @staticmethod
    def get_allocator(selector):
        """
        Return allocator at `selector` or whose name is `selector`.

        selector: int or string
            List index or name of allocator to be returned.
        """
        ram = ResourceAllocationManager._get_instance()
        with ResourceAllocationManager._lock:
            if isinstance(selector, basestring):
                for allocator in ram._allocators:
                    if allocator.name == selector:
                        return allocator
                raise ValueError('allocator %r not found' % selector)
            else:
                return ram._allocators[selector]

    @staticmethod
    def remove_allocator(selector):
        """
        Remove allocator at `selector` or whose name is `selector`.

        selector: int or string
            List index or name of allocator to be removed.
        """
        ram = ResourceAllocationManager._get_instance()
        with ResourceAllocationManager._lock:
            if isinstance(selector, basestring):
                for i, allocator in enumerate(ram._allocators):
                    if allocator.name == selector:
                        return ram._allocators.pop(i)
                raise ValueError('allocator %r not found' % selector)
            else:
                return ram._allocators.pop(selector)

    @staticmethod
    def list_allocators():
        """ Return list of allocators. """
        ram = ResourceAllocationManager._get_instance()
        with ResourceAllocationManager._lock:
            return ram._allocators

    @staticmethod
    def max_servers(resource_desc):
        """
        Returns the maximum number of servers compatible with 'resource_desc`.
        This should be considered an upper limit on the number of concurrent
        allocations attempted.

        resource_desc: dict
            Description of required resources.
        """
        ResourceAllocationManager.validate_resources(resource_desc)
        ram = ResourceAllocationManager._get_instance()
        with ResourceAllocationManager._lock:
            return ram._max_servers(resource_desc)

    def _max_servers(self, resource_desc):
        """ Return total of each allocator's max servers. """
        total = 0
        for allocator in self._allocators:
            count, criteria = allocator.max_servers(resource_desc)
            if count <= 0:
                key = criteria.keys()[0]
                info = criteria[key]
                self._logger.debug('%r incompatible: key %r: %s',
                                   allocator.name, key, info)
            else:
                self._logger.debug('%r returned %d', allocator._name, count)
                total += count
        return total

    @staticmethod
    def allocate(resource_desc):
        """
        Determine best resource for `resource_desc` and deploy.
        In the case of a tie, the first allocator in the allocators list wins.
        Returns ``(proxy-object, server-dict)``.

        resource_desc: dict
            Description of required resources.
        """
        ResourceAllocationManager.validate_resources(resource_desc)
        ram = ResourceAllocationManager._get_instance()
       #with ResourceAllocationManager._lock:
        return ram._allocate(resource_desc)

    def _allocate(self, resource_desc):
        """ Do the allocation. """
        deployment_retries = 0
        best_estimate = -1
        while best_estimate == -1:
            with ResourceAllocationManager._lock:
                best_estimate, best_criteria, best_allocator = \
                    self._get_estimates(resource_desc)
            if best_estimate >= 0:
                with ResourceAllocationManager._lock:
                    self._allocations += 1
                    name = 'Sim-%d' % self._allocations
                    self._logger.debug('deploying on %r', best_allocator._name)
                    server = best_allocator.deploy(name, resource_desc,
                                                   best_criteria)
                    if server is not None:
                        server_info = {
                            'name': name,
                            'pid':  server.pid,
                            'host': server.host
                        }
                        self._logger.info('allocated %r pid %d on %s',
                                          name, server_info['pid'],
                                          server_info['host'])
                        self._deployed_servers[id(server)] = \
                            (best_allocator, server, server_info)
                        return (server, server_info)
                    # Difficult to generate deployable request that won't deploy...
                    else:  #pragma no cover
                        deployment_retries += 1
                        if deployment_retries > 10:
                            self._logger.error('deployment failed too many times.')
                            return (None, None)
                        self._logger.warning('deployment failed, retrying.')
                        best_estimate = -1
            elif best_estimate != -1:
                return (None, None)
            # Difficult to generate deployable request that won't deploy...
            else:  #pragma no cover
                time.sleep(1)  # Wait a bit between retries.

    @staticmethod
    def get_hostnames(resource_desc):
        """
        Determine best resource for `resource_desc` and return hostnames.
        In the case of a tie, the first allocator in the allocators list wins.
        Typically used by parallel code wrappers which have MPI or something
        similar for process deployment.

        resource_desc: dict
            Description of required resources.
        """
        ResourceAllocationManager.validate_resources(resource_desc)
        ram = ResourceAllocationManager._get_instance()
        with ResourceAllocationManager._lock:
            return ram._get_hostnames(resource_desc)

    def _get_hostnames(self, resource_desc):
        """ Get the hostnames. """
        best_score = -1
        while best_score == -1:
            best_score, best_criteria, best_allocator = \
                self._get_estimates(resource_desc, need_hostnames=True)
            if best_score >= 0:
                self._logger.debug('using %s', best_criteria['hostnames'])
                return best_criteria['hostnames']
            elif best_score != -1:
                return None
            # Difficult to generate deployable request that won't deploy...
            else:  #pragma no cover
                time.sleep(1)  # Wait a bit between retries.

    def _get_estimates(self, resource_desc, need_hostnames=False):
        """ Return best (estimate, criteria, allocator). """
        best_estimate = -2
        best_criteria = None
        best_allocator = None

        for allocator in self._allocators:
            estimate, criteria = allocator.time_estimate(resource_desc)
            if estimate == -2:
                key = criteria.keys()[0]
                info = criteria[key]
                self._logger.debug('%r incompatible: key %r: %s',
                                   allocator.name, key, info)
            else:
                msg = 'OK' if estimate == 0 else 'returned %g' % estimate
                self._logger.debug('%r %s', allocator.name, msg)

            if (best_estimate == -2 and estimate >= -1) or \
               (best_estimate == 0  and estimate >  0) or \
               (best_estimate >  0  and estimate < best_estimate):
                # All current allocators support 'hostnames'.
                if estimate >= 0 and need_hostnames \
                   and not 'hostnames' in criteria:  #pragma no cover
                    self._logger.debug("%r is missing 'hostnames'",
                                       allocator.name)
                else:
                    best_estimate = estimate
                    best_criteria = criteria
                    best_allocator = allocator

        return (best_estimate, best_criteria, best_allocator)

    @staticmethod
    def release(server):
        """
        Release a server (proxy).

        server: :class:`OpenMDAO_Proxy`
            Server to be released.
        """
        ram = ResourceAllocationManager._get_instance()
        # Lock in _release() so we don't keep the lock unnecessarily.
        return ram._release(server)

    def _release(self, server):
        """ Release a server (proxy). """
        with ResourceAllocationManager._lock:
            try:
                allocator, server, server_info = self._deployed_servers[id(server)]
            # Just being defensive.
            except KeyError:  #pragma no cover
                self._logger.error('server %r not found', server)
                return
            del self._deployed_servers[id(server)]

        self._logger.info('release %r pid %d on %s', server_info['name'],
                          server_info['pid'], server_info['host'])
        try:
            allocator.release(server)
        # Just being defensive.
        except Exception as exc:  #pragma no cover
            self._logger.error("Can't release %r: %r", server_info['name'], exc)
        server._close.cancel()

    @staticmethod
    def add_remotes(server, prefix=''):
        """
        Add allocators from a remote server to the list of resource allocators.

        server: proxy for a remote server
            The server whose allocators are to be added.
            It must support :meth:`get_ram` which should return the server's
            `ResourceAllocationManager` and a `host` attribute.

        prefix: string
            Prefix for the local names of the remote allocators.
            The default is the remote hostname.
        """
        remote_ram = server.get_ram()
        total = remote_ram.get_total_allocators()
        if not prefix:
            prefix = ResourceAllocationManager._make_prefix(server.host)
        proxies = []
        for i in range(total):
            allocator = remote_ram.get_allocator_proxy(i)
            proxy = RemoteAllocator('%s_%s' % (prefix, allocator.name),
                                    allocator)
            proxies.append(proxy)
        ram = ResourceAllocationManager._get_instance()
        with ResourceAllocationManager._lock:
            ram._allocators.extend(proxies)

    @staticmethod
    def _make_prefix(hostid):
        """ Return legal prefix based on `hostid`. """
        if _IPV4_HOST.match(hostid):  # Use all digits to be unique.
            prefix = hostid.replace('.', '')
        else:  # IP hostname (letters, digits, and hyphen are legal).
            prefix, dot, rest = hostid.partition('.')
            prefix = prefix.replace('-', '')
        return prefix

    @rbac('*')
    def get_total_allocators(self):
        """ Return number of allocators for remote use. """
        return len(self._allocators)

    @rbac('*', proxy_types=[object])
    def get_allocator_proxy(self, index):
        """
        Return allocator for remote use.

        index: int
            Index of the allocator to return.
        """
        return self._allocators[index]

    @staticmethod
    def validate_resources(resource_desc):
        """
        Validate that `resource_desc` is legal.

        resource_desc: dict
            Description of required resources.
        """
        for key, value in resource_desc.items():
            try:
                if not _VALIDATORS[key](value):
                    raise ValueError('Invalid resource value for %r: %r'
                                     % (key, value))
            except KeyError:
                raise KeyError('Invalid resource key %r' % key)

        if 'max_cpus' in resource_desc:
            if 'min_cpus' not in resource_desc:
                raise KeyError('min_cpus required if max_cpus specified')
            min_cpus = resource_desc['min_cpus']
            max_cpus = resource_desc['max_cpus']
            if max_cpus < min_cpus:
                raise ValueError('max_cpus %d < min_cpus %d'
                                 % (max_cpus, min_cpus))

def _true(value):
    """ Just returns True -- these registered keys need more work. """
    return True

def _bool(value):
    """ Validate bool key value. """
    return isinstance(value, bool)

def _datetime(value):
    """ Validate datetime key value. """
    return isinstance(value, datetime.datetime)

def _int(value):
    """ Validate int key value. """
    return isinstance(value, int)

def _positive(value):
    """ Validate positive key value. """
    return isinstance(value, int) and value > 0

def _string(value):
    """ Validate string key value. """
    return isinstance(value, basestring)

def _no_whitespace(value):
    """ Validate no_whitespace key value. """
    return isinstance(value, basestring) and len(value.split(' /t/n')) == 1

def _stringlist(value):
    """ Validate sequence of strings value. """
    if not isinstance(value, (list, tuple)):
        return False
    for item in value:
        if not isinstance(item, basestring):
            return False
    return True

def _allocator(value):
    """ Validate 'allocator' key value. """
    for allocator in ResourceAllocationManager.list_allocators():
        if allocator.name == value:
            return True
    return False

def _job_environment(value):
    """ Validate 'job_environment' key value. """
    if not isinstance(value, dict):
        return False
    for key, val in value.items():
        if not isinstance(key, basestring) or len(key.split()) > 1:
            return False
        if not isinstance(val, basestring):
            return False
    return True

def _job_category(value):
    """ Validate 'job_category' key value. """
    return value in JOB_CATEGORIES

def _resource_limits(value):
    """ Validate 'resource_limits' key value. """
    if not isinstance(value, dict):
        return False
    for key, val in value.items():
        if key not in RESOURCE_LIMITS:
            return False 
        if not isinstance(val, int):
            return False
        if val < 0:
            return False
    return True

# Registry of resource validators.
_VALIDATORS = {'allocator': _allocator,
               'localhost': _bool,
               'exclude': _stringlist,
               'required_distributions': _true,
               'orphan_modules': _stringlist,
               'python_version': _true,
               'python_platform': _true,

               'min_cpus': _positive,
               'max_cpus': _positive,
               'min_phys_memory': _positive,

               'remote_command': _no_whitespace,
               'args': _stringlist,
               'submit_as_hold': _bool,
               'rerunnable': _bool,
               'job_environment': _job_environment,
               'working_directory': _string,
               'job_category': _job_category,
               'email': _stringlist,
               'email_on_started': _bool,
               'email_on_terminated': _bool,
               'job_name': _string,
               'input_path': _string,
               'output_path': _string,
               'error_path': _string,
               'join_files': _bool,
               'reservation_id': _no_whitespace,
               'queue_name': _no_whitespace,
               'priority': _int,
               'start_time': _datetime,
               'deadline_time': _datetime,
               'resource_limits': _resource_limits,
               'accounting_id': _no_whitespace,
               'native_specification': _stringlist}


class ResourceAllocator(object):
    """
    Base class for allocators. Allocators estimate the suitability of a
    resource and can deploy on that resource.

    name: string
        Name of allocator, used in log messages, etc.
        Must be alphanumeric (underscore also allowed).
    """

    def __init__(self, name):
        match = _LEGAL_NAME.match(name)
        if match is None:
            raise NameError('name %r is not alphanumeric' % name)
        self._name = name
        self._logger = logging.getLogger(name)

    @property
    def name(self):
        """ This allocator's name. """
        return self._name

    def invalidate(self):
        """
        Invalidate this allocator. This will be called by the manager when
        it detects that its allocators are copies due to a process fork.
        The default implementation does nothing.
        """
        return

    # To be implemented by real allocator.
    def configure(self, cfg):  #pragma no cover
        """
        Configure allocator from :class:`ConfigParser` instance.
        Normally only called during manager initialization.

        cfg: :class:`ConfigParser`
            Configuration data is located under the section matching
            this allocator's `name`.

        The default implementation does nothing
        """
        return

    # To be implemented by real allocator.
    def max_servers(self, resource_desc):  #pragma no cover
        """
        Return the maximum number of servers which could be deployed for
        `resource_desc`.  The value needn't be exact, but performance may
        suffer if it overestimates.  The value is used to limit the number
        of concurrent evaluations.

        resource_desc: dict
            Description of required resources.
        """
        raise NotImplementedError('max_servers')

    # To be implemented by real allocator.
    def time_estimate(self, resource_desc):  #pragma no cover
        """
        Return ``(estimate, criteria)`` indicating how well this resource
        allocator can satisfy the `resource_desc` request.  The estimate will
        be:

        - >0 for an estimate of walltime (seconds).
        -  0 for no estimate.
        - -1 for no resource at this time.
        - -2 for no support for `resource_desc`.

        The returned criteria is a dictionary containing information related
        to the estimate, such as hostnames, load averages, unsupported
        resources, etc.

        resource_desc: dict
            Description of required resources.
        """
        raise NotImplementedError('time_estimate')

    def check_compatibility(self, resource_desc):
        """
        Check compatibility with common resource attributes.

        resource_desc: dict
            Description of required resources.

        Returns ``(retcode, info)``.  If `retcode` is zero, then `info`
        is a list of keys in `recource_desc` that have not been processed.
        Otherwise `retcode` will be -2 and `info` will be a single-entry
        dictionary whose key is the incompatible key in `resource_desc`
        and value provides data regarding the incompatibility.
        """
        keys = []
        for key, value in resource_desc.items():
            if key in QUEUING_SYSTEM_KEYS:
                pass
            elif key == 'required_distributions':
                missing = self.check_required_distributions(value)
                if missing:
                    return (-2, {key: 'missing %s' % missing})
            elif key == 'orphan_modules':
                missing = self.check_orphan_modules(value)
                if missing:
                    return (-2, {key: 'missing %s' % missing})
            elif key == 'python_version':
                if sys.version[:3] != value:
                    return (-2, {key : 'want %s, have %s' % (value, sys.version[:3])})
            elif key == 'exclude':
                if socket.gethostname() in value:
                    return (-2, {key : 'excluded host %s' % socket.gethostname()})
            elif key == 'allocator':
                if self.name != value:
                    return (-2, {key : 'wrong allocator'})
            else:
                keys.append(key)
        return (0, keys)

    def check_required_distributions(self, resource_value):
        """
        Returns a list of distributions that are not available.

        resource_value: list
            List of Distributions or Requirements.
        """
        required = []
        for item in resource_value:
            if isinstance(item, pkg_resources.Distribution):
                required.append(item.as_requirement())
            else:
                required.append(item)
        return check_requirements(sorted(required))

    def check_orphan_modules(self, resource_value):
        """
        Returns a list of 'orphan' modules that are not available.

        resource_value: list
            List of 'orphan' module names.
        """
#FIXME: shouldn't pollute the environment like this does.
        not_found = []
        for module in sorted(resource_value):
            try:
                __import__(module)
            except ImportError:
                not_found.append(module)
        return not_found

    # To be implemented by real allocator.
    def deploy(self, name, resource_desc, criteria):  #pragma no cover
        """
        Deploy a server suitable for `resource_desc`.
        Returns a proxy to the deployed server.

        name: string
            Name for server.

        resource_desc: dict
            Description of required resources.

        criteria: dict
            The dictionary returned by :meth:`time_estimate`.
        """
        raise NotImplementedError('deploy')

    # To be implemented by real allocator.
    def release(self, server):  #pragma no cover
        """
        Shut-down `server`.

        .. note::

            Unlike other methods which are protected from multithreaded
            access by the manager, :meth:`release` must be multithread-safe.

        server: :class:`ObjServer`
            Server to be shut down.
        """
        raise NotImplementedError('release')


class FactoryAllocator(ResourceAllocator):
    """
    Base class for allocators using :class:`ObjServerFactory`.

    name: string
        Name of allocator, used in log messages, etc.

    authkey: string
        Authorization key for this allocator and any deployed servers.

    allow_shell: bool
        If True, :meth:`execute_command` and :meth:`load_model` are allowed
        in created servers. Use with caution!
    """
    def __init__(self, name, authkey=None, allow_shell=False):
        super(FactoryAllocator, self).__init__(name)

        if authkey is None:
            authkey = multiprocessing.current_process().authkey
            if authkey is None:
                authkey = 'PublicKey'
                multiprocessing.current_process().authkey = authkey
        self.factory = ObjServerFactory(name, authkey, allow_shell)

    def configure(self, cfg):
        """
        Configure allocator from :class:`ConfigParser` instance.
        Normally only called during manager initialization.

        cfg: :class:`ConfigParser`
            Configuration data is located under the section matching
            this allocator's `name`.

        Allows modifying `auth_key` and `allow_shell`.
        """
        if cfg.has_option(self.name, 'authkey'):
            value = cfg.get(self.name, 'authkey')
            self._logger.debug('    authkey: %s', value)
            self.factory._authkey = value

        if cfg.has_option(self.name, 'allow_shell'):
            value = cfg.getboolean(self.name, 'allow_shell')
            self._logger.debug('    allow_shell: %s', value)
            self.factory._allow_shell = value

    @rbac('*')
    def deploy(self, name, resource_desc, criteria):
        """
        Deploy a server suitable for `resource_desc`.
        Returns a proxy to the deployed server.

        name: string
            Name for server.

        resource_desc: dict
            Description of required resources.

        criteria: dict
            The dictionary returned by :meth:`time_estimate`.
        """
        credentials = get_credentials()
        allowed_users = {credentials.user: credentials.public_key}
        try:
            return self.factory.create(typname='', allowed_users=allowed_users,
                                       name=name)
        # Shouldn't happen...
        except Exception as exc:  #pragma no cover
            self._logger.error('create failed: %r', exc)
            return None

    @rbac(('owner', 'user'))
    def release(self, server):
        """
        Release `server`.

        server: typically :class:`ObjServer`
            Previously deployed server to be shut down.
        """
        self.factory.release(server)


class LocalAllocator(FactoryAllocator):
    """
    Purely local resource allocator.

    name: string
        Name of allocator, used in log messages, etc.

    total_cpus: int
        If >0, then that is taken as the number of cpus/cores available.
        Otherwise the number is taken from :meth:`multiprocessing.cpu_count`.

    max_load: float
        Specifies the maximum cpu-adjusted load (obtained from
        :meth:`os.getloadavg`) allowed when reporting :meth:`max_servers` and
        when determining if another server may be started in
        :meth:`time_estimate`.

    authkey: string
        Authorization key for this allocator and any deployed servers.

    allow_shell: bool
        If True, :meth:`execute_command` and :meth:`load_model` are allowed
        in created servers. Use with caution!

    Resource configuration file entry equivalent to the default
    ``LocalHost`` allocator::

        [LocalHost]
        classname: openmdao.main.resource.LocalAllocator
        total_cpus: 1
        max_load: 1.0
        authkey: PublicKey
        allow_shell: True

    """

    def __init__(self, name='LocalAllocator', total_cpus=0, max_load=1.0,
                 authkey=None, allow_shell=False):
        super(LocalAllocator, self).__init__(name, authkey, allow_shell)
        if total_cpus > 0:
            self.total_cpus = total_cpus
        else:
            try:
                self.total_cpus = multiprocessing.cpu_count()
            # Just being defensive (according to docs this could happen).
            except NotImplementedError:  # pragma no cover
                self.total_cpus = 1
        if max_load > 0.:
            self.max_load = max_load
        else:
            raise ValueError('%s: max_load must be > 0, got %g'
                             % (name, max_load))
    @property
    def host(self):
        """ Allocator hostname. """
        return self.factory.host
 
    @property
    def pid(self):
        """ Allocator process ID. """
        return self.factory.pid
 
    def configure(self, cfg):
        """
        Configure allocator from :class:`ConfigParser` instance.
        Normally only called during manager initialization.

        cfg: :class:`ConfigParser`
            Configuration data is located under the section matching
            this allocator's `name`.

        Allows modifying factory options, `total_cpus`, and `max_load`.
        """
        super(LocalAllocator, self).configure(cfg)

        if cfg.has_option(self.name, 'total_cpus'):
            value = cfg.getint(self.name, 'total_cpus')
            self._logger.debug('    total_cpus: %s', value)
            if value > 0:
                self.total_cpus = value
            else:
                raise ValueError('%s: total_cpus must be > 0, got %d'
                                 % (self.name, value))

        if cfg.has_option(self.name, 'max_load'):
            value = cfg.getfloat(self.name, 'max_load')
            self._logger.debug('    max_load: %s', value)
            if value > 0.:
                self.max_load = value
            else:
                raise ValueError('%s: max_load must be > 0, got %g'
                                 % (self.name, value))

    @rbac('*')
    def max_servers(self, resource_desc):
        """
        Returns `total_cpus` * `max_load` if `resource_desc` is supported,
        otherwise zero.

        resource_desc: dict
            Description of required resources.
        """
        retcode, info = self.check_compatibility(resource_desc)
        if retcode != 0:
            return (0, info)
        avail_cpus = max(int(self.total_cpus * self.max_load), 1)
        if 'min_cpus' in resource_desc:
            req_cpus = resource_desc['min_cpus']
            if req_cpus > avail_cpus:
                return (0, {'min_cpus': 'want %s, available %s'
                                        % (req_cpus, avail_cpus)})
            else:
                return (avail_cpus / req_cpus, {})
        else:
            return (avail_cpus, {})

    @rbac('*')
    def time_estimate(self, resource_desc):
        """
        Returns ``(estimate, criteria)`` indicating how well this allocator can
        satisfy the `resource_desc` request.  The estimate will be:

        - >0 for an estimate of walltime (seconds).
        -  0 for no estimate.
        - -1 for no resource at this time.
        - -2 for no support for `resource_desc`.

        The returned criteria is a dictionary containing information related
        to the estimate, such as hostnames, load averages, unsupported
        resources, etc.

        resource_desc: dict
            Description of required resources.
        """
        retcode, info = self.check_compatibility(resource_desc)
        if retcode != 0:
            return (retcode, info)

        # Check system load.
        try:
            loadavgs = os.getloadavg()
        # Not available on Windows.
        except AttributeError:  #pragma no cover
            criteria = {
                'hostnames'  : [socket.gethostname()],
                'total_cpus' : self.total_cpus,
            }
            return (0, criteria)

        self._logger.debug('loadavgs %.2f, %.2f, %.2f, max_load %.2f',
                           loadavgs[0], loadavgs[1], loadavgs[2],
                           self.max_load * self.total_cpus)
        criteria = {
            'hostnames'  : [socket.gethostname()],
            'loadavgs'   : loadavgs,
            'total_cpus' : self.total_cpus,
            'max_load'   : self.max_load
        }
        if (loadavgs[0] / self.total_cpus) < self.max_load:
            return (0, criteria)
        # Tests force max_load high to avoid other issues.
        else:  #pragma no cover
            return (-1, criteria)  # Try again later.

    def check_compatibility(self, resource_desc):
        """
        Check compatibility with resource attributes.

        resource_desc: dict
            Description of required resources.

        Returns ``(retcode, info)``. If Compatible, then `retcode` is zero
        and `info` is empty. Otherwise `retcode` will be -2 and `info` will
        be a single-entry dictionary whose key is the incompatible key in
        `resource_desc` and value provides data regarding the incompatibility.
        """
        retcode, info = \
            super(LocalAllocator, self).check_compatibility(resource_desc)
        if retcode != 0:
            return (retcode, info)

        for key in info:
            value = resource_desc[key]
            if key == 'localhost':
                if not value:
                    return (-2, {key: 'requested remote host'})
            elif key == 'min_cpus':
                if value > self.total_cpus:
                    return (-2, {key: 'want %s, have %s'
                                      % (value, self.total_cpus)})
        return (0, {})

register(LocalAllocator, mp_distributing.Cluster)
register(LocalAllocator, mp_distributing.HostManager)


class RemoteAllocator(ResourceAllocator):
    """
    Allocator which delegates to a remote allocator.
    Configuration of remote allocators is not allowed.

    name: string
        Local name for allocator.

    remote: proxy
        Proxy for remote allocator.
    """

    def __init__(self, name, remote):
        super(RemoteAllocator, self).__init__(name)
        self._lock = threading.Lock()
        self._remote = remote

    @rbac('*')
    def max_servers(self, resource_desc):
        """ Return maximum number of servers for remote allocator. """
        rdesc, info = self._check_local(resource_desc)
        if rdesc is None:
            return (0, info[1])
        return self._remote.max_servers(rdesc)

    @rbac('*')
    def time_estimate(self, resource_desc):
        """ Return the time estimate from the remote allocator. """
        rdesc, info = self._check_local(resource_desc)
        if rdesc is None:
            return info
        return self._remote.time_estimate(rdesc)

    def _check_local(self, resource_desc):
        """ Check locally-relevant resources. """
        rdesc = resource_desc.copy()
        for key in ('localhost', 'allocator'):
            if key not in rdesc:
                continue
            value = rdesc[key]
            if key == 'localhost':
                if value:
                    return None, (-2, {key: 'requested local host'})
            elif key == 'allocator':
                if value != self.name:
                    return None, (-2, {key: 'wrong allocator'})
            del rdesc[key]
        return (rdesc, None)

    @rbac('*')
    def deploy(self, name, resource_desc, criteria):
        """ Deploy on the remote allocator. """
        return self._remote.deploy(name, resource_desc, criteria)

    @rbac(('owner', 'user'))
    def release(self, server):
        """ Release a remotely allocated server. """
        with self._lock:  # Proxies are not thread-safe.
            self._remote.release(server)


# Cluster allocation requires ssh configuration and multiple hosts.
class ClusterAllocator(ResourceAllocator):  #pragma no cover
    """
    Cluster-based resource allocator.  This allocator manages a collection
    of :class:`LocalAllocator`, one for each machine in the cluster.

    name: string
        Name of allocator, used in log messages, etc.

    machines: list(dict)
        Dictionaries providing configuration data for each machine in the
        cluster.  At a minimum, each dictionary must specify a host
        address in 'hostname' and the path to the OpenMDAO Python command in
        'python'.

    authkey: string
        Authorization key to be passed-on to remote servers.

    allow_shell: bool
        If True, :meth:`execute_command` and :meth:`load_model` are allowed
        in created servers. Use with caution!

    We assume that machines in the cluster are similar enough that ranking
    by load average is reasonable.
    """

    def __init__(self, name, machines=None, authkey=None, allow_shell=False):
        super(ClusterAllocator, self).__init__(name)

        if authkey is None:
            authkey = multiprocessing.current_process().authkey
            if authkey is None:
                authkey = 'PublicKey'
                multiprocessing.current_process().authkey = authkey

        self._authkey = authkey
        self._allow_shell = allow_shell
        self._lock = threading.Lock()
        self._allocators = {}
        self._last_deployed = None
        self._reply_q = Queue.Queue()
        self._deployed_servers = {}

        if machines is not None:
            self._initialize(machines)

    def _initialize(self, machines):
        """ Setup allocators on the given machines. """
        hostnames = set()
        hosts = []
        for machine in machines:
            hostname = machine['hostname']
            if hostname in hostnames:
                self._logger.warning('Ignoring duplicate hostname %r', hostname)
                continue
            hostnames.add(hostname)
            host = mp_distributing.Host(machine['hostname'],
                                        python=machine['python'])
            host.register(LocalAllocator)
            hosts.append(host)

        self.cluster = mp_distributing.Cluster(hosts, authkey=self._authkey,
                                               allow_shell=self._allow_shell)
        self.cluster.start()
        self._logger.debug('server listening on %r', (self.cluster.address,))

        for host in self.cluster:
            manager = host.manager
            try:
                la_name = manager._name
            except AttributeError:
                la_name = 'localhost'
                host_ip = '127.0.0.1'
            else:
                # 'host' is 'Host-<ipaddr>:<port>
                dash = la_name.index('-')
                colon = la_name.index(':')
                host_ip = la_name[dash+1:colon]
                la_name = la_name.replace('-', '_')
                la_name = la_name.replace('.', '')
                la_name = la_name.replace(':', '_')

            if host_ip not in self._allocators:
                allocator = \
                    manager.openmdao_main_resource_LocalAllocator(name=la_name,
                                                  allow_shell=self._allow_shell)
                self._allocators[host_ip] = allocator
                self._logger.debug('%s allocator %r pid %s', host.hostname,
                                   la_name, allocator.pid)

    def __getitem__(self, i):
        return self._allocators[i]

    def __iter__(self):
        return iter(self._allocators)

    def __len__(self):
        return len(self._allocators)

    def configure(self, cfg):
        """
        Configure a cluster consisting of hosts with node-numbered hostnames
        all using the same Python executable. Hostnames are generated from
        `origin` to `nhosts`+`origin` from `format` (`origin` defaults to 0).
        The Python executable is specified by the `python` option. It defaults
        to the currently executing Python.

        Resource configuration file entry for a cluster named ``HX`` consisting
        of 19 hosts with the first host named ``hx00`` and using the current
        OpenMDAO Python::

            [HX]
            classname: openmdao.main.resource.ClusterAllocator
            nhosts: 19
            origin: 0
            format: hx%02d
            authkey: PublicKey
            allow_shell: True

        """
        nhosts = cfg.getint(self.name, 'nhosts')
        self._logger.debug('    nhosts: %s', nhosts)

        if cfg.has_option(self.name, 'origin'):
            origin = cfg.getint(self.name, 'origin')
        else:
            origin = 0
        self._logger.debug('    origin: %s', origin)

        pattern = cfg.get(self.name, 'format')
        self._logger.debug('    format: %s', pattern)

        if cfg.has_option(self.name, 'python'):
            python = cfg.get(self.name, 'python')
        else:
            python = sys.executable
        self._logger.debug('    python: %s', python)

        if cfg.has_option(self.name, 'authkey'):
            self._authkey = cfg.get(self.name, 'authkey')
            self._logger.debug('    authkey: %s', self._authkey)

        if cfg.has_option(self.name, 'allow_shell'):
            self._allow_shell = cfg.getboolean(self.name, 'allow_shell')
            self._logger.debug('    allow_shell: %s', self._allow_shell)

        machines = []
        for i in range(origin, nhosts+origin):
            hostname = pattern % i
            machines.append(dict(hostname=hostname, python=python))
        self._initialize(machines)

    def max_servers(self, resource_desc):
        """
        Returns the total of :meth:`max_servers` across all
        :class:`LocalAllocator` in the cluster.

        resource_desc: dict
            Description of required resources.
        """
        credentials = get_credentials()

        rdesc, info = self._check_local(resource_desc)
        if rdesc is None:
            return (0, info[1])

        with self._lock:
            # Drain _reply_q.
            while True:
                try:
                    self._reply_q.get_nowait()
                except Queue.Empty:
                    break

            # Get counts via worker threads.
            todo = []
            max_workers = 10
            for i, allocator in enumerate(self._allocators.values()):
                if i < max_workers:
                    worker_q = WorkerPool.get()
                    worker_q.put((self._get_count,
                                  (allocator, resource_desc, credentials),
                                  {}, self._reply_q))
                else:
                    todo.append(allocator)

            # Process counts.
            total = 0
            for i in range(len(self._allocators)):
                worker_q, retval, exc, trace = self._reply_q.get()
                if exc:
                    self._logger.error(trace)
                    raise exc

                try:
                    next_allocator = todo.pop(0)
                except IndexError:
                    WorkerPool.release(worker_q)
                else:
                    worker_q.put((self._get_count,
                                  (next_allocator, resource_desc, credentials),
                                  {}, self._reply_q))
                count = retval
                if count:
                    total += count

            if 'min_cpus' in resource_desc:
                req_cpus = resource_desc['min_cpus']
                if req_cpus > total:
                    return (0, {'min_cpus': 'want %s, total %s'
                                            % (req_cpus, total)})
                else:
                    return (total / req_cpus, {})
            else:
                return (total, {})

    def _get_count(self, allocator, resource_desc, credentials):
        """ Get `max_servers` from an allocator. """
        set_credentials(credentials)
        count = 0
        try:
            count, criteria = allocator.max_servers(resource_desc)
        except Exception:
            msg = traceback.format_exc()
            self._logger.error('%r max_servers() caught exception %s',
                               allocator.name, msg)
        return max(count, 0)

    def time_estimate(self, resource_desc):
        """
        Returns ``(estimate, criteria)`` indicating how well this allocator
        can satisfy the `resource_desc` request.  The estimate will be:

        - >0 for an estimate of walltime (seconds).
        -  0 for no estimate.
        - -1 for no resource at this time.
        - -2 for no support for `resource_desc`.

        The returned criteria is a dictionary containing information related
        to the estimate, such as hostnames, load averages, unsupported
        resources, etc.

        This allocator polls each :class:`LocalAllocator` in the cluster
        to find the best match and returns that.  The best allocator is saved
        in the returned criteria for a subsequent :meth:`deploy`.

        resource_desc: dict
            Description of required resources.
        """
        credentials = get_credentials()

        rdesc, info = self._check_local(resource_desc)
        if rdesc is None:
            return info

        min_cpus = rdesc.get('min_cpus', 0)
        if min_cpus:
            # Spread across LocalAllocators.
            rdesc['min_cpus'] = 1

        avail_cpus = 0
        with self._lock:
            best_estimate = -2
            best_criteria = {'': 'No LocalAllocator results'}
            best_allocator = None
 
            # Prefer not to repeat use of just-used allocator.
            prev_estimate = -2
            prev_criteria = None
            prev_allocator = self._last_deployed
            self._last_deployed = None

            # Drain _reply_q.
            while True:
                try:
                    self._reply_q.get_nowait()
                except Queue.Empty:
                    break

            # Get estimates via worker threads.
            todo = []
            max_workers = 10
            for i, allocator in enumerate(self._allocators.values()):
                if i < max_workers:
                    worker_q = WorkerPool.get()
                    worker_q.put((self._get_estimate,
                                  (allocator, rdesc, credentials),
                                  {}, self._reply_q))
                else:
                    todo.append(allocator)

            # Process estimates.
            host_loads = []  # Sorted list of (load, criteria)
            for i in range(len(self._allocators)):
                worker_q, retval, exc, trace = self._reply_q.get()
                if exc:
                    self._logger.error(trace)
                    retval = None

                try:
                    next_allocator = todo.pop(0)
                except IndexError:
                    WorkerPool.release(worker_q)
                else:
                    worker_q.put((self._get_estimate,
                                  (next_allocator, rdesc, credentials),
                                  {}, self._reply_q))

                if retval is None:
                    continue
                allocator, estimate, criteria = retval
                if estimate is None:
                    continue

                # Accumulate available cpus in cluster.
                avail_cpus += criteria['total_cpus']

                # CPU-adjusted load (if available).
                if 'loadavgs' in criteria:
                    load = criteria['loadavgs'][0] / criteria['total_cpus']
                else:  # Windows
                    load = 0.

                # Insertion sort of host_loads.
                if estimate >= 0 and min_cpus:
                    new_info = (load, criteria)
                    if host_loads:
                        for i, info in enumerate(host_loads):
                            if load < info[0]:
                                host_loads.insert(i, new_info)
                                break
                        else:
                            host_loads.append(new_info)
                    else:
                        host_loads.append(new_info)

                # Update best estimate.
                if allocator is prev_allocator:
                    prev_estimate = estimate
                    prev_criteria = criteria
                elif (best_estimate <= 0 and estimate > best_estimate) or \
                     (best_estimate >  0 and estimate < best_estimate):
                    best_estimate = estimate
                    best_criteria = criteria
                    best_allocator = allocator
                elif (best_estimate == 0 and estimate == 0):
                    best_load = best_criteria['loadavgs'][0]
                    if load < best_load:
                        best_estimate = estimate
                        best_criteria = criteria
                        best_allocator = allocator

            # If no alternative, repeat use of previous allocator.
            if best_estimate < 0 and prev_estimate >= 0:
                best_estimate = prev_estimate
                best_criteria = prev_criteria
                best_allocator = prev_allocator

            if avail_cpus < min_cpus:
                return (-2, {'min_cpus': 'want %d, available %d' \
                                         % (min_cpus, avail_cpus)})

            # Save best allocator in criteria in case we're asked to deploy.
            if best_allocator is not None:
                best_criteria['allocator'] = best_allocator
                if min_cpus:
                    # Save min_cpus hostnames in criteria.
                    hostnames = []
                    for load, criteria in host_loads:
                        hostname = criteria['hostnames'][0]
                        hostnames.append(hostname)
                        if len(hostnames) >= min_cpus:
                            break
                        total_cpus = criteria['total_cpus']
                        max_load = criteria.get('max_load', 1)
                        load *= total_cpus  # Restore from cpu-adjusted value.
                        max_load *= total_cpus
                        load += 1
                        while load < max_load and len(hostnames) < min_cpus:
                            hostnames.append(hostname)
                            load += 1
                        if len(hostnames) >= min_cpus:
                            break
                    if len(hostnames) < min_cpus:
                        return (-1, {'min_cpus': 'want %d, idle %d' \
                                                 % (min_cpus, len(hostnames))})
                    best_criteria['hostnames'] = hostnames

            return (best_estimate, best_criteria)

    def _check_local(self, resource_desc):
        """ Check locally-relevant resources. """
        rdesc = resource_desc.copy()
        for key in ('localhost', 'allocator'):
            if key not in rdesc:
                continue
            value = rdesc[key]
            if key == 'localhost':
                if value:
                    return None, (-2, {key: 'requested local host'})
            elif key == 'allocator':
                if value != self.name:
                    return None, (-2, {key: 'wrong allocator'})
            del rdesc[key]
        return (rdesc, None)

    def _get_estimate(self, allocator, resource_desc, credentials):
        """ Get (estimate, criteria) from an allocator. """
        set_credentials(credentials)
        try:
            estimate, criteria = allocator.time_estimate(resource_desc)
        except Exception:
            msg = traceback.format_exc()
            self._logger.error('%r time_estimate() caught exception %s',
                               allocator.name, msg)
            estimate = None
            criteria = None
        else:
            if estimate == 0:
                self._logger.debug('%r returned %g (%g)', allocator.name,
                                   estimate, criteria['loadavgs'][0])
            else:
                self._logger.debug('%r returned %g', allocator.name, estimate)

        return (allocator, estimate, criteria)

    def deploy(self, name, resource_desc, criteria):
        """
        Deploy a server suitable for `resource_desc`.
        Uses the allocator saved in `criteria`.
        Returns a proxy to the deployed server.

        name: string
            Name for server.

        resource_desc: dict
            Description of required resources.

        criteria: dict
            The dictionary returned by :meth:`time_estimate`.
        """
        with self._lock:
            allocator = criteria['allocator']
            self._last_deployed = allocator
            del criteria['allocator']  # Don't pass a proxy without a server!
        try:
            server = allocator.deploy(name, resource_desc, criteria)
        except Exception as exc:
            self._logger.error('%r deploy() failed for %s: %r',
                               allocator.name, name, exc)
            return None

        if server is None:
            self._logger.error('%r deployment failed for %s',
                               allocator.name, name)
        else:
            self._deployed_servers[id(server)] = (allocator, server)
        return server

    def release(self, server):
        """
        Release a server (proxy).

        server: :class:`OpenMDAO_Proxy`
            Server to be released.
        """
        with self._lock:
            try:
                allocator = self._deployed_servers[id(server)][0]
            except KeyError:
                self._logger.error('server %r not found', server)
                return
            del self._deployed_servers[id(server)]

        try:
            allocator.release(server)
        except Exception as exc:
            self._logger.error("Can't release %r: %r", server, exc)
        server._close.cancel()

    def shutdown(self):
        """ Shutdown, releasing resources. """
        self.cluster.shutdown()

