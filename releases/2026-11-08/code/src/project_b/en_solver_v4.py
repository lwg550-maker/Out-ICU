"""Same ridge/EN objective, safeguarded smoothed Newton; M0 stays on V3.

EN changes only: bounded L1 smoothing, equilibrated solves, Armijo backtracking,
and an exact-target proximal-gradient stopping rule. No coefficient truncation,
variable deletion, penalty modification, or relaxed acceptance thresholds.
"""
from __future__ import annotations

import warnings

import numpy as np
from scipy.linalg import solve
from scipy.special import expit
from lifelines.exceptions import ConvergenceWarning, ConvergenceError

from .shared_core_solver_v3 import SharedRidgeSemiParametric, SharedRidgeCoxPHFitter

SHARPNESS_MAX = 1e10
INTERNAL_PROX_TOL = 1e-7
ARMIJO = 1e-4


class SafeguardedSemiParametric(SharedRidgeSemiParametric):
    def _newton_raphson_for_efron_model(
            self, X, T, E, weights, entries, initial_point=None,
            show_progress=True, step_size=.25, precision=1e-10,
            r_precision=1e-9, max_steps=1000):
        if not (np.any(np.asarray(self.penalizer)) and self.l1_ratio > 0):
            return super()._newton_raphson_for_efron_model(
                X, T, E, weights, entries, initial_point, show_progress,
                step_size, precision, r_precision, max_steps)
        if self.strata is not None or not np.all(np.asarray(weights) == 1):
            raise ValueError('Frozen unstratified unit-weight EN scope only')
        n, d = X.shape
        events = float(E.sum())
        sd = np.asarray(self._norm_std)
        self._core_q = events/n*self.core_lambda/sd**2*(np.arange(d) < self.core_n)
        q1 = n*np.asarray(self.penalizer)*self.l1_ratio
        q2 = n*(np.asarray(self.penalizer)*(1-self.l1_ratio) + self._core_q)
        get_gradients = self._choose_gradient_calculator(T, X, entries)
        beta = np.zeros(d) if initial_point is None else np.array(initial_point, float, copy=True)
        self.numerical_trace_ = []
        self.numerical_success_ = False

        def penalty(theta, a):
            v = np.abs(theta)
            soft = v + 2/a*np.log1p(np.exp(-a*v))
            return float(q1@soft + .5*q2@(theta**2))

        def exact_prox(raw_score, theta):
            # Convert internal theta back to training-ddof0 standardized beta.
            b = theta/sd
            smooth = (-raw_score + q2*theta)*sd/events
            threshold = q1*sd/events
            z = b-smooth
            residual = b-np.sign(z)*np.maximum(np.abs(z)-threshold, 0)
            return float(np.max(np.abs(residual)))

        for i in range(1, max_steps+1):
            a = min(1.3**min(i, 100), SHARPNESS_MAX)
            h, raw_g, raw_ll = get_gradients(X, T, E, weights, entries, beta)
            if i == 1 and np.all(beta == 0):
                self._ll_null_ = raw_ll
            p = expit(a*beta)
            ll_ = raw_ll-penalty(beta, a)
            g = raw_g-q1*np.tanh(a*beta/2)-q2*beta
            information = -h + np.diag(q2+q1*2*a*p*(1-p))
            prox = exact_prox(raw_g, beta)
            row = {'iteration':i, 'sharpness':a, 'proximal_gradient_inf':prox}
            self.numerical_trace_.append(row)
            if a == SHARPNESS_MAX and prox <= INTERNAL_PROX_TOL:
                self.numerical_success_ = True
                break
            if i == max_steps:
                break
            # Symmetric diagonal equilibration prevents the smoothing diagonal
            # from overwhelming the positive but weak core ridge curvature.
            diagonal = np.sqrt(np.diag(information))
            if not np.isfinite(information).all() or np.any(diagonal <= 0):
                raise ConvergenceError('Nonfinite or nonpositive safeguarded EN information')
            scaled = information/diagonal[:, None]/diagonal[None, :]
            delta = solve(scaled, g/diagonal, assume_a='pos', check_finite=True)/diagonal
            ascent = float(g@delta)
            if not np.isfinite(delta).all() or not np.isfinite(ascent) or ascent < 0:
                raise ConvergenceError('Invalid safeguarded EN Newton direction')
            trial_step = min(1., step_size*1.3**min(i-1, 10))
            accepted = False
            for backtrack in range(40):
                trial = beta+trial_step*delta
                # Guard overflow before invoking the existing Efron backend.
                eta = np.asarray(X)@trial
                if np.isfinite(eta).all() and np.max(np.abs(eta)) < 500:
                    _, _, candidate_ll = get_gradients(X, T, E, weights, entries, trial)
                    candidate_ll -= penalty(trial, a)
                    slack = 1e-12*max(1., abs(ll_))
                    if np.isfinite(candidate_ll) and candidate_ll >= ll_+ARMIJO*trial_step*ascent-slack:
                        accepted = True
                        break
                trial_step *= .5
            row.update(armijo_accepted=accepted, step_size=trial_step, backtracks=backtrack)
            if not accepted:
                raise ConvergenceError('Safeguarded EN Armijo line search exhausted')
            beta = trial
        if not self.numerical_success_:
            warnings.warn('Safeguarded EN did not attain exact-target proximal stationarity',
                          ConvergenceWarning)
        # Lifelines expects the Hessian and likelihood at the returned theta.
        return beta, ll_, -information


class SafeguardedCoxPHFitter(SharedRidgeCoxPHFitter):
    def _fit_model_breslow(self, *args, **kwargs):
        model = SafeguardedSemiParametric(
            penalizer=self.penalizer, l1_ratio=self.l1_ratio, strata=self.strata,
            alpha=self.alpha, label=self._label,
            core_lambda=self.core_lambda, core_n=self.core_n)
        model.fit(*args, **kwargs)
        return model
