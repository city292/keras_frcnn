"""
this code will train on kitti data set
"""
from __future__ import division
import random
import pprint
import sys
import time
import os

os.environ['CUDA_VISIBLE_DEVICES'] = '1'
import numpy as np
import pickle
from keras import backend as K
from keras.optimizers import Adam, SGD, RMSprop, adadelta
from keras.layers import Input
from keras.models import Model
from keras_frcnn import config, data_generators
from keras_frcnn import losses as losses_fn
import keras_frcnn.roi_helpers as roi_helpers
from keras.utils import generic_utils
import multiprocessing
from keras_frcnn import vgg as nn
from keras_frcnn.simple_parser import get_data
import tensorflow
from keras.backend.tensorflow_backend import set_session
import datetime
configtf = tensorflow.ConfigProto()
configtf.gpu_options.allow_growth = True
set_session(tensorflow.Session(config=configtf))

def make_dir(dir):
    if not os.path.exists(dir):
        os.mkdir(dir)
    return


def train_kitti():
    # config for data argument
    cfg = config.Config()

    cfg.use_horizontal_flips = True
    cfg.use_vertical_flips = True
    cfg.rot_90 = True
    cfg.num_rois = 32
    cfg.base_net_weights = os.path.join('./model/', nn.get_weight_path())
    # cfg.base_net_weights=r''

    # TODO: the only file should to be change for other data to train
    cfg.model_path = '/media/private/Ci/log/plane/frcnn/vgg-adam'

    now = datetime.datetime.now()
    day = now.strftime('%y-%m-%d')
    for i in range(10000):
        if not os.path.exists('%s-%s-%d' % (cfg.model_path,day,i)):
            cfg.model_path = '%s-%s-%d' % (cfg.model_path,day,i)
            break

    make_dir(cfg.model_path)
    make_dir(cfg.model_path+'/loss')
    make_dir(cfg.model_path + '/loss_rpn_cls')
    make_dir(cfg.model_path + '/loss_rpn_regr')
    make_dir(cfg.model_path + '/loss_class_cls')
    make_dir(cfg.model_path + '/loss_class_regr')

    cfg.simple_label_file = '/media/public/GEOWAY/plane/plane0817.csv'

    all_images, classes_count, class_mapping = get_data(cfg.simple_label_file)

    if 'bg' not in classes_count:
        classes_count['bg'] = 0
        class_mapping['bg'] = len(class_mapping)

    cfg.class_mapping = class_mapping
    cfg.config_save_file= os.path.join(cfg.model_path,'config.pickle')
    with open(cfg.config_save_file, 'wb') as config_f:
        pickle.dump(cfg, config_f)
        print('Config has been written to {}, and can be loaded when testing to ensure correct results'.format(
            cfg.config_save_file))

    inv_map = {v: k for k, v in class_mapping.items()}

    print('Training images per class:')
    pprint.pprint(classes_count)
    print('Num classes (including bg) = {}'.format(len(classes_count)))
    random.shuffle(all_images)
    num_imgs = len(all_images)
    train_imgs = [s for s in all_images if s['imageset'] == 'trainval']
    val_imgs = [s for s in all_images if s['imageset'] == 'test']

    print('Num train samples {}'.format(len(train_imgs)))
    print('Num val samples {}'.format(len(val_imgs)))

    data_gen_train = data_generators.get_anchor_gt(train_imgs, classes_count, cfg, nn.get_img_output_length,
                                                   K.image_dim_ordering(), mode='train')
    data_gen_val = data_generators.get_anchor_gt(val_imgs, classes_count, cfg, nn.get_img_output_length,
                                                 K.image_dim_ordering(), mode='val')
    Q = multiprocessing.Manager().Queue(maxsize=30)

    def fill_Q(n):
        while True:

            if not Q.full():
                Q.put(next(data_gen_train))
                #print(Q.qsize(),'put',n)
            else:
                time.sleep(0.00001)


    threads=[]
    for i in range(4):
        thread= multiprocessing.Process(target=fill_Q,args=(i,))
        threads.append(thread)
        thread.start()


    if K.image_dim_ordering() == 'th':
        input_shape_img = (3, None, None)
    else:
        input_shape_img = (None, None, 3)

    img_input = Input(shape=input_shape_img)
    roi_input = Input(shape=(None, 4))

    # define the base network (resnet here, can be VGG, Inception, etc)
    shared_layers = nn.nn_base(img_input, trainable=True)

    # define the RPN, built on the base layers
    num_anchors = len(cfg.anchor_box_scales) * len(cfg.anchor_box_ratios)
    rpn = nn.rpn(shared_layers, num_anchors)

    classifier = nn.classifier(shared_layers, roi_input, cfg.num_rois, nb_classes=len(classes_count), trainable=True)

    model_rpn = Model(img_input, rpn[:2])
    model_classifier = Model([img_input, roi_input], classifier)

    # this is a model that holds both the RPN and the classifier, used to load/save weights for the models
    model_all = Model([img_input, roi_input], rpn[:2] + classifier)
    # model_all.summary()
    from keras.utils import plot_model
   # os.environ['PATH'] = os.environ['PATH'] + r';C:\Program Files (x86)\Graphviz2.38\bin;'

    plot_model(model_all, 'model_all.png', show_layer_names=True, show_shapes=True)
    plot_model(model_classifier, 'model_classifier.png', show_layer_names=True, show_shapes=True)
    plot_model(model_rpn, 'model_rpn.png', show_layer_names=True, show_shapes=True)
    '''
    try:
        print('loading weights from {}'.format(cfg.base_net_weights))
        model_rpn.load_weights(cfg.model_path, by_name=True)
        model_classifier.load_weights(cfg.model_path, by_name=True)
    except Exception as e:
        print(e)
        print('Could not load pretrained model weights. Weights can be found in the keras application folder '
              'https://github.com/fchollet/keras/tree/master/keras/applications')
    '''

    optimizer = adadelta()
    optimizer_classifier = adadelta()
    model_rpn.compile(optimizer=optimizer,
                      loss=[losses_fn.rpn_loss_cls(num_anchors), losses_fn.rpn_loss_regr(num_anchors)])
    model_classifier.compile(optimizer=optimizer_classifier,
                             loss=[losses_fn.class_loss_cls, losses_fn.class_loss_regr(len(classes_count) - 1)],
                             metrics={'dense_class_{}'.format(len(classes_count)): 'accuracy'})
    model_all.compile(optimizer='sgd', loss='mae')

    epoch_length = 10
    num_epochs = int(cfg.num_epochs)
    iter_num = 0

    losses = np.zeros((epoch_length, 5))
    rpn_accuracy_rpn_monitor = []
    rpn_accuracy_for_epoch = []
    start_time = time.time()

    best_loss = np.Inf
    best_rpn_cls = np.Inf
    best_rpn_regr = np.Inf
    best_class_cls = np.Inf
    best_class_regr = np.Inf

    class_mapping_inv = {v: k for k, v in class_mapping.items()}
    print('Starting training')

    vis = True

    for epoch_num in range(num_epochs):

        progbar = generic_utils.Progbar(epoch_length)
        print('Epoch {}/{}'.format(epoch_num + 1, num_epochs))

        while True:
            try:

                if len(rpn_accuracy_rpn_monitor) == epoch_length and cfg.verbose:
                    mean_overlapping_bboxes = float(sum(rpn_accuracy_rpn_monitor)) / len(rpn_accuracy_rpn_monitor)
                    rpn_accuracy_rpn_monitor = []
                    print(
                        'Average number of overlapping bounding boxes from RPN = {} for {} previous iterations'.format(
                            mean_overlapping_bboxes, epoch_length))
                    if mean_overlapping_bboxes == 0:
                        print('RPN is not producing bounding boxes that overlap'
                              ' the ground truth boxes. Check RPN settings or keep training.')

            #    X, Y, img_data = next(data_gen_train)
                while True:

                    if Q.empty():
                        time.sleep(0.00001)
                        continue

                    X, Y, img_data = Q.get()
                #    print(Q.qsize(),'get')
                    break
              #  print(X.shape,Y.shape)
                loss_rpn = model_rpn.train_on_batch(X, Y)

                P_rpn = model_rpn.predict_on_batch(X)

                result = roi_helpers.rpn_to_roi(P_rpn[0], P_rpn[1], cfg, K.image_dim_ordering(), use_regr=True,
                                                overlap_thresh=0.7,
                                                max_boxes=300)
                # note: calc_iou converts from (x1,y1,x2,y2) to (x,y,w,h) format
                X2, Y1, Y2, IouS = roi_helpers.calc_iou(result, img_data, cfg, class_mapping)

                if X2 is None:
                    rpn_accuracy_rpn_monitor.append(0)
                    rpn_accuracy_for_epoch.append(0)
                    continue

                neg_samples = np.where(Y1[0, :, -1] == 1)
                pos_samples = np.where(Y1[0, :, -1] == 0)

                if len(neg_samples) > 0:
                    neg_samples = neg_samples[0]
                else:
                    neg_samples = []

                if len(pos_samples) > 0:
                    pos_samples = pos_samples[0]
                else:
                    pos_samples = []

                rpn_accuracy_rpn_monitor.append(len(pos_samples))
                rpn_accuracy_for_epoch.append((len(pos_samples)))

                if cfg.num_rois > 1:
                    if len(pos_samples) < cfg.num_rois // 2:
                        selected_pos_samples = pos_samples.tolist()
                    else:
                        selected_pos_samples = np.random.choice(pos_samples, cfg.num_rois // 2, replace=False).tolist()
                    try:
                        selected_neg_samples = np.random.choice(neg_samples, cfg.num_rois - len(selected_pos_samples),
                                                                replace=False).tolist()
                    except:
                        selected_neg_samples = np.random.choice(neg_samples, cfg.num_rois - len(selected_pos_samples),
                                                                replace=True).tolist()

                    sel_samples = selected_pos_samples + selected_neg_samples
                else:
                    # in the extreme case where num_rois = 1, we pick a random pos or neg sample
                    selected_pos_samples = pos_samples.tolist()
                    selected_neg_samples = neg_samples.tolist()
                    if np.random.randint(0, 2):
                        sel_samples = random.choice(neg_samples)
                    else:
                        sel_samples = random.choice(pos_samples)

                loss_class = model_classifier.train_on_batch([X, X2[:, sel_samples, :]],
                                                             [Y1[:, sel_samples, :], Y2[:, sel_samples, :]])

                losses[iter_num, 0] = loss_rpn[1]
                losses[iter_num, 1] = loss_rpn[2]

                losses[iter_num, 2] = loss_class[1]
                losses[iter_num, 3] = loss_class[2]
                losses[iter_num, 4] = loss_class[3]

                iter_num += 1

                progbar.update(iter_num,
                               [('rpn_cls', np.mean(losses[:iter_num, 0])), ('rpn_regr', np.mean(losses[:iter_num, 1])),
                                ('detector_cls', np.mean(losses[:iter_num, 2])),
                                ('detector_regr', np.mean(losses[:iter_num, 3]))])

                if iter_num == epoch_length:
                    loss_rpn_cls = np.mean(losses[:, 0])
                    loss_rpn_regr = np.mean(losses[:, 1])
                    loss_class_cls = np.mean(losses[:, 2])
                    loss_class_regr = np.mean(losses[:, 3])
                    class_acc = np.mean(losses[:, 4])

                    mean_overlapping_bboxes = float(sum(rpn_accuracy_for_epoch)) / len(rpn_accuracy_for_epoch)
                    rpn_accuracy_for_epoch = []

                    if cfg.verbose:
                        print('Mean number of bounding boxes from RPN overlapping ground truth boxes: {}'.format(
                            mean_overlapping_bboxes))
                        print('Classifier accuracy for bounding boxes from RPN: {}'.format(class_acc))
                        print('Loss RPN classifier: {}'.format(loss_rpn_cls))
                        print('Loss RPN regression: {}'.format(loss_rpn_regr))
                        print('Loss Detector classifier: {}'.format(loss_class_cls))
                        print('Loss Detector regression: {}'.format(loss_class_regr))
                        print('Elapsed time: {}'.format(time.time() - start_time))

                    curr_loss = loss_rpn_cls + loss_rpn_regr + loss_class_cls + loss_class_regr
                    iter_num = 0
                    start_time = time.time()

                    if curr_loss < best_loss:
                        if cfg.verbose:
                            print('Total loss decreased from {} to {}, saving weights'.format(best_loss, curr_loss))
                        best_loss = curr_loss
                        model_all.save_weights(
                            '%s/%s/E-%d-loss-%.4f-rpnc-%.4f-rpnr-%.4f-cls-%.4f-cr-%.4f.hdf5' % (
                                cfg.model_path,'loss',epoch_num,
                                curr_loss, loss_rpn_cls, loss_rpn_regr, loss_class_cls,
                                loss_class_regr)
                        )
                    if loss_rpn_cls < best_rpn_cls:
                        if cfg.verbose:
                            print('loss_rpn_cls decreased from {} to {}, saving weights'.format(best_rpn_cls, loss_rpn_cls))
                            best_rpn_cls = loss_rpn_cls
                        model_all.save_weights(

                            '%s/%s/E-%d-loss-%.4f-rpnc-%.4f-rpnr-%.4f-cls-%.4f-cr-%.4f.hdf5' % (
                            cfg.model_path, 'loss_rpn_cls',epoch_num,
                            curr_loss, loss_rpn_cls, loss_rpn_regr, loss_class_cls,
                            loss_class_regr)
                        )
                    if loss_rpn_regr < best_rpn_regr:
                        if cfg.verbose:
                            print('loss_rpn_regr decreased from {} to {}, saving weights'.format(best_rpn_regr, loss_rpn_regr))
                            best_rpn_regr = loss_rpn_regr
                        model_all.save_weights(

                            '%s/%s/E-%d-loss-%.4f-rpnc-%.4f-rpnr-%.4f-cls-%.4f-cr-%.4f.hdf5' % (
                            cfg.model_path, 'loss_rpn_regr',epoch_num,
                            curr_loss, loss_rpn_cls, loss_rpn_regr, loss_class_cls,
                            loss_class_regr)
                        )
                    if loss_class_cls < best_class_cls:
                        if cfg.verbose:
                            print('loss_class_cls decreased from {} to {}, saving weights'.format(best_loss, loss_class_cls))
                            best_class_cls = loss_class_cls
                        model_all.save_weights(

                            '%s/%s/E-%d-loss-%.4f-rpnc-%.4f-rpnr-%.4f-cls-%.4f-cr-%.4f.hdf5' % (
                            cfg.model_path, 'loss_class_cls',epoch_num,
                            curr_loss, loss_rpn_cls, loss_rpn_regr, loss_class_cls,
                            loss_class_regr)
                        )
                    if loss_class_regr < best_class_regr:
                        if cfg.verbose:
                            print('loss_class_regr decreased from {} to {}, saving weights'.format(best_loss, loss_class_regr))
                            best_class_regr = loss_class_regr
                        model_all.save_weights(

                            '%s/%s/E-%d-loss-%.4f-rpnc-%.4f-rpnr-%.4f-cls-%.4f-cr-%.4f.hdf5' % (
                            cfg.model_path, 'loss_class_regr',epoch_num,
                            curr_loss, loss_rpn_cls, loss_rpn_regr, loss_class_cls,
                            loss_class_regr)
                        )

                    break

            except Exception as e:
             #   print('Exception: {}'.format(e))
                # save model
            #    model_all.save_weights(cfg.model_path)
                continue
    print('Training complete, exiting.')


if __name__ == '__main__':
    train_kitti()
