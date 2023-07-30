import time
from optparse import OptionParser

import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
from gat_layer import GraphAttention
from scipy.stats import spearmanr
from tensorflow import keras
from tensorflow.keras import backend as K
from tensorflow.keras import datasets, layers, models, regularizers
from tensorflow.keras.layers import Dropout, Input
from tensorflow.keras.models import Model
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.regularizers import l2


def main():
    usage = "usage: %prog [options]"
    parser = OptionParser(usage)
    parser.add_option("-c", dest="cell_line", default="K562", type="str")
    parser.add_option("-o", dest="organism", default="human", type="str")
    parser.add_option("-v", dest="valid_chr", default="chr1,chr11", type="str")
    parser.add_option("-t", dest="test_chr", default="chr2,chr12", type="str")
    parser.add_option("-p", dest="data_path", default="/media/labuser/STORAGE/GraphReg", type="str")
    parser.add_option("-a", dest="assay_type", default="HiC", type="str")
    parser.add_option("-q", dest="qval", default=0.1, type="float")
    parser.add_option("-n", dest="n_gat_layers", default=2, type="int")
    parser.add_option("-g", dest="generalizable", default=0, type="int")

    (options, args) = parser.parse_args()
    valid_chr_list = options.valid_chr.split(",")
    test_chr_list = options.test_chr.split(",")

    data_path = options.data_path
    assay_type = options.assay_type
    qval = options.qval

    if qval == 0.1:
        fdr = "1"
    elif qval == 0.01:
        fdr = "01"
    elif qval == 0.001:
        fdr = "001"
    elif qval == 0.5:
        fdr = "5"
    elif qval == 0.9:
        fdr = "9"

    print("organism:", options.organism)
    print("cell type:", options.cell_line)
    print("valid chrs: ", valid_chr_list)
    print("test chrs: ", test_chr_list)
    print("data path: ", options.data_path)
    print("3D assay type: ", options.assay_type)
    print("HiCDCPlus FDR: ", options.qval)
    print("number of GAT layers: ", options.n_gat_layers)
    print("generalizables: ", options.generalizable)

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

        return {
            "last_batch": last_batch,
            "X_epi": X_epi,
            "Y": Y,
            "adj": adj,
            "tss_idx": tss_idx,
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
            T = 400  # number of 5kb bins inside middle 2Mb region
            b = 50  # number of 100bp bins inside 5Kb region
            F = 3  # number of Epigenomic tracks used in model
            X_epi = next_datum["X_epi"]
            batch_size = tf.shape(X_epi)[0]
            X_epi = tf.reshape(X_epi, [batch_size, 3 * T * b, F])
            adj = next_datum["adj"]
            adj = tf.reshape(adj, [batch_size, 3 * T, 3 * T])

            # last_batch = next_datum['last_batch']
            tss_idx = next_datum["tss_idx"]
            tss_idx = tf.reshape(tss_idx, [3 * T])
            idx = tf.range(T, 2 * T)

            Y = next_datum["Y"]
            # Y = tf.reshape(Y, [batch_size, 3*T, b])
            # Y = tf.reduce_sum(Y, axis=2)
            Y = tf.reshape(Y, [batch_size, 3 * T])

        else:
            X_epi = 0
            Y = 0
            adj = 0
            tss_idx = 0
            idx = 0
        return data_exist, X_epi, Y, adj, idx, tss_idx

    def calculate_loss(model_gat, chr_list, cell_lines, batch_size, assay_type, fdr):
        loss_gat_all = np.array([])
        rho_gat_all = np.array([])
        Y_hat_all = np.array([])
        Y_all = np.array([])
        for num, cell_line in enumerate(cell_lines):
            for chr in chr_list:
                if options.generalizable == 0:
                    file_name = (
                        data_path
                        + "/data/tfrecords/distal_reg_paper/tfr_epi_"
                        + cell_line
                        + "_"
                        + assay_type
                        + "_FDR_"
                        + fdr
                        + "_"
                        + chr
                        + ".tfr"
                    )
                else:
                    file_name = (
                        data_path
                        + "/data/tfrecords/distal_reg_paper/tfr_epi_RPGC_"
                        + cell_line
                        + "_"
                        + assay_type
                        + "_FDR_"
                        + fdr
                        + "_"
                        + chr
                        + ".tfr"
                    )

                iterator = dataset_iterator(file_name, batch_size)
                while True:
                    data_exist, X_epi, Y, adj, idx, tss_idx = read_tf_record_1shot(iterator)
                    if data_exist:
                        if tf.reduce_sum(tf.gather(tss_idx, idx)) > 0:
                            Y_hat, _ = model_gat([X_epi, adj])
                            Y_hat_idx = tf.gather(Y_hat, idx, axis=1)
                            Y_idx = tf.gather(Y, idx, axis=1)

                            loss = poisson_loss(Y_idx, Y_hat_idx)
                            loss_gat_all = np.append(loss_gat_all, loss.numpy())
                            e1 = np.random.normal(0, 1e-6, size=len(Y_idx.numpy().ravel()))
                            e2 = np.random.normal(0, 1e-6, size=len(Y_idx.numpy().ravel()))

                            rho_gat_all = np.append(
                                rho_gat_all,
                                np.corrcoef(
                                    np.log2(Y_idx.numpy().ravel() + 1) + e1,
                                    np.log2(Y_hat_idx.numpy().ravel() + 1) + e2,
                                )[0, 1],
                            )
                            Y_hat_all = np.append(Y_hat_all, Y_hat_idx.numpy().ravel())
                            Y_all = np.append(Y_all, Y_idx.numpy().ravel())
                    else:
                        break

        print("len of test/valid Y: ", len(Y_all))
        valid_loss = np.mean(loss_gat_all)
        rho = np.mean(rho_gat_all)

        return valid_loss, rho

    # Parameters
    T = 400
    b = 50
    N = 3 * T  # number of 5Kb bins inside 6Mb region
    F = 3  # feature dimension
    F_ = 32  # output size of GraphAttention layer
    n_attn_heads = 4  # number of attention heads in GAT layers
    dropout_rate = 0.5  # dropout rate
    l2_reg = 0.0  # factor for l2 regularization
    re_load = False

    # Model definition
    if re_load:
        model_name = "model_name.h5"
        model = tf.keras.models.load_model(model_name, custom_objects={"GraphAttention": GraphAttention})
        model.summary()
    else:
        tf.keras.backend.clear_session()
        X_in = Input(shape=(3 * T * b, F))
        A_in = Input(shape=(N, N))

        x = layers.Conv1D(
            128,
            25,
            activation="relu",
            padding="same",
            kernel_regularizer=l2(l2_reg),
            bias_regularizer=l2(l2_reg),
        )(X_in)
        x = layers.BatchNormalization()(x)
        x = layers.MaxPool1D(2)(x)

        x = Dropout(dropout_rate)(x)
        x = layers.Conv1D(
            128,
            3,
            activation="relu",
            padding="same",
            kernel_regularizer=l2(l2_reg),
            bias_regularizer=l2(l2_reg),
        )(x)
        x = layers.BatchNormalization()(x)
        x = layers.MaxPool1D(5)(x)

        x = Dropout(dropout_rate)(x)
        x = layers.Conv1D(
            128,
            3,
            activation="relu",
            padding="same",
            kernel_regularizer=l2(l2_reg),
            bias_regularizer=l2(l2_reg),
        )(x)
        x = layers.BatchNormalization()(x)
        x = layers.MaxPool1D(5)(x)

        att = []
        for i in range(options.n_gat_layers):
            x, att_ = GraphAttention(
                F_,
                attn_heads=n_attn_heads,
                attn_heads_reduction="concat",
                dropout_rate=dropout_rate,
                activation="elu",
                kernel_regularizer=l2(l2_reg),
                attn_kernel_regularizer=l2(l2_reg),
            )([x, A_in])
            x = layers.BatchNormalization()(x)
            att.append(att_)

        x = Dropout(dropout_rate)(x)
        x = layers.Conv1D(
            128,
            1,
            activation="relu",
            padding="same",
            kernel_regularizer=l2(l2_reg),
            bias_regularizer=l2(l2_reg),
        )(x)
        x = layers.BatchNormalization()(x)

        mu_cage = layers.Conv1D(
            1,
            1,
            activation="exponential",
            padding="same",
            kernel_regularizer=l2(l2_reg),
            bias_regularizer=l2(l2_reg),
        )(x)
        mu_cage = layers.Reshape([3 * T])(mu_cage)

        # Build model
        model_gat = Model(inputs=[X_in, A_in], outputs=[mu_cage, att])
        model_gat._name = "Epi-GraphReg"
        model_gat.summary()
        # print(len(model_gat.trainable_variables))
        # keras.utils.plot_model(model, 'GAT.png', show_shapes = True)

    ########## training ##########

    cell_line = options.cell_line
    cell_lines = [cell_line]
    if options.generalizable == 0:
        model_name_gat = (
            data_path
            + "/models/"
            + cell_line
            + "/distal_reg_paper/Epi-GraphReg_"
            + cell_line
            + "_"
            + options.assay_type
            + "_FDR_"
            + fdr
            + "_valid_"
            + options.valid_chr
            + "_test_"
            + options.test_chr
            + ".h5"
        )
    else:
        model_name_gat = (
            data_path
            + "/models/"
            + cell_line
            + "/distal_reg_paper/Epi-GraphReg_RPGC_"
            + cell_line
            + "_"
            + options.assay_type
            + "_FDR_"
            + fdr
            + "_valid_"
            + options.valid_chr
            + "_test_"
            + options.test_chr
            + ".h5"
        )

    if options.organism == "mouse":
        train_chr_list = ["chr" + str(i) for i in range(1, 20)] + ["chrX"]
        vt = valid_chr_list + test_chr_list
        for j in range(len(vt)):
            train_chr_list.remove(vt[j])
    else:
        train_chr_list = ["chr" + str(i) for i in range(1, 23)] + ["chrX"]
        vt = valid_chr_list + test_chr_list
        for j in range(len(vt)):
            train_chr_list.remove(vt[j])

    best_loss = 1e20
    max_early_stopping = 10
    n_epochs = 100
    opt = tf.keras.optimizers.Adam(learning_rate=0.0002, decay=1e-6)
    batch_size = 1
    t0 = time.time()
    for epoch in range(1, n_epochs + 1):
        loss_gat_all = np.array([])
        rho_gat_all = np.array([])
        Y_hat_all = np.array([])
        Y_all = np.array([])
        for num, cell_line in enumerate(cell_lines):
            for chr in train_chr_list:
                if options.generalizable == 0:
                    file_name_train = (
                        data_path
                        + "/data/tfrecords/distal_reg_paper/tfr_epi_"
                        + cell_line
                        + "_"
                        + assay_type
                        + "_FDR_"
                        + fdr
                        + "_"
                        + chr
                        + ".tfr"
                    )
                else:
                    file_name_train = (
                        data_path
                        + "/data/tfrecords/distal_reg_paper/tfr_epi_RPGC_"
                        + cell_line
                        + "_"
                        + assay_type
                        + "_FDR_"
                        + fdr
                        + "_"
                        + chr
                        + ".tfr"
                    )

                iterator_train = dataset_iterator(file_name_train, batch_size)
                while True:
                    data_exist, X_epi, Y, adj, idx, tss_idx = read_tf_record_1shot(iterator_train)
                    if data_exist:
                        if tf.reduce_sum(tf.gather(tss_idx, idx)) > 0:
                            with tf.GradientTape() as tape:
                                Y_hat, _ = model_gat([X_epi, adj])
                                Y_hat_idx = tf.gather(Y_hat, idx, axis=1)
                                Y_idx = tf.gather(Y, idx, axis=1)
                                loss = poisson_loss(Y_idx, Y_hat_idx)

                            grads = tape.gradient(loss, model_gat.trainable_variables)
                            opt.apply_gradients(zip(grads, model_gat.trainable_variables))

                            loss_gat_all = np.append(loss_gat_all, loss.numpy())
                            e1 = np.random.normal(0, 1e-6, size=len(Y_idx.numpy().ravel()))
                            e2 = np.random.normal(0, 1e-6, size=len(Y_idx.numpy().ravel()))

                            rho_gat_all = np.append(
                                rho_gat_all,
                                np.corrcoef(
                                    np.log2(Y_idx.numpy().ravel() + 1) + e1,
                                    np.log2(Y_hat_idx.numpy().ravel() + 1) + e2,
                                )[0, 1],
                            )
                            Y_hat_all = np.append(Y_hat_all, Y_hat_idx.numpy().ravel())
                            Y_all = np.append(Y_all, Y_idx.numpy().ravel())
                    else:
                        break
        if epoch == 1:
            print("len of train Y: ", len(Y_all))

        train_loss = np.mean(loss_gat_all)
        rho = np.mean(rho_gat_all)
        print(
            "epoch: ",
            epoch,
            ", train loss: ",
            train_loss,
            ", train rho: ",
            rho,
            ", time passed: ",
            (time.time() - t0),
            " sec",
        )

        # if epoch%10 == 0:
        #    model_name_gat_per_epoch = data_path+'/models/'+cell_line+'/distal_reg_paper/Epi-GraphReg_RPGC_'+cell_line+'_'+options.assay_type+'_FDR_'+fdr+'_valid_chr_'+options.valid_chr+'_test_chr_'+options.test_chr+'_epoch_'+str(epoch)+'.h5'
        #    model_gat.save(model_name_gat_per_epoch)

        valid_loss, valid_rho = calculate_loss(model_gat, valid_chr_list, cell_lines, batch_size, assay_type, fdr)

        if valid_loss < best_loss:
            early_stopping_counter = 1
            best_loss = valid_loss
            model_gat.save(model_name_gat)
            print(
                "epoch: ",
                epoch,
                ", valid loss: ",
                valid_loss,
                ", valid rho: ",
                valid_rho,
                ", time passed: ",
                (time.time() - t0),
                " sec",
            )
            test_loss, test_rho = calculate_loss(model_gat, test_chr_list, cell_lines, batch_size, assay_type, fdr)
            print(
                "epoch: ",
                epoch,
                ", test loss: ",
                test_loss,
                ", test rho: ",
                test_rho,
                ", time passed: ",
                (time.time() - t0),
                " sec",
            )

        else:
            early_stopping_counter += 1
            if early_stopping_counter == max_early_stopping:
                break


################################################################################
# __main__
################################################################################
if __name__ == "__main__":
    main()
