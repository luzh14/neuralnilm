from __future__ import print_function, division
from functools import partial
import os
import shutil
from copy import copy
import numpy as np
import pandas as pd
import theano
from time import time
from pymongo import MongoClient

from lasagne.updates import nesterov_momentum
from lasagne.objectives import aggregate, squared_error
from lasagne.layers.helper import get_all_params
from .utils import sfloatX, ndim_tensor, ANSI, none_to_dict, sanitise_dict_for_mongo
from neuralnilm.data.datathread import DataThread
from neuralnilm.consts import DATA_FOLD_NAMES, COLLECTIONS
from neuralnilm.config import CONFIG

import logging
logger = logging.getLogger(__name__)


class Trainer(object):
    def __init__(self, net, data_pipeline, experiment_id,
                 mongo_host=None,
                 mongo_db='neuralnilm',
                 loss_func=squared_error,
                 loss_aggregation_mode='mean',
                 updates_func=nesterov_momentum,
                 updates_func_kwards=None,
                 learning_rates=None,
                 callbacks=None,
                 repeat_callbacks=None,
                 metrics=None):
        """
        Parameters
        ----------
        experiment_id : list of strings
            concatenated together with an underscore.
            Defines output path
        mongo_host : Address of PyMongo database.
            See http://docs.mongodb.org/manual/reference/connection-string/
        callbacks : list of 2-tuples (<iteration>, <function>)
            Function must accept a single argument: this Trainer object.
        repeat_callbacks : list of 2-tuples (<iteration>, <function>)
            Function must accept a single argument: this Trainer object.
            For example, to run validation every 100 iterations, set
            `repeat_callbacks=[(100, Trainer.validate)]`.
        metrics : neuralnilm.Metrics object
            Run during `Trainer.validation()`
        """
        # Database
        mongo_client = MongoClient(mongo_host)
        self.db = mongo_client[mongo_db]

        # Training and validation state
        self.learning_rates = (
            {0: 1E-2} if learning_rates is None else learning_rates)
        self.experiment_id = "_".join(experiment_id)
        self._train_func = None
        self.metrics = metrics
        self.net = net
        self.data_pipeline = data_pipeline
        self.min_train_cost = float("inf")

        # Check if this experiment already exists in database
        delete_or_quit = None
        if self.db.trained_nets.find_one({'_id': self.experiment_id}):
            delete_or_quit = raw_input(
                "Database already has an experiment with _id == {}."
                " Should the old experiment be deleted"
                " (both from the database and from disk)?"
                " Or quit? [Q/d] ".format(self.experiment_id)).lower()
            if delete_or_quit == 'd':
                logger.info("Deleting documents for old experiment.")
                self.db.trained_nets.delete_one({'_id': self.experiment_id})
                for collection in COLLECTIONS:
                    self.db[collection].delete_many(
                        {'experiment_id': self.experiment_id})
            else:
                raise KeyboardInterrupt()

        # Output path
        self.output_path = os.path.join(
            *([CONFIG['PATHS']['OUTPUT']] + experiment_id)
        )
        try:
            os.makedirs(self.output_path)
        except OSError as os_error:
            if os_error.errno == 17:  # file exists
                logger.info("Directory exists = '{}'".format(self.output_path))
                if delete_or_quit == 'd':
                    logger.info("  Deleting directory.")
                    shutil.rmtree(self.output_path)
                    os.makedirs(self.output_path)
                else:
                    logger.info("  Re-using directory.")
            else:
                raise

        # Loss and updates
        def aggregated_loss_func(prediction, target):
            loss = loss_func(prediction, target)
            return aggregate(loss, mode=loss_aggregation_mode)
        self.loss_func_name = loss_func.__name__
        self.loss_func = aggregated_loss_func
        self.updates_func_name = updates_func.__name__
        self.updates_func_kwards = none_to_dict(updates_func_kwards)
        self.updates_func = partial(updates_func, **self.updates_func_kwards)
        self.loss_aggregation_mode = loss_aggregation_mode

        # Learning rate
        # Set _learning_rate to -1 so when we set self.learning_rate
        # during the training loop, it will be logger correctly.
        self._learning_rate = theano.shared(sfloatX(-1), name='learning_rate')

        # Callbacks
        def callbacks_dataframe(lst):
            return pd.DataFrame(lst, columns=['iteration', 'function'])
        self.callbacks = callbacks_dataframe(callbacks)
        self.repeat_callbacks = callbacks_dataframe(repeat_callbacks)

        # Log to database
        self.db.trained_nets.insert_one(sanitise_dict_for_mongo(self.report()))

    @property
    def learning_rate(self):
        return self._learning_rate.get_value().flatten()[0]

    @learning_rate.setter
    def learning_rate(self, rate):
        rate = sfloatX(rate)
        if rate == self.learning_rate:
            logger.info(
                "Iteration {:d}: Attempted to change learning rate to {:.1E}"
                " but that is already the value!"
                .format(self.net.train_iterations, rate))
        else:
            logger.info(
                "Iteration {:d}: Change learning rate to {:.1E}"
                .format(self.net.train_iterations, rate))
            self.db.experiments.find_one_and_update(
                filter={'_id': self.experiment_id},
                update={
                    '$set':
                    {'trainer.learning_rates.{:d}'
                     .format(self.net.train_iterations):
                     float(rate)}},
                upsert=True
            )
            self._learning_rate.set_value(rate)

    def fit(self, num_iterations=None):
        self.data_thread = DataThread(self.data_pipeline)
        self.data_thread.start()

        run_menu = False

        try:
            self._training_loop(num_iterations)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt at iteration {}."
                        .format(self.net.train_iterations))
            run_menu = True
        finally:
            self.data_thread.stop()

        if run_menu:
            self._menu(num_iterations)

    def _training_loop(self, num_iterations=None):
        logger.info("Starting training for {} iterations."
                    .format(num_iterations))

        print("   Update # |  Train cost  | Secs per update | Source ID")
        print("------------|--------------|-----------------|-----------")

        while True:
            try:
                self._single_train_iteration()
            except TrainingError:
                break

            if self.net.train_iterations == num_iterations:
                break
            else:
                self.net.train_iterations += 1
        logger.info("Stopped training. Completed {} iterations."
                    .format(self.net.train_iterations))

    def _single_train_iteration(self):
        # Learning rate changes
        try:
            self.learning_rate = self.learning_rates[self.net.train_iterations]
        except KeyError:
            pass

        # Training
        time0 = time()
        batch = self.data_thread.get_batch()
        train_cost = self._get_train_func()(batch.input, batch.target)
        train_cost = float(train_cost.flatten()[0])
        duration = time() - time0

        # Save training costs
        score = {
            'experiment_id': self.experiment_id,
            'iteration': self.net.train_iterations,
            'loss': train_cost,
            'source_id': batch.metadata['source_id']
        }
        self.db.train_scores.insert_one(score)

        # Print training costs
        is_best = train_cost <= self.min_train_cost
        if is_best:
            self.min_train_cost = train_cost
        print(" {:>10d} | {}{:>10.6f}{}  |  {:>10.6f}     | {:>3d}".format(
            self.net.train_iterations,
            ANSI.BLUE if is_best else "",
            train_cost,
            ANSI.ENDC if is_best else "",
            duration, batch.metadata['source_id']))

        # Handle NaN costs
        if np.isnan(train_cost):
            msg = "training cost is NaN at iteration {}!".format(
                self.net.train_iterations)
            logger.error(msg)
            raise TrainingError(msg)

        # Callbacks
        def run_callbacks(df):
            for callback in df['function']:
                callback(self)
        repeat_callbacks = self.repeat_callbacks[
            (self.net.train_iterations %
             self.repeat_callbacks['iteration']) == 0]
        run_callbacks(repeat_callbacks)
        callbacks = self.callbacks[
            self.callbacks['iteration'] == self.net.train_iterations]
        run_callbacks(callbacks)

    def validate(self):
        sources = self.data_thread.data_pipeline.sources
        output_func = self.net.deterministic_output_func
        for source_id, source in enumerate(sources):
            for fold in DATA_FOLD_NAMES:
                batch = self.data_thread.data_pipeline.get_batch(
                    fold=fold, source_id=source_id)
                output = output_func(batch.after_processing.input)
                scores = self.metrics.compute_metrics(
                    output, batch.after_processing.target)
                self.db.validation_scores.insert_one({
                    'experiment_id': self.experiment_id,
                    'iteration': self.net.train_iterations,
                    'source_id': batch.metadata['source_id'],
                    'fold': fold,
                    'scores': scores
                })

    def _get_train_func(self):
        if self._train_func is None:
            self._train_func = self._compile_cost_func(validation=False)
        return self._train_func

    def _get_validation_cost_func(self):
        if self._validation_cost_func is None:
            self._validation_cost_func = self._compile_cost_func(
                validation=True)
        return self._validation_cost_func

    def _compile_cost_func(self, validation):
        logger.info("Compiling " + ("validation" if validation else "train") +
                    " cost function...")
        network_input = self.net.symbolic_input()
        network_output = self.net.symbolic_output(deterministic=validation)
        target_var = ndim_tensor(name='target', ndim=network_output.ndim)
        loss = self.loss_func(network_output, target_var)
        if validation:
            updates = None
        else:
            # Training
            all_params = get_all_params(self.net.layers[-1], trainable=True)
            updates = self.updates_func(
                loss, all_params, learning_rate=self._learning_rate)
        train_func = theano.function(
            inputs=[network_input, target_var],
            outputs=loss,
            updates=updates,
            on_unused_input='warn',
            allow_input_downcast=True)
        logger.info("Done compiling cost function.")
        return train_func

    def _menu(self, epochs):
        # Print menu
        print("")
        print("------------------ OPTIONS ------------------")
        print("d: Enter debugger.")
        print("s: Save plots and params.")
        print("q: Quit all experiments.")
        print("e: Change number of epochs to train this net (currently {})."
              .format(epochs))
        print("c: Continue training.")
        print("")

        # Get input
        selection_str = raw_input("Please enter one or more letters: ")
        selection_str = selection_str.lower()

        # Handle input
        for selection in selection_str:
            if selection == 'd':
                import ipdb
                ipdb.set_trace()
            elif selection == 's':
                self.net.save()
            elif selection == 'q':
                sure = raw_input("Are you sure you want to quit [Y/n]? ")
                if sure.lower() != 'n':
                    raise KeyboardInterrupt()
            elif selection == 'e':
                new_epochs = raw_input("New number of epochs (or 'None'): ")
                if new_epochs == 'None':
                    epochs = None
                else:
                    try:
                        epochs = int(new_epochs)
                    except:
                        print("'{}' not an integer!".format(new_epochs))
            elif selection == 'c':
                break
            else:
                print("Selection '{}' not recognised!".format(selection))
                break
        print("Continuing training for {} epochs...".format(epochs))
        self.fit(epochs)

    def report(self):
        report = {'trainer': copy(self.__dict__)}
        for attr in [
                'data_pipeline', 'loss_func', 'net', 'repeat_callbacks',
                'callbacks', 'db', 'experiment_id', 'metrics',
                '_learning_rate', '_train_func', 'updates_func']:
            report['trainer'].pop(attr, None)
        report['trainer']['metrics'] = self.metrics.report()
        report['data'] = self.data_pipeline.report()
        report['_id'] = self.experiment_id
        return report


class TrainingError(Exception):
    pass
