# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Generic evaluation script that evaluates a model using a given dataset."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import numpy as np
import tensorflow.compat.v1 as tf

config = tf.ConfigProto()
config.gpu_options.allow_growth = True
sess = tf.Session(config=config)

import tf_slim as slim

from tensorflow.contrib import quantize as contrib_quantize

from datasets import dataset_factory
from nets import nets_factory
from preprocessing import preprocessing_factory
from metrics import f1_score
from metrics import _streaming_confusion_matrix_at_thresholds, streaming_curve_points, streaming_auc
from metrics import streaming_accuracy, streaming_precision, streaming_recall, streaming_recall_at_k
from metrics import streaming_true_positives, streaming_true_negatives, streaming_false_positives, \
    streaming_false_negatives

tf.app.flags.DEFINE_integer(
    'batch_size', 100, 'The number of samples in each batch.')

tf.app.flags.DEFINE_integer(
    'max_num_batches', None,
    'Max number of batches to evaluate by default use all.')

tf.app.flags.DEFINE_string(
    'master', '', 'The address of the TensorFlow master to use.')

tf.app.flags.DEFINE_string(
    'checkpoint_path', None,
    'The directory where the model was written to or an absolute path to a '
    'checkpoint file.')

tf.app.flags.DEFINE_string(
    'eval_dir', None, 'Directory where the results are saved to.')

tf.app.flags.DEFINE_string(
    'output_path', None, 'Directory where checkpoints and event logs are written to.')

tf.app.flags.DEFINE_integer(
    'num_preprocessing_threads', 4,
    'The number of threads used to create the batches.')

tf.app.flags.DEFINE_string(
    'dataset_name', 'mnist', 'The name of the dataset to load.')

tf.app.flags.DEFINE_string(
    'dataset_split_name', 'test', 'The name of the train/test split.')

tf.app.flags.DEFINE_string(
    'dataset_dir', None, 'The directory where the dataset files are stored.')

tf.app.flags.DEFINE_string(
    'data_path', None, 'The directory where the original image dataset files are stored.')

tf.app.flags.DEFINE_integer(
    'labels_offset', 0,
    'An offset for the labels in the dataset. This flag is primarily used to '
    'evaluate the VGG and ResNet architectures which do not use a background '
    'class for the ImageNet dataset.')

tf.app.flags.DEFINE_string(
    'model_name', 'lenet', 'The name of the architecture to evaluate.')

tf.app.flags.DEFINE_string(
    'preprocessing_name', None, 'The name of the preprocessing to use. If left '
                                'as `None`, then the model_name flag is used.')

tf.app.flags.DEFINE_float(
    'moving_average_decay', None,
    'The decay to use for the moving average.'
    'If left as None, then moving averages are not used.')

tf.app.flags.DEFINE_integer(
    'eval_image_size', None, 'Eval image size')

tf.app.flags.DEFINE_bool(
    'quantize', False, 'whether to use quantized graph or not.')

tf.app.flags.DEFINE_bool('use_grayscale', False,
                         'Whether to convert input images to grayscale.')
tf.app.flags.DEFINE_string(
    'checkpoint_exclude_scopes', None,
    'Comma-separated list of scopes of variables to exclude when restoring '
    'from a checkpoint.')

tf.app.flags.DEFINE_string(
    'trainable_scopes', None,
    'Comma-separated list of scopes to filter the set of variables to train.'
    'By default, None would train all the variables.')

tf.app.flags.DEFINE_boolean(
    'ignore_missing_vars', False,
    'When restoring a checkpoint would ignore missing variables.')
tf.app.flags.DEFINE_string(
    'visualPath', '',
    'visual tensorboard path.')

FLAGS = tf.app.flags.FLAGS


def main(_):
    if not FLAGS.dataset_dir:
        raise ValueError('You must supply the dataset directory with --dataset_dir')

    tf.logging.set_verbosity(tf.logging.INFO)
    with tf.Graph().as_default():
        tf_global_step = slim.get_or_create_global_step()

        ######################
        # Select the dataset #
        ######################
        dataset = dataset_factory.get_dataset(
            FLAGS.dataset_name, FLAGS.dataset_split_name, FLAGS.dataset_dir)

        ####################
        # Select the model #
        ####################
        network_fn = nets_factory.get_network_fn(
            FLAGS.model_name,
            num_classes=(dataset.num_classes - FLAGS.labels_offset),
            is_training=False)

        ##############################################################
        # Create a dataset provider that loads data from the dataset #
        ##############################################################
        provider = slim.dataset_data_provider.DatasetDataProvider(
            dataset,
            shuffle=False,
            common_queue_capacity=2 * FLAGS.batch_size,
            common_queue_min=FLAGS.batch_size)
        [image, label] = provider.get(['image', 'label'])
        label -= FLAGS.labels_offset

        #####################################
        # Select the preprocessing function #
        #####################################
        preprocessing_name = FLAGS.preprocessing_name or FLAGS.model_name
        image_preprocessing_fn = preprocessing_factory.get_preprocessing(
            preprocessing_name,
            is_training=False,
            use_grayscale=FLAGS.use_grayscale)

        eval_image_size = FLAGS.eval_image_size or network_fn.default_image_size

        image = image_preprocessing_fn(image, eval_image_size, eval_image_size)

        images, labels = tf.train.batch(
            [image, label],
            batch_size=FLAGS.batch_size,
            num_threads=FLAGS.num_preprocessing_threads,
            capacity=5 * FLAGS.batch_size)

        ####################
        # Define the model #
        ####################
        logits, _ = network_fn(images)

        if FLAGS.quantize:
            contrib_quantize.create_eval_graph()

        if FLAGS.moving_average_decay:
            variable_averages = tf.train.ExponentialMovingAverage(
                FLAGS.moving_average_decay, tf_global_step)
            variables_to_restore = variable_averages.variables_to_restore(
                slim.get_model_variables())
            variables_to_restore[tf_global_step.op.name] = tf_global_step
        else:
            variables_to_restore = slim.get_variables_to_restore()

        predictions = tf.argmax(logits, 1)
        labels = tf.squeeze(labels)

        thresholds = np.arange(0, 1, 0.05).tolist()

        # Define the metrics:
        if dataset.num_classes > 2:
            names_to_values, names_to_updates = slim.metrics.aggregate_metric_map({
                'Accuracy': slim.metrics.streaming_accuracy(predictions, labels),
                'Precision': slim.metrics.streaming_precision(predictions, labels),
                'Recall': slim.metrics.streaming_recall(predictions, labels),
                'Recall_5': slim.metrics.streaming_recall_at_k(logits, labels, 5),
            })
        else:
            names_to_values, names_to_updates = slim.metrics.aggregate_metric_map({
                'Accuracy': streaming_accuracy(predictions, labels),
                'Precision': streaming_precision(predictions, labels),
                'Recall': streaming_recall(predictions, labels),
                'F1_score': f1_score(predictions, labels),
                'Auc_ROC': streaming_auc(predictions, labels, curve='ROC'),
                'Auc_PR': streaming_auc(predictions, labels, curve='PR'),
                'TP': streaming_true_positives(predictions, labels),
                'TN': streaming_true_negatives(predictions, labels),
                'FP': streaming_false_positives(predictions, labels),
                'FN': streaming_false_negatives(predictions, labels),
                # 'ROC_curve': streaming_curve_points(labels, predictions, curve='ROC'),
                # 'PR_curve': streaming_curve_points(labels, predictions, curve='PR'),
            })

        labels_to_names = dataset.labels_to_names
        if dataset.num_classes > 2:
            # Print the summaries to screen.
            for name, value in names_to_values.items():
                summary_name = 'evaluation_results/%s' % name
                op = tf.summary.scalar(summary_name, value, collections=[])
                op = tf.Print(op, [value], summary_name)
                tf.add_to_collection(tf.GraphKeys.SUMMARIES, op)
        else:
            # Print the summaries to screen.
            for name, value in names_to_values.items():
                if name in ('ROC_curve', 'PR_curve'):
                    summary_name = 'evaluation_results/%s' % name
                    op = tf.summary.tensor_summary(summary_name, value, collections=[])
                    op = tf.Print(op, [value], summary_name, summarize=9)
                else:
                    if name == 'TP':
                        name = name + ': Real-%s, Pred-%s' % (labels_to_names[1], labels_to_names[1])
                    if name == 'FN':
                        name = name + ': Real-%s, Pred-%s' % (labels_to_names[1], labels_to_names[0])
                    if name == 'FP':
                        name = name + ': Real-%s, Pred-%s' % (labels_to_names[0], labels_to_names[1])
                    if name == 'TN':
                        name = name + ': Real-%s, Pred-%s' % (labels_to_names[0], labels_to_names[0])
                    summary_name = 'evaluation_results/%s' % name
                    op = tf.summary.scalar(summary_name, value, collections=[])
                    op = tf.Print(op, [value], summary_name)
                tf.add_to_collection(tf.GraphKeys.SUMMARIES, op)

        # TODO(sguada) use num_epochs=1
        if FLAGS.max_num_batches:
            num_batches = FLAGS.max_num_batches
        else:
            # This ensures that we make a single pass over all of the data.
            num_batches = math.ceil(dataset.num_samples / float(FLAGS.batch_size))

        if tf.gfile.IsDirectory(FLAGS.checkpoint_path):
            checkpoint_path = tf.train.latest_checkpoint(FLAGS.checkpoint_path)
        else:
            checkpoint_path = FLAGS.checkpoint_path

        tf.logging.info('Evaluating %s' % checkpoint_path)

        slim.evaluation.evaluate_once(
            master=FLAGS.master,
            checkpoint_path=checkpoint_path,
            logdir=FLAGS.eval_dir,
            num_evals=num_batches,
            eval_op=list(names_to_updates.values()),
            variables_to_restore=variables_to_restore)


if __name__ == '__main__':
    if FLAGS.data_path is not None:
        FLAGS.dataset_dir = FLAGS.data_path
    if FLAGS.output_path is not None:
        FLAGS.eval_dir = FLAGS.output_path
    # 适配dog-cat
    if 'dog' in FLAGS.data_path:
        FLAGS.dataset_name = "dog-vs-cat"
        FLAGS.dataset_split_name = "validation"
        FLAGS.model_name = "inception_v3"
    tf.app.run()
