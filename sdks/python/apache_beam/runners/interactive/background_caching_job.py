#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Module to build and run background caching job.

For internal use only; no backwards-compatibility guarantees.

A background caching job is a job that caches events for all unbounded sources
of a given pipeline. With Interactive Beam, one such job is started when a
pipeline run happens (which produces a main job in contrast to the background
caching job) and meets the following conditions:

  #. The pipeline contains unbounded sources.
  #. No such background job is running.
  #. No such background job has completed successfully and the cached events are
     still valid (invalidated when unbounded sources change in the pipeline).

Once started, the background caching job runs asynchronously until it hits some
cache size limit. Meanwhile, the main job and future main jobs from the pipeline
will run using the deterministic replayable cached events until they are
invalidated.
"""

# pytype: skip-file

from __future__ import absolute_import

import logging
import threading
import time

import apache_beam as beam
from apache_beam.runners.interactive import interactive_environment as ie
from apache_beam.runners.interactive.caching import streaming_cache
from apache_beam.runners.runner import PipelineState

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.INFO)


class BackgroundCachingJob(object):
  """A simple abstraction that controls necessary components of a timed and
  space limited background caching job.

  A background caching job successfully terminates in 2 conditions:

    #. The job is finite and run into DONE state;
    #. The job is infinite but hit an Interactive Beam options configured limit
       and gets cancelled into CANCELLED state.

  In both situations, the background caching job should be treated as done
  successfully.
  """
  def __init__(self, pipeline_result, start_limit_checkers=True):
    self._pipeline_result = pipeline_result
    self._timer = threading.Timer(
        ie.current_env().options.capture_duration.total_seconds(), self._cancel)
    self._condition_checker = threading.Thread(
        target=self._background_caching_job_condition_checker)
    if start_limit_checkers:
      self._timer.start()
      self._condition_checker.start()
    self._timer_triggered = False
    self._condition_checker_triggered = False

  def _background_caching_job_condition_checker(self):
    while True:
      if self._should_end_condition_checker():
        break
      time.sleep(5)

  def _should_end_condition_checker(self):
    if ie.current_env().options.capture_control.is_capture_size_reached():
      self._condition_checker_triggered = True
      self.cancel()
      return True
    if not self._timer.is_alive():
      # If the timer is not alive any more, the background caching job must
      # have been cancelled by business logic or the timer.
      return True
    return False

  def is_done(self):
    return (
        self._pipeline_result.state is PipelineState.DONE or (
            (self._timer_triggered or
             self._condition_checker_triggered) and
            self._pipeline_result.state in (
                PipelineState.CANCELLED, PipelineState.CANCELLING)))

  def is_running(self):
    return self._pipeline_result.state is PipelineState.RUNNING

  def cancel(self):
    """Cancels this background caching job and its terminating timer because
    the job is invalidated and no longer useful.

    This process cancels any non-terminated job and its terminating timer.
    """
    self._cancel()
    # Whenever this function is invoked, the cancellation is not done by the
    # terminating timer, thus re-mark the timer as not triggered.
    self._timer_triggered = False
    if self._timer.is_alive():
      self._timer.cancel()

  def _cancel(self):
    self._timer_triggered = True
    if not PipelineState.is_terminal(self._pipeline_result.state):
      try:
        self._pipeline_result.cancel()
      except NotImplementedError:
        # Ignore the cancel invocation if it is never implemented by the runner.
        pass


def attempt_to_run_background_caching_job(runner, user_pipeline, options=None):
  """Attempts to run a background caching job for a user-defined pipeline.

  The pipeline result is automatically tracked by Interactive Beam in case
  future cancellation/cleanup is needed.
  """
  if is_background_caching_job_needed(user_pipeline):
    # Cancel non-terminal jobs if there is any before starting a new one.
    attempt_to_cancel_background_caching_job(user_pipeline)
    # Cancel the gRPC server serving the test stream if there is one.
    attempt_to_stop_test_stream_service(user_pipeline)
    # TODO(BEAM-8335): refactor background caching job logic from
    # pipeline_instrument module to this module and aggregate tests.
    from apache_beam.runners.interactive import pipeline_instrument as instr
    runner_pipeline = beam.pipeline.Pipeline.from_runner_api(
        user_pipeline.to_runner_api(use_fake_coders=True), runner, options)
    background_caching_job_result = beam.pipeline.Pipeline.from_runner_api(
        instr.build_pipeline_instrument(
            runner_pipeline).background_caching_pipeline_proto(),
        runner,
        options).run()
    ie.current_env().set_background_caching_job(
        user_pipeline, BackgroundCachingJob(background_caching_job_result))


def is_background_caching_job_needed(user_pipeline):
  """Determines if a background caching job needs to be started.

  It does several state checks and record state changes throughout the process.
  It is not idempotent to simplify the usage.
  """
  job = ie.current_env().get_background_caching_job(user_pipeline)
  # Checks if the pipeline contains any source that needs to be cached.
  need_cache = has_source_to_cache(user_pipeline)
  # If this is True, we can invalidate a previous done/running job if there is
  # one.
  cache_changed = is_source_to_cache_changed(user_pipeline)
  return (
      need_cache and
      # Checks if it's the first time running a job from the pipeline.
      (
          not job or
          # Or checks if there is no previous job.
          # DONE means a previous job has completed successfully and the
          # cached events might still be valid.
          not (
              job.is_done() or
              # RUNNING means a previous job has been started and is still
              # running.
              job.is_running()) or
          # Or checks if we can invalidate the previous job.
          cache_changed))


def has_source_to_cache(user_pipeline):
  """Determines if a user-defined pipeline contains any source that need to be
  cached. If so, also immediately wrap current cache manager held by current
  interactive environment into a streaming cache if this has not been done.
  The wrapping doesn't invalidate existing cache in any way.

  This can help determining if a background caching job is needed to write cache
  for sources and if a test stream service is needed to serve the cache.

  Throughout the check, if source-to-cache has changed from the last check, it
  also cleans up the invalidated cache early on.
  """
  from apache_beam.runners.interactive import pipeline_instrument as instr
  # TODO(BEAM-8335): we temporarily only cache replaceable unbounded sources.
  # Add logic for other cacheable sources here when they are available.
  has_cache = instr.has_unbounded_sources(user_pipeline)
  if has_cache:
    if not isinstance(ie.current_env().cache_manager(),
                      streaming_cache.StreamingCache):
      # Wrap the cache manager into a streaming cache manager. Note this
      # does not invalidate the current cache manager.
      def is_cache_complete():
        job = ie.current_env().get_background_caching_job(user_pipeline)
        is_done = job and job.is_done()
        cache_changed = is_source_to_cache_changed(
            user_pipeline, update_cached_source_signature=False)
        return is_done and not cache_changed

      ie.current_env().set_cache_manager(
          streaming_cache.StreamingCache(
              ie.current_env().cache_manager()._cache_dir,
              is_cache_complete=is_cache_complete,
              sample_resolution_sec=1.0))
  return has_cache


def attempt_to_cancel_background_caching_job(user_pipeline):
  """Attempts to cancel background caching job for a user-defined pipeline.

  If no background caching job needs to be cancelled, NOOP. Otherwise, cancel
  such job.
  """
  job = ie.current_env().get_background_caching_job(user_pipeline)
  if job:
    job.cancel()


def attempt_to_stop_test_stream_service(user_pipeline):
  """Attempts to stop the gRPC server/service serving the test stream.

  If there is no such server started, NOOP. Otherwise, stop it.
  """
  if is_a_test_stream_service_running(user_pipeline):
    ie.current_env().evict_test_stream_service_controller(user_pipeline).stop()


def is_a_test_stream_service_running(user_pipeline):
  """Checks to see if there is a gPRC server/service running that serves the
  test stream to any job started from the given user_pipeline.
  """
  return ie.current_env().get_test_stream_service_controller(
      user_pipeline) is not None


def is_source_to_cache_changed(
    user_pipeline, update_cached_source_signature=True):
  """Determines if there is any change in the sources that need to be cached
  used by the user-defined pipeline.

  Due to the expensiveness of computations and for the simplicity of usage, this
  function is not idempotent because Interactive Beam automatically discards
  previously tracked signature of transforms and tracks the current signature of
  transforms for the user-defined pipeline if there is any change.

  When it's True, there is addition/deletion/mutation of source transforms that
  requires a new background caching job.
  """
  # By default gets empty set if the user_pipeline is first time seen because
  # we can treat it as adding transforms.
  recorded_signature = ie.current_env().get_cached_source_signature(
      user_pipeline)
  current_signature = extract_source_to_cache_signature(user_pipeline)
  is_changed = not current_signature.issubset(recorded_signature)
  # The computation of extract_unbounded_source_signature is expensive, track on
  # change by default.
  if is_changed and update_cached_source_signature:
    if not recorded_signature:
      _LOGGER.info(
          'Interactive Beam has detected you have unbounded sources '
          'in your pipeline. In order to have a deterministic replay '
          'of your pipeline: {}'.format(
              ie.current_env().options.capture_control))
    else:
      _LOGGER.info(
          'Interactive Beam has detected a new streaming source was '
          'added to the pipeline. In order for the cached streaming '
          'data to start at the same time, all caches have been '
          'cleared. {}'.format(ie.current_env().options.capture_control))

    ie.current_env().cleanup()
    ie.current_env().set_cached_source_signature(
        user_pipeline, current_signature)
  return is_changed


def extract_source_to_cache_signature(user_pipeline):
  """Extracts a set of signature for sources that need to be cached in the
  user-defined pipeline.

  A signature is a str representation of urn and payload of a source.
  """
  from apache_beam.runners.interactive import pipeline_instrument as instr
  # TODO(BEAM-8335): we temporarily only cache replaceable unbounded sources.
  # Add logic for other cacheable sources here when they are available.
  unbounded_sources_as_applied_transforms = instr.unbounded_sources(
      user_pipeline)
  unbounded_sources_as_ptransforms = set(
      map(lambda x: x.transform, unbounded_sources_as_applied_transforms))
  context, _ = user_pipeline.to_runner_api(
      return_context=True, use_fake_coders=True)
  signature = set(
      map(
          lambda transform: str(transform.to_runner_api(context)),
          unbounded_sources_as_ptransforms))
  return signature
