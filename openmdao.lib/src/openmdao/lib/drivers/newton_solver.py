"""
Newton solver based around Scipy's fsolve method. More methods can be added.
"""

# pylint: disable-msg=C0103

#public symbols
__all__ = ['NewtonSolver']

from scipy.optimize import fsolve

# this little funct replaces a dependency on scipy
import numpy
npnorm = numpy.linalg.norm
def norm(a, ord=None):
    return npnorm(numpy.asarray_chkfinite(a), ord=ord)

# pylint: disable-msg=E0611, F0401
from openmdao.main.api import Driver, CyclicWorkflow
from openmdao.main.datatypes.api import Float, Int, Enum
from openmdao.main.hasparameters import HasParameters
from openmdao.main.hasconstraints import HasEqConstraints
from openmdao.main.interfaces import IHasParameters, IHasEqConstraints, \
                                     ISolver, implements
from openmdao.util.decorators import add_delegate


@add_delegate(HasParameters, HasEqConstraints)
class NewtonSolver(Driver):
    ''' Wrapper for some Newton style solvers. Currently supports
    fsolve from scipy.optimize.
    '''

    implements(IHasParameters, IHasEqConstraints, ISolver)

    # pylint: disable-msg=E1101
    tolerance = Float(1.0e-8, iotype='in', desc='Global convergence tolerance')

    max_iteration = Int(50, iotype='in', desc='Maximum number of iterations')

    method = Enum('fsolve', ['fsolve'], iotype='in',
                  desc='Solution method (currently only fsolve from scipy optimize)')

    def __init__(self):

        super(NewtonSolver, self).__init__()
        self.workflow = CyclicWorkflow()

    def check_config(self, strict=False):
        """ This solver requires a CyclicWorkflow. """

        super(NewtonSolver, self).check_config(strict=strict)

        if not isinstance(self.workflow, CyclicWorkflow):
            msg = "The NewtonSolver requires a CyclicWorkflow workflow."
            self.raise_exception(msg, RuntimeError)

    def execute(self):
        """ Pick our solver method. """

        # perform an initial run
        self.pre_iteration()
        self.run_iteration()
        self.post_iteration()

        # One choice
        #self.execute_fsolve()
        self.execute_coupled()

    def execute_coupled(self):
        """ New experimental method based on John's Newton solver.
        """
        system = self.workflow._system
        options = self.gradient_options

        converged = False
        if npnorm(system.vec['f'].array) < self.tolerance:
            converged = True

        itercount = 0
        alpha = 1.0
        while not converged:

            system.calc_newton_direction(options=options)

            system.vec['u'].array[:]  += alpha*system.vec['df'].array[:]

            self.pre_iteration()
            self.run_iteration()
            self.post_iteration()

            norm = npnorm(system.vec['f'].array)
            print "Norm:", norm
            itercount += 1
            #alpha = alpha*0.5

            if norm < self.tolerance or itercount == self.max_iteration:
                break

    def execute_fsolve(self):
        """ Solver execution loop: scipy.fsolve. """

        x0 = self.workflow.get_independents()
        fsolve(self._solve_callback, x0, fprime=self._jacobian_callback,
               maxfev=self.max_iteration, xtol=self.tolerance)

    def _solve_callback(self, vals):
        """Function hook for evaluating our equations."""

        self.workflow.set_independents(vals)

        # run the model
        self.pre_iteration()
        self.run_iteration()
        self.post_iteration()

        return self.workflow.get_dependents()

    def _jacobian_callback(self, vals):
        """This function is passed to the internal solver to return the
        jacobian of the dependents with respect to the independents."""
        return self.workflow.calc_gradient()
