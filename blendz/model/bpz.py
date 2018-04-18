from builtins import *
import numpy as np
from scipy.interpolate import interp1d
from blendz.model import ModelBase

class BPZ(ModelBase):
    def __init__(self, mag_grid_len=100, **kwargs):
        super(BPZ, self).__init__(**kwargs)

        self.mag_grid_len = mag_grid_len
        self.possible_types = self.responses.templates.possible_types
        self._loadParameterDict()
        self._calculateRedshiftPriorNorm()

    def _loadParameterDict(self):
        nt = len(self.possible_types)

        if len(self.prior_params) != 5 * len(types) - 2:
            raise ValueError('Wrong number of parameters')

        kt = {t: self.prior_params[i] for i, t in enumerate(self.possible_types[:-1])}
        ft = {t: self.prior_params[i + nt - 1] for i, t in enumerate(self.possible_types[:-1])}
        alpt = {t: self.prior_params[i + 2*nt - 2] for i, t in enumerate(self.possible_types)}
        z0t = {t: self.prior_params[i + 3*nt - 2] for i, t in enumerate(self.possible_types)}
        kmt = {t: self.prior_params[i + 4*nt - 2] for i, t in enumerate(self.possible_types)}

        self.prior_params_dict = {
            'k_t': kt, 'f_t': ft, 'alpha_t': alpt, 'z_0t': z0t, 'k_mt': kmt
        }

    def _calculateRedshiftPriorNorm(self):
        self.redshift_prior_norm = {}
        mag_range = np.linspace(self.config.ref_mag_lo, self.config.ref_mag_hi, self.mag_grid_len)
        for T in self.possible_types:
            norms = np.zeros(self.mag_grid_len)
            for i, mag in enumerate(mag_range):
                zi = np.exp(np.array([self.lnRedshiftPrior(zz, T, mag, norm=False) for zz in self.responses.zGrid]))
                norms[i] = np.log(1./np.trapz(zi[np.isfinite(zi)], x=self.responses.zGrid[np.isfinite(zi)]))
            self.redshift_prior_norm[T] = interp1d(mag_range, norms)

    def lnTemplatePrior(self, template_type, component_ref_mag):
        #All include a scaling of 1/Number of templates of that type
        if template_type in ['early', 'late']:
            Nt = self.responses.templates.numType(template_type)
            coeff = np.log(self.prior_params_dict['f_t'][template_type] / Nt)
            expon = self.prior_params_dict['k_t'][template_type] * (component_ref_mag - self.config.ref_mag_lo)
            out = coeff - expon
        elif template_type == 'irr':
            Nte = self.responses.templates.numType('early')
            Ntl = self.responses.templates.numType('late')
            Nti = self.responses.templates.numType('irr')
            expone = self.prior_params_dict['k_t']['early'] * (component_ref_mag - self.config.ref_mag_lo)
            exponl = self.prior_params_dict['k_t']['late'] * (component_ref_mag - self.config.ref_mag_lo)
            early = self.prior_params_dict['f_t']['early'] * np.exp(-expone)
            late = self.prior_params_dict['f_t']['late'] * np.exp(-exponl)
            out = np.log(1. - early - late) - np.log(Nti)
        else:
            raise ValueError('The BPZ priors are only defined for templates of \
                              types "early", "late" and "irr", but the template \
                              prior was called with type ' + template_type)
        return out

    def lnRedshiftPrior(self, redshift, template_type, component_ref_mag, norm=True):
        try:
            if redshift==0:
                first = -np.inf
            else:
                first = (self.prior_params_dict['alpha_t'][template_type] * np.log(redshift))
            second = self.prior_params_dict['z_0t'][template_type] + (self.prior_params_dict['k_mt'][template_type] * (component_ref_mag - self.config.ref_mag_lo))
            out = first - (redshift / second)**self.prior_params_dict['alpha_t'][template_type]
        except KeyError:
            raise ValueError('The BPZ priors are only defined for templates of \
                              types "early", "late" and "irr", but the redshift \
                              prior was called with type ' + template_type)
        if norm:
            try:
                out = out + self.redshift_prior_norm[template_type](component_ref_mag)
            except ValueError:
                raise ValueError('Magnitude = {} is outside of prior-precalculation range. Check your configuration ref-mag limits cover your input magnitudes.'.format(component_ref_mag))
            return out
        else:
            return out

    def correlationFunction(self, redshifts):
        if len(redshifts)==1:
            return 0.
        elif len(redshifts)==2:
            if not self.config.sort_redshifts:
                redshifts = np.sort(redshifts)
            separation = self.comovingSeparation(redshifts[0], redshifts[1])
            #Small-scale cutoff
            if separation < self.config.xi_r_cutoff:
                separation = self.config.xi_r_cutoff
            return (self.config.r0 / separation)**self.config.gamma
        else:
            raise NotImplementedError('No N>2 yet...')

    def lnMagnitudePrior(self, magnitude):
        return 0.6*(magnitude - self.config.ref_mag_hi) * np.log(10.)

    def lnPriorCalibrationPrior(self):
        '''Returns the prior on the prior parameters for the calibration procedure.'''
        #Assume a flat prior, except that sum(type fractions) <= 1. ...
        if sum(self.prior_params_dict['f_t'].values()) > 1.:
            return -np.inf
        #... and all parameters are positive
        elif np.any(self.prior_params < 0.):
            return -np.inf
        else:
            return 0.
