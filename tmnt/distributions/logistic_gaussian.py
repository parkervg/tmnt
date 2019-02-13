#coding: utf-8

import math
import mxnet as mx
from mxnet import gluon
from mxnet.gluon import HybridBlock
from tmnt.distributions.latent_distrib import LatentDistribution

__all__ = ['LogisticGaussianLatentDistribution']

class LogisticGaussianLatentDistribution(LatentDistribution):

    def __init__(self, n_latent, ctx):
        super(LogisticGaussianLatentDistribution, self).__init__(n_latent, ctx)
        self.alpha = 1.0

        prior_var = 1 / self.alpha - (2.0 / n_latent) + 1 / (self.n_latent * self.n_latent)
        self.prior_var = prior_var
        self.prior_logvar = math.log(prior_var)

        with self.name_scope():
            self.mu_encoder = gluon.nn.Dense(units = n_latent, activation=None)
            self.lv_encoder = gluon.nn.Dense(units = n_latent, activation=None)

    def _get_kl_term(self, F, mu, lv):
        posterior_var = F.exp(lv)
        delta = mu
        dt = delta * delta / self.prior_var
        v_div = posterior_var / self.prior_var
        lv_div = self.prior_logvar - lv
        return F.sum(0.5 * (F.sum((v_div + dt + lv_div), axis=1) - self.n_latent))

    def hybrid_forward(self, F, data, batch_size):
        mu = self.mu_encoder(data)
        lv = self.lv_encoder(data)
        z = self._get_gaussian_sample(F, mu, lv, batch_size)
        KL = self._get_kl_term(F, mu, lv)
        return z, KL
