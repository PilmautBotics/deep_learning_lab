#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Calculate mIOU for Deeplabv3p model on validation dataset
"""
import argparse
import itertools
import operator
import os
import time
from collections import OrderedDict

import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
import tensorflow.keras.backend as K
from labelme.utils import lblsave as label_save
from PIL import Image
from tensorflow.keras.models import load_model
from tqdm import tqdm

from deeplab.models.segmentation.data import SegmentationGenerator
from deeplab.models.segmentation.metrics import mIOU
from deeplab.src.common.data_utils import denormalize_image
from deeplab.src.common.utils import (
    get_classes,
    get_custom_objects,
    get_data_list,
    visualize_segmentation,
)

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'


def deeplab_predict_keras(model, image_data):
    prediction = model.predict(image_data)
    prediction = np.argmax(prediction, axis=-1)
    return prediction[0]


def deeplab_predict_onnx(model, image_data):
    input_tensors = []
    for i, input_tensor in enumerate(model.get_inputs()):
        input_tensors.append(input_tensor)
    # assume only 1 input tensor for image
    assert len(input_tensors) == 1, 'invalid input tensor number.'

    # check if input layout is NHWC or NCHW
    if input_tensors[0].shape[1] == 3:
        batch, channel, height, width = input_tensors[0].shape  # NCHW
    else:
        batch, height, width, channel = input_tensors[0].shape  # NHWC

    output_tensors = []
    for i, output_tensor in enumerate(model.get_outputs()):
        output_tensors.append(output_tensor)
    # assume only 1 output tensor
    assert len(output_tensors) == 1, 'invalid output tensor number.'

    # check if output layout is NHWC or NCHW, (H,W) for Deeplab
    # output tensor should be same as input tensor
    if output_tensors[0].shape[2] == height:
        # print("NCHW output layout")
        num_classes = output_tensors[0].shape[1]  # NCHW
    elif output_tensors[0].shape[1] == height:
        # print("NHWC output layout")
        num_classes = output_tensors[0].shape[-1]  # NHWC
    else:
        raise ValueError('invalid output layout or shape')

    if input_tensors[0].shape[1] == 3:
        # transpose image for NCHW layout
        image_data = image_data.transpose((0, 3, 1, 2))

    feed = {input_tensors[0].name: image_data}
    prediction = model.run(None, feed)

    if output_tensors[0].shape[1] == num_classes:
        # transpose predict mask for NCHW layout
        prediction = [p.transpose((0, 2, 3, 1)) for p in prediction]

    prediction = np.argmax(prediction, axis=-1)
    return prediction[0]


def deeplab_predict_pb(model, image_data):
    # NOTE: TF 1.x frozen pb graph need to specify input/output tensor name
    # so we need to hardcode the input/output tensor names here to get them from model
    output_tensor_name = 'graph/pred_mask/Softmax:0'

    # assume only 1 input tensor for image
    input_tensor_name = 'graph/image_input:0'

    # get input/output tensors
    image_input = model.get_tensor_by_name(input_tensor_name)
    output_tensor = model.get_tensor_by_name(output_tensor_name)

    with tf.Session(graph=model) as sess:
        prediction = sess.run(output_tensor, feed_dict={image_input: image_data})
    prediction = np.argmax(prediction, axis=-1)
    return prediction[0]


def deeplab_predict_tflite(interpreter, image_data):
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    interpreter.set_tensor(input_details[0]['index'], image_data)
    interpreter.invoke()

    prediction = []
    for output_detail in output_details:
        output_data = interpreter.get_tensor(output_detail['index'])
        prediction.append(output_data)

    prediction = np.argmax(prediction[0], axis=-1)
    return prediction[0]


def plot_confusion_matrix(
    cm, classes, mIOU, normalize=False, title='Confusion matrix', cmap=plt.cm.Blues
):
    if normalize:
        cm = cm.astype('float') / cm.sum(axis=1)[:, np.newaxis]
        cm[np.isnan(cm)] = 0
    trained_classes = classes
    plt.figure()
    plt.imshow(cm, interpolation='nearest', cmap=cmap)
    plt.title(title, fontsize=11)
    tick_marks = np.arange(len(classes))
    plt.xticks(np.arange(len(trained_classes)), classes, rotation=90, fontsize=9)
    plt.yticks(tick_marks, classes, fontsize=9)
    thresh = cm.max() / 2.0
    for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        plt.text(
            j,
            i,
            np.round(cm[i, j], 2),
            horizontalalignment="center",
            color="white" if cm[i, j] > thresh else "black",
            fontsize=7,
        )
    plt.tight_layout()
    plt.ylabel('True label', fontsize=9)
    plt.xlabel('Predicted label', fontsize=9)

    plt.title('Mean IOU: ' + str(np.round(mIOU * 100, 2)))
    output_path = os.path.join('result', 'confusion_matrix.png')
    os.makedirs('result', exist_ok=True)
    plt.savefig(output_path)
    # plt.show()

    # close the plot
    plt.close()
    return


def adjust_axes(r, t, fig, axes):
    """
    Plot - adjust axes
    """
    # get text width for re-scaling
    bb = t.get_window_extent(renderer=r)
    text_width_inches = bb.width / fig.dpi
    # get axis width in inches
    current_fig_width = fig.get_figwidth()
    new_fig_width = current_fig_width + text_width_inches
    propotion = new_fig_width / current_fig_width
    # get axis limit
    x_lim = axes.get_xlim()
    axes.set_xlim([x_lim[0], x_lim[1] * propotion])


def draw_plot_func(
    dictionary,
    n_classes,
    window_title,
    plot_title,
    x_label,
    output_path,
    to_show,
    plot_color,
    true_p_bar,
):
    """
    Draw plot using Matplotlib
    """
    # sort the dictionary by decreasing value, into a list of tuples
    sorted_dic_by_value = sorted(dictionary.items(), key=operator.itemgetter(1))
    # unpacking the list of tuples into two lists
    sorted_keys, sorted_values = zip(*sorted_dic_by_value)
    #
    if true_p_bar != "":
        """
        Special case to draw in (green=true predictions) & (red=false predictions)
        """
        fp_sorted = []
        tp_sorted = []
        for key in sorted_keys:
            fp_sorted.append(dictionary[key] - true_p_bar[key])
            tp_sorted.append(true_p_bar[key])
        plt.barh(
            range(n_classes), fp_sorted, align='center', color='crimson', label='False Predictions'
        )
        plt.barh(
            range(n_classes),
            tp_sorted,
            align='center',
            color='forestgreen',
            label='True Predictions',
            left=fp_sorted,
        )
        # add legend
        plt.legend(loc='lower right')
        """
         Write number on side of bar
        """
        fig = plt.gcf()  # gcf - get current figure
        axes = plt.gca()
        r = fig.canvas.get_renderer()
        for i, val in enumerate(sorted_values):
            fp_val = fp_sorted[i]
            tp_val = tp_sorted[i]
            fp_str_val = " " + str(fp_val)
            tp_str_val = fp_str_val + " " + str(tp_val)
            # trick to paint multicolor with offset:
            #   first paint everything and then repaint the first number
            t = plt.text(val, i, tp_str_val, color='forestgreen', va='center', fontweight='bold')
            plt.text(val, i, fp_str_val, color='crimson', va='center', fontweight='bold')
            if i == (len(sorted_values) - 1):  # largest bar
                adjust_axes(r, t, fig, axes)
    else:
        plt.barh(range(n_classes), sorted_values, color=plot_color)
        """
        Write number on side of bar
        """
        fig = plt.gcf()  # gcf - get current figure
        axes = plt.gca()
        r = fig.canvas.get_renderer()
        for i, val in enumerate(sorted_values):
            str_val = " " + str(val)  # add a space before
            if val < 1.0:
                str_val = " {0:.2f}".format(val)
            t = plt.text(val, i, str_val, color=plot_color, va='center', fontweight='bold')
            # re-set axes to show number inside the figure
            if i == (len(sorted_values) - 1):  # largest bar
                adjust_axes(r, t, fig, axes)
    # set window title
    fig.canvas.set_window_title(window_title)
    # write classes in y axis
    tick_font_size = 12
    plt.yticks(range(n_classes), sorted_keys, fontsize=tick_font_size)
    """
     Re-scale height accordingly
    """
    init_height = fig.get_figheight()
    # comput the matrix height in points and inches
    dpi = fig.dpi
    height_pt = n_classes * (tick_font_size * 1.4)  # 1.4 (some spacing)
    height_in = height_pt / dpi
    # compute the required figure height
    top_margin = 0.15  # in percentage of the figure height
    bottom_margin = 0.05  # in percentage of the figure height
    figure_height = height_in / (1 - top_margin - bottom_margin)
    # set new height
    if figure_height > init_height:
        fig.set_figheight(figure_height)

    # set plot title
    plt.title(plot_title, fontsize=14)
    # set axis titles
    # plt.xlabel('classes')
    plt.xlabel(x_label, fontsize='large')
    # adjust size of window
    fig.tight_layout()
    # save the plot
    fig.savefig(output_path)
    # show image
    if to_show:
        plt.show()
    # close the plot
    plt.close()


def plot_mIOU_result(IOUs, mIOU, num_classes):
    '''
    Draw mIOU plot (Show IOU's of all classes in decreasing order)
    '''
    window_title = "mIOU"
    plot_title = "mIOU: {0:.3f}%".format(mIOU * 100)
    x_label = "Intersection Over Union"
    output_path = os.path.join('result', 'mIOU.png')
    os.makedirs('result', exist_ok=True)
    draw_plot_func(
        IOUs,
        num_classes,
        window_title,
        plot_title,
        x_label,
        output_path,
        to_show=False,
        plot_color='royalblue',
        true_p_bar='',
    )


def save_seg_result(image, pred_mask, gt_mask, image_id, class_names):
    # save predict mask as PNG image
    mask_dir = os.path.join('result', 'predict_mask')
    os.makedirs(mask_dir, exist_ok=True)
    label_save(os.path.join(mask_dir, str(image_id) + '.png'), pred_mask)

    # visualize segmentation result
    title_str = 'Predict Segmentation\nmIOU: ' + str(mIOU(pred_mask, gt_mask))
    gt_title_str = 'GT Segmentation'
    image_array = visualize_segmentation(
        image,
        pred_mask,
        gt_mask,
        class_names=class_names,
        title=title_str,
        gt_title=gt_title_str,
        ignore_count_threshold=1,
    )

    # save result as PNG
    result_dir = os.path.join('result', 'segmentation')
    os.makedirs(result_dir, exist_ok=True)
    result_file = os.path.join(result_dir, str(image_id) + '.png')
    Image.fromarray(image_array).save(result_file)


def generate_matrix(gt_mask, pre_mask, num_classes):
    valid = (gt_mask >= 0) & (gt_mask < num_classes)
    label = num_classes * gt_mask[valid].astype('int') + pre_mask[valid]
    count = np.bincount(label, minlength=num_classes**2)
    confusion_matrix = count.reshape(num_classes, num_classes)
    return confusion_matrix


def eval_mIOU(
    model,
    model_format,
    dataset_path,
    dataset,
    class_names,
    model_input_shape,
    do_crf=False,
    save_result=False,
):
    num_classes = len(class_names)

    # prepare eval dataset generator
    eval_generator = SegmentationGenerator(
        dataset_path,
        dataset,
        1,  # batch_size
        num_classes,
        input_shape=model_input_shape,
        weighted_type=None,
        is_eval=True,
        augment=False,
    )

    # confusion matrix for all classes
    confusion_matrix = np.zeros((num_classes, num_classes), dtype=float)

    # get model prediction result
    pbar = tqdm(total=len(eval_generator), desc='Eval model')
    for n, (image_data, y_true) in enumerate(eval_generator):

        # support of tflite model
        if model_format == 'TFLITE':
            y_pred = deeplab_predict_tflite(model, image_data)
        # support of TF 1.x frozen pb model
        elif model_format == 'PB':
            y_pred = deeplab_predict_pb(model, image_data)
        # support of ONNX model
        elif model_format == 'ONNX':
            y_pred = deeplab_predict_onnx(model, image_data)
        # normal keras h5 model
        elif model_format == 'H5':
            y_pred = deeplab_predict_keras(model, image_data)
        else:
            raise ValueError('invalid model format')

        image = denormalize_image(image_data[0])
        pred_mask = y_pred.reshape(model_input_shape)
        gt_mask = y_true.reshape(model_input_shape).astype('int')

        # save segmentation result image
        if save_result:
            # get eval image name to save corresponding result
            image_list = eval_generator.get_batch_image_path(n)
            assert len(image_list) == 1, 'incorrect image batch'
            image_id = os.path.splitext(os.path.basename(image_list[0]))[0]

            save_seg_result(image, pred_mask, gt_mask, image_id, class_names)

        # update confusion matrix
        pred_mask = pred_mask.astype('int')
        gt_mask = gt_mask.astype('int')
        confusion_matrix += generate_matrix(gt_mask, pred_mask, num_classes)

        # compare prediction result with label
        # to update confusion matrix
        # flat_pred = np.ravel(pred_mask).astype('int')
        # flat_label = np.ravel(gt_mask).astype('int')
        # for p, l in zip(flat_pred, flat_label):
        # if l == num_classes or l == 255:
        # continue
        # if l < num_classes and p < num_classes:
        # confusion_matrix[l, p] += 1
        # else:
        # print('Invalid entry encountered, skipping! Label: ', l,
        #' Prediction: ', p)

        pbar.update(1)
    pbar.close()

    # calculate Pixel accuracy
    PixelAcc = np.diag(confusion_matrix).sum() / confusion_matrix.sum()

    # calculate Class accuracy
    ClassAcc = np.diag(confusion_matrix) / confusion_matrix.sum(axis=1)
    ClassAcc[np.isnan(ClassAcc)] = 0
    mClassAcc = np.nanmean(ClassAcc)

    # calculate mIoU
    I = np.diag(confusion_matrix)
    U = np.sum(confusion_matrix, axis=0) + np.sum(confusion_matrix, axis=1) - I
    IoU = I / U
    IoU[np.isnan(IoU)] = 0
    # mIoU = np.nanmean(IoU)

    # calculate FW (Frequency Weighted) IoU
    Freq = np.sum(confusion_matrix, axis=1) / np.sum(confusion_matrix)
    Freq[np.isnan(Freq)] = 0
    FWIoU = (Freq[Freq > 0] * IoU[Freq > 0]).sum()

    # calculate Dice Coefficient
    DiceCoef = 2 * I / (U + I)
    DiceCoef[np.isnan(DiceCoef)] = 0

    # collect IOU/ClassAcc/Dice/Freq for every class
    IOUs, CLASS_ACCs, DICEs, FREQs = {}, {}, {}, {}
    for i, (class_name, iou, class_acc, dice, freq) in enumerate(
        zip(class_names, IoU, ClassAcc, DiceCoef, Freq)
    ):
        IOUs[class_name] = iou
        CLASS_ACCs[class_name] = class_acc
        DICEs[class_name] = dice
        FREQs[class_name] = freq

    # sort IoU result by value, in descending order
    IOUs = OrderedDict(sorted(IOUs.items(), key=operator.itemgetter(1), reverse=True))

    # calculate mIOU from final IOU dict
    mIoU = np.nanmean(list(IOUs.values()))

    # show result
    print('\nevaluation summary')
    for class_name, iou in IOUs.items():
        print(
            '%s: IoU %.4f, Freq %.4f, ClassAcc %.4f, Dice %.4f'
            % (class_name, iou, FREQs[class_name], CLASS_ACCs[class_name], DICEs[class_name])
        )
    print('mIoU=%.3f' % (mIoU * 100))
    print('FWIoU=%.3f' % (FWIoU * 100))
    print('PixelAcc=%.3f' % (PixelAcc * 100))
    print('mClassAcc=%.3f' % (mClassAcc * 100))

    # Plot mIOU & confusion matrix
    # plot_mIOU_result(IOUs, mIoU, num_classes)
    # plot_confusion_matrix(confusion_matrix, class_names, mIoU, normalize=True)

    return mIoU


# load TF 1.x frozen pb graph
def load_graph(model_path):
    # check tf version to be compatible with TF 2.x
    global tf
    if tf.__version__.startswith('2'):
        import tensorflow.compat.v1 as tf

        tf.disable_eager_execution()

    # We parse the graph_def file
    with tf.gfile.GFile(model_path, "rb") as f:
        graph_def = tf.GraphDef()
        graph_def.ParseFromString(f.read())

    # We load the graph_def in the default graph
    with tf.Graph().as_default() as graph:
        tf.import_graph_def(
            graph_def,
            input_map=None,
            return_elements=None,
            name="graph",
            op_dict=None,
            producer_op_list=None,
        )
    return graph


def load_eval_model(model_path):
    # support of tflite model
    if model_path.endswith('.tflite'):
        from tensorflow.lite.python import interpreter as interpreter_wrapper

        model = interpreter_wrapper.Interpreter(model_path=model_path)
        model.allocate_tensors()
        model_format = 'TFLITE'

    # support of TF 1.x frozen pb model
    elif model_path.endswith('.pb'):
        model = load_graph(model_path)
        model_format = 'PB'

    # normal keras h5 model
    elif model_path.endswith('.h5'):
        custom_object_dict = get_custom_objects()

        model = load_model(model_path, compile=False, custom_objects=custom_object_dict)
        model_format = 'H5'
        K.set_learning_phase(0)
    else:
        raise ValueError('invalid model file')

    return model, model_format


def main():
    parser = argparse.ArgumentParser(
        argument_default=argparse.SUPPRESS,
        description='evaluate Deeplab model (h5/pb/tflite/mnn) with test dataset',
    )
    '''
    Command line options
    '''
    parser.add_argument('--model_path', type=str, required=True, help='path to model file')

    parser.add_argument(
        '--dataset_path',
        type=str,
        required=True,
        help='dataset path containing images and label png file',
    )

    parser.add_argument('--dataset_file', type=str, required=True, help='eval samples txt file')

    parser.add_argument(
        '--classes_path',
        type=str,
        required=False,
        default='configs/voc_classes.txt',
        help='path to class definitions, default=%(default)s',
    )

    parser.add_argument(
        '--model_input_shape',
        type=str,
        help='model image input shape as <height>x<width>, default=%(default)s',
        default='512x512',
    )

    parser.add_argument(
        '--do_crf',
        action="store_true",
        help='whether to add CRF postprocess for model output',
        default=False,
    )

    parser.add_argument(
        '--save_result',
        default=False,
        action="store_true",
        help='Save the segmentaion result image in result/segmentation dir',
    )

    args = parser.parse_args()

    # param parse
    height, width = args.model_input_shape.split('x')
    model_input_shape = (int(height), int(width))

    # get class names
    class_names = get_classes(args.classes_path)
    assert len(class_names) < 254, 'PNG image label only support less than 254 classes.'

    model, model_format = load_eval_model(args.model_path)

    # get dataset list
    dataset = get_data_list(args.dataset_file)

    start = time.time()
    eval_mIOU(
        model,
        model_format,
        args.dataset_path,
        dataset,
        class_names,
        model_input_shape,
        args.do_crf,
        args.save_result,
    )
    end = time.time()
    print("Evaluation time cost: {:.6f}s".format(end - start))


if __name__ == '__main__':
    main()
