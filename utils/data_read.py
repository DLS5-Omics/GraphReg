#!/usr/bin/env python
# Copyright 2017 Calico LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========================================================================

# This code is copied from https://github.com/calico/basenji and modified.

# =========================================================================

import collections
import os
import sys
from optparse import OptionParser

import h5py
import intervaltree
import numpy as np
import pyBigWig

# import basenji.basenji_data.ModelSeq as ModelSeq
ModelSeq = collections.namedtuple("ModelSeq", ["chr", "start", "end", "label"])


################################################################################
# main
################################################################################
def main():
    # usage = 'usage: %prog [options] <genome_cov_file> <seqs_bed_file> <seqs_cov_file>'
    usage = "usage: %prog [options]"
    parser = OptionParser(usage)
    parser.add_option(
        "-b",
        dest="blacklist_bed",
        help="Set blacklist nucleotides to a baseline value.",
    )
    parser.add_option(
        "-c",
        dest="clip",
        default=50000,
        type="float",
        help="Clip values post-summary to a maximum [Default: %default]",
    )
    parser.add_option(
        "--crop",
        dest="crop_bp",
        default=0,
        type="int",
        help="Crop bp off each end [Default: %default]",
    )
    parser.add_option(
        "-s",
        dest="scale",
        default=1.0,
        type="float",
        help="Scale values by [Default: %default]",
    )
    parser.add_option(
        "--soft",
        dest="soft_clip",
        default=False,
        action="store_true",
        help="Soft clip values, applying sqrt to the execess above the threshold [Default: %default]",
    )
    parser.add_option(
        "-u",
        dest="sum_stat",
        default="max",
        help="Summary statistic to compute in windows [Default: %default]",
    )
    parser.add_option(
        "-w",
        dest="pool_width",
        default=100,
        type="int",
        help="Average pooling width [Default: %default]",
    )
    (options, args) = parser.parse_args()

    ################################################################
    # Inputs

    organism = "human"
    cell_line = "GM12878"
    res = "5kb"
    genome = "hg38"
    data_path = "/media/labuser/STORAGE/GraphReg"

    if organism == "mouse":
        chr_list = ["chr" + str(i) for i in range(1, 20)] + ["chrX"]
    else:
        chr_list = ["chr" + str(i) for i in range(1, 23)] + ["chrX"]

    for chr_temp in chr_list:
        print(chr_temp)
        # genome_cov_file = data_path+'/data/'+cell_line+'/bam/GM12878_CAGE_binsize_5000bp.bigWig'
        genome_cov_file = data_path + "/data/" + cell_line + "/distal_reg_paper_bam/GM12878_DNase.bigWig"
        seqs_bed_file = (
            data_path + "/data/csv/seqs_bed/" + organism + "/" + genome + "/" + res + "/sequences_" + chr_temp + ".bed"
        )
        # seqs_cov_file = data_path+'/data/'+cell_line+'/seqs_cov/CAGE_cov_RPGC_'+chr_temp+'.h5'
        seqs_cov_file = (
            data_path + "/data/" + cell_line + "/seqs_cov/distal_reg_paper/DNase_cov_RPGC_" + chr_temp + ".h5"
        )

        assert options.crop_bp >= 0

        # read model sequences
        model_seqs = []
        for line in open(seqs_bed_file):
            a = line.split()
            model_seqs.append(ModelSeq(a[0], int(a[1]), int(a[2]), None))

        # read blacklist regions
        black_chr_trees = read_blacklist(options.blacklist_bed)

        # compute dimensions
        num_seqs = len(model_seqs)
        seq_len_nt = model_seqs[0].end - model_seqs[0].start
        seq_len_nt -= 2 * options.crop_bp
        target_length = seq_len_nt // options.pool_width
        assert target_length > 0

        # initialize sequences coverage file
        seqs_cov_dir = data_path + "/data/" + cell_line + "/seqs_cov"
        if not os.path.isdir(seqs_cov_dir):
            os.mkdir(seqs_cov_dir)
        seqs_cov_open = h5py.File(seqs_cov_file, "w")
        seqs_cov_open.create_dataset("seqs_cov", shape=(num_seqs, target_length), dtype="float16")

        # open genome coverage file
        genome_cov_open = CovFace(genome_cov_file)

        # for each model sequence
        for si in range(num_seqs):
            mseq = model_seqs[si]

            # read coverage
            seq_cov_nt = genome_cov_open.read(mseq.chr, mseq.start, mseq.end)

            # determine baseline coverage
            baseline_cov = np.percentile(seq_cov_nt, 10)
            baseline_cov = np.nan_to_num(baseline_cov)

            # set blacklist to baseline
            if mseq.chr in black_chr_trees:
                for black_interval in black_chr_trees[mseq.chr][mseq.start : mseq.end]:
                    # adjust for sequence indexes
                    black_seq_start = black_interval.begin - mseq.start
                    black_seq_end = black_interval.end - mseq.start
                    seq_cov_nt[black_seq_start:black_seq_end] = baseline_cov

            # set NaN's to baseline
            nan_mask = np.isnan(seq_cov_nt)
            seq_cov_nt[nan_mask] = baseline_cov

            # crop
            if options.crop_bp:
                seq_cov_nt = seq_cov_nt[options.crop_bp : -options.crop_bp]

            # sum pool
            seq_cov = seq_cov_nt.reshape(target_length, options.pool_width)
            if options.sum_stat == "sum":
                seq_cov = seq_cov.sum(axis=1, dtype="float32")
            elif options.sum_stat in ["mean", "avg"]:
                seq_cov = seq_cov.mean(axis=1, dtype="float32")
            elif options.sum_stat == "median":
                seq_cov = seq_cov.median(axis=1, dtype="float32")
            elif options.sum_stat == "max":
                seq_cov = seq_cov.max(axis=1)
            else:
                print(
                    'ERROR: Unrecognized summary statistic "%s".' % options.sum_stat,
                    file=sys.stderr,
                )
                exit(1)

            # clip
            if options.clip is not None:
                if options.soft_clip:
                    clip_mask = seq_cov > options.clip
                    seq_cov[clip_mask] = options.clip + np.sqrt(seq_cov[clip_mask] - options.clip)
                else:
                    seq_cov = np.clip(seq_cov, 0, options.clip)

            # scale
            seq_cov = options.scale * seq_cov

            # write
            seqs_cov_open["seqs_cov"][si, :] = seq_cov.astype("float16")

        # close genome coverage file
        genome_cov_open.close()

        # close sequences coverage file
        seqs_cov_open.close()


def read_blacklist(blacklist_bed, black_buffer=20):
    """Construct interval trees of blacklist
    regions for each chromosome."""
    black_chr_trees = {}

    if blacklist_bed is not None and os.path.isfile(blacklist_bed):
        for line in open(blacklist_bed):
            a = line.split()
            chrm = a[0]
            start = max(0, int(a[1]) - black_buffer)
            end = int(a[2]) + black_buffer

            if chrm not in black_chr_trees:
                black_chr_trees[chrm] = intervaltree.IntervalTree()

            black_chr_trees[chrm][start:end] = True

    return black_chr_trees


class CovFace:
    def __init__(self, cov_file):
        self.cov_file = cov_file
        self.bigwig = False

        cov_ext = os.path.splitext(self.cov_file)[1].lower()
        if cov_ext in [".bw", ".bigwig"]:
            self.cov_open = pyBigWig.open(self.cov_file, "r")
            self.bigwig = True
        elif cov_ext in [".h5", ".hdf5", ".w5", ".wdf5"]:
            self.cov_open = h5py.File(self.cov_file, "r")
        else:
            print(
                'Cannot identify coverage file extension "%s".' % cov_ext,
                file=sys.stderr,
            )
            exit(1)

    def read(self, chrm, start, end):
        if self.bigwig:
            cov = self.cov_open.values(chrm, start, end, numpy=True).astype("float16")
        else:
            if chrm in self.cov_open:
                cov = self.cov_open[chrm][start:end]
            else:
                print(
                    "WARNING: %s doesn't see %s:%d-%d. Setting to all zeros." % (self.cov_file, chrm, start, end),
                    file=sys.stderr,
                )
                cov = np.zeros(end - start, dtype="float16")
        return cov

    def close(self):
        self.cov_open.close()


################################################################################
# __main__
################################################################################
if __name__ == "__main__":
    main()
