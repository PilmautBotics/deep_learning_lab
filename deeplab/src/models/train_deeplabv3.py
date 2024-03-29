import argparse
import os
import sys

import imageio.v2 as imageio
import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf

from datetime import datetime

from deeplab.models.metrics import *
from deeplab.models.tf_logs import TensorBoardImage
from deeplab.models.deeplabv3 import *
from deeplab.src.utils.coco_dataset import CocoSegDatasetReader
from deeplab.src.utils.utils import choose_data, process_path, preprocess

# Parameters used
ROOT_CITYSCAPE_DIR_PATH = 'home/pguillemaut/deep_ws/data/Cityscapes'
ROOT_COCOSEG_DIR_PATH = '/home/pguillemaut/deep_ws/data/handsfree_charging_v01/'
IMAGE_SIZE = 512
NUM_CLASSES = 6
EPOCHS = 100
BUFFER_SIZE = 500
BATCH_SIZE = 4  # multiple of 8

def parse_args():
    """
    Necessary arguments to test the script
    """
    parser = argparse.ArgumentParser(description='DeepLabV3 training pipe')
    parser.add_argument(
        '-id',
        '--id_gpu',
        required=True,
        help='choose the gpu to use',
    )
    parser.add_argument('-n', '--is_city_training', action='store_true', help='choose the data to train')
    return parser.parse_args()


def main(args=None):
    print('Hi from DeepLab V3.')

    args = parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.id_gpu)

    # create log directory
    date_time = str(datetime.now())
    date_time = date_time.replace(' ', '-')
    date_time = date_time.replace(':', '_')
    date_time = date_time.replace('.', '_')
    
    log_dir = '../../results/deeplabv3/deeplabv3-resnet50-{}'.format(date_time)
    if not os.path.isdir(log_dir):
        os.makedirs(log_dir)

    if not args.is_city_training:
        print("coco")
        coco_dataset = CocoSegDatasetReader(ROOT_COCOSEG_DIR_PATH)
        images_list = coco_dataset.images
        masks_list = coco_dataset.masks
        image_val_list = coco_dataset.val_images
        mask_val_list = coco_dataset.masks_val
    else:
        # defined which data is going to be used for train and val dataset
        images_list = choose_data(ROOT_CITYSCAPE_DIR_PATH, 'train', 'left_rgb')
        masks_list = choose_data(ROOT_CITYSCAPE_DIR_PATH, 'train', 'left_seg')
        # val images
        image_val_list = choose_data(ROOT_CITYSCAPE_DIR_PATH, 'val', 'left_rgb')
        mask_val_list = choose_data(ROOT_CITYSCAPE_DIR_PATH, 'val', 'left_seg')

    # sorted image list
    images_list = sorted(images_list)
    masks_list = sorted(masks_list)
    image_val_list = sorted(image_val_list)
    mask_val_list = sorted(mask_val_list)

    # Split Dataset into unmasked and masked image #
    image_list_ds = tf.data.Dataset.list_files(images_list, shuffle=False)
    mask_list_ds = tf.data.Dataset.list_files(masks_list, shuffle=False)
    image_val_list_ds = tf.data.Dataset.list_files(image_val_list, shuffle=False)
    mask_val_list_ds = tf.data.Dataset.list_files(mask_val_list, shuffle=False)

    image_filenames = tf.constant(images_list)
    masks_filenames = tf.constant(masks_list)
    image_val_filenames = tf.constant(image_val_list)
    masks_val_filenames = tf.constant(mask_val_list)

    dataset = tf.data.Dataset.from_tensor_slices((image_filenames, masks_filenames))
    val_dataset = tf.data.Dataset.from_tensor_slices((image_val_filenames, masks_val_filenames))

    image_ds = dataset.map(process_path, num_parallel_calls=tf.data.AUTOTUNE)
    processed_image_ds = image_ds.map(preprocess)
    image_val_ds = val_dataset.map(process_path)
    processed_val_image_ds = image_val_ds.map(preprocess)

    # Create the model
    deeplab_v3plus = DeeplabV3Plus(image_size=IMAGE_SIZE, num_classes=NUM_CLASSES)
    # deeplab_v3plus.summary()

    # Learning rate decay
    """lr_schedule = tf.keras.optimizers.schedules.ExponentialDecay(
        initial_learning_rate=1e-2, decay_steps=10000, decay_rate=0.9
    )"""
    optimizer = tf.keras.optimizers.Adam(learning_rate=0.01)
    
    deeplab_v3plus.compile(
        optimizer=optimizer,
        loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
        metrics=['accuracy', ground_iou, Mean_IOU]
    )

    # Define training parameters #
    processed_image_ds.batch(BATCH_SIZE)
    processed_val_image_ds.batch(BATCH_SIZE)
    train_dataset = processed_image_ds.cache().shuffle(BUFFER_SIZE).batch(BATCH_SIZE)
    val_dataset = processed_val_image_ds.cache().shuffle(BUFFER_SIZE).batch(BATCH_SIZE)

    # Define callbacks
    logging = tf.keras.callbacks.TensorBoard(
        log_dir=log_dir + '/tf_logs',
        histogram_freq=0,
        write_graph=False,
        write_grads=False,
        write_images=False,
        update_freq='batch',
    )
    checkpoint = tf.keras.callbacks.ModelCheckpoint(
        os.path.join(log_dir, 'ep{epoch:03d}-loss{loss:.3f}-val_loss{val_loss:.3f}.h5'),
        monitor='val_loss',
        mode='min',
        verbose=1,
        save_weights_only=False,
        save_best_only=True,
        period=1,
    )
    early_stopping = tf.keras.callbacks.EarlyStopping(
        monitor='val_loss', min_delta=0, patience=50, verbose=1, mode='min'
    )
    reduce_lr = tf.keras.callbacks.ReduceLROnPlateau(
        monitor='val_loss', factor=0.5, mode='min', patience=10, verbose=1, cooldown=0, min_lr=1e-10
    )

    # image callbacks
    plot_logs_dir = log_dir + "/tf_logs/plots/"
    file_writer = tf.summary.create_file_writer(plot_logs_dir)
    tbi_callback = TensorBoardImage('Colored Seg Mask', 'Train images', images_list[1], plot_logs_dir, deeplab_v3plus, NUM_CLASSES, args.is_city_training)

    callbacks = [logging, checkpoint, early_stopping, reduce_lr, tbi_callback]

    # Train deepLabV3
    model_history = deeplab_v3plus.fit(
         train_dataset, validation_data=train_dataset, epochs=EPOCHS, callbacks=callbacks
     )

    # Saved model trained
    deep_dir = log_dir + "/deeplab_v3plus_resnet_saved_model"
    deeplab_v3plus.save(deep_dir)

    plt.figure(figsize=(8, 8))
    plt.title("Learning curve")
    plt.plot(model_history.history["loss"], label="loss")
    # plt.plot(model_history.history["val_loss"], label="val_loss")
    # plt.plot(
    #    np.argmin(model_history.history["val_loss"]),
    #    np.min(model_history.history["val_loss"]),
    #    marker="x",
    #    color="r",
    #    label="best model",
    #)
    plt.xlabel("Epochs")
    plt.ylabel("log_loss")
    plt.legend()
    plt.savefig("{}/{}".format(log_dir,'deeplabv3_epochs_logloss_curve.png'))


if __name__ == '__main__':
    main()
