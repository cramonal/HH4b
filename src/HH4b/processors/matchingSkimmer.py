"""
Skimmer for matching studies
Author(s): Raghav Kansal, Cristina Suarez
"""

import numpy as np
import awkward as ak
import pandas as pd

from coffea import processor
from coffea.analysis_tools import Weights, PackedSelection
from coffea.nanoevents.methods.nanoaod import JetArray
import vector

import itertools
import pathlib
import pickle
import gzip
import os

from typing import Dict
from collections import OrderedDict

from .GenSelection import gen_selection_HHbbbb
from .utils import pad_val, add_selection, concatenate_dicts, select_dicts, P4, PAD_VAL
from .common import LUMI, jec_shifts, jmsr_shifts
from .objects import *
from . import common


# mapping samples to the appropriate function for doing gen-level selections
gen_selection_dict = {
    "GluGlutoHHto4B": gen_selection_HHbbbb,
}

import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MIN_JETS = 4
MAX_JETS = 4
HIGGS_MASS = 125.0


class matchingSkimmer(processor.ProcessorABC):
    """
    Skims nanoaod files, saving selected branches and events passing preselection cuts
    (and triggers for data).
    """

    # key is name in nano files, value will be the name in the skimmed output
    skim_vars = {
        # TODO: add hadron flavour
        "FatJet": {
            **P4,
            "msoftdrop": "Msd",
            "Txbb": "PNetXbb",
            "Txjj": "PNetXjj",
            "particleNet_mass": "PNetMass",
        },
        "GenJet": P4,
    }

    # compute possible jet assignments lookup table
    JET_ASSIGNMENTS = {}
    for nj in range(MIN_JETS, MAX_JETS + 1):
        a = list(itertools.combinations(range(nj), 2))
        b = np.array(
            [(i, j) for i, j in itertools.combinations(a, 2) if len(set(i + j)) == MIN_JETS]
        )
        JET_ASSIGNMENTS[nj] = b

    def __init__(self, xsecs={}):
        super(matchingSkimmer, self).__init__()

        self.XSECS = xsecs  # in pb
        self._accumulator = processor.dict_accumulator({})

    def to_pandas(self, events: Dict[str, np.array]):
        """
        Convert our dictionary of numpy arrays into a pandas data frame
        Uses multi-index columns for numpy arrays with >1 dimension
        (e.g. FatJet arrays with two columns)
        """
        return pd.concat(
            [pd.DataFrame(v) for k, v in events.items()],
            axis=1,
            keys=list(events.keys()),
        )

    def dump_table(self, pddf: pd.DataFrame, fname: str, odir_str: str = None) -> None:
        """
        Saves pandas dataframe events to './outparquet'
        """
        import pyarrow.parquet as pq
        import pyarrow as pa

        local_dir = os.path.abspath(os.path.join(".", "outparquet"))
        if odir_str:
            local_dir += odir_str
        os.system(f"mkdir -p {local_dir}")

        # need to write with pyarrow as pd.to_parquet doesn't support different types in
        # multi-index column names
        table = pa.Table.from_pandas(pddf)
        pq.write_table(table, f"{local_dir}/{fname}")

    @property
    def accumulator(self):
        return self._accumulator

    def process(self, events: ak.Array):
        """Runs event processor for different types of jets"""

        year = events.metadata["dataset"].split("_")[0]
        dataset = "_".join(events.metadata["dataset"].split("_")[1:])

        btag_vars = {
            "btagDeepB": "btagDeepB",
            "btagDeepFlavB": "btagDeepFlavB",
        }
        # if year != "2018":
        #     # for now, only in v11_private
        #     btag_vars = {
        #         "btagPNetProb": "btagPNetProb",
        #         "btagPNetProbbb": "btagPNetProbbb",
        #         "btagPNetProbc": "btagPNetProbc",
        #         "btagPNetProbuds": "btagPNetProbuds",
        #         "btagPNetProbg": "btagPNetProbg",
        #         "btagPNetBvsAll": "btagPNetBvsAll",
        #     }

        isData = not hasattr(events, "genWeight")
        isSignal = "HHTobbbb" in dataset

        if isSignal:
            # take only signs for HH samples
            gen_weights = np.sign(events["genWeight"])
        elif not isData:
            gen_weights = events["genWeight"].to_numpy()
        else:
            gen_weights = None

        n_events = len(events) if isData else np.sum(gen_weights)
        selection = PackedSelection()
        weights = Weights(len(events), storeIndividual=True)

        cutflow = OrderedDict()
        cutflow["all"] = n_events

        selection_args = (selection, cutflow, isData, gen_weights)

        #########################
        # Object definitions
        #########################
        num_jets = 6
        jets = good_ak4jets(events.Jet, year, events.run.to_numpy(), isData)

        # sort by b
        jets = jets[ak.argsort(jets.btagDeepFlavB, ascending=False)]

        # vbf jets
        vbf_jets = jets[(jets.pt > 25) & (((jets.pt < 50) & (jets.puId >= 6)) | (jets.pt >= 50))]

        # jets p4 corrected by bjet energy regression
        jets_p4 = bregcorr(jets)

        num_fatjets = 3
        fatjets = good_ak8jets(events.FatJet)
        # sort by bb
        fatjets = fatjets[ak.argsort(fatjets.Txbb, ascending=False)]

        veto_muon_sel = good_muons(events.Muon, selection=veto_muon_selection_run2_bbbb)
        veto_electron_sel = good_electrons(
            events.Electron, selection=veto_electron_selection_run2_bbbb
        )

        #########################
        # Save / derive variables
        #########################
        skimmed_events = {}

        # Jet variables
        ak4JetVars = {
            **{
                f"ak4Jet{key}": pad_val(getattr(jets_p4, var), num_jets, axis=1)
                for (var, key) in P4.items()
            },
            **{
                f"ak4Jet{key}": pad_val(jets[var], num_jets, axis=1)
                for (var, key) in btag_vars.items()
            },
        }

        # assignment variables
        ak4JetVars = {
            **ak4JetVars,
            **self.getJetAssignmentVars(ak4JetVars),
            **self.getJetAssignmentVars(ak4JetVars, method="chi2"),
        }

        # FatJet variables
        ak8FatJetVars = {
            f"ak8FatJet{key}": pad_val(fatjets[var], num_fatjets, axis=1)
            for (var, key) in self.skim_vars["FatJet"].items()
        }

        # gen variables
        for d in gen_selection_dict:
            if d in dataset:
                vars_dict = gen_selection_dict[d](
                    events, jets, fatjets, selection, cutflow, gen_weights, P4
                )
                skimmed_events = {**skimmed_events, **vars_dict}

        ak4GenJetVars = {}
        ak8GenJetVars = {}
        if not isData:
            ak4GenJetVars = {
                f"ak4GenJet{key}": pad_val(events.GenJet[var], num_jets, axis=1)
                for (var, key) in self.skim_vars["GenJet"].items()
            }

            ak8GenJetVars = {
                f"ak8GenJet{key}": pad_val(events.GenJetAK8[var], num_fatjets, axis=1)
                for (var, key) in self.skim_vars["GenJet"].items()
            }

        skimmed_events = {
            **skimmed_events,
            **ak4JetVars,
            **ak8FatJetVars,
            # **ak4GenJetVars,
            # **ak8GenJetVars,
        }

        ######################
        # Selection
        ######################

        # # jet veto map for 2022
        # if year == "2022" and isData:
        #     jetveto = get_jetveto_event(jets, year, events.run.to_numpy())
        #     add_selection("ak4_jetveto", jetveto, *selection_args)

        # met filter selection
        met_filters = [
            "goodVertices",
            "globalSuperTightHalo2016Filter",
            "HBHENoiseFilter",
            "HBHENoiseIsoFilter",
            "EcalDeadCellTriggerPrimitiveFilter",
            "BadPFMuonFilter",
            "BadPFMuonDzFilter",
            "eeBadScFilter",
            "ecalBadCalibFilter",
        ]
        metfilters = np.ones(len(events), dtype="bool")
        metfilterkey = "data" if isData else "mc"
        for mf in met_filters:
            if mf in events.Flag.fields:
                metfilters = metfilters & events.Flag[mf]
        add_selection("met_filters", metfilters, *selection_args)

        # do not apply selection for gen studies
        # apply_selection = False
        apply_selection = True

        # require at least one ak8 jet with PNscore > 0.8
        add_selection("1ak8_pt", np.any(fatjets.pt > 200, axis=1), *selection_args)
        add_selection("1ak8_xbb", np.any(fatjets.Txbb > 0.8, axis=1), *selection_args)
        # require at least two ak4 jets with Medium DeepJetM score (0.2783 for 2018)
        add_selection("2ak4_b", ak.sum(jets.btagDeepFlavB > 0.2783, axis=1) >= 2, *selection_args)
        # veto leptons
        add_selection(
            "0lep",
            (ak.sum(veto_muon_sel, axis=1) == 0) & (ak.sum(veto_electron_sel, axis=1) == 0),
            *selection_args,
        )

        ######################
        # Weights
        ######################
        if dataset in self.XSECS:
            xsec = self.XSECS[dataset]
            weight_norm = xsec * LUMI[year]
        else:
            logger.warning("Weight not normalized to cross section")
            weight_norm = 1

        if isData:
            skimmed_events["weight"] = np.ones(n_events)
        else:
            weights.add("genweight", gen_weights)
            skimmed_events["weight"] = weights.weight() * weight_norm

        if not apply_selection:
            skimmed_events = {
                key: value.reshape(len(skimmed_events["weight"]), -1)
                for (key, value) in skimmed_events.items()
            }
        else:
            # reshape and apply selections
            sel_all = selection.all(*selection.names)
            skimmed_events = {
                key: value.reshape(len(skimmed_events["weight"]), -1)[sel_all]
                for (key, value) in skimmed_events.items()
            }

        df = self.to_pandas(skimmed_events)

        fname = events.behavior["__events_factory__"]._partition_key.replace("/", "_") + ".parquet"
        self.dump_table(df, fname)

        return {year: {dataset: {"nevents": n_events, "cutflow": cutflow}}}

    def postprocess(self, accumulator):
        return accumulator

    def getJetAssignmentVars(self, ak4JetVars, method="dhh"):
        """
        Calculates Jet assignment variables
        based on: https://github.com/cjmikkels/spanet_hh_test/blob/main/src/models/test_baseline.py
        """

        # just consider top 4 jets (already sorted by b-jet score)
        nj = 4
        jets = vector.array(
            {
                "pt": ak4JetVars["ak4JetPt"],
                "eta": ak4JetVars["ak4JetEta"],
                "phi": ak4JetVars["ak4JetPhi"],
                "M": ak4JetVars["ak4JetMass"],
            },
        )

        # get array of dijets for each possible higgs combination
        jj = jets[:, self.JET_ASSIGNMENTS[nj][:, :, 0]] + jets[:, self.JET_ASSIGNMENTS[nj][:, :, 1]]
        mjj = jj.M

        if method == "chi2":
            chi2 = ak.sum(np.square(mjj - HIGGS_MASS), axis=-1)
            index = ak.argmin(chi2, axis=-1)

            first_bb_pair = self.JET_ASSIGNMENTS[nj][index][:, 0, :]
            second_bb_pair = self.JET_ASSIGNMENTS[nj][index][:, 1, :]
            return {
                "ak4Pair0chi2": first_bb_pair,
                "ak4Pair1chi2": second_bb_pair,
            }

        elif method == "dhh":
            # https://github.com/UF-HH/bbbbAnalysis/blob/master/src/OfflineProducerHelper.cc#L4109
            mjj_sorted = ak.sort(mjj, ascending=False)

            # compute \delta d
            k = 125 / 120
            delta_d = np.absolute(mjj_sorted[:, :, 0] - k * mjj_sorted[:, :, 1]) / np.sqrt(
                1 + k**2
            )

            # take combination with smallest distance to the diagonal
            index_mindhh = ak.argmin(delta_d, axis=-1)

            # except, if |dhh^1 - dhh^2| < 30 GeV
            # this is when the pairing method starts to make mistakes
            d_sorted = ak.sort(delta_d, ascending=False)
            is_dhh_tooclose = (d_sorted[:, 0] - d_sorted[:, 1]) < 30

            # order dijets with the highest sum pt in their own event CoM frame
            # CoM frame of dijets
            cm = jj[:, :, 0] + jj[:, :, 1]
            com_pt = jj[:, :, 0].boostCM_of(cm).pt + jj[:, :, 1].boostCM_of(cm).pt
            index_max_com_pt = ak.argmax(com_pt, axis=-1)

            index = ak.where(is_dhh_tooclose, index_max_com_pt, index_mindhh)

            # TODO: is there an exception if the index chosen is the same?
            # is_same_index = (index == index_max_com_pt)

        # now get the resulting bb pairs
        first_bb_pair = self.JET_ASSIGNMENTS[nj][index][:, 0, :]
        first_bb_j1 = jets[np.arange(len(jets.pt)), first_bb_pair[:, 0]]
        first_bb_j2 = jets[np.arange(len(jets.pt)), first_bb_pair[:, 1]]
        first_bb_dijet = first_bb_j1 + first_bb_j2

        second_bb_pair = self.JET_ASSIGNMENTS[nj][index][:, 1, :]
        second_bb_j1 = jets[np.arange(len(jets.pt)), second_bb_pair[:, 0]]
        second_bb_j2 = jets[np.arange(len(jets.pt)), second_bb_pair[:, 1]]
        second_bb_dijet = second_bb_j1 + second_bb_j2

        # stack pairs
        bb_pairs = np.stack([first_bb_pair, second_bb_pair], axis=1)

        # sort by dijet pt
        bbs_jjpt = np.concatenate(
            [first_bb_dijet.pt.reshape(-1, 1), second_bb_dijet.pt.reshape(-1, 1)], axis=1
        )
        sort_by_jjpt = np.argsort(bbs_jjpt, axis=-1)[:, ::-1]

        bb_pairs_sorted = np.array(
            [
                [bb_pair_e[sort_e[0]], bb_pair_e[sort_e[1]]]
                for bb_pair_e, sort_e in zip(bb_pairs, sort_by_jjpt)
            ]
        )

        first_bb_pair_sort = bb_pairs_sorted[:, 0]
        second_bb_pair_sort = bb_pairs_sorted[:, 1]

        first_bb_j1 = jets[np.arange(len(jets.pt)), first_bb_pair_sort[:, 0]]
        first_bb_j2 = jets[np.arange(len(jets.pt)), first_bb_pair_sort[:, 1]]
        first_bb_dijet = first_bb_j1 + first_bb_j2

        second_bb_j1 = jets[np.arange(len(jets.pt)), second_bb_pair_sort[:, 0]]
        second_bb_j2 = jets[np.arange(len(jets.pt)), second_bb_pair_sort[:, 1]]
        second_bb_dijet = second_bb_j1 + second_bb_j2

        jetAssignmentDict = {
            "ak4Pair0": first_bb_pair,
            "ak4Pair1": second_bb_pair,
            "ak4DijetPt0": first_bb_dijet.pt,
            "ak4DijetEta0": first_bb_dijet.eta,
            "ak4DijetPhi0": first_bb_dijet.phi,
            "ak4DijetMass0": first_bb_dijet.mass,
            "ak4DijetPt1": second_bb_dijet.pt,
            "ak4DijetEta1": second_bb_dijet.eta,
            "ak4DijetPhi1": second_bb_dijet.phi,
            "ak4DijetMass1": second_bb_dijet.mass,
            "ak4DijetDeltaR": first_bb_dijet.deltaR(second_bb_dijet),
        }
        return jetAssignmentDict
