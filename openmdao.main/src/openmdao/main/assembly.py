""" Class definition for Assembly. """


#public symbols
__all__ = ['Assembly', 'set_as_top']

import cStringIO
import threading
import re

from zope.interface import implementedBy

# pylint: disable-msg=E0611,F0401
import networkx as nx

from openmdao.main.interfaces import implements, IAssembly, IDriver, IArchitecture, IComponent, IContainer,\
                                     ICaseIterator, ICaseRecorder, IDOEgenerator
from openmdao.main.mp_support import has_interface
from openmdao.main.container import _copydict
from openmdao.main.component import Component, Container
from openmdao.main.variable import Variable
from openmdao.main.vartree import VariableTree
from openmdao.main.datatypes.api import Slot
from openmdao.main.driver import Driver, Run_Once
from openmdao.main.hasparameters import HasParameters, ParameterGroup
from openmdao.main.hasconstraints import HasConstraints, HasEqConstraints, HasIneqConstraints
from openmdao.main.hasobjective import HasObjective, HasObjectives
from openmdao.main.rbac import rbac
from openmdao.main.mp_support import is_instance
from openmdao.main.printexpr import eliminate_expr_ws
from openmdao.main.exprmapper import ExprMapper, PseudoComponent
from openmdao.util.nameutil import partition_names_by_comp

_iodict = {'out': 'output', 'in': 'input'}


__has_top__ = False
__toplock__ = threading.RLock()


def set_as_top(cont, first_only=False):
    """Specifies that the given Container is the top of a Container hierarchy.
    If first_only is True, then only set it as a top if a global
    top doesn't already exist.
    """
    global __toplock__
    global __has_top__
    with __toplock__:
        if __has_top__ is False and isinstance(cont, Assembly):
            __has_top__ = True
        elif first_only:
            return cont
    if cont._call_cpath_updated:
        cont.cpath_updated()
    return cont


class PassthroughTrait(Variable):
    """A trait that can use another trait for validation, but otherwise is
    just a trait that lives on an Assembly boundary and can be connected
    to other traits within the Assembly.
    """

    def validate(self, obj, name, value):
        """Validation for the PassThroughTrait."""
        if self.validation_trait:
            return self.validation_trait.validate(obj, name, value)
        return value


class PassthroughProperty(Variable):
    """Replacement for PassthroughTrait when the target is a proxy/property
    trait. PassthroughTrait would get a core dump while pickling.
    """
    def __init__(self, target_trait, **metadata):
        self._trait = target_trait
        self._vals = {}
        super(PassthroughProperty, self).__init__(**metadata)

    def get(self, obj, name):
        return self._vals.get(obj, {}).get(name, self._trait.default_value)

    def set(self, obj, name, value):
        if obj not in self._vals:
            self._vals[obj] = {}
        self._vals[obj][name] = self._trait.validate(obj, name, value)


def _find_common_interface(obj1, obj2):
    for iface in (IAssembly, IComponent, IDriver, IArchitecture, IContainer,
                  ICaseIterator, ICaseRecorder, IDOEgenerator):
        if has_interface(obj1, iface) and has_interface(obj2, iface):
            return iface
    return None


class Assembly(Component):
    """This is a container of Components. It understands how to connect inputs
    and outputs between its children.  When executed, it runs the top level
    Driver called 'driver'.
    """

    implements(IAssembly)

    driver = Slot(IDriver, allow_none=True,
                    desc="The top level Driver that manages execution of "
                    "this Assembly.")

    def __init__(self):

        super(Assembly, self).__init__()

        self._exprmapper = ExprMapper(self)
        self._graph_loops = []

        # default Driver executes its workflow once
        self.add('driver', Run_Once())

        set_as_top(self, first_only=True)  # we're the top Assembly only if we're the first instantiated

    @rbac(('owner', 'user'))
    def set_itername(self, itername, seqno=0):
        """
        Set current 'iteration coordinates'. Overrides :class:`Component`
        to propagate to driver, and optionally set the initial count in the
        driver's workflow. Setting the initial count is typically done by
        :class:`CaseIterDriverBase` on a remote top level assembly.

        itername: string
            Iteration coordinates.

        seqno: int
            Initial execution count for driver's workflow.
        """
        super(Assembly, self).set_itername(itername)
        self.driver.set_itername(itername)
        if seqno:
            self.driver.workflow.set_initial_count(seqno)

    def add(self, name, obj):
        """Call the base class *add*.  Then,
        if obj is a Component, add it to the component graph.
        Returns the added object.
        """
        obj = super(Assembly, self).add(name, obj)
        if has_interface(obj, IComponent):
            kwargs = {}
            if has_interface(obj, IDriver):
                kwargs['driver'] = True
            if isinstance(obj, PseudoComponent):
                kwargs['pseudo'] = obj._pseudo_type
            self._depgraph.add_component(obj.name, 
                                         obj.list_inputs(), 
                                         obj.list_outputs(),
                                         **kwargs)
        return obj

    def find_referring_connections(self, name):
        """Returns a list of connections where the given name is referred
        to either in the source or the destination.
        """
        exprset = set(self._exprmapper.find_referring_exprs(name))
        return [(u, v) for u, v in self.list_connections(show_passthrough=True)
                                        if u in exprset or v in exprset]

    def find_in_workflows(self, name):
        """Returns a list of tuples of the form (workflow, index) for all
        workflows in the scope of this Assembly that contain the given
        component name.
        """
        wflows = []
        for item in self.list_containers():
            obj = self.get(item)
            if isinstance(obj, Driver) and name in obj.workflow:
                wflows.append((obj.workflow, obj.workflow.index(name)))
        return wflows

    def _cleanup_autopassthroughs(self, name):
        """Clean up any autopassthrough connections involving the given name.
        Returns a list containing a tuple for each removed connection.
        """
        old_autos = []
        if self.parent:
            old_rgx = re.compile(r'(\W?)%s.' % name)
            par_rgx = re.compile(r'(\W?)parent.')

            for u, v in self._depgraph.list_autopassthroughs():
                newu = re.sub(old_rgx, r'\g<1>%s.' % '.'.join([self.name, name]), u)
                newv = re.sub(old_rgx, r'\g<1>%s.' % '.'.join([self.name, name]), v)
                if newu != u or newv != v:
                    old_autos.append((u, v))
                    u = re.sub(par_rgx, r'\g<1>', newu)
                    v = re.sub(par_rgx, r'\g<1>', newv)
                    self.parent.disconnect(u, v)
        return old_autos

    def rename(self, oldname, newname):
        """Renames a child of this object from oldname to newname."""
        self._check_rename(oldname, newname)
        conns = self.find_referring_connections(oldname)
        wflows = self.find_in_workflows(oldname)
        old_autos = self._cleanup_autopassthroughs(oldname)

        obj = self.remove(oldname)
        self.add(newname, obj)

        # oldname has now been removed from workflows, but newname may be in the wrong
        # location, so force it to be at the same index as before removal
        for wflow, idx in wflows:
            wflow.remove(newname)
            wflow.add(newname, idx)

        old_rgx = re.compile(r'(\W?)%s.' % oldname)
        par_rgx = re.compile(r'(\W?)parent.')

        # recreate all of the broken connections after translating oldname to newname
        for u, v in conns:
            self.connect(re.sub(old_rgx, r'\g<1>%s.' % newname, u),
                         re.sub(old_rgx, r'\g<1>%s.' % newname, v))

        # recreate autopassthroughs
        if self.parent:
            for u, v in old_autos:
                u = re.sub(old_rgx, r'\g<1>%s.' % '.'.join([self.name, newname]), u)
                v = re.sub(old_rgx, r'\g<1>%s.' % '.'.join([self.name, newname]), v)
                u = re.sub(par_rgx, r'\g<1>', u)
                v = re.sub(par_rgx, r'\g<1>', v)
                self.parent.connect(u, v)

    def replace(self, target_name, newobj):
        """Replace one object with another, attempting to mimic the inputs and connections
        of the replaced object as much as possible.
        """
        tobj = getattr(self, target_name)

        # Save existing driver references.
        refs = {}
        if has_interface(tobj, IComponent):
            for obj in self.__dict__.values():
                if obj is not tobj and is_instance(obj, Driver):
                    refs[obj] = obj.get_references(target_name)

        if has_interface(newobj, IComponent):  # remove any existing connections to replacement object
            self.disconnect(newobj.name)
        if hasattr(newobj, 'mimic'):
            try:
                newobj.mimic(tobj)  # this should copy inputs, delegates and set name
            except Exception:
                self.reraise_exception("Couldn't replace '%s' of type %s with type %s"
                                       % (target_name, type(tobj).__name__,
                                          type(newobj).__name__))
        conns = self.find_referring_connections(target_name)
        wflows = self.find_in_workflows(target_name)
        target_rgx = re.compile(r'(\W?)%s.' % target_name)
        conns.extend([(u, v) for u, v in self._depgraph.list_autopassthroughs() if
                                 re.search(target_rgx, u) is not None or
                                 re.search(target_rgx, v) is not None])

        self.add(target_name, newobj)  # this will remove the old object (and any connections to it)

        # recreate old connections
        for u, v in conns:
            self.connect(u, v)

        # add new object (if it's a Component) to any workflows where target was
        if has_interface(newobj, IComponent):
            for wflow, idx in wflows:
                wflow.add(target_name, idx)

        # Restore driver references.
        if refs:
            for obj in self.__dict__.values():
                if obj is not newobj and is_instance(obj, Driver):
                    obj.restore_references(refs[obj], target_name)

        # Workflows need a reference to their new parent driver
        if is_instance(newobj, Driver):
            newobj.workflow._parent = newobj

    def remove(self, name):
        """Remove the named container object from this assembly and remove
        it from its workflow(s) if it's a Component."""
        cont = getattr(self, name)
        self.disconnect(name)
        if has_interface(cont, IComponent):
            for obj in self.__dict__.values():
                if obj is not cont and is_instance(obj, Driver):
                    obj.workflow.remove(name)
                    obj.remove_references(name)

        return super(Assembly, self).remove(name)

    def create_passthrough(self, pathname, alias=None):
        """Creates a PassthroughTrait that uses the trait indicated by
        pathname for validation, adds it to self, and creates a connection
        between the two. If alias is *None,* the name of the alias trait will
        be the last entry in its pathname. The trait specified by pathname
        must exist.
        """
        parts = pathname.split('.')
        if alias:
            newname = alias
        else:
            newname = parts[-1]

        if newname in self.__dict__:
            self.raise_exception("'%s' already exists" %
                                 newname, KeyError)
        if len(parts) < 2:
            self.raise_exception('destination of passthrough must be a dotted path',
                                 NameError)
        comp = self
        for part in parts[:-1]:
            try:
                comp = getattr(comp, part)
            except AttributeError:
                trait = None
                break
        else:
            trait = comp.get_trait(parts[-1])
            iotype = comp.get_iotype(parts[-1])

        if trait:
            ttype = trait.trait_type
            if ttype is None:
                ttype = trait
        else:
            if not self.contains(pathname):
                self.raise_exception("the variable named '%s' can't be found" %
                                     pathname, KeyError)
            iotype = self.get_metadata(pathname, 'iotype')

        if trait is not None and not trait.validate:
            trait = None  # no validate function, so just don't use trait for validation

        metadata = self.get_metadata(pathname)
        metadata['target'] = pathname
        # PassthroughTrait to a trait with get/set methods causes a core dump
        # in Traits (at least through 3.6) while pickling.
        if "validation_trait" in metadata:
            if metadata['validation_trait'].get is None:
                newtrait = PassthroughTrait(**metadata)
            else:
                newtrait = PassthroughProperty(metadata['validation_trait'],
                                               **metadata)
        elif trait and ttype.get:
            newtrait = PassthroughProperty(ttype, **metadata)
        else:
            newtrait = PassthroughTrait(validation_trait=trait, **metadata)
        self.add_trait(newname, newtrait)

        # Copy trait value according to 'copy' attribute in the trait
        val = self.get(pathname)

        ttype = trait.trait_type
        if ttype.copy:
            # Variable trees need to point to a new parent.
            # Also, let's not deepcopy the outside universe
            if isinstance(val, Container):
                old_parent = val.parent
                val.parent = None
                val_copy = _copydict[ttype.copy](val)
                val.parent = old_parent
                val_copy.parent = self
                val = val_copy
            else:
                val = _copydict[ttype.copy](val)

        setattr(self, newname, val)

        try:
            if iotype == 'in':
                self.connect(newname, pathname)
            else:
                self.connect(pathname, newname)
        except RuntimeError as err:
            self.remove(newname)
            raise err

        return newtrait

    def get_passthroughs(self):
        ''' Get all the inputs and outputs of the assembly's child components
            and indicate for each whether or not it is a passthrough variable.
            If it is a passthrough, provide the assembly's name for the variable.
        '''
        inputs = {}
        outputs = {}
        passthroughs = {}

        for name in self.list_inputs() + self.list_outputs():
            target = self.get_metadata(name, 'target')
            if target is not None:
                passthroughs[target] = name

        for comp in self.list_components():
            inputs[comp] = {}
            input_vars = self.get(comp).list_inputs()
            for var_name in input_vars:
                var_path = '.'.join([comp, var_name])
                if var_path in passthroughs:
                    inputs[comp][var_name] = passthroughs[var_path]
                else:
                    inputs[comp][var_name] = False

            outputs[comp] = {}
            output_vars = self.get(comp).list_outputs()
            for var_name in output_vars:
                var_path = '.'.join([comp, var_name])
                if var_path in passthroughs:
                    outputs[comp][var_name] = passthroughs[var_path]
                else:
                    outputs[comp][var_name] = False

        return {
            'inputs': inputs,
            'outputs': outputs
        }

    def _split_varpath(self, path):
        """Return a tuple of compname,component,varname given a path
        name of the form 'compname.varname'. If the name is of the form 'varname',
        then compname will be None and comp is self.
        """
        try:
            compname, varname = path.split('.', 1)
        except ValueError:
            return (None, self, path)

        t = self.get_trait(compname)
        if t and t.iotype:
            return (None, self, path)
        return (compname, getattr(self, compname), varname)

    @rbac(('owner', 'user'))
    def connect(self, src, dest):
        """Connect one src expression to one destination expression. This could be
        a normal connection between variables from two internal Components, or
        it could be a passthrough connection, which connects across the scope boundary
        of this object.  When a pathname begins with 'parent.', that indicates
        it is referring to a Variable outside of this object's scope.

        src: str
            Source expression string.

        dest: str or list(str)
            Destination expression string(s).
        """
        src = eliminate_expr_ws(src)

        if isinstance(dest, basestring):
            dest = (dest,)
        for dst in dest:
            dst = eliminate_expr_ws(dst)
            try:
                self._connect(src, dst)
            except Exception as err:
                self.raise_exception("Can't connect '%s' to '%s': %s" % (src, dst, err), RuntimeError)

    def _connect(self, src, dest):
        """Handle one connection destination. This should only be called via the connect()
        function, never directly.
        """

        # Among other things, check if already connected.
        srcexpr, destexpr, pcomp_type = \
                   self._exprmapper.check_connect(src, dest, self)

        # Check if dest is declared as a parameter in any driver in the assembly
        for item in self.list_containers():
            comp = self.get(item)
            if isinstance(comp, Driver) and \
                hasattr(comp, 'list_param_targets'):
                    if dest in comp.list_param_targets():
                        msg = "destination '%s' is a Parameter in " % dest
                        msg += "driver '%s'." % comp.name
                        self.raise_exception(msg, RuntimeError)

        if pcomp_type is not None:
            pseudocomp = PseudoComponent(self, srcexpr, destexpr, 
                                         pseudo_type=pcomp_type)
            self.add(pseudocomp.name, pseudocomp)
            pseudocomp.make_connections(self)
        else:
            pseudocomp = None
            super(Assembly, self).connect(src, dest)

        try:
            self._exprmapper.connect(srcexpr, destexpr, self, pseudocomp)
        except Exception:
            super(Assembly, self).disconnect(src, dest)
            raise

        if not srcexpr.refs_parent():
            if not destexpr.refs_parent():
                # if it's an internal connection, could change dependencies, so we have
                # to call config_changed to notify our driver
                self.config_changed(update_parent=False)

                destcompname, destcomp, destvarname = self._split_varpath(dest)

                outs = destcomp.invalidate_deps(varnames=set([destvarname]), force=True)
                if (outs is None) or outs:
                    self.child_invalidated(destcompname, outs, force=True)

    @rbac(('owner', 'user'))
    def disconnect(self, varpath, varpath2=None):
        """If varpath2 is supplied, remove the connection between varpath and
        varpath2. Otherwise, if varpath is the name of a trait, remove all
        connections to/from varpath in the current scope. If varpath is the
        name of a Component, remove all connections from all of its inputs
        and outputs.
        """
        if varpath2 is None and self.parent and '.' not in varpath:  # boundary var. make sure it's disconnected in parent
            self.parent.disconnect('.'.join([self.name, varpath]))

        to_remove, pcomps = self._exprmapper.disconnect(varpath, varpath2)

        graph = self._depgraph

        for u, v in graph.list_connections(show_external=True):
            if (u,v) in to_remove:
                super(Assembly, self).disconnect(u, v)
                
        for u, v in graph.list_autopassthroughs():
            if (u,v) in to_remove:
                super(Assembly, self).disconnect(u, v)
                
        for name in pcomps:
            try:
                self.remove_trait(name)
            except AttributeError:
                pass
            try:
                graph.remove(name)
            except nx.exception.NetworkXError:
                pass

    def config_changed(self, update_parent=True):
        """Call this whenever the configuration of this Component changes,
        for example, children are added or removed, connections are made
        or removed, etc.
        """
        super(Assembly, self).config_changed(update_parent)

        # drivers must tell workflows that config has changed because
        # dependencies may have changed
        for name in self.list_containers():
            cont = getattr(self, name)
            if isinstance(cont, Driver):
                cont.config_changed(update_parent=False)
            
        # Detect and save any loops in the graph.
        self._graph_loops = None

    def _set_failed(self, path, value, index=None, src=None, force=False):
        parts = path.split('.', 1)
        if len(parts) > 1:
            obj = getattr(self, parts[0])
            if isinstance(obj, PseudoComponent):
                obj.set(parts[1], value, index, src, force)

    def execute(self):
        """Runs driver and updates our boundary variables."""
        self.driver.run(ffd_order=self.ffd_order, case_id=self._case_id)

        valids = self._valid_dict

        # now update boundary outputs
        for expr in self._exprmapper.get_output_exprs():
            if valids[expr.text.split('[',1)[0]] is False:
                srctxt = self._depgraph.get_sources(expr.text)[0]
                srcexpr = self._exprmapper.get_expr(srctxt)
                expr.set(srcexpr.evaluate(), src=srctxt)
                # setattr(self, dest, srccomp.get_wrapped_attr(src))
            else:
                # PassthroughProperty always valid for some reason.
                try:
                    dst_type = self.get_trait(expr.text).trait_type
                except AttributeError:
                    pass
                else:
                    if isinstance(dst_type, PassthroughProperty):
                        srctxt = self._exprmapper.get_source(expr.text)
                        srcexpr = self._exprmapper.get_expr(srctxt)
                        expr.set(srcexpr.evaluate(), src=srctxt)

    def step(self):
        """Execute a single child component and return."""
        self.driver.step()

    def stop(self):
        """Stop the calculation."""
        self.driver.stop()

    def list_connections(self, show_passthrough=True, 
                               visible_only=False,
                               show_expressions=False):
        """Return a list of tuples of the form (outvarname, invarname).
        """
        #return self._exprmapper.list_connections(show_passthrough=show_passthrough,
        #                                             visible_only=visible_only)
        conns = self._depgraph.list_connections(show_passthrough=show_passthrough)
        if visible_only:
            newconns = []
            for u,v in conns:
                if u.startswith('_pseudo_'):
                    pcomp = getattr(self, u.split('.', 1)[0])
                    newconns.extend(pcomp.list_connections(is_hidden=True,
                                     show_expressions=show_expressions))
                elif v.startswith('_pseudo_'):
                    pcomp = getattr(self, v.split('.', 1)[0])
                    newconns.extend(pcomp.list_connections(is_hidden=True,
                                     show_expressions=show_expressions))
                else:
                    newconns.append((u,v))
            return newconns
        return conns


    @rbac(('owner', 'user'))
    def update_inputs(self, compname, inputs):
        """Transfer input data to input expressions on the specified component.
        The inputs iterator is assumed to contain strings that reference
        component variables relative to the component, e.g., 'abc[3][1]' rather
        than 'comp1.abc[3][1]'.
        """
        invalids = []
        conns = []
        graph = self._depgraph

        if compname is None:
            for inp in inputs:
                conns.extend(graph._var_connections(inp, 'in'))
        else:
            if inputs is None:
                conns = graph._comp_connections(compname, 'in')
            else:
                for inp in inputs:
                    conns.extend(graph._var_connections('.'.join([compname, inp]), 'in'))

        srcs = [u for u,v in conns]
        srcvars = [s.split('[',1)[0] for s in srcs]
        invalids = [srcs[i] for i,valid in enumerate(self.get_valid(srcvars)) if not valid]

        # if source vars are invalid, request an update
        if invalids:
            loops = graph.get_loops()
            
            for cname, vnames in partition_names_by_comp(invalids).items():
                if cname is None:
                    if self.parent:
                        self.parent.update_inputs(self.name, 
                                                  vnames)
                        
                # If our source component is in a loop with us, don't
                # run it. Otherwise you have infinite recursion. It is
                # the responsibility of the solver to properly execute
                # the comps in its loop.
                elif loops:
                    for loop in loops:
                        if compname in loop and cname in loop:
                            break
                    else:
                        getattr(self, cname).update_outputs(vnames)
                        
                else:
                    getattr(self, cname).update_outputs(vnames)

        # these connections all come from the depgraph, so they will only
        # contain simple expressions, i.e. only one variable ref (may be
        # an array index).
        for u,v in conns:
            try:
                srcexpr = self._exprmapper.get_expr(u)
                destexpr = self._exprmapper.get_expr(v)
                destexpr.set(srcexpr.evaluate(), src=srcexpr.text)
            except Exception as err:
                self.raise_exception("cannot set '%s' from '%s': %s" %
                                     (destexpr.text, srcexpr.text, str(err)), type(err))

    def update_outputs(self, outnames):
        """Execute any necessary internal or predecessor components in order
        to make the specified output variables valid.
        """
        for cname, vnames in partition_names_by_comp(outnames).items():
            if cname is None:  # boundary outputs
                self.update_inputs(None, vnames)
            else:
                getattr(self, cname).update_outputs(vnames)
                self.set_valid(vnames, True)

    def get_valid(self, names):
        """Returns a list of boolean values indicating whether the named
        variables are valid (True) or invalid (False). Entries in names may
        specify either direct traits of self or those of children.
        """

        vnames = [n.split('[',1)[0] for n in names]
        ret = [None] * len(vnames)
        posdict = dict([(name, i) for i, name in enumerate(vnames)])

        for compname, varnames in partition_names_by_comp(vnames).items():
            if compname is None:
                vals = super(Assembly, self).get_valid(varnames)
                for i, val in enumerate(vals):
                    ret[posdict[varnames[i]]] = val
            else:
                comp = getattr(self, compname)
                if isinstance(comp, Component) or isinstance(comp, PseudoComponent):
                    vals = comp.get_valid(varnames)
                else:
                    vals = [self._valid_dict['.'.join([compname, vname])] 
                                     for vname in varnames]
                for i, val in enumerate(vals):
                    full = '.'.join([compname, varnames[i]])
                    ret[posdict[full]] = val
        return ret

    def _input_updated(self, name, fullpath=None):
        if self._valid_dict[name.split('[',1)[0]]:  # if var is not already invalid
            outs = self.invalidate_deps(varnames=set([name]))
            if ((outs is None) or outs) and self.parent:
                self.parent.child_invalidated(self.name, outs)

    def child_invalidated(self, childname, outs=None, force=False):
        """Invalidate all variables that depend on the outputs provided
        by the child that has been invalidated.
        """
        bouts = self._depgraph.invalidate_deps(self, childname, outs, force)
        if bouts and self.parent:
            self.parent.child_invalidated(self.name, bouts, force)
        return bouts

    def invalidate_deps(self, varnames=None, force=False):
        """Mark all Variables invalid that depend on varnames.
        Returns a list of our newly invalidated boundary outputs.

        varnames: iter of str (optional)
            An iterator of names of destination variables.

        force: bool (optional)
            If True, force the invalidation to proceed beyond the
            boundary even if all outputs were already invalid.
        """
        valids = self._valid_dict
        conn_ins = set(self.list_inputs(connected=True))

        # If varnames is None, we're being called from a parent Assembly
        # as part of a higher level invalidation, so we only need to look
        # at our connected inputs
        if varnames is None:
            names = conn_ins
        else:
            names = varnames

        # We only care about inputs that are changing from valid to invalid.
        # If they're already invalid, then we've already done what we needed to do,
        # unless force is True, in which case we continue with the invalidation.
        if force:
            invalidated_ins = names
        else:
            invalidated_ins = []
            for name in names:
                short = name.split('[',1)[0]
                if ('.' not in name and valids[short]) or self.get_valid([short])[0]:
                    invalidated_ins.append(name)
            if not invalidated_ins:  # no newly invalidated inputs, so no outputs change status
                return []

        self._set_exec_state('INVALID')

        if varnames is None:
            self.set_valid(invalidated_ins, False)
        else:  # only invalidate *connected* inputs, because unconnected inputs
               # are always valid
            self.set_valid([n for n in invalidated_ins if n in conn_ins], False)

        if invalidated_ins:
            outs = self._depgraph.invalidate_deps(self, '', 
                                                  invalidated_ins, force)

        if outs:
            self.set_valid(outs, False)

        return outs

    def exec_counts(self, compnames):
        return [getattr(self, c).exec_count for c in compnames]

    def linearize(self, extra_in=None, extra_out=None):
        '''An assembly calculates its Jacobian by calling the calc_gradient
        method on its base driver. Note, derivatives are only calculated for
        floats and iterable items containing floats.'''
        
        # Only calc derivatives for inputs we need
        required_inputs = []
        if extra_in:
            for varpaths in extra_in:
                
                if not isinstance(varpaths, tuple):
                    varpaths = [varpaths]
                
                for varpath in varpaths:
                    compname, _, var = varpath.partition('.')
                    if compname == self.name:
                        required_inputs.append(var.split('[')[0])
        
        for src, target in self.parent.list_connections(): 
            compname, _, var = target.partition('.')
            if compname == self.name:
                required_inputs.append(var.split('[')[0])
                
        # Only calc derivatives for outputs we need
        required_outputs = []
        if extra_out:
            for varpaths in extra_out:
                
                if not isinstance(varpaths, tuple):
                    varpaths = [varpaths]
                
                for varpath in varpaths:
                    compname, _, var = varpath.partition('.')
                    if compname == self.name:
                        required_outputs.append(var.split('[')[0])
        
        for src, target in self.parent.list_connections(): 
            compname, _, var = src.partition('.')
            if compname == self.name:
                required_outputs.append(var.split('[')[0])
                
        # Sub-assembly sourced    
        input_keys = []
        output_keys = []
        
        # Parent-assembly sourced
        self.J_input_keys = []
        self.J_output_keys = []
        
        for src, target in self.list_connections():
            
            # Outputs
            if '.' in src and '.' not in target:
                
                if target not in required_outputs:
                    continue
                
                val = self.get(src)
                if isinstance(val, float) or hasattr(val, 'shape'):
                    output_keys.append(src)
                    self.J_output_keys.append(target)
                    
            # Inputs
            elif '.' in target and '.' not in src:
                
                if src not in required_inputs:
                    continue
                
                val = self.get(target)
                if isinstance(val, float) or hasattr(val, 'shape'):
                    input_keys.append(target)
                    self.J_input_keys.append(src)
                
        self.J = self.driver.calc_gradient(input_keys, output_keys)
        
    def provideJ(self):
        '''Provides the Jacobian calculated in linearize().'''
        
        return self.J_input_keys, self.J_output_keys, self.J
    
    def list_components(self):
        ''' List the components in the assembly.
        '''
        names = [name for name in self.list_containers()
                     if isinstance(self.get(name), Component)]
        return names

    def get_dataflow(self):
        ''' Get a dictionary of components and the connections between them
            that make up the data flow for the assembly;
            also includes parameter, constraint, and objective flows.
        '''
        components = []
        connections = []
        parameters = []
        constraints = []
        objectives = []

        # list of components (name & type) in the assembly
        g = self._depgraph
        names = [name for name in nx.algorithms.dag.topological_sort(g)
                               if not name.startswith('@')]

        # Bubble-up drivers ahead of their parameter targets.
        sorted_names = []
        for name in names:
            comp = self.get(name)
            if is_instance(comp, Driver) and hasattr(comp, '_delegates_'):
                driver_index = len(sorted_names)
                for dname, dclass in comp._delegates_.items():
                    inst = getattr(comp, dname)
                    if isinstance(inst, HasParameters):
                        refs = inst.get_referenced_compnames()
                        for ref in refs:
                            try:
                                target_index = sorted_names.index(ref)
                            except ValueError:
                                pass
                            else:
                                driver_index = min(driver_index, target_index)
                sorted_names.insert(driver_index, name)
            else:
                sorted_names.append(name)

        # Process names in new order.
        for name in sorted_names:
                comp = self.get(name)
                if is_instance(comp, Component):
                    inames = [cls.__name__
                              for cls in list(implementedBy(comp.__class__))]
                    components.append({
                        'name': comp.name,
                        'pathname': comp.get_pathname(),
                        'type': type(comp).__name__,
                        'valid': comp.is_valid(),
                        'interfaces': inames,
                        'python_id': id(comp)
                    })

                if is_instance(comp, Driver):
                    if hasattr(comp, '_delegates_'):
                        for name, dclass in comp._delegates_.items():
                            inst = getattr(comp, name)
                            if isinstance(inst, HasParameters):
                                for name, param in inst.get_parameters().items():
                                    if isinstance(param, ParameterGroup):
                                        for n, p in zip(name, tuple(param.targets)):
                                            parameters.append([comp.name + '.' + n, p])
                                    else:
                                        parameters.append([comp.name + '.' + name,
                                                           param.target])
                            elif isinstance(inst, (HasConstraints,
                                                   HasEqConstraints,
                                                   HasIneqConstraints)):
                                for path in inst.get_referenced_varpaths():
                                    name, dot, rest = path.partition('.')
                                    constraints.append([path,
                                                        comp.name + '.' + rest])
                            elif isinstance(inst, (HasObjective,
                                                   HasObjectives)):
                                for path in inst.get_referenced_varpaths():
                                    name, dot, rest = path.partition('.')
                                    objectives.append([path,
                                                       comp.name + '.' + name])

        # list of connections (convert tuples to lists)
        conntuples = self.list_connections(show_passthrough=True, 
                                           visible_only=True)
        for connection in conntuples:
            connections.append(list(connection))

        return {'components': components, 'connections': connections,
                'parameters': parameters, 'constraints': constraints,
                'objectives': objectives}

    def get_connections(self, src_name, dst_name):
        ''' Get a list of the outputs from the component *src_name* (sources),
            the inputs to the component *dst_name* (destinations) and the
            connections between them.
        '''
        conns = {}

        # outputs
        sources = []
        if src_name:
            src = self.get(src_name)
        else:
            src = self
        connected = src.list_outputs(connected=True)
        for name in src.list_outputs():
            var = src.get(name)
            vtype = type(var).__name__
            if not '.' in name:  # vartree vars handled separately
                units = ''
                meta = src.get_metadata(name)
                if meta and 'units' in meta:
                    units = meta['units']
                valid = src.get_valid([name])[0]
                sources.append({
                    'name': name,
                    'type': vtype,
                    'valid': valid,
                    'units': units,
                    'connected': (name in connected)
                })
            if isinstance(var, VariableTree):
                for var_name in var.list_vars():
                    vt_var = var.get(var_name)
                    vt_var_name = name + '.' + var_name
                    units = ''
                    meta = var.get_metadata(var_name)
                    if meta and 'units' in meta:
                        units = meta['units']
                    sources.append({
                        'name': vt_var_name,
                        'type':  type(vt_var).__name__,
                        'valid': valid,
                        'units': units,
                        'connected': (vt_var_name in connected)
                    })
            elif vtype == 'ndarray':
                for idx in range(0, len(var)):
                    vname = name + '[' + str(idx) + ']'
                    dtype = type(var[0]).__name__
                    units = ''
                    sources.append({
                        'name': vname,
                        'type': dtype,
                        'valid': valid,
                        'units': units,
                        'connected': (vname in connected)
                    })

        # connections to assembly can be passthrough (input to input)
        if src is self:
            connected = src.list_inputs(connected=True)
            for name in src.list_inputs():
                var = src.get(name)
                vtype = type(var).__name__
                if not '.' in name:  # vartree vars handled separately
                    units = ''
                    meta = src.get_metadata(name)
                    if meta and 'units' in meta:
                        units = meta['units']
                    sources.append({
                        'name': name,
                        'type': vtype,
                        'valid': src.get_valid([name])[0],
                        'units': units,
                        'connected': (name in connected)
                    })
                if isinstance(var, VariableTree):
                    for var_name in var.list_vars():
                        vt_var = var.get(var_name)
                        vt_var_name = name + '.' + var_name
                        units = ''
                        meta = var.get_metadata(var_name)
                        if meta and 'units' in meta:
                            units = meta['units']
                        sources.append({
                            'name': vt_var_name,
                            'type':  type(vt_var).__name__,
                            'valid': valid,
                            'units': units,
                            'connected': (vt_var_name in connected)
                        })
                elif vtype == 'ndarray':
                    for idx in range(0, len(var)):
                        vname = name + '[' + str(idx) + ']'
                        dtype = type(var[0]).__name__
                        units = ''
                        sources.append({
                            'name': vname,
                            'type': dtype,
                            'valid': valid,
                            'units': units,
                            'connected': (vname in connected)
                        })

        conns['sources'] = sorted(sources, key=lambda d: d['name'])

        # inputs
        dests = []
        if dst_name:
            dst = self.get(dst_name)
        else:
            dst = self
        connected = dst.list_inputs(connected=True)
        for name in dst.list_inputs():
            var = dst.get(name)
            vtype = type(var).__name__
            if not '.' in name:  # vartree vars handled separately
                units = ''
                meta = dst.get_metadata(name)
                if meta and 'units' in meta:
                    units = meta['units']
                dests.append({
                    'name': name,
                    'type': vtype,
                    'valid': dst.get_valid([name])[0],
                    'units': units,
                    'connected': (name in connected)
                })
            if isinstance(var, VariableTree):
                for var_name in var.list_vars():
                    vt_var = var.get(var_name)
                    vt_var_name = name + '.' + var_name
                    units = ''
                    meta = var.get_metadata(var_name)
                    if meta and 'units' in meta:
                        units = meta['units']
                    dests.append({
                        'name': vt_var_name,
                        'type': type(vt_var).__name__,
                        'valid': valid,
                        'units': units,
                        'connected': (vt_var_name in connected)
                    })
            elif vtype == 'ndarray':
                for idx in range(0, len(var)):
                    vname = name + '[' + str(idx) + ']'
                    dtype = type(var[0]).__name__
                    units = ''
                    dests.append({
                        'name': vname,
                        'type': dtype,
                        'valid': valid,
                        'units': units,
                        'connected': (vname in connected)
                    })

        # connections to assembly can be passthrough (output to output)
        if dst == self:
            connected = dst.list_outputs(connected=True)
            for name in dst.list_outputs():
                var = dst.get(name)
                vtype = type(var).__name__
                if not '.' in name:  # vartree vars handled separately
                    units = ''
                    meta = dst.get_metadata(name)
                    if meta and 'units' in meta:
                        units = meta['units']
                    dests.append({
                        'name': name,
                        'type': type(var).__name__,
                        'valid': dst.get_valid([name])[0],
                        'units': units,
                        'connected': (name in connected)
                    })
                if isinstance(var, VariableTree):
                    for var_name in var.list_vars():
                        vt_var = var.get(var_name)
                        vt_var_name = name + '.' + var_name
                        units = ''
                        meta = var.get_metadata(var_name)
                        if meta and 'units' in meta:
                            units = meta['units']
                        dests.append({
                            'name': vt_var_name,
                            'type': type(vt_var).__name__,
                            'valid': valid,
                            'units': units,
                            'connected': (vt_var_name in connected)
                        })
                elif vtype == 'ndarray':
                    for idx in range(0, len(var)):
                        vname = name + '[' + str(idx) + ']'
                        dtype = type(var[0]).__name__
                        units = ''
                        dests.append({
                            'name': vname,
                            'type': dtype,
                            'valid': valid,
                            'units': units,
                            'connected': (vname in connected)
                        })

        conns['destinations'] = sorted(dests, key=lambda d: d['name'])

        # connections
        connections = []
        conntuples = self.list_connections(show_passthrough=True, 
                                           visible_only=True)
        comp_names = self.list_components()
        for src_var, dst_var in conntuples:
            src_root = src_var.split('.')[0]
            dst_root = dst_var.split('.')[0]
            if (((src_name and src_root == src_name) or
                 (not src_name and src_root not in comp_names)) and
                ((dst_name and dst_root == dst_name) or
                 (not dst_name and dst_root not in comp_names))):
                connections.append([src_var, dst_var])
        conns['connections'] = connections

        return conns


def dump_iteration_tree(obj, full=False):
    """Returns a text version of the iteration tree
    of an OpenMDAO object or hierarchy.  The tree
    shows which are being iterated over by which
    drivers.
    
    If full is True, show pseudocomponents as well.
    """
    def _dump_iteration_tree(obj, f, tablevel):
        if is_instance(obj, Driver):
            f.write(' ' * tablevel)
            f.write(obj.get_pathname())
            f.write('\n')
            names = set(obj.workflow.get_names())
            for comp in obj.workflow:
                if not full and comp.name not in names:
                    continue
                if is_instance(comp, Driver) or is_instance(comp, Assembly):
                    _dump_iteration_tree(comp, f, tablevel + 3)
                else:
                    f.write(' ' * (tablevel + 3))
                    f.write(comp.get_pathname())
                    f.write('\n')
        elif is_instance(obj, Assembly):
            f.write(' ' * tablevel)
            f.write(obj.get_pathname())
            f.write('\n')
            _dump_iteration_tree(obj.driver, f, tablevel + 3)
    f = cStringIO.StringIO()
    _dump_iteration_tree(obj, f, 0)
    return f.getvalue()
