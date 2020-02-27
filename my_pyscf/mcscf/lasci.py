from pyscf.scf.rohf import get_roothaan_fock
from pyscf.mcscf import casci, casci_symm, df
from pyscf.tools import molden
from pyscf import symm, gto, scf, ao2mo, lib
from mrh.my_pyscf.fci.csfstring import CSFTransformer
from mrh.my_pyscf.fci import csf_solver
from mrh.my_pyscf.scf import hf_as
from mrh.my_pyscf.df.sparse_df import sparsedf_array
from itertools import combinations, product
from scipy.sparse import linalg as sparse_linalg
from scipy import linalg, special
import numpy as np
import time

# This must be locked to CSF solver for the forseeable future, because I know of no other way to handle spin-breaking potentials while retaining spin constraint
# There's a lot that will have to be checked in the future with spin-breaking stuff, especially if I still have the convention ms < 0, na <-> nb and h1e_s *= -1 

class LASCI_UnitaryGroupGenerators (object):
    ''' Object for packing (for root-finding algorithms) and unpacking (for direct manipulation)
    the nonredundant variables ('unitary group generators') of a LASCI problem. Selects nonredundant
    lower-triangular part ('x') of a skew-symmetric orbital rotation matrix ('kappa') and transforms
    CI transfer vectors between the determinant and configuration state function bases. Subclass me
    to apply point-group symmetry. '''

    def __init__(self, las, mo_coeff, ci):
        self.nmo = mo_coeff.shape[-1]
        self.frozen = las.frozen
        self.spin_sub = las.spin_sub
        self._init_orb (las, mo_coeff, ci)
        self._init_ci (las, mo_coeff, ci)

    def _init_orb (self, las, mo_coeff, ci):
        idx = np.zeros ((self.nmo, self.nmo), dtype=np.bool)
        sub_slice = np.cumsum ([0] + las.ncas_sub.tolist ()) + las.ncore
        idx[sub_slice[-1]:,:sub_slice[0]] = True
        for ix1, i in enumerate (sub_slice[:-1]):
            j = sub_slice[ix1+1]
            for ix2, k in enumerate (sub_slice[:ix1]):
                l = sub_slice[ix2+1]
                idx[i:j,k:l] = True
        if self.frozen is not None:
            idx[self.frozen,:] = idx[:,self.frozen] = False
        self.uniq_orb_idx = idx

    def _init_ci (self, las, mo_coeff, ci):
        self.ci_transformers = [CSFTransformer (norb, max (*nelec), min (*nelec), smult)
            for norb, nelec, smult in zip (las.ncas_sub, las.nelecas_sub, las.spin_sub)]

    def pack (self, kappa, ci_sub):
        x_orb = kappa[self.uniq_orb_idx]
        x_ci = np.concatenate ([transformer.vec_det2csf (ci, normalize=False)
            for transformer, ci in zip (self.ci_transformers, ci_sub)])
        return np.append (x_orb.ravel (), x_ci.ravel ())

    def unpack (self, x):
        kappa = np.zeros ((self.nmo, self.nmo), dtype=x.dtype)
        kappa[self.uniq_orb_idx] = x[:self.nvar_orb]
        kappa = kappa - kappa.T

        y = x[self.nvar_orb:]
        ci_sub = []
        for ncsf, transformer in zip (self.ncsf_sub, self.ci_transformers):
            ci_sub.append (transformer.vec_csf2det (y[:ncsf], normalize=False))
            y = y[ncsf:]

        return kappa, ci_sub

    @property
    def nvar_orb (self):
        return np.count_nonzero (self.uniq_orb_idx)

    @property
    def ncsf_sub (self):
        return [transformer.ncsf for transformer in self.ci_transformers]

    @property
    def nvar_tot (self):
        return self.nvar_orb + sum (self.ncsf_sub)

class LASCISymm_UnitaryGroupGenerators (LASCI_UnitaryGroupGenerators):
    def __init__(self, las, mo_coeff, ci, orbsym=None, wfnsym_sub=None):
        self.nmo = mo_coeff.shape[-1]
        self.frozen = las.frozen
        self.spin_sub = las.spin_sub
        if orbsym is None: orbsym = mo_coeff.orbsym
        if wfnsym_sub is None: wfnsym_sub = las.wfnsym_sub
        self._init_orb (las, mo_coeff, ci, orbsym, wfnsym_sub)
        self._init_ci (las, mo_coeff, ci, orbsym, wfnsym_sub)
    
    def _init_orb (self, las, mo_coeff, ci, orbsym, wfnsym_sub):
        super()._init_orb (las, mo_coeff, ci)
        orbsym = mo_coeff.orbsym
        self.symm_forbid = (orbsym[:,None] ^ orbsym[None,:]).astype (np.bool_)
        self.uniq_orb_idx[self.symm_forbid] = False

    def _init_ci (self, las, mo_coeff, ci, orbsym, wfnsym_sub):
        sub_slice = np.cumsum ([0] + las.ncas_sub.tolist ()) + las.ncore
        orbsym_sub = [orbsym[i:sub_slice[isub+1]] for isub, i in enumerate (sub_slice[:-1])]
        self.ci_transformers = [CSFTransformer (norb, nelec[0], nelec[1], smult, orbsym=orbsym_i, wfnsym=wfnsym)
            for norb, nelec, smult, orbsym_i, wfnsym in zip (las.ncas_sub, las.nelecas_sub, las.spin_sub,
            orbsym_sub, wfnsym_sub)]

def LASCI (mf_or_mol, ncas_sub, nelecas_sub, **kwargs):
    if isinstance(mf_or_mol, gto.Mole):
        mf = scf.RHF(mf_or_mol)
    else:
        mf = mf_or_mol
    if mf.mol.symmetry: 
        las = LASCISymm (mf, ncas_sub, nelecas_sub, **kwargs)
    else:
        las = LASCINoSymm (mf, ncas_sub, nelecas_sub, **kwargs)
    if getattr (mf, 'with_df', None):
        las = density_fit (las, with_df = mf.with_df) 
    return las

class _DFLASCI: # Tag
    pass

def get_grad (las, ugg=None, mo_coeff=None, ci=None, fock=None, h1eff_sub=None, h2eff_sub=None, veff=None, dm1s=None):
    ''' Return energy gradient for 1) inactive-external orbital rotation and 2) CI relaxation.
    Eventually to include 3) intersubspace orbital rotation. '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci
    if ugg is None: ugg = las.get_ugg (las, mo_coeff, ci)
    if dm1s is None: dm1s = las.make_rdm1s (mo_coeff=mo_coeff, ci=ci)
    if h2eff_sub is None: h2eff_sub = las.get_h2eff (mo_coeff)
    if veff is None:
        veff = las.get_veff (dm1s = dm1s.sum (0))
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci)
    if h1eff_sub is None: h1eff_sub = las.get_h1eff (mo_coeff, ci=ci, veff=veff, h2eff_sub=h2eff_sub)
    nao, nmo = mo_coeff.shape
    ncore = las.ncore
    ncas = las.ncas
    nocc = las.ncore + las.ncas
    nvirt = nmo - nocc

    # The orbrot part
    h1s = las.get_hcore ()[None,:,:] + veff
    f1 = h1s[0] @ dm1s[0] + h1s[1] @ dm1s[1]
    f1 = mo_coeff.conjugate ().T @ f1 @ las._scf.get_ovlp () @ mo_coeff # <- I need the ovlp there to get dm1s back into its correct basis
    casdm2_sub = las.make_casdm2_sub (ci=ci)
    eri = h2eff_sub.reshape (nmo, ncas, ncas*(ncas+1)//2)
    eri = lib.numpy_helper.unpack_tril (eri).reshape (nmo, ncas, ncas, ncas)
    for isub, (ncas, casdm2) in enumerate (zip (las.ncas_sub, casdm2_sub)):
        i = ncore + sum (las.ncas_sub[:isub])
        j = i + ncas
        smo = las._scf.get_ovlp () @ mo_coeff[:,i:j]
        smoH = smo.conjugate ().T
        casdm1s = [smoH @ d @ smo for d in dm1s]
        casdm1 = casdm1s[0] + casdm1s[1]
        dm1_outer = np.multiply.outer (casdm1, casdm1)
        dm1_outer -= sum ([np.multiply.outer (d,d).transpose (0,3,2,1) for d in casdm1s])
        casdm2 -= dm1_outer
        k = i - ncore
        l = j - ncore
        f1[:,i:j] += np.tensordot (eri[:,k:l,k:l,k:l], casdm2, axes=((1,2,3),(1,2,3)))
    gorb = f1 - f1.T

    # Split into internal and external parts
    idx = np.zeros (gorb.shape, dtype=np.bool_)
    idx[ncore:nocc,:ncore] = True
    idx[nocc:,ncore:nocc] = True
    if isinstance (ugg, LASCISymm_UnitaryGroupGenerators):
        idx[ugg.symm_forbid] = False
    gx = gorb[idx]

    # The CI part
    gci = []
    for isub, (h1eff, ci0, ncas, nelecas) in enumerate (zip (h1eff_sub, ci, las.ncas_sub, las.nelecas_sub)):
        eri_cas = las.get_h2eff_slice (h2eff_sub, isub, compact=8)
        max_memory = max(400, las.max_memory-lib.current_memory()[0])
        h1eff_c = (h1eff[0] + h1eff[1]) / 2
        h1eff_s = (h1eff[0] - h1eff[1]) / 2
        nel = nelecas
        # CI solver has enforced convention: na >= nb
        if nelecas[0] < nelecas[1]:
            nel = (nel[1], nel[0])
            h1eff_s *= -1
        h1e = (h1eff_c, h1eff_s)
        if getattr(las.fcisolver, 'gen_linkstr', None):
            linkstrl = las.fcisolver.gen_linkstr(ncas, nelecas, True)
            linkstr  = las.fcisolver.gen_linkstr(ncas, nelecas, False)
        else:
            linkstrl = linkstr  = None
        h2eff = las.fcisolver.absorb_h1e(h1e, eri_cas, ncas, nelecas, .5)
        hc0 = las.fcisolver.contract_2e(h2eff, ci0, ncas, nelecas, link_index=linkstrl).ravel()
        ci0 = ci0.ravel ()
        eci0 = ci0.dot(hc0)
        gci.append ((hc0 - ci0 * eci0).ravel ())

    gint = ugg.pack (gorb, gci)
    gorb = gint[:ugg.nvar_orb]
    gci = gint[ugg.nvar_orb:]
    return gorb, gci, gx.ravel ()

def density_fit (las, auxbasis=None, with_df=None):
    ''' Here I ONLY need to attach the tag and the df object because I put conditionals in LASCINoSymm to make my life easier '''
    las_class = las.__class__
    if with_df is None:
        if (getattr(las._scf, 'with_df', None) and
            (auxbasis is None or auxbasis == las._scf.with_df.auxbasis)):
            with_df = las._scf.with_df
        else:
            with_df = df.DF(las.mol)
            with_df.max_memory = las.max_memory
            with_df.stdout = las.stdout
            with_df.verbose = las.verbose
            with_df.auxbasis = auxbasis
    class DFLASCI (las_class, _DFLASCI):
        def __init__(self, my_las):
            self.__dict__.update(my_las.__dict__)
            #self.grad_update_dep = 0
            self.with_df = with_df
            self._keys = self._keys.union(['with_df'])
    return DFLASCI (las)

def h1e_for_cas (las, mo_coeff=None, ncas=None, ncore=None, nelecas=None, ci=None, ncas_sub=None, nelecas_sub=None, spin_sub=None, veff=None, h2eff_sub=None, casdm1s_sub=None, veff_sub_test=None):
    ''' Effective one-body Hamiltonians (plural) for a LASCI problem

    Args:
        las: a LASCI object

    Kwargs:
        mo_coeff: ndarray of shape (nao,nmo)
            Orbital coefficients ordered on the columns as: 
            core orbitals, subspace 1, subspace 2, ..., external orbitals
        ncas: integer
            As in PySCF's existing CASCI/CASSCF implementation
        nelecas: sequence of 2 integers
            As in PySCF's existing CASCI/CASSCF implementation
        ci: list of ndarrays of length (nsub)
            CI coefficients
            used to generate 1-RDMs in active subspaces; overrides casdm0_sub
        ncas_sub: ndarray of shape (nsub)
            Number of active orbitals in each subspace
        nelecas_sub: ndarray of shape (nsub,2)
            na, nb in each subspace
        spin_sub: ndarray of shape (nsub)
            Total spin quantum numbers in each subspace
        veff: ndarray of shape (2, nao, nao)
            If you precalculated this, pass it to save on calls to get_jk

    Returns:
        h1e: list like [ndarray of shape (2, isub, isub) for isub in ncas_sub]
            Spin-separated 1-body Hamiltonian operator for each active subspace
    '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ncas is None: ncas = las.ncas
    if ncore is None: ncore = las.ncore
    if ncas_sub is None: ncas_sub = las.ncas_sub
    if nelecas_sub is None: nelecas_sub = las.nelecas_sub
    if spin_sub is None: spin_sub = las.spin_sub
    if ncore is None: ncore = las.ncore
    if ci is None: ci = las.ci
    if h2eff_sub is None: h2eff_sub = las.get_h2eff (mo_coeff)
    if casdm1s_sub is None: casdm1s_sub = las.make_casdm1s_sub (ci=ci)
    if veff is None:
        veff = las.get_veff (dm1s = las.make_rdm1 (mo_coeff=mo_coeff, ci=ci))
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci, casdm1s_sub=casdm1s_sub)
    
    mo_cas = [las.get_mo_slice (idx, mo_coeff) for idx in range (len (ncas_sub))]
    moH_cas = [mo.conjugate ().T for mo in mo_cas]
    h1e = las.get_hcore ()[None,:,:] + veff # JK of inactive orbitals
    # Subtract double-counting
    h2e_sub = [las.get_h2eff_slice (h2eff_sub, ix) for ix, ncas in enumerate (ncas_sub)]
    j_sub = [np.tensordot (casdm1s, h2e, axes=((1,2),(2,3))) for casdm1s, h2e in zip (casdm1s_sub, h2e_sub)]
    k_sub = [np.tensordot (casdm1s, h2e, axes=((1,2),(2,1))) for casdm1s, h2e in zip (casdm1s_sub, h2e_sub)]
    veff_sub = [j[0][None,:,:] + j[1][None,:,:] - k for j, k in zip (j_sub, k_sub)]
    h1e_sub = [np.tensordot (moH, np.dot (h1e, mo), axes=((1),(1))).transpose (1,0,2) - v
        for moH, mo, v in zip (moH_cas, mo_cas, veff_sub)]
    return h1e_sub

def kernel (las, mo_coeff=None, ci0=None, casdm0_sub=None, conv_tol_grad=1e-5, verbose=lib.logger.NOTE):
#conv_tol_grad=1e-4 was tightened by riddhish
    if mo_coeff is None: mo_coeff = las.mo_coeff
    log = lib.logger.new_logger(las, verbose)
    t0 = (time.clock(), time.time())
    log.debug('Start LASCI')

    h2eff_sub = las.get_h2eff (mo_coeff)
    t1 = log.timer('integral transformation to LAS space', *t0)

    # In the first cycle, I may pass casdm0_sub instead of ci0. Therefore, I need to work out this get_veff call separately.
    if ci0 is not None:
        veff = las.get_veff (dm1s = las.make_rdm1 (mo_coeff=mo_coeff, ci=ci0))
        casdm1s_sub = las.make_casdm1s_sub (ci=ci0)
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci0, casdm1s_sub=casdm1s_sub)
    elif casdm0_sub is not None:
        dm1_core = mo_coeff[:,:las.ncore] @ mo_coeff[:,:las.ncore].conjugate ().T
        dm1s_sub = [np.stack ([dm1_core, dm1_core], axis=0)]
        for idx, casdm1s in enumerate (casdm0_sub):
            mo = las.get_mo_slice (idx, mo_coeff=mo_coeff)
            moH = mo.conjugate ().T
            dm1s_sub.append (np.tensordot (mo, np.dot (casdm1s, moH), axes=((1),(1))).transpose (1,0,2))
        dm1s_sub = np.stack (dm1s_sub, axis=0)
        dm1s = dm1s_sub.sum (0)
        veff = las.get_veff (dm1s=dm1s.sum (0))
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, casdm1s_sub=casdm0_sub)
        casdm1s_sub = casdm0_sub
    t1 = log.timer('LASCI initial get_veff', *t1)

    ugg = None
    converged = False
    for it in range (las.max_cycle_macro):
        e_cas, ci1 = ci_cycle (las, mo_coeff, ci0, veff, h2eff_sub, casdm1s_sub, log)
        if ugg is None: ugg = las.get_ugg (las, mo_coeff, ci1)
        log.info ('LASCI subspace CI energies: {}'.format (e_cas))
        t1 = log.timer ('LASCI ci_cycle', *t1)

        veff = veff.sum (0)/2
        casdm1s_new = las.make_casdm1s_sub (ci=ci1)
        if not isinstance (las, _DFLASCI) or las.verbose > lib.logger.DEBUG:
            #veff = las.get_veff (mo_coeff=mo_coeff, ci=ci1)
            veff_new = las.get_veff (dm1s = las.make_rdm1 (mo_coeff=mo_coeff, ci=ci1))
            if not isinstance (las, _DFLASCI): veff = veff_new
        if isinstance (las, _DFLASCI):
            dcasdm1s = [dm_new - dm_old for dm_new, dm_old in zip (casdm1s_new, casdm1s_sub)]
            veff += las.fast_veffa (dcasdm1s, h2eff_sub, mo_coeff=mo_coeff, ci=ci1) 
            if las.verbose > lib.logger.DEBUG:
                errmat = veff - veff_new
                lib.logger.debug (las, 'fast_veffa error: {}'.format (linalg.norm (errmat)))
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci1)
        casdm1s_sub = casdm1s_new

        t1 = log.timer ('LASCI get_veff after ci', *t1)
        H_op = LASCI_HessianOperator (las, ugg, mo_coeff=mo_coeff, ci=ci1, h2eff_sub=h2eff_sub, veff=veff)
        g_vec = H_op.get_grad ()
        gx = H_op.get_gx ()
        prec_op = H_op.get_prec ()
        prec = prec_op (np.ones_like (g_vec)) # Check for divergences
        norm_gorb = linalg.norm (g_vec[:ugg.nvar_orb]) if ugg.nvar_orb else 0.0
        norm_gci = linalg.norm (g_vec[ugg.nvar_orb:]) if sum (ugg.ncsf_sub) else 0.0
        norm_gx = linalg.norm (gx) if gx.size else 0.0
        x0 = prec_op._matvec (-g_vec)
        norm_xorb = linalg.norm (x0[:ugg.nvar_orb]) if ugg.nvar_orb else 0.0
        norm_xci = linalg.norm (x0[ugg.nvar_orb:]) if sum (ugg.ncsf_sub) else 0.0
        lib.logger.info (las, 'LASCI macro %d : E = %.15g ; |g_int| = %.15g ; |g_ci| = %.15g ; |x_orb| = %.15g ; |x_ci| = %.15g', it, H_op.e_tot, norm_gorb, norm_gci, norm_xorb, norm_xci)
        #log.info ('LASCI micro init : E = %.15g ; |g_orb| = %.15g ; |g_ci| = %.15g ; |x0_orb| = %.15g ; |x0_ci| = %.15g',
        #    H_op.e_tot, norm_gorb, norm_gci, norm_xorb, norm_xci)
        if (norm_gorb < conv_tol_grad and norm_gci < conv_tol_grad) or ((norm_gorb + norm_gci) < norm_gx/10):
            converged = True
            break
        H_op._init_df () # Take this part out of the true initialization b/c if I'm already converged I don't want to waste the cycles
        t1 = log.timer ('LASCI Hessian constructor', *t1)
        microit = [0]
        def my_callback (x):
            microit[0] += 1
            norm_xorb = linalg.norm (x[:ugg.nvar_orb]) if ugg.nvar_orb else 0.0
            norm_xci = linalg.norm (x[ugg.nvar_orb:]) if sum (ugg.ncsf_sub) else 0.0
            if las.verbose > lib.logger.INFO:
                Hx = H_op._matvec (x) # This doubles the price of each iteration!!
                resid = g_vec + Hx
                norm_gorb = linalg.norm (resid[:ugg.nvar_orb]) if ugg.nvar_orb else 0.0
                norm_gci = linalg.norm (resid[ugg.nvar_orb:]) if sum (ugg.ncsf_sub) else 0.0
                Ecall = H_op.e_tot + x.dot (g_vec + (Hx/2))
                log.info ('LASCI micro %d : E = %.15g ; |g_orb| = %.15g ; |g_ci| = %.15g ; |x_orb| = %.15g ; |x_ci| = %.15g', microit[0], Ecall, norm_gorb, norm_gci, norm_xorb, norm_xci)
            else:
                log.info ('LASCI micro %d : |x_orb| = %.15g ; |x_ci| = %.15g', microit[0], norm_xorb, norm_xci)

        my_tol = max (conv_tol_grad, norm_gx/10)
        x, info_int = sparse_linalg.cg (H_op, -g_vec, x0=x0, tol=my_tol, maxiter=las.max_cycle_micro,
         callback=my_callback, M=prec_op)  ##RP atol was changed to tol as python 3.6.3 did not recognize the keyword tol
        t1 = log.timer ('LASCI {} microcycles'.format (microit[0]), *t1)
        mo_coeff, ci1, h2eff_sub = H_op.update_mo_ci_eri (x, h2eff_sub)
        casdm1s_sub = las.make_casdm1s_sub (ci=ci1)
        t1 = log.timer ('LASCI Hessian update', *t1)

        #veff = las.get_veff (mo_coeff=mo_coeff, ci=ci1)
        veff = las.get_veff (dm1s = las.make_rdm1 (mo_coeff=mo_coeff, ci=ci1))
        veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci1)
        t1 = log.timer ('LASCI get_veff after secondorder', *t1)

    e_tot = las.energy_nuc () + las.energy_elec (mo_coeff=mo_coeff, ci=ci1, h2eff=h2eff_sub, veff=veff)
    # I need the true veff, with f^a_a and f^i_i spin-separated, in order to use the Hessian properly later on
    # Better to do it here with bmPu than in localintegrals
    veff_a = las.fast_veffa (casdm1s_sub, h2eff_sub, mo_coeff=mo_coeff, ci=ci1, _full=True)
    veff_c = (veff.sum (0) - veff_a.sum (0))/2 # veff's spin-summed component should be correct because I called get_veff with spin-summed rdm
    veff = veff_c[None,:,:] + veff_a 

    lib.logger.info (las, 'LASCI %s after %d cycles', ('not converged', 'converged')[converged], it+1)
    lib.logger.info (las, 'LASCI E = %.15g ; |g_int| = %.15g ; |g_ci| = %.15g ; |g_ext| = %.15g', e_tot, norm_gorb, norm_gci, norm_gx)
    t1 = log.timer ('LASCI wrap-up', *t1)
    mo_coeff, mo_energy, mo_occ, ci1, h2eff_sub = las.canonicalize (mo_coeff, ci1, veff, h2eff_sub)
    t1 = log.timer ('LASCI canonicalization', *t1)
    veff = lib.tag_array (veff, veff_c=veff_c)
    return converged, e_tot, mo_energy, mo_coeff, e_cas, ci1, h2eff_sub, veff

def ci_cycle (las, mo, ci0, veff, h2eff_sub, casdm1s_sub, log, veff_sub_test=None):
    if ci0 is None: ci0 = [None for idx in range (len (las.ncas_sub))]
    # CI problems
    t1 = (time.clock(), time.time())
    h1eff_sub = las.get_h1eff (mo, veff=veff, h2eff_sub=h2eff_sub, casdm1s_sub=casdm1s_sub, veff_sub_test=veff_sub_test)
    ncas_cum = np.cumsum ([0] + las.ncas_sub.tolist ()) + las.ncore
    e_cas = []
    ci1 = []
    e0 = 0.0 
    for isub, (ncas, nelecas, spin, h1eff, fcivec) in enumerate (zip (las.ncas_sub, las.nelecas_sub, las.spin_sub, h1eff_sub, ci0)):
        eri_cas = las.get_h2eff_slice (h2eff_sub, isub, compact=8)
        max_memory = max(400, las.max_memory-lib.current_memory()[0])
        h1eff_c = (h1eff[0] + h1eff[1]) / 2
        h1eff_s = (h1eff[0] - h1eff[1]) / 2
        nel = nelecas
        # CI solver has enforced convention: na >= nb
        if nelecas[0] < nelecas[1]:
            nel = (nel[1], nel[0])
            h1eff_s *= -1
        h1e = (h1eff_c, h1eff_s)
        wfnsym = orbsym = None
        if hasattr (las, 'wfnsym_sub') and hasattr (mo, 'orbsym'):
            wfnsym = las.wfnsym_sub[isub]
            i = ncas_cum[isub]
            j = ncas_cum[isub+1]
            orbsym = mo.orbsym[i:j]
            wfnsym_str = wfnsym if isinstance (wfnsym, str) else symm.irrep_id2name (las.mol.groupname, wfnsym)
            log.info ("LASCI subspace {} with irrep {}".format (isub, wfnsym_str))
        e_sub, fcivec = las.fcisolver.kernel(h1e, eri_cas, ncas, nel,
                                               ci0=fcivec, verbose=log,
                                               max_memory=max_memory,
                                               ecore=e0, smult=spin,
                                               wfnsym=wfnsym, orbsym=orbsym)
        e_cas.append (e_sub)
        ci1.append (fcivec)
        t1 = log.timer ('FCI solver for subspace {}'.format (isub), *t1)
    return e_cas, ci1

def get_fock (las, mo_coeff=None, ci=None, eris=None, casdm1s=None, verbose=None, veff=None, dm1s=None):
    ''' f_pq = h_pq + (g_pqrs - g_psrq/2) D_rs, AO basis
    Note the difference between this and h1e_for_cas: h1e_for_cas only has
    JK terms from electrons outside the "current" active subspace; get_fock
    includes JK from all electrons. This is also NOT the "generalized Fock matrix"
    of orbital gradients (but it can be used in calculating those if you do a
    semi-cumulant decomposition).
    The "eris" kwarg does not do anything and is retained only for backwards
    compatibility (also why I don't just call las.make_rdm1) '''
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci
    if casdm1s is None: casdm1s = las.make_casdm1s (ci=ci)
    if dm1s is None:
        mo_cas = mo_coeff[:,las.ncore:][:,:las.ncas]
        moH_cas = mo_cas.conjugate ().T
        mo_core = mo_coeff[:,:las.ncore]
        moH_core = mo_core.conjugate ().T
        dm1s = [(mo_core @ moH_core) + (mo_cas @ d @ moH_cas) for d in list(casdm1s)]
    if veff is not None:
        fock = las.get_hcore()[None,:,:] + veff
        return get_roothaan_fock (fock, dm1s, las._scf.get_ovlp ())
    dm1 = dm1s[0] + dm1s[1]
    if isinstance (las, _DFLASCI):
        vj, vk = las.with_df.get_jk(dm1, hermi=1)
    else:
        vj, vk = las._scf.get_jk(las.mol, dm1, hermi=1)
    fock = las.get_hcore () + vj - (vk/2)
    return fock

def canonicalize (las, mo_coeff=None, ci=None, veff=None, h2eff_sub=None, orbsym=None):
    if mo_coeff is None: mo_coeff = las.mo_coeff
    if ci is None: ci = las.ci

    nao, nmo = mo_coeff.shape
    ncore = las.ncore
    nocc = ncore + las.ncas
    ncas_sub = las.ncas_sub
    nelecas_sub = las.nelecas_sub

    '''
    orbsym = None
    if isinstance (las, LASCISymm):
        print ("This is the first call to label_orb_symm inside of canonicalize")
        orbsym = symm.label_orb_symm (las.mol, las.mol.irrep_id,
                                      las.mol.symm_orb, mo_coeff,
                                      s=las._scf.get_ovlp ())
        #mo_coeff = casci_symm.label_symmetry_(las, mo_coeff, None)
        #orbsym = mo_coeff.orbsym
    '''
    casdm1s_sub = las.make_casdm1s_sub (ci=ci)
    umat = np.zeros_like (mo_coeff)
    dm1s = np.stack ([np.eye (nmo), np.eye (nmo)], axis=0)
    casdm1s = np.stack ([linalg.block_diag (*[dm[0] for dm in casdm1s_sub]),
                         linalg.block_diag (*[dm[1] for dm in casdm1s_sub])], axis=0)
    fock = mo_coeff.conjugate ().T @ las.get_fock (mo_coeff=mo_coeff, casdm1s=casdm1s, veff=veff) @ mo_coeff
    casdm1_sub = [dm[0] + dm[1] for dm in casdm1s_sub]
    # Inactive-inactive
    orbsym_i = None if orbsym is None else orbsym[:ncore]
    fock_i = fock[:ncore,:ncore]
    ene, umat[:ncore,:ncore] = las._eig (fock_i, 0, 0, orbsym_i)
    idx = np.argsort (ene)
    umat[:ncore,:ncore] = umat[:ncore,:ncore][:,idx]
    if orbsym_i is not None: orbsym[:ncore] = orbsym[:ncore][idx]
    # Active-active
    for isub, (lasdm1, ncas, nelecas, ci_i) in enumerate (zip (casdm1_sub, ncas_sub, nelecas_sub, ci)):
        i = sum (ncas_sub[:isub]) + ncore
        j = i + ncas
        orbsym_i = None if orbsym is None else orbsym[i:j]
        occ, umat[i:j,i:j] = las._eig (lasdm1, 0, 0, orbsym_i)
        idx = np.argsort (occ)[::-1]
        umat[i:j,i:j] = umat[i:j,i:j][:,idx]
        if orbsym_i is not None: orbsym[ncore:][i:j] = orbsym[ncore:][i:j][idx]
        # CI solver enforced convention na >= nb
        nel = nelecas
        if nelecas[1] > nelecas[0]:
            nel = (nelecas[1], nelecas[0])

        norb = las.ncas_sub[isub]
        nelec = las.nelecas_sub[isub]
        ndeta = special.comb (norb, max(nelec[0],nelec[1]) , exact=True) ##RP taking max,min because sometimes beta electrons are more than the alphs (antiferro magnetic fragments). In this case the shaping needs to be different 
        ndetb = special.comb (norb, min(nelec[0],nelec[1]), exact=True)
        ci_i = ci_i.reshape ( ndeta, ndetb )   ###Riddhish added this reshaping on 01/29/20
        ci[isub] = las.fcisolver.transform_ci_for_orbital_rotation (ci_i, ncas, nel, umat[i:j,i:j])
    # External-external
    orbsym_i = None if orbsym is None else orbsym[nocc:]
    fock_i = fock[nocc:,nocc:]
    ene, umat[nocc:,nocc:] = las._eig (fock_i, 0, 0, orbsym_i)
    idx = np.argsort (ene)
    umat[nocc:,nocc:] = umat[nocc:,nocc:][:,idx]
    if orbsym_i is not None: orbsym[nocc:] = orbsym[nocc:][idx]
    # Final
    mo_occ = np.zeros (nmo, dtype=ene.dtype)
    mo_occ[:ncore] = 2
    ucas = umat[ncore:nocc,ncore:nocc]
    mo_occ[ncore:nocc] = ((casdm1s.sum (0) @ ucas) * ucas).sum (0)
    mo_ene = umat.conjugate ().T @ fock @ umat
    mo_coeff = mo_coeff @ umat
    if orbsym is not None:
        '''
        print ("This is the second call to label_orb_symm inside of canonicalize") 
        orbsym = symm.label_orb_symm (las.mol, las.mol.irrep_id,
                                      las.mol.symm_orb, mo_coeff,
                                      s=las._scf.get_ovlp ())
        #mo_coeff = las.label_symmetry_(mo_coeff)
        '''
        mo_coeff = lib.tag_array (mo_coeff, orbsym=orbsym)
    if h2eff_sub is not None:
        h2eff_sub = lib.numpy_helper.unpack_tril (h2eff_sub.reshape (nmo*las.ncas, -1)).reshape (nmo, las.ncas, las.ncas, las.ncas)
        h2eff_sub = np.tensordot (umat, h2eff_sub, axes=((0),(0)))
        h2eff_sub = np.tensordot (ucas, h2eff_sub, axes=((0),(1))).transpose (1,0,2,3)
        h2eff_sub = np.tensordot (ucas, h2eff_sub, axes=((0),(2))).transpose (1,2,0,3)
        h2eff_sub = np.tensordot (ucas, h2eff_sub, axes=((0),(3))).transpose (1,2,3,0)
        h2eff_sub = lib.numpy_helper.pack_tril (h2eff_sub.reshape (nmo*las.ncas, las.ncas, las.ncas)).reshape (nmo, -1)
    return mo_coeff, mo_ene, mo_occ, ci, h2eff_sub


class LASCINoSymm (casci.CASCI):

    def __init__(self, mf, ncas, nelecas, ncore=None, spin_sub=None, frozen=None, **kwargs):
        ncas_tot = sum (ncas)
        nel_tot = [0, 0]
        for nel in nelecas:
            if isinstance (nel, (int, np.integer)):
                nb = nel // 2
                na = nb + (nel % 2)
            else:
                na, nb = nel
            nel_tot[0] += na
            nel_tot[1] += nb
        super().__init__(mf, ncas=ncas_tot, nelecas=nel_tot, ncore=ncore)
        if spin_sub is None: spin_sub = [0 for sub in ncas]
        self.ncas_sub = np.asarray (ncas)
        self.nelecas_sub = np.asarray (nelecas)
        self.spin_sub = np.asarray (spin_sub)
        self.frozen = frozen
        self.conv_tol_grad = 1e-5  ##Tightened by Riddhish form 1e-4
        self.ah_level_shift = 1e-8
        self.max_cycle_macro = 10## changed from 50 because generally it either takes a few or doesnt converge
        self.max_cycle_micro = 5
        keys = set(('ncas_sub', 'nelecas_sub', 'spin_sub', 'conv_tol_grad', 'max_cycle_macro', 'max_cycle_micro', 'ah_level_shift'))
        self._keys = set(self.__dict__.keys()).union(keys)
        self.fcisolver = csf_solver (self.mol, smult=0)

    def get_mo_slice (self, idx, mo_coeff=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        mo = mo_coeff[:,self.ncore:]
        for offs in self.ncas_sub[:idx]:
            mo = mo[:,offs:]
        mo = mo[:,:self.ncas_sub[idx]]
        return mo

    def ao2mo (self, mo_coeff=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        nao, nmo = mo_coeff.shape
        ncore, ncas = self.ncore, self.ncas
        nocc = ncore + ncas
        mo_cas = mo_coeff[:,ncore:nocc]
        mo = [mo_coeff, mo_cas, mo_cas, mo_cas]
        if getattr (self, 'with_df', None) is not None:
            # Store intermediate with one contracted ao index for faster calculation of exchange corrections!
            bPmn = sparsedf_array (self.with_df._cderi)
            bmuP = bPmn.contract1 (mo_cas)
            buvP = np.tensordot (mo_cas.conjugate (), bmuP, axes=((0),(0)))
            eri_muxy = np.tensordot (bmuP, buvP, axes=((2),(2)))
            eri = lib.pack_tril (np.tensordot (mo_coeff.conjugate (), eri_muxy, axes=((0),(0))).reshape (nmo*ncas, ncas, ncas)).reshape (nmo, -1)
            eri = lib.tag_array (eri, bmPu=bmuP.transpose (0,2,1))
            if self.verbose > lib.logger.DEBUG:
                eri_comp = self.with_df.ao2mo (mo, compact=True)
                lib.logger.debug (self, "CDERI two-step error: {}".format (linalg.norm (eri-eri_comp)))
        elif getattr (self._scf, '_eri', None) is not None:
            eri = ao2mo.incore.general (self._scf._eri, mo, compact=True).reshape (nmo, -1)
        else:
            eri = ao2mo.outcore.general_iofree (self.mol, mo, compact=True).reshape (nmo, -1)
        return eri

    def get_h2eff_slice (self, h2eff, idx, compact=None):
        ncas_cum = np.cumsum ([0] + self.ncas_sub.tolist ())
        i = ncas_cum[idx] 
        j = ncas_cum[idx+1]
        ncore = self.ncore
        nocc = ncore + self.ncas
        eri = h2eff[ncore:nocc,:].reshape (self.ncas*self.ncas, -1)
        ix_i, ix_j = np.tril_indices (self.ncas)
        eri = eri[(ix_i*self.ncas)+ix_j,:]
        eri = ao2mo.restore (1, eri, self.ncas)[i:j,i:j,i:j,i:j]
        if compact: eri = ao2mo.restore (compact, eri, j-i)
        return eri

    get_h1eff = get_h1cas = h1e_for_cas = h1e_for_cas
    get_h2eff = ao2mo
    '''
    def get_h2eff (self, mo_coeff=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if isinstance (self, _DFLASCI):
            mo_cas = mo_coeff[:,self.ncore:][:,:self.ncas]
            return self.with_df.ao2mo (mo_cas)
        return self.ao2mo (mo_coeff)
    '''

    get_fock = get_fock
    get_grad = get_grad
    canonicalize = canonicalize

    def kernel(self, mo_coeff=None, ci0=None, casdm0_sub=None, conv_tol_grad=None, verbose=None):
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        else:
            self.mo_coeff = mo_coeff
        if ci0 is None: ci0 = self.ci
        if verbose is None: verbose = self.verbose
        if conv_tol_grad is None: conv_tol_grad = self.conv_tol_grad
        log = lib.logger.new_logger(self, verbose)

        if self.verbose >= lib.logger.WARN:
            self.check_sanity()
        self.dump_flags(log)

        self.converged, self.e_tot, self.mo_energy, self.mo_coeff, self.e_cas, self.ci, h2eff_sub, veff = \
                kernel(self, mo_coeff, ci0=ci0, verbose=verbose, casdm0_sub=casdm0_sub, conv_tol_grad=conv_tol_grad)

        '''
        if self.canonicalization:
            self.canonicalize_(mo_coeff, self.ci,
                               sort=self.sorting_mo_energy,
                               cas_natorb=self.natorb, verbose=log)

        if getattr(self.fcisolver, 'converged', None) is not None:
            self.converged = np.all(self.fcisolver.converged)
            if self.converged:
                log.info('CASCI converged')
            else:
                log.info('CASCI not converged')
        else:
            self.converged = True
        self._finalize()
        '''
        return self.e_tot, self.e_cas, self.ci, self.mo_coeff, self.mo_energy, h2eff_sub, veff

    def make_casdm1s_sub (self, ci=None, ncas_sub=None, nelecas_sub=None, **kwargs):
        ''' Spin-separated 1-RDMs in the MO basis for each subspace in sequence '''
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if ci is None:
            return [np.zeros ((2,ncas,ncas)) for ncas in ncas_sub]
        casdm1s = []
        for idx, (ci_i, ncas, nelecas) in enumerate (zip (ci, ncas_sub, nelecas_sub)):
            if ci_i is None:
                dm1a = dm1b = np.zeros ((ncas, ncas))
            else: 
                # CI solver enforced convention na >= nb 
                nel = (nelecas[1], nelecas[0]) if nelecas[1] > nelecas[0] else nelecas
                dm1a, dm1b = self.fcisolver.make_rdm1s (ci_i, ncas, nel)
                if nelecas[1] > nelecas[0]: dm1a, dm1b = dm1b, dm1a
            casdm1s.append (np.stack ([dm1a, dm1b], axis=0))
        return casdm1s

    def make_casdm2_sub (self, ci=None, ncas_sub=None, nelecas_sub=None, **kwargs):
        ''' Spin-separated 1-RDMs in the MO basis for each subspace in sequence '''
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        casdm2 = []
        for idx, (ci_i, ncas, nelecas) in enumerate (zip (ci, ncas_sub, nelecas_sub)):
            nel = (nelecas[1], nelecas[0]) if nelecas[1] > nelecas[0] else nelecas
            casdm2.append (self.fcisolver.make_rdm2 (ci_i, ncas, nel))
        return casdm2

    def make_rdm1s_sub (self, mo_coeff=None, ci=None, ncas_sub=None, nelecas_sub=None, include_core=False, **kwargs):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        ''' Same as make_casdm1s_sub, but in the ao basis '''
        casdm1s_sub = self.make_casdm1s_sub (ci=ci, ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, **kwargs)
        rdm1s = []
        for idx, casdm1s in enumerate (casdm1s_sub):
            mo = self.get_mo_slice (idx, mo_coeff=mo_coeff)
            moH = mo.conjugate ().T
            rdm1s.append (np.tensordot (mo, np.dot (casdm1s, moH), axes=((1),(1))).transpose (1,0,2))
        if include_core and self.ncore:
            mo_core = mo_coeff[:,:self.ncore]
            moH_core = mo_core.conjugate ().T
            dm_core = mo_core @ moH_core
            rdm1s = [np.stack ([dm_core, dm_core], axis=0)] + rdm1s
        rdm1s = np.stack (rdm1s, axis=0)
        return rdm1s

    def make_rdm1_sub (self, **kwargs):
        return self.make_rdm1s_sub (**kwargs).sum (1)

    def make_rdm1s (self, mo_coeff=None, ncore=None, **kwargs):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ncore is None: ncore = self.ncore
        mo = mo_coeff[:,:ncore]
        moH = mo.conjugate ().T
        dm_core = mo @ moH
        dm_cas = self.make_rdm1s_sub (mo_coeff=mo_coeff, **kwargs).sum (0)
        return dm_core[None,:,:] + dm_cas

    def make_rdm1 (self, **kwargs):
        return self.make_rdm1s (**kwargs).sum (0)


    def make_casdm1s (self, **kwargs):
        ''' Make the full-dimensional casdm1s spanning the collective active space '''
        casdm1s_sub = self.make_casdm1s_sub (**kwargs)
        casdm1a = linalg.block_diag (*[dm[0] for dm in casdm1s_sub])
        casdm1b = linalg.block_diag (*[dm[1] for dm in casdm1s_sub])
        return np.stack ([casdm1a, casdm1b], axis=0)

    def make_casdm1 (self, **kwargs):
        ''' Spin-sum make_casdm1s '''
        return self.make_casdm1s (**kwargs).sum (0)

    def make_casdm2 (self, ci=None, ncas_sub=None, nelecas_sub=None, **kwargs):
        ''' Make the full-dimensional casdm2 spanning the collective active space '''
        if ci is None: ci = self.ci
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        ncas = sum (ncas_sub)
        ncas_cum = np.cumsum ([0] + ncas_sub.tolist ())
        casdm2s_sub = self.make_casdm2_sub (ci=ci, ncas_sub=ncas_sub, nelecas_sub=nelecas_sub, **kwargs)
        casdm2 = np.zeros ((ncas,ncas,ncas,ncas))
        # Diagonal 
        for isub, dm2 in enumerate (casdm2s_sub):
            i = ncas_cum[isub]
            j = ncas_cum[isub+1]
            casdm2[i:j, i:j, i:j, i:j] = dm2
        # Off-diagonal
        casdm1s_sub = self.make_casdm1s_sub (ci=ci)
        for (isub1, dm1s1), (isub2, dm1s2) in combinations (enumerate (casdm1s_sub), 2):
            i = ncas_cum[isub1]
            j = ncas_cum[isub1+1]
            k = ncas_cum[isub2]
            l = ncas_cum[isub2+1]
            dma1, dmb1 = dm1s1[0], dm1s1[1]
            dma2, dmb2 = dm1s2[0], dm1s2[1]
            # Coulomb slice
            casdm2[i:j, i:j, k:l, k:l] = np.multiply.outer (dma1+dmb1, dma2+dmb2)
            casdm2[k:l, k:l, i:j, i:j] = casdm2[i:j, i:j, k:l, k:l].transpose (2,3,0,1)
            # Exchange slice
            casdm2[i:j, k:l, k:l, i:j] = -(np.multiply.outer (dma1, dma2) + np.multiply.outer (dmb1, dmb2)).transpose (0,3,2,1)
            casdm2[k:l, i:j, i:j, k:l] = casdm2[i:j, k:l, k:l, i:j].transpose (1,0,3,2)
        return casdm2 

    def get_veff (self, mol=None, dm1s=None, hermi=1, spin_sep=False, **kwargs):
        ''' Returns a spin-summed veff! If dm1s isn't provided, builds from self.mo_coeff, self.ci etc. '''
        if mol is None: mol = self.mol
        nao = mol.nao_nr ()
        if dm1s is None: dm1s = self.make_rdm1 (include_core=True, **kwargs).reshape (nao, nao)
        dm1s = np.asarray (dm1s)
        if dm1s.ndim == 2: dm1s = dm1s[None,:,:]
        if isinstance (self, _DFLASCI):
            vj, vk = self.with_df.get_jk(dm1s, hermi=hermi)
        else:
            vj, vk = self._scf.get_jk(mol, dm1s, hermi=hermi)
        if spin_sep:
            assert (dm1s.shape[0] == 2)
            return vj.sum (0)[None,:,:] - vk
        else:
            veff = np.stack ([j - k/2 for j, k in zip (vj, vk)], axis=0)
            return np.squeeze (veff)

    def split_veff (self, veff, h2eff_sub, mo_coeff=None, ci=None, casdm1s_sub=None):
        ''' Split a spin-summed veff into alpha and beta terms using the h2eff eri array.
        Note that this will omit v(up_active - down_active)^virtual_inactive by necessity; 
        this won't affect anything because the inactive density matrix has no spin component.
        On the other hand, it ~is~ necessary to correctly do v(up_active - down_active)^unactive_active
        in order to calculate the external orbital gradient at the end of the calculation.
        This means that I need h2eff_sub spanning both at least two active subspaces
        ~and~ the full orbital range. '''
        veff_c = veff.copy ()
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        if casdm1s_sub is None: casdm1s_sub = self.make_casdm1s_sub (ci = ci)
        ncore = self.ncore
        ncas = self.ncas
        nocc = ncore + ncas
        nao, nmo = mo_coeff.shape
        moH_coeff = mo_coeff.conjugate ().T
        smo_coeff = self._scf.get_ovlp () @ mo_coeff
        smoH_coeff = smo_coeff.conjugate ().T
        veff_s = np.zeros_like (veff_c)
        for ix, (ncas_i, casdm1s) in enumerate (zip (self.ncas_sub, casdm1s_sub)):
            i = sum (self.ncas_sub[:ix])
            j = i + ncas_i
            eri_k = h2eff_sub.reshape (nmo, ncas, -1)[:,i:j,...].reshape (nmo*ncas_i, -1)
            eri_k = lib.numpy_helper.unpack_tril (eri_k)[:,i:j,:].reshape (nmo, ncas_i, ncas_i, ncas)
            sdm = casdm1s[0] - casdm1s[1]
            vk_pa = -np.tensordot (eri_k, sdm, axes=((1,2),(0,1))) / 2
            veff_s[:,ncore:nocc] += vk_pa
            veff_s[ncore:nocc,:] += vk_pa.T
            veff_s[ncore:nocc,ncore:nocc] -= vk_pa[ncore:nocc,:] / 2
            veff_s[ncore:nocc,ncore:nocc] -= vk_pa[ncore:nocc,:].T / 2
        veff_s = smo_coeff @ veff_s @ smoH_coeff
        veffa = veff_c + veff_s
        veffb = veff_c - veff_s
        return np.stack ([veffa, veffb], axis=0)
         

    def energy_elec (self, mo_coeff=None, ncore=None, ncas=None, ncas_sub=None, nelecas_sub=None, ci=None, h2eff=None, veff=None, **kwargs):
        ''' Since the LASCI energy cannot be calculated as simply as ecas + ecore, I need this function '''
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ncore is None: ncore = self.ncore
        if ncas is None: ncas = self.ncas
        if ncas_sub is None: ncas_sub = self.ncas_sub
        if nelecas_sub is None: nelecas_sub = self.nelecas_sub
        if ci is None: ci = self.ci
        if h2eff is None: h2eff = self.get_h2eff (mo_coeff)
        casdm1s_sub = self.make_casdm1s_sub (ci=ci, ncas_sub=ncas_sub, nelecas_sub=nelecas_sub)
        if veff is None:
            veff = self.get_veff (dm1s = self.make_rdm1 (mo_coeff=mo_coeff, ci=ci))
            veff = self.split_veff (veff, h2eff, mo_coeff=mo_coeff, ci=ci, casdm1s_sub=casdm1s_sub)

        # 1-body veff terms
        h1e = self.get_hcore ()[None,:,:] + veff/2
        dm1s = self.make_rdm1s (mo_coeff=mo_coeff, ncore=ncore, ci=ci, ncas_sub=ncas_sub, nelecas_sub=nelecas_sub)
        energy_elec = e1 = np.dot (h1e.ravel (), dm1s.ravel ())
        # 2-body cumulant terms
        casdm2_sub = self.make_casdm2_sub (ci=ci, ncas_sub=ncas_sub, nelecas_sub=nelecas_sub)
        e2 = 0
        for isub, (dm1s, dm2) in enumerate (zip (casdm1s_sub, casdm2_sub)):
            dm1a, dm1b = dm1s[0], dm1s[1]
            dm1 = dm1a + dm1b
            cdm2 = dm2 - np.multiply.outer (dm1, dm1)
            cdm2 += np.multiply.outer (dm1a, dm1a).transpose (0,3,2,1)
            cdm2 += np.multiply.outer (dm1b, dm1b).transpose (0,3,2,1)
            eri = self.get_h2eff_slice (h2eff, isub)
            te2 = np.tensordot (eri, cdm2, axes=4) / 2
            energy_elec += te2
            e2 += te2
        e0 = self.energy_nuc ()
        return energy_elec

    get_ugg = LASCI_UnitaryGroupGenerators

    def cderi_ao2mo (self, mo_i, mo_j, compact=False):
        assert (isinstance (self, _DFLASCI))
        nmo_i, nmo_j = mo_i.shape[-1], mo_j.shape[-1]
        if compact:
            assert (nmo_i == nmo_j)
            bPij = np.empty ((self.with_df.get_naoaux (), nmo_i*(nmo_i+1)//2), dtype=mo_i.dtype)
        else:
            bPij = np.empty ((self.with_df.get_naoaux (), nmo_i, nmo_j), dtype=mo_i.dtype)
        ijmosym, mij_pair, moij, ijslice = ao2mo.incore._conc_mos (mo_i, mo_j, compact=compact)
        b0 = 0
        for eri1 in self.with_df.loop ():
            b1 = b0 + eri1.shape[0]
            eri2 = bPij[b0:b1]
            eri2 = ao2mo._ao2mo.nr_e2 (eri1, moij, ijslice, aosym='s2', mosym=ijmosym, out=eri2)
            b0 = b1
        return bPij

    def fast_veffa (self, casdm1s_sub, h2eff_sub, mo_coeff=None, ci=None, _full=False):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        assert (isinstance (self, _DFLASCI) or _full)
        ncore = self.ncore
        ncas_sub = self.ncas_sub
        ncas = sum (ncas_sub)
        nocc = ncore + ncas
        nao, nmo = mo_coeff.shape

        mo_cas = mo_coeff[:,ncore:nocc]
        moH_cas = mo_cas.conjugate ().T
        moH_coeff = mo_coeff.conjugate ().T
        dma = linalg.block_diag (*[dm[0] for dm in casdm1s_sub])
        dmb = linalg.block_diag (*[dm[1] for dm in casdm1s_sub])
        casdm1s = np.stack ([dma, dmb], axis=0)
        if not (isinstance (self, _DFLASCI)):
            dm1s = np.dot (mo_cas, np.dot (casdm1s, moH_cas)).transpose (1,0,2)
            return self.get_veff (dm1s = dm1s, spin_sep=True)
        casdm1 = casdm1s.sum (0)
        dm1 = np.dot (mo_cas, np.dot (casdm1, moH_cas))
        bPmn = sparsedf_array (self.with_df._cderi)

        # vj
        dm_tril = dm1 + dm1.T - np.diag (np.diag (dm1.T))
        rho = np.dot (bPmn, lib.pack_tril (dm_tril))
        vj = lib.unpack_tril (np.dot (rho, bPmn))

        # vk
        bmPu = h2eff_sub.bmPu
        if _full:
            vmPsu = np.dot (bmPu, casdm1s)
            vk = np.tensordot (vmPsu, bmPu, axes=((1,3),(1,2))).transpose (1,0,2)
            return vj[None,:,:] - vk
        else:
            vmPu = np.dot (bmPu, casdm1)
            vk = np.tensordot (vmPu, bmPu, axes=((1,2),(1,2)))
            return vj - vk/2

class LASCISymm (casci_symm.CASCI, LASCINoSymm):

    def __init__(self, mf, ncas, nelecas, ncore=None, spin_sub=None, wfnsym_sub=None, frozen=None, **kwargs):
        LASCINoSymm.__init__(self, mf, ncas, nelecas, ncore=ncore, spin_sub=spin_sub, frozen=frozen, **kwargs)
        if wfnsym_sub is None: wfnsym_sub = [0 for icas in self.ncas_sub]
        self.wfnsym_sub = wfnsym_sub
        keys = set(('wfnsym_sub'))
        self._keys = set(self.__dict__.keys()).union(keys)

    make_rdm1s = LASCINoSymm.make_rdm1s
    make_rdm1 = LASCINoSymm.make_rdm1
    get_veff = LASCINoSymm.get_veff
    get_h1eff = get_h1cas = h1e_for_cas 
    get_ugg = LASCISymm_UnitaryGroupGenerators

    @property
    def wfnsym (self):
        ''' This now returns the product of the irreps of the subspaces '''
        wfnsym = 0
        for ir in self.wfnsym_sub: wfnsym ^= ir
        return wfnsym
    @wfnsym.setter
    def wfnsym (self, ir):
        raise RuntimeError ("Cannot assign the whole-system symmetry of a LASCI wave function. Address the individual subspaces at lasci.wfnsym_sub instead.")

    def kernel(self, mo_coeff=None, ci0=None, casdm0_sub=None, verbose=None):
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        if ci0 is None:
            ci0 = self.ci

        # Initialize/overwrite mo_coeff.orbsym. Don't pass ci0 because it's not the right shape
        lib.logger.info (self, "LASCI lazy hack note: lines below reflect the point-group symmetry of the whole molecule but not of the individual subspaces")
        self.fcisolver.wfnsym = self.wfnsym
        '''
        print ("This is the pre-calculation call to label_orb_symm")
        orbsym = symm.label_orb_symm (self.mol, self.mol.irrep_id,
                                      self.mol.symm_orb, mo_coeff,
                                      s=self._scf.get_ovlp ())
        '''
        mo_coeff = self.mo_coeff = self.label_symmetry_(mo_coeff)
        #mo_coeff = self.mo_coeff = lib.tag_array (mo_coeff, orbsym=orbsym) #casci_symm.label_symmetry_(self, mo_coeff, None)
        return LASCINoSymm.kernel(self, mo_coeff=mo_coeff, ci0=ci0, casdm0_sub=casdm0_sub, verbose=verbose)

    def canonicalize (self, mo_coeff=None, ci=None, veff=None, h2eff_sub=None):
        if mo_coeff is None: mo_coeff = self.mo_coeff
        mo_coeff = self.label_symmetry_(mo_coeff)
        return canonicalize (self, mo_coeff=mo_coeff, ci=ci, h2eff_sub=h2eff_sub, orbsym=mo_coeff.orbsym)

    def label_symmetry_(self, mo_coeff=None):
        if mo_coeff is None: mo_coeff=self.mo_coeff
        ncore = self.ncore
        ncas_sub = self.ncas_sub
        nocc = ncore + sum (ncas_sub)
        mo_coeff[:,:ncore] = symm.symmetrize_space (self.mol, mo_coeff[:,:ncore])
        for isub, ncas in enumerate (ncas_sub):
            i = ncore + sum (ncas_sub[:isub])
            j = i + ncas
            mo_coeff[:,i:j] = symm.symmetrize_space (self.mol, mo_coeff[:,i:j])
        mo_coeff[:,nocc:] = symm.symmetrize_space (self.mol, mo_coeff[:,nocc:])
        orbsym = symm.label_orb_symm (self.mol, self.mol.irrep_id,
                                      self.mol.symm_orb, mo_coeff,
                                      s=self._scf.get_ovlp ())
        mo_coeff = lib.tag_array (mo_coeff, orbsym=orbsym)
        return mo_coeff
        

        
class LASCI_HessianOperator (sparse_linalg.LinearOperator):

    def __init__(self, las, ugg, mo_coeff=None, ci=None, ncore=None, ncas_sub=None, nelecas_sub=None, h2eff_sub=None, veff=None):
        if mo_coeff is None: mo_coeff = las.mo_coeff
        if ci is None: ci = las.ci
        if ncore is None: ncore = las.ncore
        if ncas_sub is None: ncas_sub = las.ncas_sub
        if nelecas_sub is None: nelecas_sub = las.nelecas_sub
        self.las = las
        self.ah_level_shift = las.ah_level_shift
        self.ugg = ugg
        self.mo_coeff = mo_coeff
        self.ci = ci = [c.ravel () for c in ci]
        self.ncore = ncore
        self.ncas_sub = ncas_sub
        self.nelecas_sub = nelecas_sub
        self.ncas = ncas = sum (ncas_sub)
        self.nao = nao = mo_coeff.shape[0]
        self.nmo = nmo = mo_coeff.shape[-1]
        self.nocc = nocc = ncore + ncas
        self.fcisolver = las.fcisolver
        self.bPpj = None

        # Density matrices
        self.casdm1s_sub = las.make_casdm1s_sub (ci=ci, ncas_sub=ncas_sub, nelecas_sub=nelecas_sub)
        casdm1a = linalg.block_diag (*[dm[0] for dm in self.casdm1s_sub])
        casdm1b = linalg.block_diag (*[dm[1] for dm in self.casdm1s_sub])
        casdm1 = casdm1a + casdm1b
        self.casdm2 = las.make_casdm2 (ci=ci, ncas_sub=ncas_sub, nelecas_sub=nelecas_sub)
        self.casdm2c = self.casdm2 - np.multiply.outer (casdm1, casdm1)
        self.casdm2c += np.multiply.outer (casdm1a, casdm1a).transpose (0,3,2,1)
        self.casdm2c += np.multiply.outer (casdm1b, casdm1b).transpose (0,3,2,1)
        self.dm1s = np.stack ([np.eye (self.nmo, dtype=self.dtype), np.eye (self.nmo, dtype=self.dtype)], axis=0)
        self.dm1s[0,ncore:nocc,ncore:nocc] = casdm1a
        self.dm1s[1,ncore:nocc,ncore:nocc] = casdm1b
        self.dm1s[:,nocc:,nocc:] = 0


        # ERI in active superspace and fixed (within macrocycle) veff-related things
        # h1e_ab is for gradient response
        # h1e_ab_sub is for ci response
        if h2eff_sub is None: h2eff_sub = las.get_h2eff (mo_coeff)
        moH_coeff = mo_coeff.conjugate ().T
        if veff is None: 
            self._init_df ()
            if isinstance (las, _DFLASCI):
                # Can't use this module's get_veff because here I need to have f_aa and f_ii correctly
                # On the other hand, I know that dm1s spans only the occupied orbitals
                rho = np.tensordot (self.bPpj[:,:nocc,:], self.dm1s[:,:nocc,:nocc].sum (0))
                vj_ao = np.zeros (nao*(nao+1)//2, dtype=rho.dtype)
                b0 = 0
                for eri1 in self.with_df.loop ():
                    b1 = b0 + eri1.shape[0]
                    vj_ao += np.dot (rho[b0:b1], eri1)
                    b0 = b1
                vj_mo = moH_coeff @ lib.unpack_tril (vj_ao) @ mo_coeff
                vPpi = self.bPpj[:,:,:ncore] * np.sqrt (2.0)
                no_occ, no_coeff = linalg.eigh (casdm1)
                no_occ[no_occ<0] = 0.0
                no_coeff *= np.sqrt (no_occ)[None,:]
                vPpu = np.dot (self.bPpj[:,:,ncore:nocc], no_coeff)
                vPpj = np.append (vPpi, vPpu, axis=2)
                vk_mo = np.tensordot (vPpj, vPpj, axes=((0,2),(0,2)))
                smo = las._scf.get_ovlp () @ mo_coeff
                smoH = smo.conjugate ().T
                veff = smo @ (vj_mo - vk_mo/2) @ smoH
            else:
                veff = las.get_veff (dm1s = np.dot (mo_coeff, np.dot (self.dm1s.sum (0), moH_coeff)))
            veff = las.split_veff (veff, h2eff_sub, mo_coeff=mo_coeff, ci=ci, casdm1s_sub=self.casdm1s_sub)
        h2eff_sub = lib.numpy_helper.unpack_tril (h2eff_sub.reshape (nmo*ncas, ncas*(ncas+1)//2)).reshape (nmo, ncas, ncas, ncas)
        self.eri_cas = h2eff_sub[ncore:nocc,:,:,:]
        h1e_ab = las.get_hcore ()[None,:,:] + veff
        h1e_ab = np.dot (h1e_ab, mo_coeff)
        self.h1e_ab = np.dot (moH_coeff, h1e_ab).transpose (1,0,2)
        self.h1e_ab_sub = np.stack ([self.h1e_ab[:,ncore:nocc,ncore:nocc].copy (),] * len (self.casdm1s_sub), axis=0)
        for ix, casdm1s in enumerate (self.casdm1s_sub):
            i = sum (ncas_sub[:ix])
            j = i + ncas_sub[ix]
            casdm1 = casdm1s[0] + casdm1s[1]
            self.h1e_ab_sub[ix,:,:,:] -= np.tensordot (casdm1,
                self.eri_cas[i:j,i:j,:,:], axes=2)[None,:,:] # double-counting: J
            self.h1e_ab_sub[ix,:,:,:] += np.tensordot (casdm1s,
                self.eri_cas[:,i:j,i:j,:], axes=((1,2),(2,1))) # double-counting: K

        # Fock1 matrix (for gradient and subtrahend terms in Hx)
        self.fock1 = sum ([f @ d for f,d in zip (list (self.h1e_ab), list (self.dm1s))])
        self.fock1[:,ncore:nocc] += np.tensordot (h2eff_sub, self.casdm2c, axes=((1,2,3),(1,2,3)))

        # Total energy (for callback)
        h1 = (self.h1e_ab + (moH_coeff @ las.get_hcore () @ mo_coeff)[None,:,:]) / 2
        self.e_tot = las.energy_nuc () + np.dot (h1.ravel (), self.dm1s.ravel ()) + np.tensordot (self.eri_cas, self.casdm2c, axes=4) / 2

        # CI stuff
        if getattr(self.fcisolver, 'gen_linkstr', None):
            self.linkstrl = [self.fcisolver.gen_linkstr(no, ne, True) for no, ne in zip (ncas_sub, nelecas_sub)]
            self.linkstr  = [self.fcisolver.gen_linkstr(no, ne, False) for no, ne in zip (ncas_sub, nelecas_sub)]
        else:
            self.linkstrl = self.linkstr  = None
        self.hci0 = self.Hci_all ([0.0,] * len (ci), self.h1e_ab_sub, self.eri_cas, ci)
        self.e0 = [hc.dot (c) for hc, c in zip (self.hci0, ci)]
        self.hci0 = [hc - c*e for hc, c, e in zip (self.hci0, ci, self.e0)]


        # That should be everything!

    def _init_df (self):
        if isinstance (self.las, _DFLASCI):
            self.with_df = self.las.with_df
            if self.bPpj is None: self.bPpj = np.ascontiguousarray (
                self.las.cderi_ao2mo (self.mo_coeff, self.mo_coeff[:,:self.nocc],
                compact=False))

    @property
    def dtype (self):
        return self.mo_coeff.dtype

    @property
    def shape (self):
        return ((self.ugg.nvar_tot, self.ugg.nvar_tot))

    def Hci (self, no, ne, h0e, h1e_ab, h2e, ci, linkstrl=None):
        h1e_cs = ((h1e_ab[0] + h1e_ab[1])/2,
                  (h1e_ab[0] - h1e_ab[1])/2)
        # CI solver has enforced convention na >= nb
        if ne[1] > ne[0]:
            h1e_cs = (h1e_cs[0], -h1e_cs[1])
            ne = (ne[1], ne[0])
        h = self.fcisolver.absorb_h1e (h1e_cs, h2e, no, ne, 0.5)
        hc = self.fcisolver.contract_2e (h, ci, no, ne, link_index=linkstrl).ravel ()
        return hc

    def Hci_all (self, h0e_sub, h1e_ab_sub, h2e, ci_sub):
        ''' Assumes h2e is in the active superspace MO basis and h1e_ab_sub is in the full MO basis '''
        hc = []
        for isub, (h0e, h1e_ab, ci) in enumerate (zip (h0e_sub, h1e_ab_sub, ci_sub)):
            if self.linkstrl is not None: linkstrl = self.linkstrl[isub]
            ncas = self.ncas_sub[isub]
            nelecas = self.nelecas_sub[isub]
            i = sum (self.ncas_sub[:isub])
            j = i + ncas
            h2e_i = h2e[i:j,i:j,i:j,i:j]
            h1e_i = h1e_ab[:,i:j,i:j]
            hc.append (self.Hci (ncas, nelecas, h0e, h1e_i, h2e_i, ci, linkstrl=linkstrl))
        return hc

    def make_odm1s2c_sub (self, kappa):
        odm1s_sub = np.zeros ((len (self.ci)+1, 2, self.nmo, self.nmo), dtype=self.dtype)
        odm2c_sub = np.zeros ([len (self.ci)] + [self.ncas,]*4, dtype=self.dtype)
        odm1s_sub[0,:,self.nocc:,:self.ncore] = kappa[self.nocc:,:self.ncore]
        for isub, (ncas, casdm1s) in enumerate (zip (self.ncas_sub, self.casdm1s_sub)):
            i = self.ncore + sum (self.ncas_sub[:isub])
            j = i + ncas
            odm1s_sub[isub+1,:,i:j,:] -= np.dot (casdm1s, kappa[i:j,:])
            k = i - self.ncore
            l = j - self.ncore
            casdm2c = self.casdm2c[k:l,k:l,k:l,k:l]
            odm2c_sub[isub,k:l,k:l,k:l,:] -= np.dot (casdm2c, kappa[i:j,self.ncore:self.nocc])
        odm1s_sub += odm1s_sub.transpose (0,1,3,2) 
        odm2c_sub += odm2c_sub.transpose (0,2,1,4,3)        
        odm2c_sub += odm2c_sub.transpose (0,3,4,1,2)        

        return odm1s_sub, odm2c_sub    

    def make_tdm1s2c_sub (self, ci1):
        tdm1s_sub = np.zeros ((len (ci1), 2, self.ncas, self.ncas), dtype=self.dtype)
        tdm2c_sub = np.zeros ([len (ci1)] + [self.ncas,]*4, dtype=self.dtype)
        for isub, (ncas, nelecas, c1, c0, casdm1s) in enumerate (
          zip (self.ncas_sub, self.nelecas_sub, ci1, self.ci, self.casdm1s_sub)):
            s01 = c1.dot (c0)
            i = sum (self.ncas_sub[:isub])
            j = i + ncas
            casdm2 = self.casdm2[i:j,i:j,i:j,i:j]
            linkstr = None if self.linkstr is None else self.linkstr[isub]
            # CI solver enforced convention na >= nb
            nel = (nelecas[1], nelecas[0]) if nelecas[1] >= nelecas[0] else nelecas
            tdm1s, tdm2c = self.fcisolver.trans_rdm12s (c1, c0, ncas, nel, link_index=linkstr)
            if nelecas[1] > nelecas[0]: tdm1s = (tdm1s[1], tdm1s[0])
            # Subtrahend: super important, otherwise the veff part of CI response is even more of a nightmare
            # With this in place, I don't have to worry about subtracting an overlap times a gradient
            tdm1s = np.stack (tdm1s, axis=0)
            tdm1s -= casdm1s * s01
            tdm2c = (sum (tdm2c) - (casdm2*s01)) / 2 
            # Cumulant decomposition so I only have to do one jk call for orbrot response
            # The only rules are 1) the sectors that you think are zero must really be zero, and
            #                    2) you subtract here what you add later
            tdm2c -= np.multiply.outer (tdm1s[0] + tdm1s[1], casdm1s[0] + casdm1s[1])
            tdm2c += np.multiply.outer (tdm1s[0], casdm1s[0]).transpose (0,3,2,1)
            tdm2c += np.multiply.outer (tdm1s[1], casdm1s[1]).transpose (0,3,2,1)
            tdm1s_sub[isub,:,i:j,i:j] = tdm1s
            tdm2c_sub[isub,i:j,i:j,i:j,i:j] = tdm2c

        # Two transposes 
        tdm1s_sub += tdm1s_sub.transpose (0,1,3,2) 
        tdm2c_sub += tdm2c_sub.transpose (0,2,1,4,3)        
        tdm2c_sub += tdm2c_sub.transpose (0,3,4,1,2)        

        return tdm1s_sub, tdm2c_sub    

    def get_veff_Heff (self, odm1s_sub, tdm1s_sub):
        ''' Returns the veff for the orbital part and the h1e shifts for the CI part arising from the contraction
        of shifted or 'effective' 1-rdms in the two sectors with the Hamiltonian. Return values do not include
        veffs with the external indices rotated (i.e., in the CI part). Uses the cached eris for the latter in the hope that
        this is faster than calling get_jk with many dms. '''

        dm1s_mo = odm1s_sub.copy ().sum (0)
        dm1s_mo[:,self.ncore:self.nocc,self.ncore:self.nocc] += tdm1s_sub.sum (0)
        mo = self.mo_coeff
        moH = mo.conjugate ().T

        # Overall veff for gradient: the one and only jk call per microcycle that I will allow.
        veff_mo = self.get_veff (dm1s_mo=dm1s_mo)
        veff_mo = self.split_veff (veff_mo, dm1s_mo)
        
        # SO, individual CI problems!
        # 1) There is NO constant term. Constant terms immediately drop out via the unitary group generator definition!
        # 2) veff_mo has the effect I want for the orbrots, so long as I choose not to explicitly add h.c. at the end
        # 3) If I don't add h.c., then the (non-self) mean-field effect of the 1-tdms needs to be twice as strong
        # 4) Of course, self-interaction (from both 1-odms and 1-tdms) needs to be completely eliminated
        h1e_ab_sub = np.stack ([veff_mo[:,self.ncore:self.nocc,self.ncore:self.nocc].copy (),]*tdm1s_sub.shape[0], axis=0)
        for isub, (tdm1s, odm1s) in enumerate (zip (tdm1s_sub, odm1s_sub[1:])):
            err_dm1s = (2*tdm1s) - tdm1s_sub.sum (0)
            err_dm1s += odm1s[:,self.ncore:self.nocc,self.ncore:self.nocc]
            err_veff = np.tensordot (err_dm1s, self.eri_cas, axes=((1,2),(2,3)))
            err_veff += err_veff[::-1] # ja + jb
            err_veff -= np.tensordot (err_dm1s, self.eri_cas, axes=((1,2),(2,1))) 
            h1e_ab_sub[isub,:,:,:] -= err_veff

        return veff_mo, h1e_ab_sub

    def get_veff (self, dm1s_mo=None):
        mo = self.mo_coeff
        moH = mo.conjugate ().T
        nmo = mo.shape[-1]
        dm1_mo = dm1s_mo.sum (0)
        if getattr (self, 'bPpj', None) is None:
            dm1_ao = np.dot (mo, np.dot (dm1_mo, moH))
            veff_ao = np.squeeze (self.las.get_veff (dm1s=dm1_ao))
            return np.dot (moH, np.dot (veff_ao, mo)) 
        ncore, nocc, ncas = self.ncore, self.nocc, self.ncas
        # vj
        t0 = (time.clock (), time.time ())
        veff_mo = np.zeros_like (dm1_mo)
        dm1_rect = dm1_mo + dm1_mo.T
        dm1_rect[ncore:nocc,ncore:nocc] /= 2
        dm1_rect = dm1_rect[:,:nocc]
        rho = np.tensordot (self.bPpj, dm1_rect, axes=2)
        vj_pj = np.tensordot (rho, self.bPpj, axes=((0),(0)))
        t1 = lib.logger.timer (self.las, 'vj_mo in microcycle', *t0)
        dm_bj = dm1_mo[ncore:,:nocc]
        vPpj = np.ascontiguousarray (self.las.cderi_ao2mo (mo, mo[:,ncore:] @ dm_bj, compact=False))
        # Don't ask my why this is faster than doing the two degrees of freedom separately...
        t1 = lib.logger.timer (self.las, 'vk_mo vPpj in microcycle', *t1)
        # vk (aa|ii), (uv|xy), (ua|iv), (au|vi)
        vPbj = vPpj[:,ncore:,:] #np.dot (self.bPpq[:,ncore:,ncore:], dm_ai)
        vk_bj = np.tensordot (vPbj, self.bPpj[:,:nocc,:], axes=((0,2),(0,1)))
        t1 = lib.logger.timer (self.las, 'vk_mo (bb|jj) in microcycle', *t1)
        # vk (ai|ai), (ui|av)
        dm_ai = dm1_mo[nocc:,:ncore]
        vPji = vPpj[:,:nocc,:ncore] #np.dot (self.bPpq[:,:nocc, nocc:], dm_ai)
        # I think this works only because there is no dm_ui in this case, so I've eliminated all the dm_uv by choosing this range
        bPbi = self.bPpj[:,ncore:,:ncore]
        vk_bj += np.tensordot (bPbi, vPji, axes=((0,2),(0,2)))
        t1 = lib.logger.timer (self.las, 'vk_mo (bi|aj) in microcycle', *t1)
        # veff
        vj_bj = vj_pj[ncore:,:]
        vj_ai = vj_bj[ncas:,:ncore]
        vk_ai = vk_bj[ncas:,:ncore]
        veff_mo[ncore:,:nocc] = vj_bj
        veff_mo[:ncore,nocc:] = vj_ai.T
        veff_mo[ncore:,:nocc] -= vk_bj/2
        veff_mo[:ncore,nocc:] -= vk_ai.T/2
        return veff_mo

    def split_veff (self, veff_mo, dm1s_mo):
        veff_c = veff_mo.copy ()
        ncore = self.ncore
        nocc = self.nocc
        dm1s_cas = dm1s_mo[:,ncore:nocc,ncore:nocc]
        sdm = dm1s_cas[0] - dm1s_cas[1]
        vk_aa = -np.tensordot (self.eri_cas, sdm, axes=((1,2),(0,1))) / 2
        veff_s = np.zeros_like (veff_c)
        veff_s[ncore:nocc, ncore:nocc] = vk_aa
        veffa = veff_c + veff_s
        veffb = veff_c - veff_s
        return np.stack ([veffa, veffb], axis=0)

    def _matvec (self, x):
        kappa1, ci1 = self.ugg.unpack (x)

        # Effective density matrices, veffs, and overlaps from linear response
        odm1s_sub, odm2c_sub = self.make_odm1s2c_sub (kappa1)
        tdm1s_sub, tdm2c_sub = self.make_tdm1s2c_sub (ci1)
        veff_prime, h1e_ab_prime = self.get_veff_Heff (odm1s_sub, tdm1s_sub)

        # Responses!
        kappa2 = self.orbital_response (odm1s_sub, odm2c_sub, tdm1s_sub, tdm2c_sub, veff_prime)
        ci2 = self.ci_response_offdiag (kappa1, h1e_ab_prime)
        ci2 = [x+y for x,y in zip (ci2, self.ci_response_diag (ci1))]

        return self.ugg.pack (kappa2, ci2)

    _rmatvec = _matvec # Hessian is Hermitian in this context!

    def orbital_response (self, odm1s_sub, odm2c_sub, tdm1s_sub, tdm2c_sub, veff_prime):
        ''' Formally, orbital response if F'_pq - F'_qp, F'_pq = h_pq D'_pq + g_prst d'_qrst.
        Applying the cumulant decomposition requires veff(D').D == veff'.D as well as veff.D'. '''
        ncore, nocc = self.ncore, self.nocc
        edm1s = odm1s_sub.sum (0)
        edm1s[:,ncore:nocc,ncore:nocc] += tdm1s_sub.sum (0)
        edm2c = odm2c_sub.sum (0) + tdm2c_sub.sum (0)
        fock1  = self.h1e_ab[0] @ edm1s[0] + self.h1e_ab[1] @ edm1s[1]
        fock1 += veff_prime[0] @ self.dm1s[0] + veff_prime[1] @ self.dm1s[1]
        fock1[ncore:nocc,ncore:nocc] += np.tensordot (self.eri_cas, edm2c, axes=((1,2,3),(1,2,3)))
        return fock1 - fock1.T

    def ci_response_offdiag (self, kappa1, h1e_ab_prime):
        ''' Rotate external indices with kappa1; add contributions from rotated internal indices
        and mean-field intersubspace response in h1e_ab_prime. I have set it up so that
        I do NOT add h.c. (multiply by 2) at the end. '''
        ncore, nocc = self.ncore, self.nocc
        kappa1_cas = kappa1[ncore:nocc, ncore:nocc]
        h1e_ab = np.dot (self.h1e_ab_sub, kappa1_cas)
        h1e_ab += h1e_ab.transpose (0,1,3,2)
        h2e = np.dot (self.eri_cas, kappa1_cas)
        h2e += h2e.transpose (2,3,0,1)
        h2e += h2e.transpose (1,0,3,2)
        h1e_ab += h1e_ab_prime
        Kci0 = self.Hci_all ([0.0,] * len (self.ci), h1e_ab, h2e, self.ci)
        Kci0 = [Kc - c*(c.dot (Kc)) for Kc, c in zip (Kci0, self.ci)]
        # ^ The definition of the unitary group generator compels you to do this always!!!
        return Kci0

    def ci_response_diag (self, ci1):
        ci1HmEci0 = [c.dot (Hci) for c, Hci in zip (ci1, self.hci0)]
        s01 = [c1.dot (c0) for c1,c0 in zip (ci1, self.ci)]
        ci2 = self.Hci_all ([-e for e in self.e0], self.h1e_ab_sub, self.eri_cas, ci1)
        ci2 = [x-(y*z) for x,y,z in zip (ci2, self.ci, ci1HmEci0)]
        ci2 = [x-(y*z) for x,y,z in zip (ci2, self.hci0, s01)]
        return [x*2 for x in ci2]

    def get_prec (self):
        fock = np.stack ([np.diag (h) for h in list (self.h1e_ab)], axis=0)
        num = np.stack ([np.diag (d) for d in list (self.dm1s)], axis=0)
        Horb_diag = sum ([np.multiply.outer (f,n) for f,n in zip (fock, num)])
        Horb_diag -= np.diag (self.fock1)[None,:]
        Horb_diag += Horb_diag.T
        # This is where I stop unless I want to add the split-c and split-x terms
        # Split-c and split-x, for inactive-external rotations, requires I calculate a bunch
        # of extra eris (g^aa_ii, g^ai_ai)
        Hci_diag = []
        for ix, (norb, nelec, smult, h1e_full, csf) in enumerate (zip (self.ncas_sub,
         self.nelecas_sub, self.ugg.spin_sub, self.h1e_ab_sub, self.ugg.ci_transformers)):
            i = sum (self.ncas_sub[:ix])
            j = i + norb
            h2e = self.eri_cas[i:j,i:j,i:j,i:j]
            h1e = ((h1e_full[0,i:j,i:j] + h1e_full[1,i:j,i:j])/2,
                   (h1e_full[0,i:j,i:j] - h1e_full[1,i:j,i:j])/2)
            # CI solver has enforced convention neleca >= nelecb
            ne = nelec
            if nelec[1] > nelec[0]:
                h1e = (h1e[0], -h1e[1])
                ne = (nelec[1], nelec[0])
            self.fcisolver.norb = norb
            self.fcisolver.nelec = ne
            self.fcisolver.smult = smult
            self.fcisolver.orbsym = csf.orbsym
            self.fcisolver.wfnsym = csf.wfnsym
            Hci_diag.append (csf.pack_csf (self.fcisolver.make_hdiag_csf (h1e, h2e, norb, ne)))
        Hdiag = np.concatenate ([Horb_diag[self.ugg.uniq_orb_idx]] + Hci_diag)
        Hdiag += self.ah_level_shift

        Hdiag[np.abs (Hdiag)<1e-8] = 1e-8
        return sparse_linalg.LinearOperator (self.shape, matvec=(lambda x:x/Hdiag), dtype=self.dtype)

    def update_mo_ci_eri (self, x, h2eff_sub):
        nmo, ncore, ncas, nocc = self.nmo, self.ncore, self.ncas, self.nocc
        kappa, dci = self.ugg.unpack (x)
        umat = linalg.expm (kappa)
        mo1 = self.mo_coeff @ umat
        ci1 = [c + dc for c,dc in zip (self.ci, dci)]
        norm_ci = [np.sqrt (c.dot (c)) for c in ci1]
        ci1 = [c/n for c,n in zip (ci1, norm_ci)]
        if hasattr (self.mo_coeff, 'orbsym'):
            mo1 = lib.tag_array (mo1, orbsym=self.mo_coeff.orbsym)
        ucas = umat[ncore:nocc, ncore:nocc]
        bmPu = None
        if hasattr (h2eff_sub, 'bmPu'): bmPu = h2eff_sub.bmPu
        h2eff_sub = h2eff_sub.reshape (nmo*ncas, ncas*(ncas+1)//2)
        h2eff_sub = lib.numpy_helper.unpack_tril (h2eff_sub)
        h2eff_sub = h2eff_sub.reshape (nmo, ncas, ncas, ncas)
        h2eff_sub = np.tensordot (ucas, h2eff_sub, axes=((0),(1))) # bpaa
        h2eff_sub = np.tensordot (umat, h2eff_sub, axes=((0),(1))) # qbaa
        h2eff_sub = np.tensordot (h2eff_sub, ucas, axes=((2),(0))) # qbab
        h2eff_sub = np.tensordot (h2eff_sub, ucas, axes=((2),(0))) # qbbb
        ix_i, ix_j = np.tril_indices (ncas)
        h2eff_sub = h2eff_sub.reshape (nmo, ncas, ncas*ncas)
        h2eff_sub = h2eff_sub[:,:,(ix_i*ncas)+ix_j]
        h2eff_sub = h2eff_sub.reshape (nmo, -1)
        if bmPu is not None:
            bmPu = np.dot (bmPu, ucas)
            h2eff_sub = lib.tag_array (h2eff_sub, bmPu = bmPu)
        return mo1, ci1, h2eff_sub

    def get_grad (self):
        gorb = self.fock1 - self.fock1.T
        gci = [2*hci0 for hci0 in self.hci0]
        return self.ugg.pack (gorb, gci)

    def get_gx (self):
        gorb = self.fock1 - self.fock1.T
        idx = np.zeros (gorb.shape, dtype=np.bool_)
        ncore, nocc = self.ncore, self.nocc
        idx[ncore:nocc,:ncore] = True
        idx[nocc:,ncore:nocc] = True
        if isinstance (self.ugg, LASCISymm_UnitaryGroupGenerators):
            idx[self.ugg.symm_forbid] = False
        gx = gorb[idx]
        return gx
