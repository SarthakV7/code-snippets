
# Copyright 2019 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Adapted in part from:
# https://github.com/GoogleCloudPlatform/data-science-on-gcp/blob/master/09_cloudml/flights_model_tf2.ipynb
# by Valliappa Lakshmanan.  (See that repo for more info about the accompanying book,
# "Data Science on the Google Cloud Platform", from O'Reilly.)



import argparse
import logging
import os, json, math, time, shutil
import numpy as np

import pathlib2
import tensorflow as tf

import bwmodel.model as bwmodel

DEVELOP_MODE = False
NBUCKETS = 5 # for embeddings
NUM_EXAMPLES = 1000*1000 * 20 # assume 20 million examples
# DNN_HIDDEN_UNITS = '128,64,32'

# CSV_COLUMNS  = ('duration,end_station_id,bike_id,ts,day_of_week,start_station_id' +
#                 ',start_latitude,start_longitude,end_latitude,end_longitude' +
#                 ',euclidean,loc_cross,prcp,max,min,temp,dewp').split(',')
# LABEL_COLUMN = 'duration'
# DEFAULTS     = [[0.0],['na'],['na'],[0.0],['na'],['na'],
#                [0.0],[0.0],[0.0],[0.0],
#                [0.0],['na'],[0.0],[0.0],[0.0],[0.0], [0.0]]

STRATEGY = tf.distribute.MirroredStrategy()
TRAIN_BATCH_SIZE = 64 * STRATEGY.num_replicas_in_sync

TRAIN_OUTPUT_PATH = '/tmp/train_output_path.txt'

# def load_dataset(pattern, batch_size=1):
#   return tf.data.experimental.make_csv_dataset(pattern, batch_size, CSV_COLUMNS, DEFAULTS)

# def features_and_labels(features):
#   label = features.pop('duration') # this is what we will train for
#   features.pop('bike_id')
#   return features, label

# def read_dataset(pattern, batch_size, mode=tf.estimator.ModeKeys.TRAIN, truncate=None):
#   dataset = load_dataset(pattern, batch_size)
#   dataset = dataset.map(features_and_labels, num_parallel_calls=tf.data.experimental.AUTOTUNE)
#   if mode == tf.estimator.ModeKeys.TRAIN:
#     dataset = dataset.repeat().shuffle(batch_size*10)
#     # dataset = dataset.repeat()
#   dataset = dataset.prefetch(buffer_size=tf.data.experimental.AUTOTUNE)
#   # dataset = dataset.prefetch(1)
#   if truncate is not None:
#     dataset = dataset.take(truncate)
#   return dataset


# # Build a wide-and-deep model.
# def wide_and_deep_classifier(inputs, linear_feature_columns, dnn_feature_columns,
#     num_hidden_layers, dnn_hidden_units1, learning_rate):
#     deep = tf.keras.layers.DenseFeatures(dnn_feature_columns, name='deep_inputs')(inputs)
#     # layers = [int(x) for x in dnn_hidden_units.split(',')]
#     layers = [dnn_hidden_units1]
#     if num_hidden_layers > 1:
#       layers += [int(dnn_hidden_units1/(x*2)) for x in range(1, num_hidden_layers)]
#     # layers = [dnn_hidden_units1, dnn_hidden_units1/2, dnn_hidden_units1/4]  # using hp tuning val, but hardwired to 3 layers currently.
#     for layerno, numnodes in enumerate(layers):
#         deep = tf.keras.layers.Dense(numnodes, activation='relu', name='dnn_{}'.format(layerno+1))(deep)
#     wide = tf.keras.layers.DenseFeatures(linear_feature_columns, name='wide_inputs')(inputs)
#     both = tf.keras.layers.concatenate([deep, wide], name='both')
#     output = tf.keras.layers.Dense(1, name='dur')(both)
#     model = tf.keras.Model(inputs, output)
#     optimizer = tf.keras.optimizers.RMSprop(learning_rate)
#     model.compile(loss='mse', optimizer=optimizer,
#                  metrics=['mse', 'mae', tf.keras.metrics.RootMeanSquaredError()])
#     return model


def create_model(learning_rate, hidden_size, num_hidden_layers):

  # duration,end_station_id,bike_id,ts,day_of_week,start_station_id,start_latitude,start_longitude,end_latitude,end_longitude,
  # euclidean,loc_cross,prcp,max,min,temp,dewp

  real = {
      colname : tf.feature_column.numeric_column(colname)
            for colname in
  #            ('ts,start_latitude,start_longitude,end_latitude,end_longitude,euclidean,prcp,max,min,temp,dewp').split(',')
              ('ts,euclidean,prcp,max,min,temp,dewp').split(',')

  }
  sparse = {
        'day_of_week': tf.feature_column.categorical_column_with_vocabulary_list('day_of_week',
                    vocabulary_list='1,2,3,4,5,6,7'.split(',')),
        'end_station_id' : tf.feature_column.categorical_column_with_hash_bucket('end_station_id', hash_bucket_size=800),
        'start_station_id' : tf.feature_column.categorical_column_with_hash_bucket('start_station_id', hash_bucket_size=800),
        'loc_cross' : tf.feature_column.categorical_column_with_hash_bucket('loc_cross', hash_bucket_size=21000),
  #      'bike_id' : tf.feature_column.categorical_column_with_hash_bucket('bike_id', hash_bucket_size=14000)
  }

  inputs = {
      colname : tf.keras.layers.Input(name=colname, shape=(), dtype='float32')
            for colname in real.keys()
  }
  inputs.update({
      colname : tf.keras.layers.Input(name=colname, shape=(), dtype='string')
            for colname in sparse.keys()
  })

  # embed all the sparse columns
  embed = {
         'embed_{}'.format(colname) : tf.feature_column.embedding_column(col, 10)
            for colname, col in sparse.items()
  }
  real.update(embed)

  # one-hot encode the sparse columns
  sparse = {
      colname : tf.feature_column.indicator_column(col)
            for colname, col in sparse.items()
  }

  if DEVELOP_MODE:
      print(sparse.keys())
      print(real.keys())

  model = None
  print('num replicas...')
  print(STRATEGY.num_replicas_in_sync)

  with STRATEGY.scope():  # hmmm
    model = bwmodel.wide_and_deep_classifier(
        inputs,
        linear_feature_columns = sparse.values(),
        dnn_feature_columns = real.values(),
        num_hidden_layers = num_hidden_layers,
        dnn_hidden_units1 = hidden_size,
        learning_rate=learning_rate)


  model.summary()
  return model


def main():

  logging.getLogger().setLevel(logging.INFO)
  parser = argparse.ArgumentParser(description='ML Trainer')
  parser.add_argument(
      '--epochs', type=int, default=1)
  parser.add_argument(
      '--hptune-results', required=True)  # e.g. {"num_hidden_layers": 3, "hidden_size": 96, "learning_rate": 0.01}
  parser.add_argument(
      '--steps-per-epoch', type=int,
      default=-1)  # if set to -1, don't override the normal calcs for this
  parser.add_argument(
      '--hp-idx', type=int,
      default=0)      
  parser.add_argument(
      '--workdir', required=True)
  parser.add_argument(
      '--data-dir', default='gs://aju-dev-demos-codelabs/bikes_weather/')
  # use this arg to load the model weights from a pre-existing checkpoint (this is not the
  # same as the 'checkpoint_path'). These weights must be from a model of the same architecture.
  # parser.add_argument(
  #     '--load-checkpoint',
  #     )
  # parser.add_argument(
  #     '--train-output-path',
  #     )

  args = parser.parse_args()
  logging.info("Tensorflow version " + tf.__version__)

  logging.info('got hptune results: %s', args.hptune_results)
  hptune_info = json.loads(str(args.hptune_results))
  logging.info('hptune_info: %s', hptune_info)
  # extract hptuning best params results
  learning_rate = hptune_info[args.hp_idx]['learning_rate']
  hidden_size = hptune_info[args.hp_idx]['hidden_size']
  num_hidden_layers = hptune_info[args.hp_idx]['num_hidden_layers']
  logging.info('using: %s, %s, %s', learning_rate, hidden_size, num_hidden_layers)

  TRAIN_DATA_PATTERN = args.data_dir + "train*"
  EVAL_DATA_PATTERN = args.data_dir + "test*"
  OUTPUT_DIR='{}/bwmodel/trained_model'.format(args.workdir)
  logging.info('Writing trained model to {}'.format(OUTPUT_DIR))

  train_batch_size = TRAIN_BATCH_SIZE
  eval_batch_size = 1000
  if args.steps_per_epoch == -1:  # calc based on dataset size
    steps_per_epoch = NUM_EXAMPLES // train_batch_size
  else:
    steps_per_epoch = args.steps_per_epoch
  logging.info('using {} steps per epoch'.format(steps_per_epoch))

  train_dataset = bwmodel.read_dataset(TRAIN_DATA_PATTERN, train_batch_size)
  eval_dataset = bwmodel.read_dataset(EVAL_DATA_PATTERN, eval_batch_size, tf.estimator.ModeKeys.EVAL,
     eval_batch_size * 100 * STRATEGY.num_replicas_in_sync
  )

  model = create_model(learning_rate, hidden_size, num_hidden_layers)

  checkpoint_path = '{}/checkpoints/bikes_weather.cpt'.format(OUTPUT_DIR)
  logging.info("checkpoint path: %s", checkpoint_path)
  cp_callback = tf.keras.callbacks.ModelCheckpoint(checkpoint_path,
                                                   save_weights_only=True,
                                                   verbose=1)
  tb_callback = tf.keras.callbacks.TensorBoard(log_dir='{}/logs'.format(OUTPUT_DIR),
                                               update_freq=10000)

  logging.info("training model....")
  history = model.fit(train_dataset,
                      validation_data=eval_dataset,
                      validation_steps=eval_batch_size,
                      epochs=args.epochs,
                      steps_per_epoch=steps_per_epoch,
                      callbacks=[cp_callback  # , tb_callback
                      ]
                     )
  logging.info(history.history.keys())

  ts = str(int(time.time()))
  export_dir = '{}/export/bikesw/{}'.format(OUTPUT_DIR, ts)
  logging.info('Exporting to {}'.format(export_dir))

  try:
    pathlib2.Path(TRAIN_OUTPUT_PATH).parent.mkdir(parents=True)
  except FileExistsError as e1:
    logging.info(e1)
  try:
    logging.info("exporting model....")
    tf.saved_model.save(model, export_dir)
    logging.info("train_output_path: %s", TRAIN_OUTPUT_PATH)
    export_path = '{}/export/bikesw'.format(OUTPUT_DIR)
    logging.info('export path: {}'.format(export_path))
    pathlib2.Path(TRAIN_OUTPUT_PATH).write_text(export_path)
  except Exception as e:  # retry once if error
    logging.warning(e)
    logging.info("retrying...")
    time.sleep(10)
    logging.info("again ... exporting model....")
    tf.saved_model.save(model, export_dir)
    export_path = '{}/export/bikesw'.format(OUTPUT_DIR)
    pathlib2.Path(TRAIN_OUTPUT_PATH).write_text(export_path)


if __name__ == "__main__":
  main()
