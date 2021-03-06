# coding=utf-8
# Copyright 2020 The SimCLR Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific simclr governing permissions and
# limitations under the License.
# ==============================================================================
"""Specification for modified SimCLR model. Changes hiddens from one model into hiddens1 and hiddens2, which are distince models."""


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from absl import flags

import data_util as data_util
import model_util as model_util
import objective as obj_lib

import tensorflow.compat.v1 as tf
import tensorflow.compat.v2 as tf2

import resnet

FLAGS = flags.FLAGS


# assume model is a list of [model_full, model_cropped] (is order correct?), creates hiddens1, hiddens2
# assumes we only perform transforms (color, blur, crop) for model_cropped. Is this correct?
def build_model_fn(model, num_classes, num_train_examples):
  """Build model function."""
  def model_fn(features, labels, mode, params=None):
    """Build model and optimizer."""
    is_training = mode == tf.estimator.ModeKeys.TRAIN

    # Check training mode.
    if FLAGS.train_mode == 'pretrain':
      num_transforms = 2
      if FLAGS.fine_tune_after_block > -1:
        raise ValueError('Does not support layer freezing during pretraining,'
                         'should set fine_tune_after_block<=-1 for safety.')
    elif FLAGS.train_mode == 'finetune':
      num_transforms = 1
    else:
      raise ValueError('Unknown train_mode {}'.format(FLAGS.train_mode))

    # Split channels, and optionally apply extra batched augmentation.
    features_list = tf.split(
        features, num_or_size_splits=num_transforms, axis=-1)  # splits into 2 tensors with 3 channels instead of 1 tensor with 6 channels
    if FLAGS.use_blur and is_training and FLAGS.train_mode == 'pretrain':
      features_list = data_util.batch_random_blur(
          features_list, FLAGS.image_size, FLAGS.image_size)
    # crop images now that all other preprocessing has finished
    if is_training and FLAGS.train_mode == 'pretrain':
        features_list = data_util.batch_random_crop(features_list, FLAGS.crop_size, FLAGS.crop_size)  # cropped -> hiddens1
    # features = tf.concat(features_list, 0)  # (num_transforms * bsz, h, w, c)
    # Concatenating again is not needed since list elements are used separately from now on
    features = features_list
    if FLAGS.train_mode == 'finetune':
        features = tf.concat(features_list, 0)
        features = [features, features]  # since num_transforms is 1, was never split into list. Only one network's output is used in eval, so they are never compared.

    # Base network forward pass.
    with tf.variable_scope('base_model'):
      if FLAGS.train_mode == 'finetune' and FLAGS.fine_tune_after_block >= 4:  # train_mode is either 'finetune' or 'pretrain', 'finetune' used for just training linear head
        # Finetune just supervised (linear) head will not update BN stats.
        model_train_mode = False
      else:
        # Pretrain or finetuen anything else will update BN stats.
        model_train_mode = is_training
      # hiddens = model(features, is_training=model_train_mode)  # model_train_mode=True if fine_tune_after_block < 4, bug??
      hiddens_f = model['model_full'](features[0], is_training=model_train_mode)  # output of full model
      hiddens_c = model['model_cropped'](features[1], is_training=model_train_mode)  # output of cropped model

    # Add head and loss.
    if FLAGS.train_mode == 'pretrain':
      tpu_context = params['context'] if 'context' in params else None
      # hiddens_proj = model_util.projection_head(hiddens, is_training)  # by default adds 3 nonlinear layers, paper claims 2 only
      hiddens_proj_f = model_util.projection_head(hiddens_f, is_training)
      hiddens_proj_c = model_util.projection_head(hiddens_c, is_training)

      # calculate attention mask
      attn_mask = model_util.attn_mask_head(10*hiddens_proj_c, is_training, name='attn_network') # 10* helps converge faster
      if FLAGS.attention_output == 'hard':
        attn_mask = tf.cast(attn_mask >= 0.5, tf.float32)  # use softmax instead? L2 norm? alter 10* also?
      elif FLAGS.attention_output == 'softmax':
        attn_mask = tf.nn.softmax(attn_mask)  # performed along last dim
      elif FLAGS.attention_output == 'L2':
        attn_mask = tf.math.l2_normalize(attn_mask)
      else:
        raise ValueError('Unknown attention_output {}'.format(FLAGS.attention_output))
      # apply attention mask
      hiddens_proj_f = hiddens_proj_f * attn_mask
      hiddens_proj_c = hiddens_proj_c * attn_mask

      contrast_loss, logits_con, labels_con = obj_lib.add_contrastive_loss_2(
          hiddens_proj_f, hiddens_proj_c,
          hidden_norm=FLAGS.hidden_norm,
          temperature=FLAGS.temperature,
          tpu_context=tpu_context if is_training else None)
      logits_sup = tf.zeros([params['batch_size'], num_classes])
    else:
      contrast_loss = tf.zeros([])
      logits_con = tf.zeros([params['batch_size'], 10])
      labels_con = tf.zeros([params['batch_size'], 10])
      # hiddens = model_util.projection_head(hiddens, is_training)  # adds 3 nonlinear layers by default
      hiddens_f = model_util.projection_head(hiddens_f, is_training)
      hiddens_c = model_util.projection_head(hiddens_c, is_training)
      logits_sup = model_util.supervised_head(  # supervised head is just one linear layer, but 3 nonlinear layrs already added above
          hiddens_f, num_classes, is_training)  # only evaluate on output from model_full (otherwise need another param to choose)
      obj_lib.add_supervised_loss(  # just softmax_cross_entropy
          labels=labels['labels'],
          logits=logits_sup,
          weights=labels['mask'])  # what does labels[mask] do?

    # Add weight decay to loss, for non-LARS optimizers.
    model_util.add_weight_decay(adjust_per_optimizer=True)
    loss = tf.losses.get_total_loss()

    if FLAGS.train_mode == 'pretrain':
      variables_to_train = tf.trainable_variables()
    else:
      collection_prefix = 'trainable_variables_inblock_'
      variables_to_train = []
      for j in range(FLAGS.fine_tune_after_block + 1, 6):
        variables_to_train += tf.get_collection(collection_prefix + str(j))
      assert variables_to_train, 'variables_to_train shouldn\'t be empty!'

    tf.logging.info('===============Variables to train (begin)===============')
    tf.logging.info(variables_to_train)
    tf.logging.info('================Variables to train (end)================')

    learning_rate = model_util.learning_rate_schedule(
        FLAGS.learning_rate, num_train_examples)

    if is_training:
      if FLAGS.train_summary_steps > 0:
        # Compute stats for the summary.
        prob_con = tf.nn.softmax(logits_con)
        entropy_con = - tf.reduce_mean(
            tf.reduce_sum(prob_con * tf.math.log(prob_con + 1e-8), -1))

        summary_writer = tf2.summary.create_file_writer(FLAGS.model_dir)
        # TODO(iamtingchen): remove this control_dependencies in the future.
        with tf.control_dependencies([summary_writer.init()]):
          with summary_writer.as_default():
            should_record = tf.math.equal(
                tf.math.floormod(tf.train.get_global_step(),
                                 FLAGS.train_summary_steps), 0)
            with tf2.summary.record_if(should_record):
              contrast_acc = tf.equal(
                  tf.argmax(labels_con, 1), tf.argmax(logits_con, axis=1))
              contrast_acc = tf.reduce_mean(tf.cast(contrast_acc, tf.float32))
              label_acc = tf.equal(
                  tf.argmax(labels['labels'], 1), tf.argmax(logits_sup, axis=1))
              label_acc = tf.reduce_mean(tf.cast(label_acc, tf.float32))
              tf2.summary.scalar(
                  'train_contrast_loss',
                  contrast_loss,
                  step=tf.train.get_global_step())
              tf2.summary.scalar(
                  'train_contrast_acc',
                  contrast_acc,
                  step=tf.train.get_global_step())
              tf2.summary.scalar(
                  'train_label_accuracy',
                  label_acc,
                  step=tf.train.get_global_step())
              tf2.summary.scalar(
                  'contrast_entropy',
                  entropy_con,
                  step=tf.train.get_global_step())
              tf2.summary.scalar(
                  'learning_rate', learning_rate,
                  step=tf.train.get_global_step())

              if FLAGS.train_mode == 'pretrain':
                tf2.summary.histogram(
                    'mask_hist', attn_mask,
                    step=tf.train.get_global_step())

      optimizer = model_util.get_optimizer(learning_rate)
      control_deps = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
      if FLAGS.train_summary_steps > 0:
        control_deps.extend(tf.summary.all_v2_summary_ops())
      with tf.control_dependencies(control_deps):
        train_op = optimizer.minimize(
            loss, global_step=tf.train.get_or_create_global_step(),
            var_list=variables_to_train)

      if FLAGS.checkpoint:
        def scaffold_fn():
          """Scaffold function to restore non-logits vars from checkpoint."""
          tf.train.init_from_checkpoint(
              FLAGS.checkpoint,
              {v.op.name: v.op.name
               for v in tf.global_variables(FLAGS.variable_schema)})

          if FLAGS.zero_init_logits_layer:
            # Init op that initializes output layer parameters to zeros.
            output_layer_parameters = [
                var for var in tf.trainable_variables() if var.name.startswith(
                    'head_supervised')]
            tf.logging.info('Initializing output layer parameters %s to zero',
                            [x.op.name for x in output_layer_parameters])
            with tf.control_dependencies([tf.global_variables_initializer()]):
              init_op = tf.group([
                  tf.assign(x, tf.zeros_like(x))
                  for x in output_layer_parameters])
            return tf.train.Scaffold(init_op=init_op)
          else:
            return tf.train.Scaffold()
      else:
        scaffold_fn = None

      return tf.estimator.tpu.TPUEstimatorSpec(
          mode=mode, train_op=train_op, loss=loss, scaffold_fn=scaffold_fn)
    else:

      def metric_fn(logits_sup, labels_sup, logits_con, labels_con, mask,
                    **kws):
        """Inner metric function."""
        metrics = {k: tf.metrics.mean(v, weights=mask)
                   for k, v in kws.items()}
        metrics['label_top_1_accuracy'] = tf.metrics.accuracy(
            tf.argmax(labels_sup, 1), tf.argmax(logits_sup, axis=1),
            weights=mask)
        metrics['label_top_5_accuracy'] = tf.metrics.recall_at_k(
            tf.argmax(labels_sup, 1), logits_sup, k=5, weights=mask)
        metrics['contrastive_top_1_accuracy'] = tf.metrics.accuracy(
            tf.argmax(labels_con, 1), tf.argmax(logits_con, axis=1),
            weights=mask)
        metrics['contrastive_top_5_accuracy'] = tf.metrics.recall_at_k(
            tf.argmax(labels_con, 1), logits_con, k=5, weights=mask)
        return metrics

      metrics = {
          'logits_sup': logits_sup,
          'labels_sup': labels['labels'],
          'logits_con': logits_con,
          'labels_con': labels_con,
          'mask': labels['mask'],
          'contrast_loss': tf.fill((params['batch_size'],), contrast_loss),
          'regularization_loss': tf.fill((params['batch_size'],),
                                         tf.losses.get_regularization_loss()),
      }

      return tf.estimator.tpu.TPUEstimatorSpec(
          mode=mode,
          loss=loss,
          eval_metrics=(metric_fn, metrics),
          scaffold_fn=None)

  return model_fn


def get_models():
    model_cropped = resnet.resnet_v1(
        resnet_depth=FLAGS.resnet_depth,
        width_multiplier=FLAGS.width_multiplier,
        cifar_stem=FLAGS.crop_size <= 32)

    # generic resnet_v2 keras

    model_full = resnet.resnet_v1(
        resnet_depth=FLAGS.resnet_depth,
        width_multiplier=FLAGS.width_multiplier,
        cifar_stem=FLAGS.image_size <= 32)

    # # with tf.variable_scope(name, reuse=tf.AUTO_REUSE):
    # # uses call(inputs, training) instead of call(inputs, is_training)
    # # does call() in keras use call(imputs, is_training)? (not tf.keras, just keras) No, they are now in sync
    # # tf.disable_eager_execution()
    # # with tf.Graph().as_default():
    # model_cropped = tf2.keras.applications.resnet.ResNet50(include_top=False, weights=None, input_tensor=None,
    #                                                       input_shape=(FLAGS.image_size, FLAGS.image_size, 3),
    #                                                       pooling=None)
    # model_full = tf2.keras.applications.resnet.ResNet50(include_top=False, weights=None, input_tensor=None,
    #                                                    input_shape=(FLAGS.image_size, FLAGS.image_size, 3),
    #                                                    pooling=None)
    # model_cropped.summary()
    # model_full.summary()
    # # ValueError: Tensor("conv1_conv_1/kernel/Read/ReadVariableOp:0", shape=(7, 7, 3, 64), dtype=float32) must be from the same graph as Tensor("base_model/resnet50/conv1_pad/Pad:0", shape=(128, 38, 38, 3), dtype=float32).

    models = {'model_full': model_full, 'model_cropped': model_cropped}

    return models



