#!/bin/env python

import numpy as np

class GMM:
    def __init__(self, K=1, D=1, data=None, s=None, w=0., n_impute=0, sel_callback=None, verbose=False):
        self.verbose = verbose
        if data is not None:
            self.D = data.shape[1]
            self.w = w
            self.initializeModel(K, s, data)
            self._run_EM(data, n_impute=n_impute, sel_callback=sel_callback)
        else:
            self.D = D
            self.amp = np.zeros((K))
            self.mean = np.empty((K,D))
            self.covar = np.empty((K,D,D))
            
    @property
    def K(self):
        return self.amp.size

    def draw(self, size=1, sel_callback=None, invert_callback=False):
        # draw indices for components given amplitudes
        ind = np.random.choice(self.K, size=size, p=self.amp)
        samples = np.empty((size, self.D))
        counter = 0
        if size > self.K:
            bc = np.bincount(ind)
            components = np.arange(ind.size)[bc > 0]
            for c in components:
                mask = ind == c
                s = mask.sum()
                samples[counter:counter+s] = np.random.multivariate_normal(self.mean[c], self.covar[c], size=s)
                counter += s
        else:
            for i in ind:
                samples[counter] = np.random.multivariate_normal(self.mean[i], self.covar[i], size=1)
                counter += 1

        # if subsample with selection is required
        if sel_callback is not None:
            sel_ = sel_callback(samples)
            if invert_callback:
                sel_ = np.invert(sel_)
            size_in = sel_.sum()
            if size_in != size:
                ssamples = self.draw(size=size-size_in, sel_callback=sel_callback, invert_callback=invert_callback)
                samples = np.concatenate((samples[sel_], ssamples))
        return samples

    def logL(self, data):
        log_p = self._E(data)
        return self._logsum(log_p.T)

    def _run_EM(self, data, n_impute=0, sel_callback=None, tol=1e-3):
        maxiter = 100
        
        # standard EM
        it = 0
        logL0 = None
        while it < maxiter: # limit loop in case of no convergence
            amp_ = self.amp.copy()
            mean_ = self.mean.copy()
            covar_ = self.covar.copy()

            log_p = self._E(data)
            # compute logL from E before M modifies qij
            logL_ = self._logsum(log_p.T).mean()
            self._M(data, log_p)
            if self.verbose:
                print " iter %d: %.3f" % (it, logL_)

            # convergence test
            if it > 0 and logL_ - logL0 < tol:
                break
            else:
                logL0 = logL_
            it += 1

        # do we need imputation?
        if n_impute > 0:

            # for each iteration, draw several fake data sets
            # estimate mean and std of their logL, test for convergence,
            # and adopt their _mean_ model for the next iteration
            it = 0
            logL0 = None
            RD = 5 # repeated draws for imputation
            logL__ = np.empty(RD)
            amp__ = np.empty((RD, self.K))
            mean__ = np.empty((RD, self.K, self.D))
            covar__ = np.empty((RD, self.K, self.D, self.D))
            
            while it < maxiter:
                amp_ = self.amp.copy()
                mean_ = self.mean.copy()
                covar_ = self.covar.copy()
                
                rd = 0
                while rd < RD:
                    
                    # reset model to current
                    self.amp[:] = amp_[:]
                    self.mean[:,:] = mean_[:,:]
                    self.covar[:,:,:] = covar_[:,:,:]
                    
                    data_out = self._I(n_impute, sel_callback=sel_callback)
                    data_ = np.concatenate((data, data_out), axis=0)

                    # perform EM on extended data
                    log_p = self._E(data_)
                    logL__[rd] = self._logsum(log_p.T).mean()
                    self._M(data_, log_p, n_impute=n_impute)

                    # save model
                    amp__[rd,:] = self.amp[:]
                    mean__[rd,:,:] = self.mean[:,:]
                    covar__[rd,:,:,:] = self.covar[:,:,:]
                    if self.verbose:
                        print "   iter %d/%d: %.3f" % (it, rd, logL__[rd])
                    rd += 1
                    
                # convergence test:
                # in principle one can do Welch's t-test wrt iteration before
                # but the actual risk here is a run-away, which
                # drastically _reduces_ the likelihood, at which point we abort
                if self.verbose:
                    print " iter %d: %.3f" % (it, np.array(logL__).mean())

                if it > 0 and logL__.mean() - logL0 < tol:
                    break
                else:
                    logL0 = logL__.mean()

                # because the components remain ordered, we can
                # adopt the mean of the repeated draws as new model
                self.amp = amp__.mean(axis=0) 
                self.mean = mean__.mean(axis=0) 
                self.covar = covar__.mean(axis=0)
                it += 1

    def initializeModel(self, K, s, data):
        # set model to random positions with equally sized spheres
        self.amp = np.ones(K)/K # now self.K works
        min_pos = data.min(axis=0)
        max_pos = data.max(axis=0)
        self.mean = min_pos + (max_pos-min_pos)*np.random.random(size=(self.K, self.D))
        # if s is not set: use volume filling argument:
        # K spheres of radius s [having volume s^D * pi^D/2 / gamma(D/2+1)]
        # should completely fill the volume spanned by data.
        if s is None:
            from scipy.special import gamma
            vol_data = np.prod(max_pos-min_pos)
            s = (vol_data / self.K * gamma(self.D*0.5 + 1))**(1./self.D) / np.sqrt(np.pi)
            if self.verbose:
                print "initializing spheres with s=%.2f" % s
        self.covar = np.tile(s**2 * np.eye(self.D), (self.K,1,1))

    def _E(self, data):
        # compute p(x | k)
        # NOTE: normally the E step computes the fractional probability
        # p (x | k) / (sum_k p(x | k))
        # We defer this to the M step because we need the proper probs
        # for per-point likelihoods in some cases.
        log_p = np.empty((data.shape[0], self.K))
        log2piD2 = np.log(2*np.pi)*(0.5*self.D)
        for k in xrange(self.K):
            dx = data - self.mean[k]
            chi2 = np.einsum('...j,j...', dx, np.dot(np.linalg.inv(self.covar[k]), dx.T))
            # prevent tiny negative determinants to mess up
            (sign, logdet) = np.linalg.slogdet(self.covar[k])
            log_p[:,k] = np.log(self.amp[k]) - log2piD2 - sign*logdet/2 - chi2/2
        return log_p

    def _logsum(self, ll):
        """Computes log of sum of likelihoods for GMM components.

        This method tries hard to avoid over- or underflow that may arise
        when computing exp(log(p(x | k)).

        See appendix A of Bovy, Hogg, Roweis (2009).

        Args:
        ll: (K, N) log-likelihoods from K calls to logL_K() with N coordinates

        Returns:
        (N, 1) of log of total likelihood

        """
        floatinfo = np.finfo(ll.dtype)
        underflow = np.log(floatinfo.tiny) - ll.min(axis=0)
        overflow = np.log(floatinfo.max) - ll.max(axis=0) - np.log(ll.shape[0])
        c = np.where(underflow < overflow, underflow, overflow)
        return np.log(np.exp(ll + c).sum(axis=0)) - c


    def _M(self, data, log_p, n_impute=0):
        N = data.shape[0]

        # before we modify log_p, we need to store the fractional probability
        # of imputed points (compared to all) for each component
        if n_impute > 0:
            frac_p_out = np.exp(self._logsum(log_p[-n_impute:]) - self._logsum(log_p))
            
        # log of fractional probability log_q, modifies log_p in place
        log_q = log_p
        logsum_k_p = self._logsum(log_p.T) # summed over k, function of i
        for k in xrange(self.K):
            log_q[:,k] -= logsum_k_p
        sum_i_q = np.exp(self._logsum(log_q))

        # amplitude update
        self.amp[:] = sum_i_q/N
        
        # covariance: with imputation we need add penalty term from
        # the conditional probability of drawing points from the model:
        # p_out / p * Sigma_k (i.e. fractional prob of imputed points)
        if n_impute == 0:
            self.covar[:,:,:] = 0
        else:
            self.covar *= frac_p_out[:, None, None]

        # update all k component means and covariances
        for k in xrange(self.K):
            qk = np.exp(log_q[:,k])

            # mean
            self.mean[k,:] = (data * qk[:,None]).sum(axis=0)/sum_i_q[k]

            # funny way of saying: for each point i, do the outer product
            # of d_m with its transpose, multiply with pi[i], and sum over i
            d_m = data - self.mean[k]
            self.covar[k,:,:] += (qk[:, None, None] * d_m[:, :, None] * d_m[:, None, :]).sum(axis=0)
            
            # Bayesian regularization term
            if self.w > 0:
                self.covar[k,:,:] += self.w*np.eye(self.D)
                self.covar[k,:,:] /= sum_i_q[k] + 1
            else:
                self.covar[k,:,:] /= sum_i_q[k]

    def _I(self, n_impute, sel_callback=None):
        # create imputation sample from the current model
        # we don know the number if missing values exactly, so
        # draw from a Poisson distribution
        n_samples = np.random.poisson(n_impute)
        return self.draw(size=n_samples, sel_callback=sel_callback, invert_callback=True)




            










