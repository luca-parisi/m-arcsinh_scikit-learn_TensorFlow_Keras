"""
Python implementation of the fast ICA algorithms.

References: 

1. Tables 8.3 and 8.4 page 196 in the book:
Independent Component Analysis, by  Hyvarinen et al.

2. When using the 'm_arcsinh' Kernel (m-ar-K) as G function:
M-ar-K-Fast Independent Component Analysis, 
by Parisi, L. (2021). https://arxiv.org/abs/2108.07908.

"""

# Authors: Pierre Lafaye de Micheaux, Stefan van der Walt, Gael Varoquaux,
#          Bertrand Thirion, Alexandre Gramfort, Denis A. Engemann, and 
#          Luca Parisi <luca.parisi@ieee.org> (only for 'm_arcsinh' 
#          used as G function and its derivative)
# License: BSD 3 clause

import warnings

import numpy as np
from scipy import linalg

from ..base import BaseEstimator, TransformerMixin
from ..exceptions import ConvergenceWarning

from ..utils import check_array, as_float_array, check_random_state
from ..utils.validation import check_is_fitted
from ..utils.validation import FLOAT_DTYPES
from ..utils.validation import _deprecate_positional_args

__all__ = ['fastica', 'FastICA']


def _gs_decorrelation(w, W, j):
    """
    Orthonormalize w wrt the first j rows of W

    Parameters
    ----------
    w : ndarray of shape(n)
        Array to be orthogonalized

    W : ndarray of shape(p, n)
        Null space definition

    j : int < p
        The no of (from the first) rows of Null space W wrt which w is
        orthogonalized.

    Notes
    -----
    Assumes that W is orthogonal
    w changed in place
    """
    w -= np.dot(np.dot(w, W[:j].T), W[:j])
    return w


def _sym_decorrelation(W):
    """ Symmetric decorrelation
    i.e. W <- (W * W.T) ^{-1/2} * W
    """
    s, u = linalg.eigh(np.dot(W, W.T))
    # u (resp. s) contains the eigenvectors (resp. square roots of
    # the eigenvalues) of W * W.T
    return np.dot(np.dot(u * (1. / np.sqrt(s)), u.T), W)


def _ica_def(X, tol, g, fun_args, max_iter, w_init):
    """Deflationary FastICA using fun approx to neg-entropy function

    Used internally by FastICA.
    """

    n_components = w_init.shape[0]
    W = np.zeros((n_components, n_components), dtype=X.dtype)
    n_iter = []

    # j is the index of the extracted component
    for j in range(n_components):
        w = w_init[j, :].copy()
        w /= np.sqrt((w ** 2).sum())

        for i in range(max_iter):
            gwtx, g_wtx = g(np.dot(w.T, X), fun_args)

            w1 = (X * gwtx).mean(axis=1) - g_wtx.mean() * w

            _gs_decorrelation(w1, W, j)

            w1 /= np.sqrt((w1 ** 2).sum())

            lim = np.abs(np.abs((w1 * w).sum()) - 1)
            w = w1
            if lim < tol:
                break

        n_iter.append(i + 1)
        W[j, :] = w

    return W, max(n_iter)


def _ica_par(X, tol, g, fun_args, max_iter, w_init):
    """Parallel FastICA.

    Used internally by FastICA --main loop

    """
    W = _sym_decorrelation(w_init)
    del w_init
    p_ = float(X.shape[1])
    for ii in range(max_iter):
        gwtx, g_wtx = g(np.dot(W, X), fun_args)
        W1 = _sym_decorrelation(np.dot(gwtx, X.T) / p_
                                - g_wtx[:, np.newaxis] * W)
        del gwtx, g_wtx
        # builtin max, abs are faster than numpy counter parts.
        lim = max(abs(abs(np.diag(np.dot(W1, W.T))) - 1))
        W = W1
        if lim < tol:
            break
    else:
        warnings.warn('FastICA did not converge. Consider increasing '
                      'tolerance or the maximum number of iterations.',
                      ConvergenceWarning)

    return W, ii + 1


# Some standard non-linear functions.
# XXX: these should be optimized, as they can be a bottleneck.
def _logcosh(x, fun_args=None):
    alpha = fun_args.get('alpha', 1.0)  # comment it out?

    x *= alpha
    gx = np.tanh(x, x)  # apply the tanh inplace
    g_x = np.empty(x.shape[0])
    # XXX compute in chunks to avoid extra allocation
    for i, gx_i in enumerate(gx):  # please don't vectorize.
        g_x[i] = (alpha * (1 - gx_i ** 2)).mean()
    return gx, g_x


def _exp(x, fun_args):
    exp = np.exp(-(x ** 2) / 2)
    gx = x * exp
    g_x = (1 - x ** 2) * exp
    return gx, g_x.mean(axis=-1)


def _cube(x, fun_args):
    return x ** 3, (3 * x ** 2).mean(axis=-1)


def _m_arcsinh(x, fun_args):
    """Compute the modified arcsinh (m-arcsinh) kernel function 
	  and its derivative in place.

    It exploits the fact that the derivative is a simple function of the 
    output value from the m-arcsinh kernel.

    Further details on this function are available at:  
    https://arxiv.org/abs/2009.07530 and https://arxiv.org/abs/2108.07908
    (Parisi, L., 2020, 2021; 
    License: http://creativecommons.org/licenses/by/4.0/). 
    If you are using this function, please cite these papers as follows: 
    arXiv:2009.07530 [cs.LG] and arXiv:2108.07908 [cs.LG].

    Parameters
    ----------
    X : {array-like, sparse matrix}, shape (n_samples, n_features)
        The input data.
    Returns
    ----------
    A tuple containing the value of the function, and that of its derivative.
    
    """
    return (1/3*np.arcsinh(x))*(1/4*np.sqrt(np.abs(x))), 
           (np.sqrt(np.abs(x))/(12*np.sqrt(x**2+1)) + 
           (x*np.arcsinh(x))/(24*np.abs(x)**(3/2))).mean(axis=-1)


@_deprecate_positional_args
def fastica(X, n_components=None, *, algorithm="parallel", whiten=True,
            fun="logcosh", fun_args=None, max_iter=200, tol=1e-04, w_init=None,
            random_state=None, return_X_mean=False, compute_sources=True,
            return_n_iter=False):
    """Perform Fast Independent Component Analysis.

    Read more in the :ref:`User Guide <ICA>`.

    Parameters
    ----------
    X : array-like, shape (n_samples, n_features)
        Training vector, where n_samples is the number of samples and
        n_features is the number of features.

    n_components : int, optional
        Number of components to extract. If None no dimension reduction
        is performed.

    algorithm : {'parallel', 'deflation'}, optional
        Apply a parallel or deflational FASTICA algorithm.

    whiten : boolean, optional
        If True perform an initial whitening of the data.
        If False, the data is assumed to have already been
        preprocessed: it should be centered, normed and white.
        Otherwise you will get incorrect results.
        In this case the parameter n_components will be ignored.

    fun : string or function, optional. Default: 'logcosh'
        The functional form of the G function used in the
        approximation to neg-entropy. Could be either 'logcosh', 'exp',
        or 'cube'.
        You can also provide your own function. It should return a tuple
        containing the value of the function, and of its derivative, in the
        point. The derivative should be averaged along its last dimension.
        Example:

        def my_g(x):
            return x ** 3, np.mean(3 * x ** 2, axis=-1)

    fun_args : dictionary, optional
        Arguments to send to the functional form.
        If empty or None and if fun='logcosh', fun_args will take value
        {'alpha' : 1.0}

    max_iter : int, optional
        Maximum number of iterations to perform.

    tol : float, optional
        A positive scalar giving the tolerance at which the
        un-mixing matrix is considered to have converged.

    w_init : (n_components, n_components) array, optional
        Initial un-mixing array of dimension (n.comp,n.comp).
        If None (default) then an array of normal r.v.'s is used.

    random_state : int, RandomState instance, default=None
        Used to initialize ``w_init`` when not specified, with a
        normal distribution. Pass an int, for reproducible results
        across multiple function calls.
        See :term:`Glossary <random_state>`.

    return_X_mean : bool, optional
        If True, X_mean is returned too.

    compute_sources : bool, optional
        If False, sources are not computed, but only the rotation matrix.
        This can save memory when working with big data. Defaults to True.

    return_n_iter : bool, optional
        Whether or not to return the number of iterations.

    Returns
    -------
    K : array, shape (n_components, n_features) | None.
        If whiten is 'True', K is the pre-whitening matrix that projects data
        onto the first n_components principal components. If whiten is 'False',
        K is 'None'.

    W : array, shape (n_components, n_components)
        The square matrix that unmixes the data after whitening.
        The mixing matrix is the pseudo-inverse of matrix ``W K``
        if K is not None, else it is the inverse of W.

    S : array, shape (n_samples, n_components) | None
        Estimated source matrix

    X_mean : array, shape (n_features, )
        The mean over features. Returned only if return_X_mean is True.

    n_iter : int
        If the algorithm is "deflation", n_iter is the
        maximum number of iterations run across all components. Else
        they are just the number of iterations taken to converge. This is
        returned only when return_n_iter is set to `True`.

    Notes
    -----

    The data matrix X is considered to be a linear combination of
    non-Gaussian (independent) components i.e. X = AS where columns of S
    contain the independent components and A is a linear mixing
    matrix. In short ICA attempts to `un-mix' the data by estimating an
    un-mixing matrix W where ``S = W K X.``
    While FastICA was proposed to estimate as many sources
    as features, it is possible to estimate less by setting
    n_components < n_features. It this case K is not a square matrix
    and the estimated A is the pseudo-inverse of ``W K``.

    This implementation was originally made for data of shape
    [n_features, n_samples]. Now the input is transposed
    before the algorithm is applied. This makes it slightly
    faster for Fortran-ordered input.

    Implemented using FastICA:
    *A. Hyvarinen and E. Oja, Independent Component Analysis:
    Algorithms and Applications, Neural Networks, 13(4-5), 2000,
    pp. 411-430*

    """

    est = FastICA(n_components=n_components, algorithm=algorithm,
                  whiten=whiten, fun=fun, fun_args=fun_args,
                  max_iter=max_iter, tol=tol, w_init=w_init,
                  random_state=random_state)
    sources = est._fit(X, compute_sources=compute_sources)

    if whiten:
        if return_X_mean:
            if return_n_iter:
                return (est.whitening_, est._unmixing, sources, est.mean_,
                        est.n_iter_)
            else:
                return est.whitening_, est._unmixing, sources, est.mean_
        else:
            if return_n_iter:
                return est.whitening_, est._unmixing, sources, est.n_iter_
            else:
                return est.whitening_, est._unmixing, sources

    else:
        if return_X_mean:
            if return_n_iter:
                return None, est._unmixing, sources, None, est.n_iter_
            else:
                return None, est._unmixing, sources, None
        else:
            if return_n_iter:
                return None, est._unmixing, sources, est.n_iter_
            else:
                return None, est._unmixing, sources


class FastICA(TransformerMixin, BaseEstimator):
    """FastICA: a fast algorithm for Independent Component Analysis.

    Read more in the :ref:`User Guide <ICA>`.

    Parameters
    ----------
    n_components : int, optional
        Number of components to use. If none is passed, all are used.

    algorithm : {'parallel', 'deflation'}
        Apply parallel or deflational algorithm for FastICA.

    whiten : boolean, optional
        If whiten is false, the data is already considered to be
        whitened, and no whitening is performed.

    fun : string or function, optional. Default: 'logcosh'
        The functional form of the G function used in the
        approximation to neg-entropy. Could be either 'logcosh', 'exp',
        or 'cube'.
        You can also provide your own function. It should return a tuple
        containing the value of the function, and of its derivative, in the
        point. Example:

    fun_args : dictionary, optional
        Arguments to send to the functional form.
        If empty and if fun='logcosh', fun_args will take value
        {'alpha' : 1.0}.

    max_iter : int, optional
        Maximum number of iterations during fit.

    tol : float, optional
        Tolerance on update at each iteration.

    w_init : None of an (n_components, n_components) ndarray
        The mixing matrix to be used to initialize the algorithm.

    random_state : int, RandomState instance, default=None
        Used to initialize ``w_init`` when not specified, with a
        normal distribution. Pass an int, for reproducible results
        across multiple function calls.
        See :term:`Glossary <random_state>`.

    Attributes
    ----------
    components_ : 2D array, shape (n_components, n_features)
        The linear operator to apply to the data to get the independent
        sources. This is equal to the unmixing matrix when ``whiten`` is
        False, and equal to ``np.dot(unmixing_matrix, self.whitening_)`` when
        ``whiten`` is True.

    mixing_ : array, shape (n_features, n_components)
        The pseudo-inverse of ``components_``. It is the linear operator
        that maps independent sources to the data.

    mean_ : array, shape(n_features)
        The mean over features. Only set if `self.whiten` is True.

    n_iter_ : int
        If the algorithm is "deflation", n_iter is the
        maximum number of iterations run across all components. Else
        they are just the number of iterations taken to converge.

    whitening_ : array, shape (n_components, n_features)
        Only set if whiten is 'True'. This is the pre-whitening matrix
        that projects data onto the first `n_components` principal components.

    Examples:
    For FastICA:
    --------
    >>> from sklearn.datasets import load_digits
    >>> from sklearn.decomposition import FastICA
    >>> X, _ = load_digits(return_X_y=True)
    >>> transformer = FastICA(n_components=7,
    ...         random_state=0)
    >>> X_transformed = transformer.fit_transform(X)
    >>> X_transformed.shape
    (1797, 7)
    
    For m-ar-K-FastICA:
    --------
    >>> from sklearn.datasets import load_digits
    >>> from sklearn.decomposition import FastICA
    >>> X, _ = load_digits(return_X_y=True)
    >>> transformer = FastICA(n_components=7,
    ...         random_state=0, fun='m_arcsinh')
    >>> X_transformed = transformer.fit_transform(X)
    >>> X_transformed.shape
    (1797, 7) 

    Notes
    -----
    For FastICA:
    Implementation based on
    *A. Hyvarinen and E. Oja, Independent Component Analysis:
    Algorithms and Applications, Neural Networks, 13(4-5), 2000,
    pp. 411-430*
    
    For m-ar-K-FastICA:
    Implementation from
    *L. Parisi, M-ar-K-Fast Independent Component Analysis, 
    arXiv, 2108.07908, 2021,
    pp. 1-17*

    """
    @_deprecate_positional_args
    def __init__(self, n_components=None, *, algorithm='parallel', whiten=True,
                 fun='logcosh', fun_args=None, max_iter=200, tol=1e-4,
                 w_init=None, random_state=None):
        super().__init__()
        if max_iter < 1:
            raise ValueError("max_iter should be greater than 1, got "
                             "(max_iter={})".format(max_iter))
        self.n_components = n_components
        self.algorithm = algorithm
        self.whiten = whiten
        self.fun = fun
        self.fun_args = fun_args
        self.max_iter = max_iter
        self.tol = tol
        self.w_init = w_init
        self.random_state = random_state

    def _fit(self, X, compute_sources=False):
        """Fit the model

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data, where n_samples is the number of samples
            and n_features is the number of features.

        compute_sources : bool
            If False, sources are not computes but only the rotation matrix.
            This can save memory when working with big data. Defaults to False.

        Returns
        -------
            X_new : array-like, shape (n_samples, n_components)
        """

        X = self._validate_data(X, copy=self.whiten, dtype=FLOAT_DTYPES,
                                ensure_min_samples=2).T
        fun_args = {} if self.fun_args is None else self.fun_args
        random_state = check_random_state(self.random_state)

        alpha = fun_args.get('alpha', 1.0)
        if not 1 <= alpha <= 2:
            raise ValueError('alpha must be in [1,2]')

        if self.fun == 'logcosh':
            g = _logcosh
        elif self.fun == 'exp':
            g = _exp
        elif self.fun == 'cube':
            g = _cube
        elif self.fun == 'm_arcsinh':
            g = _m_arcsinh
        elif callable(self.fun):
            def g(x, fun_args):
                return self.fun(x, **fun_args)
        else:
            exc = ValueError if isinstance(self.fun, str) else TypeError
            raise exc(
                "Unknown function %r;"
                " should be one of 'logcosh', 'exp', 'cube', 'm_arcsinh', or callable"
                % self.fun
            )

        n_samples, n_features = X.shape

        n_components = self.n_components
        if not self.whiten and n_components is not None:
            n_components = None
            warnings.warn('Ignoring n_components with whiten=False.')

        if n_components is None:
            n_components = min(n_samples, n_features)
        if (n_components > min(n_samples, n_features)):
            n_components = min(n_samples, n_features)
            warnings.warn(
                'n_components is too large: it will be set to %s'
                % n_components
            )

        if self.whiten:
            # Centering the columns (ie the variables)
            X_mean = X.mean(axis=-1)
            X -= X_mean[:, np.newaxis]

            # Whitening and preprocessing by PCA
            u, d, _ = linalg.svd(X, full_matrices=False)

            del _
            K = (u / d).T[:n_components]  # see (6.33) p.140
            del u, d
            X1 = np.dot(K, X)
            # see (13.6) p.267 Here X1 is white and data
            # in X has been projected onto a subspace by PCA
            X1 *= np.sqrt(n_features)
        else:
            # X must be casted to floats to avoid typing issues with numpy
            # 2.0 and the line below
            X1 = as_float_array(X, copy=False)  # copy has been taken care of

        w_init = self.w_init
        if w_init is None:
            w_init = np.asarray(random_state.normal(
                size=(n_components, n_components)), dtype=X1.dtype)

        else:
            w_init = np.asarray(w_init)
            if w_init.shape != (n_components, n_components):
                raise ValueError(
                    'w_init has invalid shape -- should be %(shape)s'
                    % {'shape': (n_components, n_components)})

        kwargs = {'tol': self.tol,
                  'g': g,
                  'fun_args': fun_args,
                  'max_iter': self.max_iter,
                  'w_init': w_init}

        if self.algorithm == 'parallel':
            W, n_iter = _ica_par(X1, **kwargs)
        elif self.algorithm == 'deflation':
            W, n_iter = _ica_def(X1, **kwargs)
        else:
            raise ValueError('Invalid algorithm: must be either `parallel` or'
                             ' `deflation`.')
        del X1

        if compute_sources:
            if self.whiten:
                S = np.dot(np.dot(W, K), X).T
            else:
                S = np.dot(W, X).T
        else:
            S = None

        self.n_iter_ = n_iter

        if self.whiten:
            self.components_ = np.dot(W, K)
            self.mean_ = X_mean
            self.whitening_ = K
        else:
            self.components_ = W

        self.mixing_ = linalg.pinv(self.components_)
        self._unmixing = W

        if compute_sources:
            self.__sources = S

        return S

    def fit_transform(self, X, y=None):
        """Fit the model and recover the sources from X.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data, where n_samples is the number of samples
            and n_features is the number of features.

        y : Ignored

        Returns
        -------
        X_new : array-like, shape (n_samples, n_components)
        """
        return self._fit(X, compute_sources=True)

    def fit(self, X, y=None):
        """Fit the model to X.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training data, where n_samples is the number of samples
            and n_features is the number of features.

        y : Ignored

        Returns
        -------
        self
        """
        self._fit(X, compute_sources=False)
        return self

    def transform(self, X, copy=True):
        """Recover the sources from X (apply the unmixing matrix).

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Data to transform, where n_samples is the number of samples
            and n_features is the number of features.

        copy : bool (optional)
            If False, data passed to fit are overwritten. Defaults to True.

        Returns
        -------
        X_new : array-like, shape (n_samples, n_components)
        """
        check_is_fitted(self)

        X = check_array(X, copy=copy, dtype=FLOAT_DTYPES)
        if self.whiten:
            X -= self.mean_

        return np.dot(X, self.components_.T)

    def inverse_transform(self, X, copy=True):
        """Transform the sources back to the mixed data (apply mixing matrix).

        Parameters
        ----------
        X : array-like, shape (n_samples, n_components)
            Sources, where n_samples is the number of samples
            and n_components is the number of components.
        copy : bool (optional)
            If False, data passed to fit are overwritten. Defaults to True.

        Returns
        -------
        X_new : array-like, shape (n_samples, n_features)
        """
        check_is_fitted(self)

        X = check_array(X, copy=(copy and self.whiten), dtype=FLOAT_DTYPES)
        X = np.dot(X, self.mixing_.T)
        if self.whiten:
            X += self.mean_

        return X
