from chainer import Chain
from chainer import links as L
from chainer import functions as F
from chainer import reporter
import numpy as np

from fm import FM


class VFM(Chain):
    _mask = None

    def __init__(self, n_features=None, n_dim=8, lossfun=F.mean_squared_error,
                 lambda0=5e-3, lambda1=5e-3, lambda2=5e-3, init_bias_mu=0.0,
                 init_bias_lv=0.0, intx_term=True, total_nobs=1):
        self.n_dim = n_dim
        self.n_features = n_features
        self.lossfun = lossfun
        self.lambda0 = lambda0
        self.lambda1 = lambda1
        self.lambda2 = lambda2
        self.intx_term = intx_term
        self.total_nobs = total_nobs

        # In contrast to the FM model, the slopes and latent vectors
        # will have means (mu) and log variances (lv) for each component.
        super(VFM, self).__init__(bias_mu=L.Bias(shape=(1,)),
                                  bias_lv=L.Bias(shape=(1,)),
                                  slope_mu=L.EmbedID(n_features, 1),
                                  slope_lv=L.EmbedID(n_features, 1),
                                  prior_slope_mu=L.Bias(shape=(1, 1, 1)),
                                  prior_slope_lv=L.Bias(shape=(1, 1, 1)),
                                  latent_mu=L.EmbedID(n_features, n_dim),
                                  latent_lv=L.EmbedID(n_features, n_dim),
                                  prior_latent_mu=L.Bias(shape=(1, 1, n_dim)),
                                  prior_latent_lv=L.Bias(shape=(1, 1, n_dim)))


        # Xavier initialize weights
        c = np.sqrt(n_features * n_dim)
        d = np.sqrt(n_features)
        self.latent_mu.W.data[...] = np.random.randn(n_features, n_dim) / c
        self.latent_lv.W.data[...] = np.random.randn(n_features, n_dim) / c - 2.
        self.slope_mu.W.data[...] = np.random.randn(n_features, 1) / d
        self.slope_lv.W.data[...] = np.random.randn(n_features, 1) / d - 2.
        self.bias_mu.b.data[...] *= 0.0
        self.bias_mu.b.data[...] += init_bias_mu
        self.bias_lv.b.data[...] *= 0.0
        self.bias_lv.b.data[...] += init_bias_lv

    def mask(self, bs, nf):
        if self._mask is None or self._mask.shape[0] != bs:
            mask = self.xp.ones((nf, nf), dtype='float32')
            mask -= self.xp.eye(nf, dtype='float32')
            masks = self.xp.tile(mask, (bs, 1, 1))
            self._mask = masks
        return self._mask

    def forward(self, val, loc, y, is_test, lv_floor=-100):
        """ Given the sparse feature vector defined by location
        integers for the column index and the value at that index.
        y ~ c + sum(w_i x_i) + sum_ij( <v_i, v_j> * x_i * x_j)

        Parameters
        ----------
        val : array of float
        Values in the feature array. Should of shape (batchsize, n_feat_max)

        loc : array of int
        Location of the non-zero columns in the sparse vector. Should be of
        shape (batchsize, n_feat_max)

        y : array of float
        Array of expected outcome.

        is_test : array of float
        Dummy array that is 0 if the batch is the training dataset and 1 if
        the example is in the test set. In the latter case, this sets the
        variance to nearly zero such that parameters are always set to the
        mean with no variance.

        """
        bs = val.data.shape[0]
        nf = val.data.shape[1]
        mask = self.mask(bs, nf)

        # Compute overall bias
        # Broadcast to shape of batchsize
        bs_mu = F.broadcast_to(self.bias_mu.b, y.data.shape)
        bs_lv = F.broadcast_to(self.bias_lv.b, y.data.shape)
        bs_lv += is_test * lv_floor
        bias = F.gaussian(bs_mu, bs_lv)
        bias = F.broadcast_to(bias, y.data.shape)

        # Slope coupled to each active feature
        # loc & self.slope(loc) are shape (batchsize, n_feat_max)
        # val is also (batchsize, n_feat_max)
        shape = (bs, nf, 1)
        pr_mu = F.broadcast_to(self.prior_slope_mu.b, shape)
        pr_lv = F.broadcast_to(self.prior_slope_lv.b, shape)
        sl_mu = self.slope_mu(loc) + pr_mu
        sl_lv = self.slope_lv(loc) + pr_lv
        sl_lvc = F.broadcast_to(F.reshape(is_test, (bs, 1, 1)), (bs, nf, 1))
        sl_lvc *= lv_floor
        coef = F.reshape(F.gaussian(sl_mu, sl_lv + sl_lvc), val.data.shape)
        slop = F.sum(coef * val, axis=1)

        # Form interaction vectors
        # Input shape is (batchsize, n_feat_max) and
        # v is (batchsize, n_feat_max, n_dim)
        shape = (bs, nf, self.n_dim)
        pr_mu = F.broadcast_to(self.prior_latent_mu.b, shape)
        pr_lv = F.broadcast_to(self.prior_latent_lv.b, shape)
        vi_mu = self.latent_mu(loc) + pr_mu
        vi_lv = self.latent_lv(loc) + pr_lv
        vi_lvc = F.broadcast_to(F.reshape(is_test, (bs, 1, 1)), shape)
        vi_lvc *= lv_floor
        vi = F.gaussian(vi_mu, vi_lv + vi_lvc)
        # Form square latent interaction matrix of shape
        # (batchsize, n_feat_max, n_feat_max)
        vij = F.batch_matmul(vi, vi, transb=True)
        # Form square observed feature matrix of shape
        # (batchsize, n_feat_max, n_feat_max)
        xij = F.batch_matmul(val, val, transb=True)
        # This double sums all of the interaction terms aside
        # from the computational burden this shouldn't be a problem.
        # TODO: implement the trick in Rendle's paper
        # that makes this O(kN) instead of O(kN^2)
        intx = F.sum(vij * xij * mask, axis=(1, 2))

        # Optionally choose to include the interaction term
        # without this is linear regression
        pred = bias + slop
        if self.intx_term:
            pred += intx

        # Compute MSE loss
        mse = F.mean_squared_error(pred, y)
        rmse = F.sqrt(mse)  # Only used for reporting

        # Now compute the priors / regularization
        frac = loc.data.shape[0] * 1.0 / self.total_nobs
        reg0 = F.gaussian_kl_divergence(self.bias_mu.b, self.bias_lv.b)
        reg1 = F.gaussian_kl_divergence(self.slope_mu.W, self.slope_lv.W)
        reg2 = F.gaussian_kl_divergence(self.latent_mu.W, self.latent_lv.W)
        reg3 = F.gaussian_kl_divergence(self.prior_latent_mu.b,
                                        self.prior_latent_lv.b)
        reg4 = F.gaussian_kl_divergence(self.prior_slope_mu.b,
                                        self.prior_slope_lv.b)
        regt = reg0 * self.lambda0 + reg1 * self.lambda1 + reg2 * self.lambda2
        regt += reg3 * self.lambda0 + reg4 * self.lambda0

        # Total loss is MSE plus regularization losses
        loss = mse + regt * frac

        # Log the errors
        logs = {'loss': loss, 'rmse': rmse, 'reg0': reg0, 'regt': regt,
                'reg1': reg1, 'reg2': reg2, 'bias': F.sum(self.bias_mu.b)}
        reporter.report(logs, self)
        return loss

    def __call__(self, val, loc, y, is_test):
        return self.forward(val, loc, y, is_test)