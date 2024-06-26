from functools import cached_property, lru_cache
from typing import Union, Sequence

import numpy as np

from .base import RandomProcess
from .cont_dist import Exponential
from .variables import VariablesFactory, Variable
from pyqumo.matrix import order_of, fix_infinitesimal, is_subinfinitesimal, \
    fix_stochastic, is_pmf, cbdiag, check_markovian_arrival, \
    fix_markovian_arrival, str_array
from ..errors import MatrixShapeError
from ..chains import ContinuousTimeMarkovChain, DiscreteTimeMarkovChain


class MarkovArrival(RandomProcess):
    """
    Markovian arrival process (MAP).
    """
    def __init__(
            self,
            d0: Union[np.ndarray, Sequence[Sequence[float]]],
            d1: Union[np.ndarray, Sequence[Sequence[float]]],
            safe: bool = False,
            tol: float = 1e-3,
            factory: VariablesFactory = None):
        """
        Create MAP process with the given D0 and D1 matrices.

        If `safe = False` is set (this is default), we validate matrices
        D0 and D1. These matrices must comply three requirements:

        1) Sum `D0 + D1` must be an infinitesimal matrix
        2) All elements of D1 must be non-negative
        3) All elements of D0 except the main diagonal must be non-negative

        To avoid problems with "1e-12 is not zero", constructor accepts
        parameters `tol` (tolerance). If matrices D0 and D1 fail the check,
        it tries to fix them using `fix_markovian_arrival()` call with this
        tolerance.

        Sometimes it is useful to avoid matrices validation (e.g., when MAP
        matrices are obtained as a result of another computation). To disable
        validation, set `safe = True`.

        Parameters
        ----------
        d0 : array_like
            Matrix D0, it must be subinfinitesimal
        d1 : array_like
            Matrix D1, all its elements must be non-negative
        safe : bool, optional
            Flag indicating whether not to validate or try to fix matrices D0
            and D1 if they don't satisfy requirements. By default, `False`.
        tol : float, optional
            If `safe = False` and matrices D0 and D1 violate requirements,
            try to fix errors those are less then `tol`. If `safe = True`
            this parameter is ignored. Default: 1e-3.
        """
        super().__init__(factory)
        need_copy = [False, False]
        matrices = [d0, d1]

        for i in range(2):
            if not isinstance(matrices[i], np.ndarray):
                matrices[i] = np.asarray(matrices[i])
            else:
                need_copy[i] = True

        if not safe and not check_markovian_arrival(matrices):
            matrices, _ = fix_markovian_arrival(matrices, tol=tol)
            # Since matrices are re-built, no need to copy them:
            for i in range(2):
                need_copy[i] = False

        # Copy matrices if needed:
        for i in range(2):
            if need_copy[i]:
                matrices[i] = matrices[i].copy()

        # Extract matrices:
        self._matrices = tuple(matrices)
        self._generator = sum(self._matrices)
        self._order = order_of(self._matrices[0])
        self._inv_d0 = np.linalg.inv(self._matrices[0])

        # Since we will use Mmap for data generation, we need to generate
        # special matrices for transitions probabilities and rates.

        # Store rates:
        self._rates = -self._matrices[0].diagonal()

        # Build embedded DTMC and CTMC:
        self._ctmc = ContinuousTimeMarkovChain(
            self._generator,
            safe=True
        )
        self._dtmc = DiscreteTimeMarkovChain(
            -self._inv_d0.dot(self.d1),
            safe=True
        )
        self._states = [
            Exponential(rate, factory=factory)
            for rate in self._rates
        ]

    @staticmethod
    def erlang(shape: int, rate: float) -> 'MarkovArrival':
        """
        MAP representation of Erlang process with the given shape and rate.

        Parameters
        ----------
        shape : int
            Number of phases
        rate : float
            Rate at each phase
        """
        d0 = cbdiag(shape, [
            (0, np.asarray([[-rate]])),
            (1, np.asarray([[rate]]))
        ])
        d1 = np.zeros((shape, shape))
        d1[shape-1, 0] = rate
        return MarkovArrival(d0, d1)

    @staticmethod
    def poisson(rate: float) -> 'MarkovArrival':
        """
        MAP representation of a Poisson process with the given rate.

        Parameters
        ----------
        rate : float
            Exponential distribution rate
        """
        return MarkovArrival([[-rate]], [[rate]])

    @staticmethod
    def phase_type(
            s: Sequence[Sequence[float]],
            p: Sequence[float]) -> 'MarkovArrival':
        """
        MAP representation of a PH distribution.

        Parameters
        ----------
        s : square matrix
            PH distribution generator (order NxN)
        p : vector
            PH distribution initial probabilities (order N)
        """
        if not isinstance(s, np.ndarray):
            s = np.asarray(s)
        if not isinstance(p, np.ndarray):
            p = np.asarray(p)

        # Validate:
        order = order_of(s)
        if order != order_of(p):
            raise MatrixShapeError(f'({order},)', p.shape, 'PMF')
        if not is_subinfinitesimal(s):
            s = fix_infinitesimal(s, sub=True)
        if not is_pmf(p):
            p = fix_stochastic(p)

        # Build:
        d0 = s
        d1 = (-s.dot(np.ones((order, 1)))).dot(p.reshape((1, order)))
        return MarkovArrival(d0, d1)

    def copy(self) -> 'MarkovArrival':
        """
        Build a new MAP with the same matrices D0 and D1 without validation.
        """
        return MarkovArrival(self.d0, self.d1, safe=True)

    @property
    def d0(self) -> np.ndarray:
        """
        Get matrix D0.
        """
        return self._matrices[0]

    @property
    def d1(self) -> np.ndarray:
        """
        Get matrix D1.
        """
        return self._matrices[1]

    def d(self, n: int) -> np.ndarray:
        """
        Get matrix Dn for n = 0 or n = 1.

        Parameters
        ----------
        n : int (0 or 1)
        """
        return self._matrices[n]

    @cached_property
    def generator(self) -> np.ndarray:
        return self._generator

    @cached_property
    def order(self) -> int:
        return self._order

    def scale(self, k) -> 'MarkovArrival':
        """
        Get a new MAP with mean = (this MAP) x K.
        """
        return MarkovArrival(self.d0 / k, self.d1 / k)

    @lru_cache
    def d0n(self, k: int) -> np.ndarray:
        """
        Returns :math:`(-D0)^{k}`.
        """
        if k == -1:
            return -self._inv_d0
        if k == 0:
            return np.eye(self.order)
        if k > 0:
            return self.d0n(k - 1).dot(-self.d0)
        # If we are here, degree <= -2
        return self.d0n(k + 1).dot(self.d0n(-1))

    @lru_cache
    def _moment(self, n: int) -> float:
        pi = self.dtmc.steady_pmf
        x = np.math.factorial(n)*pi.dot(self.d0n(-n)).dot(np.ones(self.order))
        return x.item()

    @lru_cache
    def _lag(self, n: int) -> float:
        #
        # Computing lag-k as:
        #
        #   r^2 * pi * (-D0)^(-1) * P^k * (-D0)^(-1) * 1s - 1
        #   -------------------------------------------------- ,
        #   2 * r^2 * pi * (-D0)^(-2) * 1s - 1
        #
        # where r - rate (\lambda), pi - stationary distribution of the
        # embedded DTMC, 1s - vector of ones of MAP order
        #
        dtmc_matrix_k = self._pow_dtmc_matrix(n)
        pi = self.dtmc.steady_pmf
        rate2 = pow(self.rate, 2.0)
        e = np.ones(self.order)
        d0ni = self.d0n(-1)
        d0ni2 = self.d0n(-2)

        num = rate2 * pi.dot(d0ni).dot(dtmc_matrix_k).dot(d0ni).dot(e) - 1
        den = (2 * rate2 * pi.dot(d0ni2).dot(e) - 1)
        return num / den

    @property
    def ctmc(self) -> ContinuousTimeMarkovChain:
        """
        Get background continuous-time Markov chain (CTMC).
        """
        return self._ctmc

    @property
    def dtmc(self) -> DiscreteTimeMarkovChain:
        """
        Get embedded (at arrivals) discrete-time Markov chain (DTMC).
        """
        return self._dtmc

    @cached_property
    def rnd(self) -> Variable:
        # We need to store cumulative transition probabilities P.
        #    We store them in a stochastic matrix of shape N x (K*N):
        #      P[I, J] is a probability, that:
        #      - new state will be J (mod N), and
        #      - if J < N, then no packet is generated, or
        #      - if J >= N, then packet of type J // N is generated.
        vars = [state.rnd for state in self._states]
        trans_pmf = np.hstack((
            self._matrices[0] + np.diag(self._rates),
            self._matrices[1]
        )) / self._rates[:, None]
        return self.factory.semi_markov_arrival(
            vars, self.dtmc.steady_pmf, trans_pmf
        )

    def compose(self, other: 'MarkovArrival') -> 'MarkovArrival':
        # TODO:  write unit tests
        if not isinstance(other, MarkovArrival):
            raise TypeError(f'expected MarkovArrivalProcess, '
                            f'{type(other)} found')
        self_eye = np.eye(self.order)
        other_eye = np.eye(other.order)
        d0_out = np.kron(self.d0, other_eye) + np.kron(other.d0, self_eye)
        d1_out = np.kron(self.d1, other_eye) + np.kron(other.d1, self_eye)
        return MarkovArrival(d0_out, d1_out)

    @lru_cache
    def _pow_dtmc_matrix(self, k):
        if k == 0:
            return np.eye(self.order)
        elif k > 0:
            return self._pow_dtmc_matrix(k - 1).dot(self.dtmc.matrix)
        raise ValueError(f"k={k} must be non-negative")

    def __repr__(self):
        return f'(MAP: d0={str_array(self.d0)}, d1={str_array(self.d1)})'
