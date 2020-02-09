import numpy as np
from scipy import linalg
from mrh.my_dmet import localintegrals
import os, time
import sys, copy
from pyscf import gto, scf, ao2mo, mcscf, fci, lib, dft
import time
from pyscf import dft, ao2mo, fci, mcscf
from pyscf.lib import logger, temporary_env
from pyscf.mcscf import mc_ao2mo
from pyscf.mcscf.addons import StateAverageMCSCFSolver
#from mrh.my_pyscf.grad.mcpdft import Gradients
#from mrh.my_pyscf.mcpdft import pdft_veff
from mrh.my_pyscf.mcpdft.otpd import get_ontop_pair_density
from mrh.my_pyscf.mcpdft.otfnal import otfnal, transfnal, ftransfnal
from mrh.util.rdm import get_2CDM_from_2RDM, get_2CDMs_from_2RDMs
from mrh.my_pyscf.mcpdft.otfnal import transfnal


def get_las_pdft (las, rdm, my_ot, my_grid):


    print ( 'you are doing a calculation using', my_ot)
    ks = dft.RKS (las.mol)
    if my_ot[:1].upper () == 'T':
        ks.xc = my_ot[1:]
        otfnal = transfnal (ks)
    elif my_ot[:2].upper () == 'FT':
        ks.xc = my_ot[2:]
        otfnal = ftransfnal (ks)

    grids = dft.gen_grid.Grids(las.mol)
    grids.level = my_grid
    otfnal.grids = grids
    otfnal.verbose = 4 
    e_tot, E_ot = kernel (las, rdm, otfnal)
    print ('Final LAS-PDFT energy is', e_tot, E_ot)

def kernel (mc, rdm, ot, root=-1):
    ''' Calculate MC-PDFT total energy

        Args:
            mc : an instance of CASSCF or CASCI class
                Note: this function does not currently run the CASSCF or CASCI calculation itself
                prior to calculating the MC-PDFT energy. Call mc.kernel () before passing to this function!
            ot : an instance of on-top density functional class - see otfnal.py

        Kwargs:
            root : int
                If mc describes a state-averaged calculation, select the root (0-indexed)
                Negative number requests state-averaged MC-PDFT results (i.e., using state-averaged density matrices)

        Returns:
            Total MC-PDFT energy including nuclear repulsion energy.
    '''
    t0 = (time.clock (), time.time ())
    amo = mc.mo_coeff[:,mc.ncore:mc.ncore+mc.ncas]
    # make_rdm12s returns (a, b), (aa, ab, bb)
    if isinstance (mc.ci, list) and root >= 0:
        mc = mcscf.CASCI (mc._scf, mc.ncas, mc.nelecas)
        mc.fcisolver = fci.solver (mc._scf.mol, singlet = False, symm = False)
        mc.mo_coeff = mc.mo_coeff
        mc.ci = mc.ci[root]
        mc.e_tot = mc.e_tot
    dm1s = np.asarray ( mc.make_rdm1s () )
###    dm1s = np.load ('/panfs/roc/groups/0/cramercj/pandh009/learning/dmet/LAS_PDFT/Test1/dm1s_cas.npy')
    adm1s = np.stack (mc.make_casdm1s () , axis=0 )
    adm2 =  get_2CDM_from_2RDM (mc.make_casdm2(), adm1s)
#    if ot.verbose >= logger.DEBUG:
#        adm2s = get_2CDMs_from_2RDMs (mc.make_casdm2s (), adm1s)
#        adm2s_ss = adm2s[0] + adm2s[2]
#        adm2s_os = adm2s[1]

    spin = abs(mc.nelecas[0] - mc.nelecas[1])
    t0 = logger.timer (ot, 'rdms', *t0)
    omega, alpha, hyb = ot._numint.rsh_and_hybrid_coeff(ot.otxc, spin=spin)
    Vnn = mc._scf.energy_nuc ()
    h = mc._scf.get_hcore ()
    dm1 = dm1s[0] + dm1s[1]
    if ot.verbose >= logger.DEBUG or abs (hyb) > 1e-10:
        vj, vk = mc._scf.get_jk (dm=dm1s)
        vj = vj[0] + vj[1]
    else:
        vj = mc._scf.get_j (dm=dm1)
    Te_Vne = np.tensordot (h, dm1)

    # (vj_a + vj_b) * (dm_a + dm_b)
    E_j = np.tensordot (vj, dm1) / 2
    # (vk_a * dm_a) + (vk_b * dm_b) Mind the difference!
    if ot.verbose >= logger.DEBUG or abs (hyb) > 1e-10:
        E_x = -(np.tensordot (vk[0], dm1s[0]) + np.tensordot (vk[1], dm1s[1])) / 2
    else:
        E_x = 0

    logger.debug (ot, 'CAS energy decomposition:')
    logger.debug (ot, 'Vnn = %s', Vnn)
    logger.debug (ot, 'Te + Vne = %s', Te_Vne)
    logger.debug (ot, 'E_j = %s', E_j)
    logger.debug (ot, 'E_x = %s', E_x)
#    if ot.verbose >= logger.DEBUG:
#        # g_pqrs * l_pqrs / 2
#        #if ot.verbose >= logger.DEBUG:
#        aeri = ao2mo.restore (1, mc.get_h2eff (mc.mo_coeff), mc.ncas)
#        E_c = np.tensordot (aeri, adm2, axes=4) / 2
#        E_c_ss = np.tensordot (aeri, adm2s_ss, axes=4) / 2
#        E_c_os = np.tensordot (aeri, adm2s_os, axes=4) # ab + ba -> factor of 2
#        logger.info (ot, 'E_c = %s', E_c)
#        logger.info (ot, 'E_c (SS) = %s', E_c_ss)
#        logger.info (ot, 'E_c (OS) = %s', E_c_os)
#        e_err = E_c_ss + E_c_os - E_c
#        assert (abs (e_err) < 1e-8), e_err
#        if isinstance (mc.e_tot, float):
#            e_err = mc.e_tot - (Vnn + Te_Vne + E_j + E_x + E_c)
#            assert (abs (e_err) < 1e-8), e_err
    if abs (hyb) > 1e-10:
        logger.debug (ot, 'Adding %s * %s CAS exchange to E_ot', hyb, E_x)
    t0 = logger.timer (ot, 'Vnn, Te, Vne, E_j, E_x', *t0)
    E_ot = get_E_ot (ot, dm1s, adm2, amo)
    t0 = logger.timer (ot, 'E_ot', *t0)
    e_tot = Vnn + Te_Vne + E_j + (hyb * E_x) + E_ot
    logger.info (ot, 'MC-PDFT E = %s, Eot(%s) = %s', e_tot, ot.otxc, E_ot)
    return e_tot, E_ot

def get_E_ot (ot, oneCDMs, twoCDM_amo, ao2amo, max_memory=20000, hermi=1):
    ni, xctype, dens_deriv = ot._numint, ot.xctype, ot.dens_deriv
    norbs_ao = ao2amo.shape[0]
    E_ot = 0.0
    t0 = (time.clock (), time.time ())
    make_rho = tuple (ni._gen_rho_evaluator (ot.mol, oneCDMs[i,:,:], hermi) for i in range(2))
    for ao, mask, weight, coords in ni.block_loop (ot.mol, ot.grids, norbs_ao, dens_deriv, max_memory):
        rho = np.asarray ([m[0] (0, ao, mask, xctype) for m in make_rho])
        if ot.verbose > logger.DEBUG and dens_deriv > 0:
            for ideriv in range (1,4):
                rho_test  = np.einsum ('ijk,aj,ak->ia', oneCDMs, ao[ideriv], ao[0])
                rho_test += np.einsum ('ijk,ak,aj->ia', oneCDMs, ao[ideriv], ao[0])
                logger.debug (ot, "Spin-density derivatives, |PySCF-einsum| = %s", linalg.norm (rho[:,ideriv,:]-rho_test))
        t0 = logger.timer (ot, 'untransformed density', *t0)
        Pi = get_ontop_pair_density (ot, rho, ao, oneCDMs, twoCDM_amo, ao2amo, dens_deriv)
        t0 = logger.timer (ot, 'on-top pair density calculation', *t0)
        E_ot += ot.get_E_ot (rho, Pi, weight)
        t0 = logger.timer (ot, 'on-top exchange-correlation energy calculation', *t0)
    return E_ot

