try:
    from mpi4py import MPI
    MPI_RANK = MPI.COMM_WORLD.Get_rank()
except:
    MPI_RANK = 0

from builtins import *
import sys
import os
import warnings
from math import ceil
from multiprocessing import cpu_count
import itertools as itr
import numpy as np
from scipy.integrate import simps
import nestle
import emcee
from tqdm import tqdm
import dill
import blendz
from blendz import Configuration
from blendz.fluxes import Responses
from blendz.photometry import Photometry, SimulatedPhotometry
from blendz.utilities import incrementCount, Silence

try:
    import pymultinest
    PYMULTINEST_AVAILABLE = True
except ImportError:
    PYMULTINEST_AVAILABLE = False
    warnings.warn('PyMultinest not installed, so falling back to (slower) python implementation.'
        + ' See http://johannesbuchner.github.com/PyMultiNest/install.html for installation help.')
except (SystemExit, OSError):
    PYMULTINEST_AVAILABLE = False
    warnings.warn('PyMultinest failed to load, so falling back to (slower) python implementation.'
        + ' See http://johannesbuchner.github.com/PyMultiNest/install.html for installation help.')


class Photoz(object):
    def __init__(self, model=None, photometry=None, config=None,\
                 load_state_path=None, **kwargs):
        if load_state_path is not None:
            self.loadState(load_state_path)
        else:
            #Warn user is config and either/or responses/photometry given that config ignored
            if ((model is not None and config is not None) or
                    (photometry is not None and config is not None)):
                warnings.warn('A configuration object was provided to Photoz object '
                              + 'as well as a Model/Photometry object, though these '
                              + 'should be mutually exclusive. The configuration '
                              + 'provided will be ignored.')
            #Responses and photometry given, merge their configs
            if (model is not None) and (photometry is not None):
                self.config = Configuration(**kwargs)
                self.config.mergeFromOther(model.config)
                self.config.mergeFromOther(photometry.config)
                self.model = model
                self.responses = self.model.responses
                self.photometry = photometry
            #Only responses given, use its config to load photometry
            elif (model is not None) and (photometry is None):
                self.config = Configuration(**kwargs)
                self.config.mergeFromOther(model.config)
                self.model = model
                self.responses = self.model.responses
                self.photometry = Photometry(config=self.config)
            #Only photometry given, use its config to load responses
            elif (model is None) and (photometry is not None):
                self.config = Configuration(**kwargs)
                self.config.mergeFromOther(photometry.config)
                self.photometry = photometry

                self.model = blendz.model.BPZ(config=self.config)
                self.responses = self.model.responses
            #Neither given, load both from provided (or default, if None) config
            else:
                self.config = Configuration(**kwargs)
                if config is not None:
                    self.config.mergeFromOther(config)

                self.photometry = Photometry(config=self.config)

                self.model = blendz.model.BPZ(config=self.config)
                self.responses = self.model.responses

            # Get max_ref_mag_hi from photometry, which already deals with sigma vs fixed
            self.max_ref_mag_hi = np.max([g.ref_mag_hi for g in self.photometry])
            self.model.max_ref_mag_hi = self.max_ref_mag_hi

            self.num_templates = self.responses.templates.num_templates
            self.num_measurements = self.responses.filters.num_filters
            self.num_galaxies = self.photometry.num_galaxies

            self.tmp_ind_to_type_ind = self.responses.templates.tmp_ind_to_type_ind
            self.possible_types = self.responses.templates.possible_types
            self.num_types = len(self.possible_types)

            #TODO: Check this
            #This was defaulted to assume a single component, present in all measurements
            #not sure why it needs to be set up here though, so comment out for now
            #self.model._setMeasurementComponentMapping(None, 1)

            #Set up empty dictionaries to put results into
            self._samples = {}
            self._logevd = {}
            self._logevd_error = {}
            for g in range(self.num_galaxies):
                #Each value is a dictionary which will be filled by sample function
                #The keys of this inner dictionary will be the number of blends for run
                self._samples[g] = {}
                self._logevd[g] = {}
                self._logevd_error[g] = {}

    def saveState(self, filepath):
        """Save this entire Photoz instance to file.

        This saves the exact state of the current object, including all data and any
        reults from sampling.

        Args:
            filepath (str): Path to file to save to.
        """
        if isinstance(self.photometry, SimulatedPhotometry):
            try:
                current_seed = self.photometry.sim_seed.next()
                self.photometry.sim_seed = current_seed
            except:
                warnings.warn('SimulatedPhotometry seed not saved.')
        with open(filepath, 'wb') as f:
            state = {key: val for key, val in self.__dict__.items() if key not in ['pbar', 'breakSilence']}
            dill.dump(state, f)
        #Put the random seed back how it was after the saving is done
        if isinstance(self.photometry, SimulatedPhotometry):
            try:
                self.photometry.sim_seed = incrementCount(current_seed)
            except:
                pass

    def loadState(self, filepath):
        with open(filepath, 'rb') as f:
            self.__dict__.update(dill.load(f))
        #If the photometry is simulated, replace the seed currently saved as
        #a number with the generator it was before saving
        if isinstance(self.photometry, SimulatedPhotometry):
            try:
                current_seed = self.photometry.sim_seed
                self.photometry.sim_seed = incrementCount(current_seed)
            except:
                warnings.warn('SimulatedPhotometry seed not loaded.')

    def _lnLikelihood_flux(self, model_flux):
        chi_sq = -1. * np.sum((self.photometry.current_galaxy.flux_data_noRef - model_flux)**2 / self.photometry.current_galaxy.flux_sigma_noRef**2)
        return chi_sq

    def _lnLikelihood_mag(self, total_ref_flux):
        #Depending on the measurement-component mapping, the galaxy ref_flux/sigma
        #can be an array of length either 1 or num_components.
        #total_ref_flux should be of the same length

        #TODO: No calls should be made where this isn't the case, so remove the assert
        tmp_a = len(self.photometry.current_galaxy.ref_flux_data)
        try:
            tmp_b = len(total_ref_flux)
        except TypeError:
            total_ref_flux = np.array([total_ref_flux])
            tmp_b = len(total_ref_flux)
        assert(tmp_a == tmp_b)

        chi_sq = -1. * np.sum((self.photometry.current_galaxy.ref_flux_data - total_ref_flux)**2 / self.photometry.current_galaxy.ref_flux_sigma**2)
        return chi_sq

    def _lnPosterior(self, params):
        num_components = int(len(params) // 2)
        redshifts = params[:num_components]
        magnitudes = params[num_components:]

        if not self.model._obeyPriorConditions(redshifts, magnitudes, self.photometry.current_galaxy.ref_mag_hi):
            return -np.inf
        else:
            #Precalculate all quantities we'll need in the template loop
            #Single interp call -> Shape = (N_template, N_band, N_component)
            model_fluxes = self.responses.interp(redshifts)

            priors = np.zeros((num_components, self.num_types))
            for nb in range(num_components):
                priors[nb, :] = self.model.lnPrior(redshifts[nb], magnitudes[nb])

            redshift_correlation = np.log(1. + self.model.correlationFunction(redshifts))

            #Get total flux in reference band  = transform to flux & sum
            # total_ref_flux should be either len 1 or len==num_components
            # This should match the number of reference bands, so check
            # If 1, add components together (blend). If not, treat seperately
            if len(self.config.ref_band)==1:
                total_ref_flux = np.sum(10.**(-0.4 * magnitudes))
            else:
                total_ref_flux = 10.**(-0.4 * magnitudes) #Array with len==len(magnitudes)

            #Check whether the selection band is ref or not
            #Only need this if it is
            if np.all(self.config.ref_band == self.config.select_band):
                selection_effect_ref = self.model.lnSelection(total_ref_flux,
                        self.photometry.current_galaxy)

            #Loop over all templates - discrete marginalisation
            #All log probabilities so (multiply -> add) and (add -> logaddexp)
            lnProb = -np.inf

            #At each iteration template_combo is a tuple of (T_1, T_2... T_num_components)
            for template_combo in itr.product(*itr.repeat(range(self.num_templates), num_components)):
                #One redshift/template/magnitude prior and model flux for each blend component
                tmp = 0.
                blend_flux = np.zeros(self.num_measurements)
                component_scaling_norm = 0.
                for nb in range(num_components):
                    T = template_combo[nb]
                    #TODO: Check this!
                    if len(self.config.ref_band)==1:
                        #Only one reference band but it's still an array, so get element
                        component_scaling = 10.**(-0.4*magnitudes[nb]) / model_fluxes[T, self.config.ref_band[0], nb]
                    else:
                        component_scaling = 10.**(-0.4*magnitudes[nb]) / model_fluxes[T, self.config.ref_band[nb], nb]
                    blend_flux += model_fluxes[T, :, nb] * component_scaling * self.model.mc_map_matrix[nb, :]
                    type_ind = self.tmp_ind_to_type_ind[T]
                    tmp += priors[nb, type_ind]

                #Check whether the selection band is ref or not
                #If not, we need to use the template fluxes
                if np.all(self.config.ref_band == self.config.select_band):
                    tmp += selection_effect_ref
                else:
                    select_flux = blend_flux[self.config.select_band]
                    tmp += self.model.lnSelection(select_flux,
                                                  self.photometry.current_galaxy)

                #Remove ref_band from blend_fluxes, as that goes into the ref-mag
                #likelihood, not the flux likelihood
                blend_flux = blend_flux[self.config.non_ref_bands]

                #Other terms only appear once per summation-step
                tmp += redshift_correlation
                tmp += self._lnLikelihood_flux(blend_flux)
                tmp += self._lnLikelihood_mag(total_ref_flux)


                #logaddexp contribution from this template to marginalise
                lnProb = np.logaddexp(lnProb, tmp)

            return lnProb - self.prior_norm


    def _priorTransform(self, params):
        '''
        Transform params from [0, 1] uniform random to [min, max] uniform random,
        where the min redshift is zero, max redshift is set in the configuration,
        and the min/max magnitudes (numerically, not brightness) are set by configuration.
        '''
        num_components = int(len(params) // 2)

        trans = np.ones(len(params))
        trans[:num_components] = self.config.z_hi - self.config.z_lo
        trans[num_components:] = self.photometry.current_galaxy.ref_mag_hi - self.config.ref_mag_lo

        shift = np.zeros(len(params))
        shift[:num_components] = self.config.z_lo
        shift[num_components:] = self.config.ref_mag_lo
        return (params * trans) + shift


    def _priorTransform_multinest(self, cube, ndim, nparams):
        '''
        Transform params from [0, 1] uniform random to [0, max] uniform random,
        where the max redshift is set in the configuration, and the max fraction
        is 1.
        '''
        num_components = ndim // 2

        trans = np.zeros(ndim)
        trans[:num_components] = self.config.z_hi
        trans[num_components:] = self.photometry.current_galaxy.ref_mag_hi - self.config.ref_mag_lo

        shift = np.zeros(ndim)
        shift[:num_components] = self.config.z_lo
        shift[num_components:] = self.config.ref_mag_lo

        for i in range(ndim):
            cube[i] = (cube[i] * trans[i]) + shift[i]

    def _lnPosterior_multinest(self, cube, ndim, nparams):
        self.num_posterior_evals += 1

        params = np.array([cube[i] for i in range(ndim)])

        with self.breakSilence():
            if (self.num_posterior_evals%self.num_between_print==0) and MPI_RANK==0:
                self.pbar.set_description('[Cmp: {}/{}, Itr: {}] '.format(self.blend_count,
                                                                          self.num_components_sampling,
                                                                          self.num_posterior_evals))
                self.pbar.refresh()

        return self._lnPosterior(params)

    def _sampleProgressUpdate(self, info):
        if (info['it']%self.num_between_print==0) and MPI_RANK==0:
            self.pbar.set_description('[Cmp: {}/{}, Itr: {}] '.format(self.blend_count,
                                                                      self.num_components_sampling,
                                                                      info['it']))
            self.pbar.refresh()

    def _full_lnPrior(self, params):
        num_components = int(len(params) // 2)
        redshifts = params[:num_components]
        magnitudes = params[num_components:]

        if not self.model._obeyPriorConditions(redshifts, magnitudes, self.photometry.current_galaxy.ref_mag_hi):
            return -np.inf
        else:

            cmp_priors = np.zeros((num_components, self.num_types))
            for nb in range(num_components):
                cmp_priors[nb, :] = self.model.lnPrior(redshifts[nb], magnitudes[nb])

            redshift_correlation = np.log(1. + self.model.correlationFunction(redshifts))

            if len(self.config.ref_band)==1:
                total_ref_flux = np.sum(10.**(-0.4 * magnitudes))
            else:
                total_ref_flux = 10.**(-0.4 * magnitudes) #Array with len==len(magnitudes)

            #Check whether the selection band is ref or not
            #If it is, we can use a single selection effect for every template
            if np.all(self.config.ref_band == self.config.select_band):
                selection_effect_ref = self.model.lnSelection(total_ref_flux,
                        self.photometry.current_galaxy)
            #If not, we need to use the model fluxes, so get them here
            else:
                model_fluxes = self.responses.interp(redshifts)

            lnProb = -np.inf
            for template_combo in itr.product(*itr.repeat(range(self.num_templates), num_components)):
                tmp = 0.
                blend_flux = np.zeros(self.num_measurements)
                for nb in range(num_components):
                    T = template_combo[nb]
                    type_ind = self.tmp_ind_to_type_ind[T]
                    tmp += cmp_priors[nb, type_ind]
                    #If selection band is not reference, we need the template fluxes
                    if not np.all(self.config.ref_band == self.config.select_band):
                        if len(self.config.ref_band)==1:
                            #Only one reference band but it's still an array, so get element
                            component_scaling = 10.**(-0.4*magnitudes[nb]) / model_fluxes[T, self.config.ref_band[0], nb]
                        else:
                            component_scaling = 10.**(-0.4*magnitudes[nb]) / model_fluxes[T, self.config.ref_band[nb], nb]
                        blend_flux += model_fluxes[T, :, nb] * component_scaling * self.model.mc_map_matrix[nb, :]

                #Check whether the selection band is ref or not
                #If not, we need to use the template fluxes
                if np.all(self.config.ref_band == self.config.select_band):
                    tmp += selection_effect_ref
                else:
                    select_flux = blend_flux[self.config.select_band]
                    tmp += self.model.lnSelection(select_flux,
                                                  self.photometry.current_galaxy)
                #Other terms only appear once per summation-step
                tmp += redshift_correlation
                lnProb = np.logaddexp(lnProb, tmp)

            return lnProb

    def normalise_prior(self, galind, num_components,
                        npoints=50, seed=False, method='multi'):
        #Setup random seeding
        if seed is False:
            rstate = np.random.RandomState()
        elif seed is True:
            rstate = np.random.RandomState(galind)
        else:
            rstate = np.random.RandomState(seed + galind)

        self.model._setMeasurementComponentMapping(num_components)
        results = nestle.sample(self._full_lnPrior, self._priorTransform,
                                num_components*2, method=method, npoints=npoints,
                                rstate=rstate)#, callback=self._sampleProgressUpdate)
        self.prior_norm = results.logz


    def sample(self, num_components, galaxy=None, nresample=1000, seed=False,
               mc_map_matrix=None, npoints=150, print_interval=10,
               use_pymultinest=None, save_path=None, save_interval=None):
        """Sample the posterior for a particular number of components.

        Args:
            num_components (int):
                Sample the posterior defined for this number of components in the source.

            galaxy (int or None):
                Index of the galaxy to sample. If None, sample every galaxy in the
                photometry. Defaults to None.

            nresample (int):
                Number of non-weighted samples to draw from the weighted samples
                distribution from Nested Sampling. Defaults to 1000.

            seed (bool or int):
                Random seed for sampling to ensure deterministic results when
                ampling again. If False, do not seed. If True, seed with value
                derived from galaxy index. If int, seed with specific value.

            mc_map_matrix (None or list of tuples):
                If None, sample from the fully blended posterior. For a partially
                blended posterior, this should be a list of tuples (length = number of
                measurements), where each tuples contains the (zero-based) indices of
                the components that measurement contains. Defaults to None.

            npoints (int):
                Number of live points for the Nested Sampling algorithm. Defaults to 150.

            print_interval (int):
                Update the progress bar with number of posterior evaluations every
                print_interval calls. Defaults to 10.

            save_path (None or str):
                Filepath for saving the Photoz object for reloading with `Photoz.loadState`.
                If None, do not automatically save. If given, the Photoz object will
                be saved to this path after all galaxies are sampled. If save_interval
                is also not None, the Photoz object will be saved to this path every
                save_interval galaxies. Defaults to None.

            save_interval (None or int)
                If given and save_path is not None, the Photoz object will be
                saved to save_path every save_interval galaxies. Defaults to None.

            use_pymultinest (bool or None)
                If True, sample using the pyMultinest sampler. This requires PyMultiNest
                to be installed separately. If False, sample using the Nestle sampler,
                which is always installed when blendz is. If None, check whether pyMultinest
                is installed and use it if it is, otherwise use Nestle. Defaults to None.
        """

        if use_pymultinest is None:
            use_pymultinest = PYMULTINEST_AVAILABLE

        if isinstance(num_components, int):
            num_components = [num_components]
        else:
            if mc_map_matrix is not None:
                #TODO: This is a time-saving hack to avoid dealing with multiple specifications
                #The solution would probably be to rethink the overall design
                raise ValueError('mc_map_matrix cannot be set when sampling multiple numbers of components in one call. Do the separate cases separately.')

        self.num_components_sampling = len(num_components)
        self.num_between_print = float(round(print_interval))

        if galaxy is None:
            start = None
            stop = None
            self.num_galaxies_sampling = self.num_galaxies
        elif isinstance(galaxy, int):
            start = galaxy
            stop = galaxy + 1
            self.num_galaxies_sampling = 1
        else:
            raise TypeError('galaxy may be either None or an integer, but got {} instead'.format(type(galaxy)))

        with tqdm(total=self.num_galaxies_sampling, unit='galaxy') as self.pbar:
            self.gal_count = 1
            for gal in self.photometry.iterate(start, stop):
                self.blend_count = 1
                for nb in num_components:

                    if seed is False:
                        rstate = np.random.RandomState()
                    elif seed is True:
                        rstate = np.random.RandomState(gal.index)
                    else:
                        rstate = np.random.RandomState(seed + gal.index)

                    num_param = 2 * nb
                    self.model._setMeasurementComponentMapping(nb)

                    self.normalise_prior(gal.index, nb)

                    if use_pymultinest:
                        if not os.path.exists('chains'):
                            os.makedirs('chains')
                        with Silence() as self.breakSilence:
                            self.num_posterior_evals = 0
                            pymultinest.run(self._lnPosterior_multinest, self._priorTransform_multinest,
                                            num_param, resume=False, verbose=False, sampling_efficiency='model',
                                            n_live_points=npoints)#,
                                            #outputfiles_basename=os.path.join(blendz.CHAIN_PATH, 'chain_'))
                            results = pymultinest.analyse.Analyzer(num_param)#, outputfiles_basename=os.path.join(blendz.CHAIN_PATH, 'chain_'))

                        self._samples[gal.index][nb] = results.get_equal_weighted_posterior()[:, :-1]
                        self._logevd[gal.index][nb] = results.get_mode_stats()['global evidence']
                        self._logevd_error[gal.index][nb] = results.get_mode_stats()['global evidence error']

                    else:
                        results = nestle.sample(self._lnPosterior, self._priorTransform,
                                                num_param, method='multi', npoints=npoints,
                                                rstate=rstate, callback=self._sampleProgressUpdate)
                        self._samples[gal.index][nb] = results.samples[rstate.choice(len(results.weights), size=nresample, p=results.weights)]
                        self._logevd[gal.index][nb] = results.logz
                        self._logevd_error[gal.index][nb] = results.logzerr

                    self.blend_count += 1

                self.gal_count += 1
                if MPI_RANK==0:
                    self.pbar.update()
                if (save_path is not None) and (save_interval is not None):
                    if gal.index % save_interval == 0:
                        self.saveState(save_path)
        if save_path is not None:
            self.saveState(save_path)

    def _cacheTruthLikelihood(self):
        self.model._setMeasurementComponentMapping(1)
        #Single interp call -> Shape = (N_template, N_band, N_component)
        fixed_model_fluxes = {}
        fixed_lnLikelihood_flux = np.zeros((self.photometry.num_galaxies,
                                            self.responses.templates.num_templates))
        for g in self.photometry:
            fixed_model_fluxes[g.index] = self.responses.interp(np.array([g.truth[0]['redshift']]))
            for T in range(self.responses.templates.num_templates):
                #For the calibration, we know sources are one component only, so
                #we can assume that ref_band is only length 1
                scaling = 10.**(-0.4*g.ref_mag_data) / fixed_model_fluxes[g.index][T, self.config.ref_band[0], 0]
                fixed_model_fluxes[g.index][T, :, 0] *= scaling
                #Cache the flux likelihoods
                non_ref_flux = fixed_model_fluxes[g.index][T, self.config.non_ref_bands, 0]
                fixed_lnLikelihood_flux[g.index, T] = self._lnLikelihood_flux(non_ref_flux)
        return fixed_lnLikelihood_flux


    def calibrate(self, **kwargs):
        cached_likelihood = self._cacheTruthLikelihood()
        self.model.calibrate(self.photometry, cached_likelihood, **kwargs)

    def chain(self, num_components, galaxy=None):
        """Return the (unweighted) posterior samples.

        Args:
            num_components (int):
                Number of components.

            galaxy (int or None):
                Index of the galaxy to calculate log-evidence for. If None, return array
                of log-evidence for every galaxy. Defaults to None.
        """
        if galaxy is None:
            return [self._samples[g][num_components] for g in range(self.num_galaxies)]
        else:
            return self._samples[galaxy][num_components]

    def logevd(self, num_components, galaxy=None, return_error=False):
        """Return the natural log of the evidence.

        Args:
            num_components (int):
                Number of components.

            galaxy (int or None):
                Index of the galaxy to calculate log-evidence for. If None, return array
                of log-evidence for every galaxy. Defaults to None.

            return_error (bool):
                If True, also return the error on the log-evidence. If galaxy is None, this
                is also an array. Defaults to False.
        """
        if galaxy is None:
            if return_error:
                return np.array([self._logevd[g][num_components] for g in range(self.num_galaxies)]),\
                            np.array([self._logevd_error[g][num_components] for g in range(self.num_galaxies)])
            else:
                return np.array([self._logevd[g][num_components] for g in range(self.num_galaxies)])
        else:
            if return_error:
                return self._logevd[galaxy][num_components], self._logevd_error[galaxy][num_components]
            else:
                return self._logevd[galaxy][num_components]

    def logbayes(self, m, n, base=None, galaxy=None):
        """Return the log of the Bayes factor between m and n components, log[B_mn].

        A positive value suggests that that evidence prefers the m-component model over
        the n-component model.

        Args:
            m (int):
                First number of components.

            n (int):
                Second number of components.

            base (float or None):
                Base of the log to return. If None, uses natural log.
                Defaults to None.

            galaxy (int or None):
                Index of the galaxy to calculate B_mn for. If None, return array
                of B_mn for every galaxy. Defaults to None.
        """
        if base is None:
            alter_base = 1.
        else:
            alter_base = np.log(base)
        return (self.logevd(m, galaxy=galaxy) - self.logevd(n, galaxy=galaxy)) / alter_base

    def applyToMarginals(self, func, num_components, galaxy=None, **kwargs):
        """Apply a function to the 1D marginal distribution samples of each parameter.

        Args:
            func (function):
                The function to apply to the marginal distribution samples.
                It should accept an array of the samples as its first argument,
                with optional keyword arguments.

            num_components (int):
                Number of components.

            galaxy (int or None):
                Index of the galaxy to apply the function to. If None, return
                array with a row for each galaxy. Defaults to None.

            **kwargs:
                Any optional keyword arguments to pass to the function.
        """
        if galaxy is None:
            out = np.zeros((self.num_galaxies, num_components * 2))
            for g in range(self.num_galaxies):
                for n in range(num_components * 2):
                    out[g, n] = func(self._samples[g][num_components][:, n], **kwargs)
        else:
            out = np.zeros(num_components * 2)
            for n in range(num_components * 2):
                out[n] = func(self._samples[galaxy][num_components][:, n], **kwargs)
        return out

    def _MAP1d(self, samps, bins=50):
        vals, edges = np.histogram(samps, bins=bins)
        return edges[np.argmax(vals)] + ((edges[1] - edges[0]) / 2.)

    def max(self, num_components, galaxy=None, bins=20):
        """Return the maximum-a-posteriori point for the 1D marginal distribution of each parameter.

        This is calculated by forming a 1D histogram of each marginal distribution
        and assigning the MAP of that parameter as the centre of the tallest bin.

        Args:
            num_components (int):
                Number of components.

            galaxy (int or None):
                Index of the galaxy to calculate the MAP for. If None, return array
                with rows of MAPs for each galaxy. Defaults to None.

            bins (int):
                Number of bins to use for each 1D histogram.
        """
        return self.applyToMarginals(self._MAP1d, num_components, galaxy=galaxy, bins=bins)

    def mean(self, num_components, galaxy=None):
        """Return the mean point for the 1D marginal distribution of each parameter.

        Args:
            num_components (int):
                Number of components.

            galaxy (int or None):
                Index of the galaxy to calculate the MAP for. If None, return array
                with rows of means for each galaxy. Defaults to None.
        """
        return self.applyToMarginals(np.mean, num_components, galaxy=galaxy)


    def std(self, num_components, galaxy=None):
        """Return the standard deviation for the 1D marginal distribution of each parameter.

        Args:
            num_components (int):
                Number of components.

            galaxy (int or None):
                Index of the galaxy to calculate the MAP for. If None, return array
                with rows of means for each galaxy. Defaults to None.
        """
        return self.applyToMarginals(np.std, num_components, galaxy=galaxy)

    def quantiles(self, num_components, galaxy=None, q=(0.16, 0.84)):
        try:
            pcnt = [qq*100. for qq in q]
        except TypeError:
            pcnt = [100. * q]
        #call numpy.percentile with q *= 100
        return [self.applyToMarginals(np.percentile, num_components, galaxy=galaxy, q=pp) for pp in pcnt]
