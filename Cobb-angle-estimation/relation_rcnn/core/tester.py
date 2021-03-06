# --------------------------------------------------------
# Relation Networks for Object Detection
# Copyright (c) 2017 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Modified by Jiayuan Gu, Dazhi Cheng, Yuwen Xiong
# --------------------------------------------------------
# Based on:
# MX-RCNN
# Copyright (c) 2016 by Contributors
# Licence under The Apache 2.0 License
# https://github.com/ijkguo/mx-rcnn/
# --------------------------------------------------------


import cPickle
import os
import time
import mxnet as mx
import numpy as np

from module import MutableModule
from utils import image
from bbox.bbox_transform import bbox_pred, clip_boxes
from nms.nms import py_nms_wrapper, py_softnms_wrapper, cpu_nms_wrapper, gpu_nms_wrapper
from utils.PrefetchingIter import PrefetchingIterV2 as PrefetchingIter
import scipy.io

class Predictor(object):
    def __init__(self, symbol, data_names, label_names,
                 context=mx.cpu(), max_data_shapes=None, max_label_shapes=None,
                 provide_data=None, provide_label=None,
                 arg_params=None, aux_params=None):
        self._mod = MutableModule(symbol, data_names, label_names,
                                  context=context, max_data_shapes=max_data_shapes, max_label_shapes=max_label_shapes)
        self._mod.bind(provide_data, provide_label, for_training=False)
        self._mod.init_params(arg_params=arg_params, aux_params=aux_params)

    def predict(self, data_batch):
        self._mod.forward(data_batch)
        # [dict(zip(self._mod.output_names, _)) for _ in zip(*self._mod.get_outputs(merge_multi_context=False))]
        return [dict(zip(self._mod.output_names, _)) for _ in zip(*self._mod.get_outputs(merge_multi_context=False))]


def im_proposal(predictor, data_batch, data_names, scales):
    output_all = predictor.predict(data_batch)

    data_dict_all = [dict(zip(data_names, data_batch.data[i])) for i in xrange(len(data_batch.data))]
    scores_all = []
    boxes_all = []

    for output, data_dict, scale in zip(output_all, data_dict_all, scales):
        # drop the batch index
        boxes = output['rois_output'].asnumpy()[:, 1:]
        scores = output['rois_score'].asnumpy()

        # transform to original scale
        boxes = boxes / scale
        scores_all.append(scores)
        boxes_all.append(boxes)

    return scores_all, boxes_all, data_dict_all


def generate_proposals(predictor, test_data, imdb, cfg, vis=False, thresh=0.):
    """
    Generate detections results using RPN.
    :param predictor: Predictor
    :param test_data: data iterator, must be non-shuffled
    :param imdb: image database
    :param vis: controls visualization
    :param thresh: thresh for valid detections
    :return: list of detected boxes
    """
    assert vis or not test_data.shuffle
    data_names = [k[0] for k in test_data.provide_data[0]]

    if not isinstance(test_data, PrefetchingIter):
        test_data = PrefetchingIter(test_data)

    idx = 0
    t = time.time()
    imdb_boxes = list()
    original_boxes = list()
    for im_info, data_batch in test_data:
        t1 = time.time() - t
        t = time.time()

        scales = [iim_info[0, 2] for iim_info in im_info]
        scores_all, boxes_all, data_dict_all = im_proposal(predictor, data_batch, data_names, scales)
        t2 = time.time() - t
        t = time.time()
        for delta, (scores, boxes, data_dict, scale) in enumerate(zip(scores_all, boxes_all, data_dict_all, scales)):
            # assemble proposals
            dets = np.hstack((boxes, scores))
            original_boxes.append(dets)

            # filter proposals
            keep = np.where(dets[:, 4:] > thresh)[0]
            dets = dets[keep, :]
            imdb_boxes.append(dets)

            if vis:
                vis_all_detection(data_dict['data'].asnumpy(), [dets], ['obj'], scale, cfg)

            print 'generating %d/%d' % (idx + 1, imdb.num_images), 'proposal %d' % (dets.shape[0]), \
                'data %.4fs net %.4fs' % (t1, t2 / test_data.batch_size)
            idx += 1


    assert len(imdb_boxes) == imdb.num_images, 'calculations not complete'

    # save results
    rpn_folder = os.path.join(imdb.result_path, 'rpn_data')
    if not os.path.exists(rpn_folder):
        os.mkdir(rpn_folder)

    rpn_file = os.path.join(rpn_folder, imdb.name + '_rpn.pkl')
    with open(rpn_file, 'wb') as f:
        cPickle.dump(imdb_boxes, f, cPickle.HIGHEST_PROTOCOL)

    if thresh > 0:
        full_rpn_file = os.path.join(rpn_folder, imdb.name + '_full_rpn.pkl')
        with open(full_rpn_file, 'wb') as f:
            cPickle.dump(original_boxes, f, cPickle.HIGHEST_PROTOCOL)

    print 'wrote rpn proposals to {}'.format(rpn_file)
    return imdb_boxes


def im_detect(predictor, data_batch, data_names, scales, cfg):
    output_all = predictor.predict(data_batch)
#    print output_all
    data_dict_all = [dict(zip(data_names, idata)) for idata in data_batch.data]
    scores_all = []
    pred_boxes_all = []
    pred_points_all = []
    for output, data_dict, scale in zip(output_all, data_dict_all, scales):
        if cfg.TEST.HAS_RPN or cfg.network.ROIDispatch:
            rois = output['rois_output'].asnumpy()[:, 1:]
        else:
            rois = data_dict['rois'].asnumpy().reshape((-1, 5))[:, 1:]
        im_shape = data_dict['data'].shape

        # save output
        if cfg.TEST.LEARN_NMS:
            pred_boxes = output['learn_nms_sorted_bbox'].asnumpy()
            # raw_scores = output['sorted_score_output'].asnumpy()
            scores = output['nms_final_score_output'].asnumpy()
        else:
            scores = output['cls_prob_reshape_output'].asnumpy()[0]
            bbox_deltas = output['bbox_pred_reshape_output'].asnumpy()[0]
          
            # post processing
            pred_boxes = bbox_pred(rois, bbox_deltas)
            pred_boxes = clip_boxes(pred_boxes, im_shape[-2:])
            pred_points = output['points_pred_reshape_output'].asnumpy()[0]

        # we used scaled image & roi to train, so it is necessary to transform them back
        pred_boxes = pred_boxes / scale

        scores_all.append(scores)
        pred_boxes_all.append(pred_boxes)
        pred_points_all.append(pred_points)
    return scores_all, pred_boxes_all, pred_points_all, data_dict_all

def iou(box1, box2):
    area1 = (box1[3]-box1[1])*(box1[2]-box1[0])
    area2 = (box2[3]-box2[1])*(box2[2]-box2[0])
    zuo = max(box1[0],box2[0])
    you = min(box1[2],box2[2])
    sha = max(box1[1],box2[1])
    xia = min(box1[3],box2[3])
    hen = max(0,you-zuo)
    shu = max(0,xia-sha)
    overlap = hen*shu
    iou = float(overlap)/(area1+area2-overlap)

    return iou

def pred_eval(predictor, test_data, imdb, cfg, vis=False, thresh=1e-3, logger=None, ignore_cache=True):
    """
    wrapper for calculating offline validation for faster data analysis
    in this example, all threshold are set by hand
    :param predictor: Predictor
    :param test_data: data iterator, must be non-shuffle
    :param imdb: image database
    :param vis: controls visualization
    :param thresh: valid detection threshold
    :return:
    """

    det_file = os.path.join(imdb.result_path, imdb.name + '_detections.pkl')
    if os.path.exists(det_file) and not ignore_cache:
        with open(det_file, 'rb') as fid:
            all_boxes = cPickle.load(fid)
        info_str = imdb.evaluate_detections(all_boxes)
        if logger:
            logger.info('evaluate detections: \n{}'.format(info_str))
        return

    assert vis or not test_data.shuffle
    data_names = [k[0] for k in test_data.provide_data[0]]
    if not isinstance(test_data, PrefetchingIter):
        test_data = PrefetchingIter(test_data)

    #if cfg.TEST.SOFTNMS:
    #    nms = py_softnms_wrapper(cfg.TEST.NMS)
    #else:
    #    nms = py_nms_wrapper(cfg.TEST.NMS)

    if cfg.TEST.SOFTNMS:
        nms = py_softnms_wrapper(cfg.TEST.NMS)
    else:
        nms = py_nms_wrapper(cfg.TEST.NMS)


    # limit detections to max_per_image over all classes
    max_per_image = cfg.TEST.max_per_image

    num_images = imdb.num_images
    # all detections are collected into:
    #    all_boxes[cls][image] = N x 5 array of detections in
    #    (x1, y1, x2, y2, score)
    all_boxes = [[[] for _ in range(num_images)]
                 for _ in range(imdb.num_classes)]
    class_lut = [[] for _ in range(imdb.num_classes)]
    valid_tally = 0
    valid_sum = 0

    idx = 0
    t = time.time()
    inference_count = 0
    all_inference_time = []
    post_processing_time = []
    for im_info, data_batch, im_name in test_data:
        t1 = time.time() - t
        t = time.time()

        scales = [iim_info[0, 2] for iim_info in im_info]
        scores_all, boxes_all, points_all, data_dict_all = im_detect(predictor, data_batch, data_names, scales, cfg)

        tmp_score = scores_all[0][:,1]
        tmp_points = points_all[0][:,:]
        tmp_boxes = boxes_all[0][:,4:8]
        tmp_index = np.argsort(tmp_score)
        tmp_score = tmp_score[tmp_index]
        tmp_boxes = tmp_boxes[tmp_index,:]
        tmp_points = tmp_points[tmp_index, :]
        tmp_score = tmp_score[::-1]
        tmp_boxes = tmp_boxes[::-1,:]
        tmp_points = tmp_points[::-1,:]
        tmp_flag = np.zeros((tmp_score.shape[0],))
        my_boxes = []
        my_score = []
        my_points = []
        for k in range(0, tmp_score.shape[0]):
            if(tmp_flag[k]==0):
                tmp_flag[k] = 1
                if(tmp_score[k]>0.1):
                    my_boxes.append(tmp_boxes[k,:])
                    my_score.append(tmp_score[k])
                    my_points.append(tmp_points[k,:])
                for s in range(k+1, tmp_score.shape[0]):
                    if(tmp_flag[s]==0):
                        if(iou(tmp_boxes[k],tmp_boxes[s]) > 0.3):
                            tmp_flag[s] = 1


        print len(my_boxes)
        scipy.io.savemat(im_name[0][:-4]+'_piece.mat',{'roi':my_boxes,'score:':my_score, 'points':my_points})
        t2 = time.time() - t
        t = time.time()
        for delta, (scores, boxes, data_dict) in enumerate(zip(scores_all, boxes_all, data_dict_all)):
            if cfg.TEST.LEARN_NMS:
                for j in range(1, imdb.num_classes):
                    indexes = np.where(scores[:, j-1] > thresh)[0]
                    cls_scores = scores[indexes, j-1:j]
                    cls_boxes = boxes[indexes, j-1, :]
                    cls_dets = np.hstack((cls_boxes, cls_scores))
                    # count the valid ground truth
                    if len(cls_scores) > 0:
                        class_lut[j].append(idx + delta)
                        valid_tally += len(cls_scores)
                        valid_sum += len(scores)
                    all_boxes[j][idx + delta] = cls_dets
            else:
                for j in range(1, imdb.num_classes):
                    indexes = np.where(scores[:, j] > thresh)[0]
                    if cfg.TEST.FIRST_N > 0:
                        # todo: check whether the order affects the result
                        sort_indices = np.argsort(scores[:, j])[-cfg.TEST.FIRST_N:]
                        # sort_indices = np.argsort(-scores[:, j])[0:cfg.TEST.FIRST_N]
                        indexes = np.intersect1d(sort_indices, indexes)

                    cls_scores = scores[indexes, j, np.newaxis]
                    cls_boxes = boxes[indexes, 4:8] if cfg.CLASS_AGNOSTIC else boxes[indexes, j * 4:(j + 1) * 4]
                    # count the valid ground truth
                    if len(cls_scores) > 0:
                        class_lut[j].append(idx+delta)
                        valid_tally += len(cls_scores)
                        valid_sum += len(scores)
                        # print np.min(cls_scores), valid_tally, valid_sum
                        # cls_scores = scores[:, j, np.newaxis]
                        # cls_scores[cls_scores <= thresh] = thresh
                        # cls_boxes = boxes[:, 4:8] if cfg.CLASS_AGNOSTIC else boxes[:, j * 4:(j + 1) * 4]
                    cls_dets = np.hstack((cls_boxes, cls_scores))
                    if cfg.TEST.SOFTNMS:
                        all_boxes[j][idx + delta] = nms(cls_dets)
                    else:
                        keep = nms(cls_dets)
                        all_boxes[j][idx + delta] = cls_dets[keep, :]

#            print all_boxes 
#            tmp_boxes = all_boxes[0][:,4:8]
#            print (len(tmp_boxes))
#            scipy.io.savemat(im_name[0][:-4]+'_piece.mat',{'roi':tmp_boxes})
            if max_per_image > 0:
                image_scores = np.hstack([all_boxes[j][idx+delta][:, -1]
                                          for j in range(1, imdb.num_classes)])
                if len(image_scores) > max_per_image:
                    image_thresh = np.sort(image_scores)[-max_per_image]
                    for j in range(1, imdb.num_classes):
                        keep = np.where(all_boxes[j][idx+delta][:, -1] >= image_thresh)[0]
                        all_boxes[j][idx+delta] = all_boxes[j][idx+delta][keep, :]

            if vis:
                boxes_this_image = [[]] + [all_boxes[j][idx+delta] for j in range(1, imdb.num_classes)]
                vis_all_detection(data_dict['data'].asnumpy(), boxes_this_image, imdb.classes, scales[delta], cfg)

        idx += test_data.batch_size
        t3 = time.time() - t
        t = time.time()
        post_processing_time.append(t3)
        all_inference_time.append(t1 + t2 + t3)
        inference_count += 1
        if inference_count % 200 == 0:
            valid_count = 500 if inference_count > 500 else inference_count
            print("--->> running-average inference time per batch: {}".format(float(sum(all_inference_time[-valid_count:]))/valid_count))
            print("--->> running-average post processing time per batch: {}".format(float(sum(post_processing_time[-valid_count:]))/valid_count))
        print 'testing {}/{} data {:.4f}s net {:.4f}s post {:.4f}s'.format(idx, imdb.num_images, t1, t2, t3)
        if logger:
            logger.info('testing {}/{} data {:.4f}s net {:.4f}s post {:.4f}s'.format(idx, imdb.num_images, t1, t2, t3))

    with open(det_file, 'wb') as f:
        cPickle.dump(all_boxes, f, protocol=cPickle.HIGHEST_PROTOCOL)

    # np.save('class_lut.npy', class_lut)
#    print (all_boxes)
    info_str = imdb.evaluate_detections(all_boxes)
    if logger:
        logger.info('evaluate detections: \n{}'.format(info_str))
        num_valid_classes = [len(x) for x in class_lut]
        logger.info('valid class ratio:{}'.format(np.sum(num_valid_classes)/float(num_images)))
        logger.info('valid score ratio:{}'.format(float(valid_tally)/float(valid_sum+0.01)))


def vis_all_detection(im_array, detections, class_names, scale, cfg, threshold=1e-3):
    """
    visualize all detections in one image
    :param im_array: [b=1 c h w] in rgb
    :param detections: [ numpy.ndarray([[x1 y1 x2 y2 score]]) for j in classes ]
    :param class_names: list of names in imdb
    :param scale: visualize the scaled image
    :return:
    """
    import matplotlib.pyplot as plt
    import random
    im = image.transform_inverse(im_array, cfg.network.PIXEL_MEANS)
    plt.imshow(im)
    for j, name in enumerate(class_names):
        if name == '__background__':
            continue
        color = (random.random(), random.random(), random.random())  # generate a random color
        dets = detections[j]
        for det in dets:
            bbox = det[:4] * scale
            score = det[-1]
            if score < threshold:
                continue
            rect = plt.Rectangle((bbox[0], bbox[1]),
                                 bbox[2] - bbox[0],
                                 bbox[3] - bbox[1], fill=False,
                                 edgecolor=color, linewidth=3.5)
            plt.gca().add_patch(rect)
            plt.gca().text(bbox[0], bbox[1] - 2,
                           '{:s} {:.3f}'.format(name, score),
                           bbox=dict(facecolor=color, alpha=0.5), fontsize=12, color='white')
    plt.show()

