# Copyright 2024 Google LLC.
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
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

"""Tests for gp_ucb_pe."""

import copy
from typing import Any, Tuple

import jax
import numpy as np
from vizier import pyvizier as vz
from vizier._src.algorithms.core import abstractions
from vizier._src.algorithms.designers import gp_ucb_pe
from vizier._src.algorithms.optimizers import eagle_strategy as es
from vizier._src.algorithms.optimizers import vectorized_base as vb
from vizier.jax import optimizers
from vizier.pyvizier.converters import padding
from vizier.testing import test_studies

from absl.testing import absltest
from absl.testing import parameterized

ensemble_ard_optimizer = optimizers.default_optimizer()


def _extract_predictions(
    metadata: Any,
) -> Tuple[float, float, float, float, bool]:
  pred = metadata.ns('prediction_in_warped_y_space')
  return (
      float(pred['mean']),
      float(pred['stddev']),
      float(pred['stddev_from_all']),
      float(pred['acquisition']),
      bool(pred['use_ucb'] == 'True'),
  )


class GpUcbPeTest(parameterized.TestCase):

  @parameterized.parameters(
      dict(iters=3, batch_size=5, num_seed_trials=5),
      dict(iters=5, batch_size=1, num_seed_trials=2),
      dict(iters=5, batch_size=3, num_seed_trials=2, ensemble_size=3),
      dict(iters=3, batch_size=5, num_seed_trials=5, applies_padding=True),
      dict(iters=5, batch_size=1, num_seed_trials=2, pe_overwrite=True),
      dict(
          iters=3,
          batch_size=5,
          num_seed_trials=5,
          applies_padding=True,
          optimize_set_acquisition_for_exploration=True,
      ),
      dict(
          iters=3,
          batch_size=5,
          num_seed_trials=5,
          applies_padding=True,
          optimize_set_acquisition_for_exploration=True,
          search_space=test_studies.flat_categorical_space(),
      ),
  )
  def test_on_flat_space(
      self,
      iters: int = 5,
      batch_size: int = 1,
      num_seed_trials: int = 1,
      ard_optimizer: str = 'default',
      ensemble_size: int = 1,
      applies_padding: bool = False,
      pe_overwrite: bool = False,
      optimize_set_acquisition_for_exploration: bool = False,
      search_space: vz.SearchSpace = (
          test_studies.flat_continuous_space_with_scaling()
      ),
  ):
    # We use string names so that test case names are readable. Convert them
    # to objects.
    if ard_optimizer == 'default':
      ard_optimizer = optimizers.default_optimizer()
    problem = vz.ProblemStatement(search_space)
    problem.metric_information.append(
        vz.MetricInformation(
            name='metric', goal=vz.ObjectiveMetricGoal.MAXIMIZE
        )
    )
    vectorized_optimizer_factory = vb.VectorizedOptimizerFactory(
        strategy_factory=es.VectorizedEagleStrategyFactory(),
        max_evaluations=100,
    )
    designer = gp_ucb_pe.VizierGPUCBPEBandit(
        problem,
        acquisition_optimizer_factory=vectorized_optimizer_factory,
        num_seed_trials=num_seed_trials,
        ard_optimizer=ard_optimizer,
        metadata_ns='gp_ucb_pe_bandit_test',
        config=gp_ucb_pe.UCBPEConfig(
            ucb_coefficient=10.0,
            explore_region_ucb_coefficient=0.5,
            cb_violation_penalty_coefficient=10.0,
            ucb_overwrite_probability=0.0,
            pe_overwrite_probability=1.0 if pe_overwrite else 0.0,
            optimize_set_acquisition_for_exploration=(
                optimize_set_acquisition_for_exploration
            ),
        ),
        ensemble_size=ensemble_size,
        padding_schedule=padding.PaddingSchedule(
            num_trials=padding.PaddingType.MULTIPLES_OF_10
            if applies_padding
            else padding.PaddingType.NONE,
        ),
        rng=jax.random.PRNGKey(1),
    )

    all_active_trials = []
    all_trials = []
    trial_id = 1
    # Simulates batch suggestions with delayed feedback: the first two batches
    # are generated by the designer without any completed trials (but all with
    # active trials). Starting from the third batch, the oldest batch gets
    # completed and updated to the new designer with all the active trials, and
    # the designer then makes a new batch of suggestions. The last two batches
    # of suggestions are again made with only active trials being updated to
    # the designer.
    for idx in range(iters + 2):
      suggestions = designer.suggest(batch_size)
      self.assertLen(suggestions, batch_size)
      for suggestion in suggestions:
        problem.search_space.assert_contains(suggestion.parameters)
        all_active_trials.append(suggestion.to_trial(trial_id))
        all_trials.append(copy.deepcopy(all_active_trials[-1]))
        trial_id += 1
      completed_trials = []
      # Starting from the second until the last but two batch, complete the
      # oldest batch of suggestions.
      if idx > 0 and idx < iters:
        for _ in range(batch_size):
          measurement = vz.Measurement()
          for mi in problem.metric_information:
            measurement.metrics[mi.name] = float(
                jax.random.uniform(
                    jax.random.PRNGKey(1),
                    minval=mi.min_value_or(lambda: -10.0),
                    maxval=mi.max_value_or(lambda: 10.0),
                )
            )
          completed_trials.append(
              all_active_trials.pop(0).complete(measurement)
          )
      designer.update(
          completed=abstractions.CompletedTrials(completed_trials),
          all_active=abstractions.ActiveTrials(all_active_trials),
      )

    self.assertLen(all_trials, (iters + 2) * batch_size)

    # The suggestions after the seeds up to the first two batches are expected
    # to be generated by the PE acquisition function.
    for jdx in range(2 * batch_size):
      # Before the designer was updated with enough trials, the suggested
      # batches were seeds, not from acquisition optimization.
      if (jdx // batch_size) * batch_size >= num_seed_trials:
        _, _, _, acq, use_ucb = _extract_predictions(
            all_trials[jdx].metadata.ns('gp_ucb_pe_bandit_test')
        )
        self.assertFalse(use_ucb)
        if not optimize_set_acquisition_for_exploration:
          self.assertGreaterEqual(acq, 0.0, msg=f'suggestion: {jdx}')

    for idx in range(2, iters + 2):
      # Skips seed trials, which are not generated by acquisition function
      # optimization.
      if idx * batch_size < num_seed_trials:
        continue
      set_acq_value = None
      stddev_from_all_list = []
      for jdx in range(batch_size):
        mean, stddev, stddev_from_all, acq, use_ucb = _extract_predictions(
            all_trials[idx * batch_size + jdx].metadata.ns(
                'gp_ucb_pe_bandit_test'
            )
        )
        if jdx == 0 and idx < (iters + 1) and not pe_overwrite:
          # Except for the last batch of suggestions, the acquisition value of
          # the first suggestion in a batch is expected to be UCB, which
          # combines the predicted mean based only on completed trials and the
          # predicted standard deviation based on all trials.
          self.assertAlmostEqual(mean + 10.0 * stddev_from_all, acq)
          self.assertTrue(use_ucb)
          continue

        self.assertFalse(use_ucb)
        if optimize_set_acquisition_for_exploration:
          stddev_from_all_list.append(stddev_from_all)
          if set_acq_value is None:
            set_acq_value = acq
          else:
            self.assertAlmostEqual(set_acq_value, acq)
        else:
          # Because `ucb_overwrite_probability` is set to 0.0, when the designer
          # makes suggestions without seeing newer completed trials, it uses the
          # Pure-Exploration acquisition function. In this test, that happens
          # on the entire last batch and the second until the last suggestions
          # in every batch. The Pure-Exploration acquisition values are standard
          # deviation predictions based on all trials (completed and pending),
          # and are expected to be not much larger than the standard deviation
          # predictions based only on completed trials.
          self.assertLessEqual(
              acq, 2 * stddev, msg=f'batch: {idx}, suggestion: {jdx}'
          )
      if optimize_set_acquisition_for_exploration:
        geometric_mean_of_pred_cov_eigs = np.exp(
            set_acq_value / (batch_size - 1)
        )
        arithmetic_mean_of_pred_cov_eigs = np.mean(
            np.square(stddev_from_all_list)
        )
        self.assertLessEqual(
            geometric_mean_of_pred_cov_eigs, arithmetic_mean_of_pred_cov_eigs
        )

  def test_ucb_overwrite(self):
    problem = vz.ProblemStatement(
        test_studies.flat_continuous_space_with_scaling()
    )
    problem.metric_information.append(
        vz.MetricInformation(
            name='metric', goal=vz.ObjectiveMetricGoal.MAXIMIZE
        )
    )
    vectorized_optimizer_factory = vb.VectorizedOptimizerFactory(
        strategy_factory=es.VectorizedEagleStrategyFactory(),
        max_evaluations=100,
    )
    designer = gp_ucb_pe.VizierGPUCBPEBandit(
        problem,
        acquisition_optimizer_factory=vectorized_optimizer_factory,
        metadata_ns='gp_ucb_pe_bandit_test',
        num_seed_trials=1,
        config=gp_ucb_pe.UCBPEConfig(
            ucb_coefficient=10.0,
            explore_region_ucb_coefficient=0.5,
            cb_violation_penalty_coefficient=10.0,
            ucb_overwrite_probability=1.0,
        ),
        padding_schedule=padding.PaddingSchedule(
            num_trials=padding.PaddingType.MULTIPLES_OF_10
        ),
        rng=jax.random.PRNGKey(1),
    )

    trial_id = 1
    batch_size = 5
    iters = 3
    rng = jax.random.PRNGKey(1)
    all_trials = []
    # Simulates a batch suggestion loop that completes a full batch of
    # suggestions before asking for the next batch.
    for _ in range(iters):
      suggestions = designer.suggest(count=batch_size)
      self.assertLen(suggestions, batch_size)
      completed_trials = []
      for suggestion in suggestions:
        problem.search_space.assert_contains(suggestion.parameters)
        trial_id += 1
        measurement = vz.Measurement()
        for mi in problem.metric_information:
          measurement.metrics[mi.name] = float(
              jax.random.uniform(
                  rng,
                  minval=mi.min_value_or(lambda: -10.0),
                  maxval=mi.max_value_or(lambda: 10.0),
              )
          )
          rng, _ = jax.random.split(rng)
        completed_trials.append(
            suggestion.to_trial(trial_id).complete(measurement)
        )
      all_trials.extend(completed_trials)
      designer.update(
          completed=abstractions.CompletedTrials(completed_trials),
          all_active=abstractions.ActiveTrials(),
      )

    self.assertLen(all_trials, iters * batch_size)

    for idx, trial in enumerate(all_trials):
      if idx < batch_size:
        # Skips the first batch of suggestions, which are generated by the
        # seeding designer, not acquisition function optimization.
        continue
      # Because `ucb_overwrite_probability` is 1, all suggestions after the
      # first batch are expected to be generated by UCB. Within a batch, the
      # first suggestion's UCB value is expected to use predicted standard
      # deviation based only on completed trials, while the UCB values of
      # the second to the last suggestions are expected to use the predicted
      # standard deviations based on completed and active trials.
      mean, stddev, stddev_from_all, acq, use_ucb = _extract_predictions(
          trial.metadata.ns('gp_ucb_pe_bandit_test')
      )
      self.assertAlmostEqual(
          mean + 10.0 * (stddev_from_all if idx % batch_size > 0 else stddev),
          acq,
      )
      self.assertTrue(use_ucb)


if __name__ == '__main__':
  jax.config.update('jax_enable_x64', True)
  absltest.main()
