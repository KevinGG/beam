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

# pytype: skip-file

from __future__ import absolute_import

import unittest

from apache_beam import coders
from apache_beam.options.pipeline_options import StandardOptions
from apache_beam.portability.api.beam_interactive_api_pb2 import TestStreamFileHeader
from apache_beam.portability.api.beam_interactive_api_pb2 import TestStreamFileRecord
from apache_beam.portability.api.beam_runner_api_pb2 import TestStreamPayload
from apache_beam.runners.interactive.cache_manager import SafeFastPrimitivesCoder
from apache_beam.runners.interactive.caching.streaming_cache import StreamingCache
from apache_beam.runners.interactive.testing.test_cache_manager import FileRecordsBuilder
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.test_stream import TestStream
from apache_beam.testing.util import *
from apache_beam.transforms.window import TimestampedValue

# Nose automatically detects tests if they match a regex. Here, it mistakens
# these protos as tests. For more info see the Nose docs at:
# https://nose.readthedocs.io/en/latest/writing_tests.html
TestStreamPayload.__test__ = False
TestStreamFileHeader.__test__ = False
TestStreamFileRecord.__test__ = False


class StreamingCacheTest(unittest.TestCase):
  def setUp(self):
    pass

  def test_exists(self):
    cache = StreamingCache(cache_dir=None)
    self.assertFalse(cache.exists('my_label'))
    cache.write([TestStreamFileRecord()], 'my_label')
    self.assertTrue(cache.exists('my_label'))

  def test_single_reader(self):
    """Tests that we expect to see all the correctly emitted TestStreamPayloads.
    """
    CACHED_PCOLLECTION_KEY = 'arbitrary_key'

    builder = FileRecordsBuilder(tag=CACHED_PCOLLECTION_KEY)
    (builder
     .add_element(
         element=0,
         event_time_secs=0,
         processing_time_secs=0)
     .add_element(
         element=1,
         event_time_secs=1,
         processing_time_secs=1)
     .add_element(
         element=2,
         event_time_secs=2,
         processing_time_secs=2))  # yapf: disable

    cache = StreamingCache(cache_dir=None)
    cache.write(builder.build(), CACHED_PCOLLECTION_KEY)

    reader, _ = cache.read(CACHED_PCOLLECTION_KEY)
    coder = coders.FastPrimitivesCoder()
    events = list(reader)

    # Units here are in microseconds.
    expected = [
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode(0), timestamp=0)
                ],
                tag=CACHED_PCOLLECTION_KEY)),
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=1 * 10**6)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode(1), timestamp=1 * 10**6)
                ],
                tag=CACHED_PCOLLECTION_KEY)),
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=1 * 10**6)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode(2), timestamp=2 * 10**6)
                ],
                tag=CACHED_PCOLLECTION_KEY)),
    ]
    self.assertSequenceEqual(events, expected)

  def test_multiple_readers(self):
    """Tests that the service advances the clock with multiple outputs.
    """

    CACHED_LETTERS = 'letters'
    CACHED_NUMBERS = 'numbers'
    CACHED_LATE = 'late'

    letters = FileRecordsBuilder(CACHED_LETTERS)
    (letters
     .advance_watermark(
         watermark_secs=0,
         processing_time_secs=1)
     .add_element(
         element='a',
         event_time_secs=0,
         processing_time_secs=1)
     .advance_watermark(
         watermark_secs=10,
         processing_time_secs=11)
     .add_element(
         element='b',
         event_time_secs=10,
         processing_time_secs=11))  # yapf: disable

    numbers = FileRecordsBuilder(CACHED_NUMBERS)
    (numbers
     .add_element(
         element=1,
         event_time_secs=0,
         processing_time_secs=2)
     .add_element(
         element=2,
         event_time_secs=0,
         processing_time_secs=3)
     .add_element(
         element=2,
         event_time_secs=0,
         processing_time_secs=4))  # yapf: disable

    late = FileRecordsBuilder(CACHED_LATE)
    late.add_element(
        element='late', event_time_secs=0, processing_time_secs=101)

    cache = StreamingCache(cache_dir=None)
    cache.write(letters.build(), CACHED_LETTERS)
    cache.write(numbers.build(), CACHED_NUMBERS)
    cache.write(late.build(), CACHED_LATE)

    reader = cache.read_multiple([[CACHED_LETTERS], [CACHED_NUMBERS],
                                  [CACHED_LATE]])
    coder = coders.FastPrimitivesCoder()
    events = list(reader)

    # Units here are in microseconds.
    expected = [
        # Advances clock from 0 to 1
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=1 * 10**6)),
        TestStreamPayload.Event(
            watermark_event=TestStreamPayload.Event.AdvanceWatermark(
                new_watermark=0, tag=CACHED_LETTERS)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('a'), timestamp=0)
                ],
                tag=CACHED_LETTERS)),

        # Advances clock from 1 to 2
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=1 * 10**6)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode(1), timestamp=0)
                ],
                tag=CACHED_NUMBERS)),

        # Advances clock from 2 to 3
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=1 * 10**6)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode(2), timestamp=0)
                ],
                tag=CACHED_NUMBERS)),

        # Advances clock from 3 to 4
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=1 * 10**6)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode(2), timestamp=0)
                ],
                tag=CACHED_NUMBERS)),

        # Advances clock from 4 to 11
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=7 * 10**6)),
        TestStreamPayload.Event(
            watermark_event=TestStreamPayload.Event.AdvanceWatermark(
                new_watermark=10 * 10**6, tag=CACHED_LETTERS)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('b'), timestamp=10 * 10**6)
                ],
                tag=CACHED_LETTERS)),

        # Advances clock from 11 to 101
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=90 * 10**6)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('late'), timestamp=0)
                ],
                tag=CACHED_LATE)),
    ]

    self.assertSequenceEqual(events, expected)

  def test_read_and_write(self):
    """An integration test between the Sink and Source.

    This ensures that the sink and source speak the same language in terms of
    coders, protos, order, and units.
    """

    # Units here are in seconds.
    test_stream = (TestStream()
        .advance_watermark_to(0, tag='records')
        .advance_processing_time(5)
        .add_elements(['a', 'b', 'c'], tag='records')
        .advance_watermark_to(10, tag='records')
        .advance_processing_time(1)
        .add_elements([TimestampedValue('1', 15),
                       TimestampedValue('2', 15),
                       TimestampedValue('3', 15)],
                      tag='records'))  # yapf: disable

    coder = SafeFastPrimitivesCoder()
    cache = StreamingCache(cache_dir=None, sample_resolution_sec=1.0)

    options = StandardOptions(streaming=True)
    with TestPipeline(options=options) as p:
      # pylint: disable=expression-not-assigned
      p | test_stream | cache.sink(['records'])

    reader, _ = cache.read('records')
    actual_events = list(reader)

    # Units here are in microseconds.
    expected_events = [
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=5 * 10**6)),
        TestStreamPayload.Event(
            watermark_event=TestStreamPayload.Event.AdvanceWatermark(
                new_watermark=0, tag='records')),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('a'), timestamp=0),
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('b'), timestamp=0),
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('c'), timestamp=0),
                ],
                tag='records')),
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=1 * 10**6)),
        TestStreamPayload.Event(
            watermark_event=TestStreamPayload.Event.AdvanceWatermark(
                new_watermark=10 * 10**6, tag='records')),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('1'), timestamp=15 *
                        10**6),
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('2'), timestamp=15 *
                        10**6),
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('3'), timestamp=15 *
                        10**6),
                ],
                tag='records')),
    ]
    self.assertEqual(actual_events, expected_events)

  def test_read_and_write_multiple_outputs(self):
    """An integration test between the Sink and Source with multiple outputs.

    This tests the funcionatlity that the StreamingCache reads from multiple
    files and combines them into a single sorted output.
    """
    LETTERS_TAG = 'letters'
    NUMBERS_TAG = 'numbers'

    # Units here are in seconds.
    test_stream = (TestStream()
        .advance_watermark_to(0, tag=LETTERS_TAG)
        .advance_processing_time(5)
        .add_elements(['a', 'b', 'c'], tag=LETTERS_TAG)
        .advance_watermark_to(10, tag=NUMBERS_TAG)
        .advance_processing_time(1)
        .add_elements([TimestampedValue('1', 15),
                       TimestampedValue('2', 15),
                       TimestampedValue('3', 15)],
                      tag=NUMBERS_TAG))  # yapf: disable

    cache = StreamingCache(cache_dir=None, sample_resolution_sec=1.0)

    coder = SafeFastPrimitivesCoder()

    options = StandardOptions(streaming=True)
    with TestPipeline(options=options) as p:
      # pylint: disable=expression-not-assigned
      events = p | test_stream
      events[LETTERS_TAG] | 'Letters sink' >> cache.sink([LETTERS_TAG])
      events[NUMBERS_TAG] | 'Numbers sink' >> cache.sink([NUMBERS_TAG])

    reader = cache.read_multiple([[LETTERS_TAG], [NUMBERS_TAG]])
    actual_events = list(reader)

    # Units here are in microseconds.
    expected_events = [
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=5 * 10**6)),
        TestStreamPayload.Event(
            watermark_event=TestStreamPayload.Event.AdvanceWatermark(
                new_watermark=0, tag=LETTERS_TAG)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('a'), timestamp=0),
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('b'), timestamp=0),
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('c'), timestamp=0),
                ],
                tag=LETTERS_TAG)),
        TestStreamPayload.Event(
            processing_time_event=TestStreamPayload.Event.AdvanceProcessingTime(
                advance_duration=1 * 10**6)),
        TestStreamPayload.Event(
            watermark_event=TestStreamPayload.Event.AdvanceWatermark(
                new_watermark=0, tag=LETTERS_TAG)),
        TestStreamPayload.Event(
            watermark_event=TestStreamPayload.Event.AdvanceWatermark(
                new_watermark=10 * 10**6, tag=NUMBERS_TAG)),
        TestStreamPayload.Event(
            element_event=TestStreamPayload.Event.AddElements(
                elements=[
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('1'), timestamp=15 *
                        10**6),
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('2'), timestamp=15 *
                        10**6),
                    TestStreamPayload.TimestampedElement(
                        encoded_element=coder.encode('3'), timestamp=15 *
                        10**6),
                ],
                tag=NUMBERS_TAG)),
    ]
    self.assertEqual(actual_events, expected_events)


if __name__ == '__main__':
  unittest.main()
