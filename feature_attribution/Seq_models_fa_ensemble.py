import sys

sys.path.insert(0, "../train")

import time
from collections import Counter

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyBigWig
import shap
import tensorflow as tf
from gat_layer import GraphAttention
from scipy.stats import spearmanr
from sklearn.metrics import auc, average_precision_score, precision_recall_curve
from tensorflow import keras
from tensorflow.keras import backend as K
from tensorflow.keras import datasets, layers, models, regularizers
from tensorflow.keras.layers import Dropout, Input
from tensorflow.keras.models import Model
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.regularizers import l2

##### Input
data_path = "/media/labuser/STORAGE/GraphReg"  # data path
qval = 0.1  # 0.1, 0.01, 0.001
assay_type = "HiChIP"  # HiChIP, HiC, MicroC, HiCAR

if qval == 0.1:
    fdr = "1"
elif qval == 0.01:
    fdr = "01"
elif qval == 0.001:
    fdr = "001"


def poisson_loss(y_true, mu_pred):
    nll = tf.reduce_mean(tf.math.lgamma(y_true + 1) + mu_pred - y_true * tf.math.log(mu_pred))
    return nll


def parse_proto(example_protos):
    features = {
        "last_batch": tf.io.FixedLenFeature([1], tf.int64),
        "adj": tf.io.FixedLenFeature([], tf.string),
        #'adj_real': tf.io.FixedLenFeature([], tf.string),
        "tss_idx": tf.io.FixedLenFeature([], tf.string),
        "X_1d": tf.io.FixedLenFeature([], tf.string),
        "Y": tf.io.FixedLenFeature([], tf.string),
        "bin_idx": tf.io.FixedLenFeature([], tf.string),
        "sequence": tf.io.FixedLenFeature([], tf.string),
    }

    parsed_features = tf.io.parse_example(example_protos, features=features)

    last_batch = parsed_features["last_batch"]

    adj = tf.io.decode_raw(parsed_features["adj"], tf.float16)
    adj = tf.cast(adj, tf.float32)

    # adj_real = tf.io.decode_raw(parsed_features['adj_real'], tf.float16)
    # adj_real = tf.cast(adj_real, tf.float32)

    tss_idx = tf.io.decode_raw(parsed_features["tss_idx"], tf.float16)
    tss_idx = tf.cast(tss_idx, tf.float32)

    X_epi = tf.io.decode_raw(parsed_features["X_1d"], tf.float16)
    X_epi = tf.cast(X_epi, tf.float32)

    Y = tf.io.decode_raw(parsed_features["Y"], tf.float16)
    Y = tf.cast(Y, tf.float32)

    bin_idx = tf.io.decode_raw(parsed_features["bin_idx"], tf.int64)
    bin_idx = tf.cast(bin_idx, tf.int64)

    seq = tf.io.decode_raw(parsed_features["sequence"], tf.float64)
    seq = tf.cast(seq, tf.float32)

    return {
        "seq": seq,
        "last_batch": last_batch,
        "X_epi": X_epi,
        "Y": Y,
        "adj": adj,
        "tss_idx": tss_idx,
        "bin_idx": bin_idx,
    }


def file_to_records(filename):
    return tf.data.TFRecordDataset(filename, compression_type="ZLIB")


def dataset_iterator(file_name, batch_size):
    dataset = tf.data.Dataset.list_files(file_name)
    dataset = dataset.flat_map(file_to_records)
    dataset = dataset.batch(batch_size)
    dataset = dataset.map(parse_proto)
    iterator = dataset.make_one_shot_iterator()
    return iterator


def read_tf_record_1shot(iterator):
    try:
        next_datum = iterator.get_next()
        data_exist = True
    except tf.errors.OutOfRangeError:
        data_exist = False
    if data_exist:
        T = 400
        b = 5000
        F = 4
        seq = next_datum["seq"]
        batch_size = tf.shape(seq)[0]
        seq = tf.reshape(seq, [60, 100000, F])
        adj = next_datum["adj"]
        adj = tf.reshape(adj, [batch_size, 3 * T, 3 * T])

        last_batch = next_datum["last_batch"]
        tss_idx = next_datum["tss_idx"]
        tss_idx = tf.reshape(tss_idx, [3 * T])
        bin_idx = next_datum["bin_idx"]
        bin_idx = tf.reshape(bin_idx, [3 * T])

        if last_batch == 0:
            idx = tf.range(T, 2 * T)
        else:
            idx = tf.range(T, 3 * T)

        # bin_idx = tf.gather(bin_idx, idx)
        num_zero = np.sum(bin_idx.numpy() == 0)
        if num_zero == T + T // 2 + 1 and bin_idx[0] == 0:
            start = bin_idx[T + T // 2].numpy()
            end = bin_idx[-1].numpy() + 5000
            pos1 = np.arange(start, end, 100).astype(int)
            pad = -np.flip(np.arange(100, 3000000 + 100, 100).astype(int))  # -3000000, -2999900, ... , -200, -100
            pos = np.append(pad, pos1).astype(int)

        elif num_zero == T // 2 + 1 and bin_idx[0] == 0:
            start = bin_idx[T // 2].numpy()
            end = bin_idx[-1].numpy() + 5000
            pos1 = np.arange(start, end, 100).astype(int)
            pad = -np.flip(np.arange(100, 1000000 + 100, 100).astype(int))  # -1000000, -999900, ... , -200, -100
            pos = np.append(pad, pos1).astype(int)

        elif bin_idx[-1] == 0:
            start = bin_idx[0].numpy()
            i0 = np.where(bin_idx.numpy() == 0)[0][0]
            end = bin_idx[i0 - 1].numpy() + 5000
            pos1 = np.arange(start, end, 100).astype(int)
            l = 60000 - len(pos1)
            pad = 10**15 * np.ones(l)
            pos = np.append(pos1, pad).astype(int)

        else:
            start = bin_idx[0].numpy()
            end = bin_idx[-1].numpy() + 5000
            pos = np.arange(start, end, 100).astype(int)

        Y = next_datum["Y"]
        Y = tf.reshape(Y, [batch_size, 3 * T, 50])
        Y = tf.reduce_sum(Y, axis=2)
        Y = tf.reshape(Y, [batch_size, 3 * T])
        # Y = tf.gather(Y, idx, axis=1)

        X_epi = next_datum["X_epi"]
        X_epi = tf.reshape(X_epi, [1, 60000, 3])
        X_epi = tf.math.log(X_epi + 1)

    else:
        X_epi = 0
        seq = 0
        Y = 0
        adj = 0
        tss_idx = 0
        idx = 0
        pos = 0
    return data_exist, seq, X_epi, Y, adj, idx, tss_idx, pos


def calculate_loss(
    cell_lines,
    gene_names_list,
    gene_tss_list,
    chr_list,
    batch_size,
    saliency_method,
    write_bw,
    load_fa,
    organism,
    genome,
):
    # x_background = np.random.normal(0,1,size=[1000,60000,4]).astype('float32')
    # adj_background = np.zeros([1,1200,1200]).astype('float32')
    for num, cell_line in enumerate(cell_lines):
        for i, chrm in enumerate(chr_list):
            print(gene_names_list[i])
            file_name = (
                data_path
                + "/data/tfrecords/tfr_seq_"
                + cell_line
                + "_"
                + assay_type
                + "_FDR_"
                + fdr
                + "_"
                + chrm
                + ".tfr"
            )
            iterator = dataset_iterator(file_name, batch_size)
            while True:
                (
                    data_exist,
                    seq,
                    X_epi,
                    Y,
                    adj,
                    idx,
                    tss_idx,
                    pos,
                ) = read_tf_record_1shot(iterator)
                if data_exist:
                    if tf.reduce_sum(tf.gather(tss_idx, idx)) > 0:
                        if (
                            pos[0] > 0
                            and pos[-1] < 1e15
                            and gene_tss_list[i] >= pos[0] + 2000000
                            and gene_tss_list[i] < pos[0] + 4000000
                        ):
                            print("pos[0]: ", pos[0])

                            ############# Feature Attribution of Seq-CNN #############

                            if saliency_method == "saliency":
                                if load_fa == False:
                                    explain_output_idx_cnn = np.floor((gene_tss_list[i] - pos[0]) / 5000).astype(
                                        "int64"
                                    )
                                    print(
                                        "explain_output_idx_cnn: ",
                                        explain_output_idx_cnn,
                                    )
                                    grads_cnn = 0
                                    for j in range(1, 1 + 10):
                                        if organism == "mouse" and i == 9:
                                            iv2 = i + 10
                                            it2 = 1
                                        elif organism == "mouse" and i == 10:
                                            iv2 = 1
                                            it2 = 2
                                        else:
                                            iv2 = i + 10
                                            it2 = i + 11
                                        valid_chr_list = [i, iv2]
                                        test_chr_list = [i + 1, it2]

                                        test_chr_str = [str(i) for i in test_chr_list]
                                        test_chr_str = ",".join(test_chr_str)
                                        valid_chr_str = [str(i) for i in valid_chr_list]
                                        valid_chr_str = ",".join(valid_chr_str)

                                        model_name_cnn = (
                                            data_path
                                            + "/models/"
                                            + cell_line
                                            + "/Seq-CNN_e2e_"
                                            + cell_line
                                            + "_"
                                            + assay_type
                                            + "_FDR_"
                                            + fdr
                                            + "_valid_chr_"
                                            + valid_chr_str
                                            + "_test_chr_"
                                            + test_chr_str
                                            + ".h5"
                                        )
                                        model_cnn = tf.keras.models.load_model(model_name_cnn)
                                        model_cnn.trainable = False
                                        model_cnn._name = "Seq-CNN"

                                        with tf.GradientTape(persistent=True) as tape:
                                            inp = seq
                                            tape.watch(inp)
                                            preds, _, _, _, h = model_cnn(inp)
                                            # target_cnn = preds[:, explain_output_idx_cnn-1]+preds[:, explain_output_idx_cnn]+preds[:, explain_output_idx_cnn+1]
                                            target_cnn = preds[:, explain_output_idx_cnn]
                                        grads_cnn = grads_cnn + h * tape.gradient(target_cnn, h)
                                    grads_cnn = grads_cnn / 10
                                    np.save(
                                        data_path
                                        + "/results/numpy/feature_attribution/Seq-CNN_"
                                        + saliency_method
                                        + "_"
                                        + cell_line
                                        + "_"
                                        + gene_names_list[i]
                                        + ".npy",
                                        grads_cnn,
                                    )
                                else:
                                    grads_cnn = np.load(
                                        data_path
                                        + "/results/numpy/feature_attribution/Seq-CNN_"
                                        + saliency_method
                                        + "_"
                                        + cell_line
                                        + "_"
                                        + gene_names_list[i]
                                        + ".npy"
                                    )

                                scores_cnn = K.reshape(grads_cnn, [60000, 64])
                                scores_cnn = K.sum(scores_cnn, axis=1).numpy()
                                print(
                                    "cnn: ",
                                    scores_cnn.shape,
                                    np.min(scores_cnn),
                                    np.max(scores_cnn),
                                    np.mean(scores_cnn),
                                )

                            ############# Feature Attribution of Seq-GraphReg #############

                            if saliency_method == "saliency":
                                if load_fa == False:
                                    explain_output_idx_gat = np.floor((gene_tss_list[i] - pos[0]) / 5000).astype(
                                        "int64"
                                    )
                                    print(
                                        "explain_output_idx_gat: ",
                                        explain_output_idx_gat,
                                    )
                                    grads_gat = 0
                                    for j in range(1, 1 + 10):
                                        if organism == "mouse" and i == 9:
                                            iv2 = i + 10
                                            it2 = 1
                                        elif organism == "mouse" and i == 10:
                                            iv2 = 1
                                            it2 = 2
                                        else:
                                            iv2 = i + 10
                                            it2 = i + 11
                                        valid_chr_list = [i, iv2]
                                        test_chr_list = [i + 1, it2]

                                        test_chr_str = [str(i) for i in test_chr_list]
                                        test_chr_str = ",".join(test_chr_str)
                                        valid_chr_str = [str(i) for i in valid_chr_list]
                                        valid_chr_str = ",".join(valid_chr_str)

                                        model_name_gat = (
                                            data_path
                                            + "/models/"
                                            + cell_line
                                            + "/Seq-GraphReg_e2e_"
                                            + cell_line
                                            + "_"
                                            + assay_type
                                            + "_FDR_"
                                            + fdr
                                            + "_valid_chr_"
                                            + valid_chr_str
                                            + "_test_chr_"
                                            + test_chr_str
                                            + ".h5"
                                        )
                                        model_gat = tf.keras.models.load_model(
                                            model_name_gat,
                                            custom_objects={"GraphAttention": GraphAttention},
                                        )
                                        model_gat.trainable = False
                                        model_gat._name = "Seq-GraphReg"

                                        with tf.GradientTape(persistent=True) as tape:
                                            inp = seq
                                            tape.watch(inp)
                                            preds, _, _, _, h, _ = model_gat([inp, adj])
                                            target_gat = (
                                                preds[:, explain_output_idx_gat - 1]
                                                + preds[:, explain_output_idx_gat]
                                                + preds[:, explain_output_idx_gat + 1]
                                            )
                                            # target_gat = preds[:, explain_output_idx_gat]
                                        grads_gat = grads_gat + h * tape.gradient(target_gat, h)
                                    grads_gat = grads_gat / 10
                                    np.save(
                                        data_path
                                        + "/results/numpy/feature_attribution/Seq-GraphReg_"
                                        + saliency_method
                                        + "_"
                                        + cell_line
                                        + "_"
                                        + gene_names_list[i]
                                        + ".npy",
                                        grads_gat,
                                    )
                                else:
                                    grads_gat = np.load(
                                        data_path
                                        + "/results/numpy/feature_attribution/Seq-GraphReg_"
                                        + saliency_method
                                        + "_"
                                        + cell_line
                                        + "_"
                                        + gene_names_list[i]
                                        + ".npy"
                                    )

                                scores_gat = K.reshape(grads_gat, [60000, 64])
                                scores_gat = K.sum(scores_gat, axis=1).numpy()
                                print(
                                    "gat: ",
                                    scores_gat.shape,
                                    np.min(scores_gat),
                                    np.max(scores_gat),
                                    np.mean(scores_gat),
                                )

                            ########## Write bigwig saliency files #########
                            if write_bw == True and organism == "human" and genome == "hg19":
                                header = [
                                    ("chr1", 249250621),
                                    ("chr2", 243199373),
                                    ("chr3", 198022430),
                                    ("chr4", 191154276),
                                    ("chr5", 180915260),
                                    ("chr6", 171115067),
                                    ("chr7", 159138663),
                                    ("chr8", 146364022),
                                    ("chr9", 141213431),
                                    ("chr10", 135534747),
                                    ("chr11", 135006516),
                                    ("chr12", 133851895),
                                    ("chr13", 115169878),
                                    ("chr14", 107349540),
                                    ("chr15", 102531392),
                                    ("chr16", 90354753),
                                    ("chr17", 81195210),
                                    ("chr18", 78077248),
                                    ("chr19", 59128983),
                                    ("chr20", 63025520),
                                    ("chr21", 48129895),
                                    ("chr22", 51304566),
                                ]

                            if write_bw == True and organism == "human" and genome == "hg38":
                                header = [
                                    ("chr1", 248956422),
                                    ("chr2", 242193529),
                                    ("chr3", 198295559),
                                    ("chr4", 190214555),
                                    ("chr5", 181538259),
                                    ("chr6", 170805979),
                                    ("chr7", 159345973),
                                    ("chr8", 145138636),
                                    ("chr9", 138394717),
                                    ("chr10", 133797422),
                                    ("chr11", 135086622),
                                    ("chr12", 133275309),
                                    ("chr13", 114364328),
                                    ("chr14", 107043718),
                                    ("chr15", 101991189),
                                    ("chr16", 90338345),
                                    ("chr17", 83257441),
                                    ("chr18", 80373285),
                                    ("chr19", 58617616),
                                    ("chr20", 64444167),
                                    ("chr21", 46709983),
                                    ("chr22", 50818468),
                                ]

                            if write_bw == True and organism == "mouse":
                                header = [
                                    ("chr1", 195465000),
                                    ("chr2", 182105000),
                                    ("chr3", 160030000),
                                    ("chr4", 156500000),
                                    ("chr5", 151825000),
                                    ("chr6", 149730000),
                                    ("chr7", 145435000),
                                    ("chr8", 129395000),
                                    ("chr9", 124590000),
                                    ("chr10", 130685000),
                                    ("chr11", 122075000),
                                    ("chr12", 120120000),
                                    ("chr13", 120415000),
                                    ("chr14", 124895000),
                                    ("chr15", 104035000),
                                    ("chr16", 98200000),
                                    ("chr17", 94980000),
                                    ("chr18", 90695000),
                                    ("chr19", 61425000),
                                ]

                            if write_bw == True:
                                bw_GraphReg = pyBigWig.open(
                                    data_path
                                    + "/results/bigwig/feature_attribution/Seq-models/Seq-GraphReg_"
                                    + saliency_method
                                    + "_"
                                    + cell_line
                                    + "_"
                                    + gene_names_list[i]
                                    + ".bw",
                                    "w",
                                )
                                bw_GraphReg.addHeader(header)
                                bw_CNN = pyBigWig.open(
                                    data_path
                                    + "/results/bigwig/feature_attribution/Seq-models/Seq-CNN_"
                                    + saliency_method
                                    + "_"
                                    + cell_line
                                    + "_"
                                    + gene_names_list[i]
                                    + ".bw",
                                    "w",
                                )
                                bw_CNN.addHeader(header)

                                chroms = np.array([chrm] * len(pos))
                                print(chrm, len(pos))
                                starts = pos.astype(np.int64)
                                ends = starts + 100
                                ends = ends.astype(np.int64)

                                bw_GraphReg.addEntries(chroms, starts, ends=ends, values=scores_gat.ravel())
                                bw_CNN.addEntries(chroms, starts, ends=ends, values=scores_cnn.ravel())

                                bw_GraphReg.close()
                                bw_CNN.close()

                else:
                    break

    return "done"


#################### load model ####################
batch_size = 1
organism = "human"  # human/mouse
genome = "hg38"  # hg19/hg38
cell_line = ["hESC"]  # K562/GM12878/mESC/hESC
write_bw = True  # write the feature attribution scores to bigwig files
load_fa = False  # load feature attribution numpy files
saliency_method = "saliency"  # 'saliency'

df_candidate_genes = pd.read_csv(
    data_path + "/results/csv/cage_prediction/" + cell_line[0] + "_candidate_genes.csv",
    sep="\t",
    index_col=False,
)
df_candidate_genes = df_candidate_genes.drop(["Unnamed: 0"], axis=1)
gene_names_list = df_candidate_genes["gene"].values
chr_list = df_candidate_genes["chr"].values
gene_tss_list = df_candidate_genes["tss"].values

print(len(gene_names_list), gene_names_list)
print(len(chr_list), chr_list)
print(len(gene_tss_list), gene_tss_list)

calculate_loss(
    cell_line,
    gene_names_list,
    gene_tss_list,
    chr_list,
    batch_size,
    saliency_method,
    write_bw,
    load_fa,
    organism,
    genome,
)
