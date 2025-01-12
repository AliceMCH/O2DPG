#!/usr/bin/env python3
#
# This is a short script to conveniently wrap the ROOT macro used for release validation comparisons
#
# Help message:
# usage: o2dpg_release_validation.py [-h] -f INPUT_FILES INPUT_FILES -t {1,2,3,4,5,6,7} [--chi2-value CHI2_VALUE] [--rel-bc-diff REL_BC_DIFF] [--rel-entries-diff REL_ENTRIES_DIFF] [--select-critical]
# 
# Wrapping ReleaseValidation macro
# 
# optional arguments:
#   -h, --help            show this help message and exit
#   -f INPUT_FILES INPUT_FILES, --input-files INPUT_FILES INPUT_FILES
#                         input files for comparison
#   -t {1,2,3,4,5,6,7}, --test {1,2,3,4,5,6,7}
#                         index of test case
#   --chi2-value CHI2_VALUE
#                         Chi2 threshold
#   --rel-bc-diff REL_BC_DIFF
#                         Threshold of relative difference in normalised bin content
#   --rel-entries-diff REL_ENTRIES_DIFF
#                         Threshold of relative difference in number of entries
#   --select-critical     Select the critical histograms and dump to file

import sys
import argparse
from os import environ, makedirs
from os.path import join, abspath, exists, isfile, isdir
from glob import glob
from subprocess import Popen
from pathlib import Path
from itertools import combinations
from shlex import split
import json

# make sure O2DPG + O2 is loaded
O2DPG_ROOT=environ.get('O2DPG_ROOT')

if O2DPG_ROOT is None:
    print('ERROR: This needs O2DPG loaded')
    sys.exit(1)

ROOT_MACRO=join(O2DPG_ROOT, "RelVal", "ReleaseValidation.C")

from ROOT import TFile, gDirectory, gROOT, TChain

DETECTORS_OF_INTEREST_HITS = ["ITS", "TOF", "EMC", "TRD", "PHS", "FT0", "HMP", "MFT", "FDD", "FV0", "MCH", "MID", "CPV", "ZDC", "TPC"]
gROOT.SetBatch()

def is_sim_dir(path):
    """
    Decide whether or not path points to a simulation directory
    """
    if not isdir(path):
        return False
    if not glob(f"{path}/pipeline*"):
        # assume there must be pipeline_{metrics,action} in there
        return False
    return True


def find_mutual_files(dirs, glob_pattern, *, grep=None):
    """
    Find mutual files recursively in list of dirs

    Args:
        dirs: iterable
            directories to take into account
        glob_pattern: str
            pattern used to apply glob to only seach for some files
        grep: iterable
            additional list of patterns to grep for
    Returns:
        list: intersection of found files
    """
    files = []
    for d in dirs:
        glob_path = f"{d}/**/{glob_pattern}"
        files.append(glob(glob_path, recursive=True))

    for f, d in zip(files, dirs):
        f.sort()
        for i, _ in enumerate(f):
            # strip potential leading /
            f[i] = f[i][len(d):].lstrip("/")

    # build the intersection
    if not files:
        return []

    intersection = files[0]
    for f in files[1:]:
        intersection = list(set(intersection) & set(f))

    # apply additional grepping if patterns are given
    if grep:
        intersection_cache = intersection.copy()
        intersection = []
        for g in grep:
            for ic in intersection_cache:
                if g in ic:
                    intersection.append(ic)

    # Sort for convenience
    intersection.sort()

    return intersection


def exceeding_difference_thresh(sizes, threshold=0.1):
    """
    Find indices in sizes where value exceeds threshold
    """
    diff_indices = []
    for i1, i2 in combinations(range(len(sizes)), 2):
        diff = abs(sizes[i1] - sizes[i2])
        if diff / sizes[i2] > threshold or diff / sizes[i2] > threshold:
            diff_indices.append((i1, i2))
    return diff_indices


def file_sizes(dirs, threshold):
    """
    Compare file sizes of mutual files in given dirs
    """
    intersection = find_mutual_files(dirs, "*.root")

    # prepare for convenient printout
    max_col_lengths = [0] * (len(dirs) + 1)
    sizes = [[] for _ in dirs]

    # extract file sizes
    for f in intersection:
        max_col_lengths[0] = max(max_col_lengths[0], len(f))
        for i, d in enumerate(dirs):
            size = Path(join(d, f)).stat().st_size
            max_col_lengths[i + 1] = max(max_col_lengths[i + 1], len(str(size)))
            sizes[i].append(size)

    # prepare dictionary to be dumped and prepare printout
    collect_dict = {"directories": dirs, "files": {}, "threshold": threshold}
    top_row = "| " + " | ".join(dirs) + " |"
    print(f"\n{top_row}\n")
    for i, f in enumerate(intersection):
        compare_sizes = []
        o = f"{f:<{max_col_lengths[0]}}"
        for j, s in enumerate(sizes):
            o += f" | {str(s[i]):<{max_col_lengths[j+1]}}"
            compare_sizes.append(s[i])
        o = f"| {o} |"

        diff_indices =  exceeding_difference_thresh(compare_sizes, threshold)
        if diff_indices:
            o += f"  <==  EXCEEDING threshold of {threshold} at columns {diff_indices} |"
            collect_dict["files"][f] = compare_sizes
        else:
            o += " OK |"
        print(o)
    return collect_dict


def load_root_file(path, option="READ"):
    """
    Convenience wrapper to open a ROOT file
    """
    f = TFile.Open(path, option)
    if not f or f.IsZombie():
        print(f"WARNING: ROOT file {path} might not exist or could not be opened")
        return None
    return f


def make_generic_histograms_from_chain(filenames1, filenames2, output_filepath1, output_filepath2, treename="o2sim"):
    """
    Create all possible histograms of TLeaf from given TChains
    """

    chain1 = TChain(treename)
    for f in filenames1:
        chain1.Add(f)
    chain2 = TChain(treename)
    for f in filenames2:
        chain2.Add(f)

    # focus only on these TLeaf types
    accepted_types = ["UInt_t", "Int_t", "Float_t", "Double_t", "Double32_t"]

    # A bit cumbersome but let's just use some code duplication to the same for 2 chains
    # 1. extract names with accepted types
    branch_names1 = []
    branch_names2 = []
    for l in chain1.GetListOfLeaves():
        if l.GetTypeName() not in accepted_types:
            continue
        branch_names1.append(l.GetName())
    for l in chain2.GetListOfLeaves():
        if l.GetTypeName() not in accepted_types:
            continue
        branch_names2.append(l.GetName())

    # 2. sort then in order to...
    branch_names1.sort()
    branch_names2.sort()

    # 3. ...compare whether we have the same leaves
    if branch_names1 != branch_names2:
        print("WARNING: Found different branches in input files")
        # 4. if not, warn and shrink to intersection
        branch_names1 = list(set(branch_names1) & set(branch_names2))

    # Reset and use later
    branch_names2 = []
    histograms = []
    output_file1 = load_root_file(output_filepath1, "RECREATE")
    for b in branch_names1:
        h_name = b.replace(".", "_")
        if chain1.Draw(f"{b}>>{h_name}") == -1:
            # in case of error, skip it
            continue
        # if successful, append histogram and necessary info
        branch_names2.append(b)
        hist = gDirectory.Get(h_name)
        histograms.append(hist)
        output_file1.cd()
        hist.Write()

    # Extract the second bunch of histograms
    output_file2 = load_root_file(output_filepath2, "RECREATE")
    for b, h in zip(branch_names2, histograms):
        # reset the histogram and re-use to guarantee same binning
        h.Reset("ICEMS")
        # change current directory and set it for this histogram
        output_file2.cd()
        h.SetDirectory(output_file2)
        # append to the existing (empty) histogram
        chain2.Draw(f"{b}>>+{h.GetName()}")
        h.Write()

    output_file1.Close()
    output_file2.Close()


def rel_val_files(file1, file2, args, output_dir):
    """
    RelVal for 2 ROOT files, simply a wrapper around ReleaseValidation.C macro
    """
    if not exists(output_dir):
        makedirs(output_dir)
    select_critical = "kTRUE" if args.select_critical else "kFALSE"
    no_plots = "kTRUE" if args.no_plots else "kFALSE"
    cmd = f"\\(\\\"{abspath(file1)}\\\",\\\"{abspath(file2)}\\\",{args.test},{args.chi2_value},{args.rel_mean_diff},{args.rel_entries_diff},{select_critical},{no_plots}\\)"
    cmd = f"root -l -b -q {ROOT_MACRO}{cmd}"
    print(f"Running {cmd}")
    p = Popen(split(cmd), cwd=output_dir)
    p.wait()
    return 0


def has_severity(filename, severity=("BAD", "CRIT_NC")):
    """
    Check if any 2 histograms have a given severity level after RelVal
    """
    res = None
    ret = False
    with open(filename, "r") as f:
        # NOTE For now care about the summary. However, we have each test individually, so we could do a more detailed check in the future
        res = json.load(f)["test_summary"]
    for s in severity:
        names = res.get(s)
        if not names:
            continue
        print(f"Histograms for severity {s}:")
        for n in names:
            print(f"    {n}")
        ret = True
    return ret


def add_to_summary(paths_to_summary, key, summary_dict):
    """
    Extend a list of paths with given severity
    """
    summary_list = []
    for p in paths_to_summary:
        if has_severity(p):
            summary_list.append(p)
    summary_dict[key] = summary_list

def rel_val_ttree(dir1, dir2, files, output_dir, args, treename="o2sim", *, combine_patterns=None):
    """
    RelVal for 2 ROOT files containing a TTree to be compared
    """

    # Prepare file paths for TChain
    to_be_chained1 = []
    to_be_chained2 = []
    output_dirs = []

    # possibly combine common files, for instance when they come from different timeframes
    if combine_patterns:
        for cp in combine_patterns:
            to_be_chained1.append([join(dir1, hf) for hf in files if cp in hf])
            to_be_chained2.append([join(dir2, hf) for hf in files if cp in hf])
            output_dirs.append(f"{cp}_dir")
    else:
        to_be_chained1 = []
        to_be_chained2 = []
        for hf in files:
            to_be_chained1.append(join(dir1, hf))
            to_be_chained2.append(join(dir1, hf))
            output_dirs.append(f"{hf}_dir")

    # paths for chains prepared, output directory names specified, do RelVal
    for tbc1, tbc2, od in zip(to_be_chained1, to_be_chained2, output_dirs):
        output_dir_hf = join(output_dir, od)
        if not exists(output_dir_hf):
            makedirs(output_dir_hf)

        make_generic_histograms_from_chain(tbc1, tbc2, join(output_dir_hf, "file1.root"), join(output_dir_hf, "file2.root"), treename)
        # after we created files containing histograms, they can be compared with the standard RelVal
        rel_val_files(abspath(join(output_dir_hf, "file1.root")), abspath(join(output_dir_hf, "file2.root")), args, output_dir_hf)
    return 0


def rel_val_histograms(dir1, dir2, files, output_dir, args):
    """
    Simply another wrapper to combine multiple files where we expect them to contain histograms already
    """
    for f in files:
        output_dir_f = join(output_dir, f"{f}_dir")
        if not exists(output_dir_f):
            makedirs(output_dir_f)
        rel_val_files(join(dir1, f), join(dir2, f), args, output_dir_f)


def rel_val_sim_dirs(args):
    """
    Make full RelVal for 2 simulation directories
    """
    dir1 = args.input[0]
    dir2 = args.input[1]
    output_dir = args.output

    look_for = "Summary.json"
    summary_dict = {}

    # file sizes
    file_sizes_to_json = file_sizes([dir1, dir2], 0.5)
    with open(join(output_dir, "file_sizes.json"), "w") as f:
        json.dump(file_sizes_to_json, f, indent=2)

    # enable all if everything is disabled
    if not any((args.with_hits, args.with_tpctracks, args.with_kine, args.with_analysis, args.with_qc)):
        args.with_hits, args.with_tpctracks, args.with_kine, args.with_analysis, args.with_qc = (True,) * 5

    # hits
    if args.with_hits:
        hit_files = find_mutual_files((dir1, dir2), "*Hits*.root", grep=args.detectors)
        output_dir_hits = join(output_dir, "hits")
        if not exists(output_dir_hits):
            makedirs(output_dir_hits)
        rel_val_ttree(dir1, dir2, hit_files, output_dir_hits, args, combine_patterns=[f"Hits{d}" for d in args.detectors])
        add_to_summary(glob(f"{output_dir_hits}/**/{look_for}", recursive=True), "hist", summary_dict)

    # TPC tracks
    if args.with_tpctracks:
        tpctrack_files = find_mutual_files((dir1, dir2), "tpctracks.root")
        output_dir_tpctracks = join(output_dir, "tpctracks")
        if not exists(output_dir_tpctracks):
            makedirs(output_dir_tpctracks)
        rel_val_ttree(dir1, dir2, tpctrack_files, output_dir_tpctracks, args, "tpcrec", combine_patterns=["tpctracks.root"])
        add_to_summary(glob(f"{output_dir_tpctracks}/**/{look_for}", recursive=True), "tpctracks", summary_dict)

    # TPC tracks
    if args.with_kine:
        kine_files = find_mutual_files((dir1, dir2), "*Kine.root")
        output_dir_kine = join(output_dir, "kine")
        if not exists(output_dir_kine):
            makedirs(output_dir_kine)
        rel_val_ttree(dir1, dir2, kine_files, output_dir_kine, args, combine_patterns=["Kine.root"])
        add_to_summary(glob(f"{output_dir_kine}/**/{look_for}", recursive=True), "kine", summary_dict)

    # Analysis
    if args.with_analysis:
        dir_analysis1 = join(dir1, "Analysis")
        dir_analysis2 = join(dir2, "Analysis")
        analysis_files = find_mutual_files((dir_analysis1, dir_analysis2), "*.root")
        output_dir_analysis = join(output_dir, "analysis")
        print(output_dir, output_dir_analysis)
        if not exists(output_dir_analysis):
            makedirs(output_dir_analysis)
        rel_val_histograms(dir_analysis1, dir_analysis2, analysis_files, output_dir_analysis, args)
        add_to_summary(glob(f"{output_dir_analysis}/**/{look_for}", recursive=True), "analysis", summary_dict)

    # QC
    if args.with_qc:
        dir_qc1 = join(dir1, "QC")
        dir_qc2 = join(dir2, "QC")
        qc_files = find_mutual_files((dir_qc1, dir_qc2), "*.root")
        output_dir_qc = join(output_dir, "qc")
        if not exists(output_dir_qc):
            makedirs(output_dir_qc)
        rel_val_histograms(dir_qc1, dir_qc2, qc_files, output_dir_qc, args)
        add_to_summary(glob(f"{output_dir_qc}/**/{look_for}", recursive=True), "qc", summary_dict)

    with open(join(output_dir, "SummaryToBeChecked.json"), "w") as f:
        json.dump(summary_dict, f, indent=2)

def rel_val(args):
    """
    Entry point for RelVal
    """
    func = None
    if isfile(args.input[0]) and isfile(args.input[1]):
        # simply check if files, assume that they would be ROOT files in that case
        func = rel_val_files
    if is_sim_dir(args.input[0]) and is_sim_dir(args.input[1]):
        func = rel_val_sim_dirs
    if not func:
        print("Please provide either 2 files or 2 simulation directories as input.")
        return 1
    if not exists(args.output):
        makedirs(args.output)
    return func(args)


def inspect(args):
    """
    Inspect a Summary.json in view of RelVal severity
    """
    return has_severity(args.file, args.severity)

def main():
    """entry point when run directly from command line"""
    parser = argparse.ArgumentParser(description='Wrapping ReleaseValidation macro')

    common_file_parser = argparse.ArgumentParser(add_help=False)
    common_file_parser.add_argument("-i", "--input", nargs=2, help="2 input files for comparison OR 2 input directories from simulation for comparison", required=True)

    sub_parsers = parser.add_subparsers(dest="command")
    rel_val_parser = sub_parsers.add_parser("rel-val", parents=[common_file_parser])
    rel_val_parser.add_argument("-t", "--test", type=int, help="index of test case", choices=list(range(1, 8)), required=True)
    rel_val_parser.add_argument("--chi2-value", dest="chi2_value", type=float, help="Chi2 threshold", default=1.5)
    rel_val_parser.add_argument("--rel-mean-diff", dest="rel_mean_diff", type=float, help="Threshold of relative difference in mean", default=1.5)
    rel_val_parser.add_argument("--rel-entries-diff", dest="rel_entries_diff", type=float, help="Threshold of relative difference in number of entries", default=0.01)
    rel_val_parser.add_argument("--select-critical", dest="select_critical", action="store_true", help="Select the critical histograms and dump to file")
    rel_val_parser.add_argument("--threshold", type=float, default=0.1, help="threshold for how far file sizes are allowed to diverge before warning")
    rel_val_parser.add_argument("--with-hits", dest="with_hits", action="store_true", help="include hit comparison when RelVal when run on simulation directories")
    rel_val_parser.add_argument("--detectors", nargs="*", help="include these detectors for hit RelVal", default=DETECTORS_OF_INTEREST_HITS, choices=DETECTORS_OF_INTEREST_HITS)
    rel_val_parser.add_argument("--with-tpctracks", dest="with_tpctracks", action="store_true", help="include TPC tracks RelVal when run on simulation directories")
    rel_val_parser.add_argument("--with-kine", dest="with_kine", action="store_true", help="include kine RelVal when run on simulation directories")
    rel_val_parser.add_argument("--with-analysis", dest="with_analysis", action="store_true", help="include analysis RelVal when run on simulation directories")
    rel_val_parser.add_argument("--with-qc", dest="with_qc", action="store_true", help="include QC RelVal when run on simulation directories")
    rel_val_parser.add_argument("--no-plots", dest="no_plots", action="store_true", help="disable plotting")
    rel_val_parser.add_argument("--output", "-o", help="output directory", default="./")
    rel_val_parser.set_defaults(func=rel_val)

    inspect_parser = sub_parsers.add_parser("inspect")
    inspect_parser.add_argument("file", help="pass a JSON produced from ReleaseValidation (rel-val)")
    inspect_parser.add_argument("--severity", nargs="*", default=["BAD", "CRIT_NC"], choices=["GOOD", "WARNING", "BAD", "CRIT_NC", "NONCRIT_NC"], help="Choose severity levels to search for")
    inspect_parser.set_defaults(func=inspect)

    args = parser.parse_args()
    return(args.func(args))

if __name__ == "__main__":
    sys.exit(main())
