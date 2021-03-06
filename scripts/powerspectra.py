#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from pathlib import Path
import click
import h5py
import yaml
import lynx
import sys
import hoover
import pymaster as nmt
from scipy.optimize import minimize 
import matplotlib.pyplot as plt
import numpy as np
from lynx import Masking

_logger = logging.getLogger(__name__)


@click.command()
@click.option('-d', '--data_path', 'data_path', required=True,
                type=click.Path(exists=True), help='path to data configuration')
@click.option('-m', '--model_path', 'model_path', required=True,
                type=click.Path(exists=False), help='path to model configuration')
@click.option('-p', '--mask_path', 'mask_path', required=True,
                type=click.Path(exists=True), help='path to power spectrum configuration')
@click.option('-n', '--estimate_noise/--no-estimate_noise', 'estimate_noise', help="estimate noise power spectrum", default=False)
@click.option('--quiet', 'log_level', flag_value=logging.WARNING, default=True)
@click.option('-v', '--verbose', 'log_level', flag_value=logging.INFO)
@click.option('-vv', '--very-verbose', 'log_level', flag_value=logging.DEBUG)
@click.version_option(lynx.__version__)
def main(data_path: Path, model_path: Path, mask_path: Path, estimate_noise: bool, log_level: int):
    logging.basicConfig(stream=sys.stdout,
                        level=log_level,
                        datefmt='%Y-%m-%d %H:%M',
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    with open(data_path) as f:
        data_cfg = yaml.load(f, Loader=yaml.FullLoader)

    data_id = data_cfg['identifier']
    nmc = data_cfg['monte_carlo']
    hdf5_path = data_cfg['hdf5_path']

    logging.info(f"Working on data: {data_id}")

    masking = lynx.Masking(mask_path)
    fitting_masks = list(masking.get_fitting_indices())

    model_identifier, lnP = hoover.LogProb.load_model_from_yaml(model_path)

    for mask_name, wsp, mask, binning, beam in masking.get_powerspectrum_tools():
        # get the bandpower window function, which will be saved with the
        # computed spectra for comparison with theory later.
        bpw_window_function = wsp.get_bandpower_windows()

        logging.info(f"Working on power spectra for mask: {mask_name}")

        for fitting_name, _ in fitting_masks:
            
            logging.info(f"Working on fitting scheme: {fitting_name}") 

            hdf5_record_amp = Path(model_identifier) / fitting_name
            hdf5_record_spec = hdf5_record_amp / 'spectra' / mask_name
            
            with h5py.File(hdf5_path, 'a') as f:
                # Create a group which contains results for this sky
                # patch, model, and MC realization.
                spec = f.require_group(f"{hdf5_record_spec}")
                spec.attrs.update({'config': yaml.dump(masking.cfg)})
                # save bandpower window function
                dset = spec.require_dataset('bpw_window_function', shape=bpw_window_function.shape, dtype=bpw_window_function.dtype)
                dset[...] = bpw_window_function
                dset = spec.require_dataset('beam', shape=beam.shape, dtype=beam.dtype)
                dset[...] = beam

                # Create a dataset for the whole sky, and log the
                # results for this patch in the corresponding indices.
                # Do the same for the spectral parameters.
                for component in lnP._components:
                    logging.info(f"Working on component: {component}")
                    cl_mc = np.zeros((int(nmc / 2), 4, binning.get_n_bands()))
                    for ispec, imc in enumerate(np.arange(nmc)[::2]):
                        jmc = imc + 1
                        logging.info(f"Working on Monte Carlo realizations: {imc:d}, {jmc:d}")
                        T_bar_1 = f[f"{hdf5_record_amp / component}_T_bar"][imc, ...]
                        T_bar_2 = f[f"{hdf5_record_amp / component}_T_bar"][jmc, ...]
                        cl_mc[ispec] = compute_nmt_spectra(T_bar_1, T_bar_2, mask, wsp)

                        if component == 'cmb':
                            N_T_1 = f[f"{hdf5_record_amp}"]['cmb_N_T'][...]
                            N_T_2 = f[f"{hdf5_record_amp}"]['cmb_N_T'][...]

                            noise_mc = 30
                            cl_n = np.zeros((noise_mc, binning.get_n_bands()))

                    logging.info(f"Saving in {spec.name}/{component}")
                    cl_dset = spec.require_dataset(component, dtype=cl_mc.dtype, shape=cl_mc.shape)             
                    cl_dset[...] = cl_mc
                
                if estimate_noise:

                    for k in range(noise_mc):
                        n1 = get_realization(N_T_1)
                        n2 = get_realization(N_T_2)
                        cl_n[k] = compute_nmt_spectra(n1, n2, mask, wsp)[3]

                    cl_n_mean, cl_n_cov = compute_mean_cov(cl_n)
                    cl_n_dset = spec.require_dataset(f"{component}_cln_mean", dtype=cl_n_mean.dtype, shape=cl_n_mean.shape)
                    cl_n_dset[...] = cl_n_mean
                    cl_n_dset = spec.require_dataset(f"{component}_cln_cov", dtype=cl_n_cov.dtype, shape=cl_n_cov.shape)
                    cl_n_dset[...] = cl_n_cov

def compute_mean_cov(arr):
    assert arr.ndim == 2
    nmc = float(arr.shape[0])
    mean = np.mean(arr, axis=0)
    diff = arr - mean[None, :]
    cov = diff[:, None, :] * diff[:, :, None]
    cov = np.sum(cov, axis=0) / nmc
    return mean, cov
        
def get_realization(N_T):
    return np.random.randn(*N_T.shape) * np.sqrt(N_T)

def compute_nmt_spectra(qu1, qu2, mask, wsp):
    f1 = nmt.NmtField(mask, qu1, purify_b=True)
    f2 = nmt.NmtField(mask, qu2, purify_b=True) 
    cl_coupled = nmt.compute_coupled_cell(f1, f2)
    cl_decoupled = np.array(wsp.decouple_cell(cl_coupled))
    return cl_decoupled

if __name__ == '__main__':
    main()