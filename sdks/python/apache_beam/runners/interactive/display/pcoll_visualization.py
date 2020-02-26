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

"""Module visualizes PCollection data.

For internal use only; no backwards-compatibility guarantees.
Only works with Python 3.5+.
"""
# pytype: skip-file

from __future__ import absolute_import

import base64
import datetime
import logging
import sys
from datetime import timedelta
from dateutil import tz

from apache_beam import pvalue
from apache_beam.portability.api.beam_runner_api_pb2 import TestStreamPayload
from apache_beam.runners.interactive import interactive_environment as ie
from apache_beam.runners.interactive import pipeline_instrument as instr
from apache_beam.runners.interactive.utils import pcoll_to_df
from apache_beam.transforms.window import GlobalWindow
from apache_beam.transforms.window import IntervalWindow
from apache_beam.utils.windowed_value import WindowedValue

try:
  from IPython import get_ipython  # pylint: disable=import-error
  from IPython.core.display import HTML  # pylint: disable=import-error
  from IPython.core.display import Javascript  # pylint: disable=import-error
  from IPython.core.display import display  # pylint: disable=import-error
  from IPython.core.display import display_javascript  # pylint: disable=import-error
  from IPython.core.display import update_display  # pylint: disable=import-error
  from facets_overview.generic_feature_statistics_generator import GenericFeatureStatisticsGenerator  # pylint: disable=import-error
  from timeloop import Timeloop  # pylint: disable=import-error

  if get_ipython():
    _pcoll_visualization_ready = True
  else:
    _pcoll_visualization_ready = False
except ImportError:
  _pcoll_visualization_ready = False

_LOGGER = logging.getLogger(__name__)

# 1-d types that need additional normalization to be compatible with DataFrame.
_one_dimension_types = (int, float, str, bool, list, tuple)

_CSS = """
            <style>
              .p-Widget.jp-OutputPrompt.jp-OutputArea-prompt:empty {{
                padding: 0;
                border: 0;
              }}
              .p-Widget.jp-RenderedJavaScript.jp-mod-trusted.jp-OutputArea-output:empty {{
                padding: 0;
                border: 0;
              }}
            </style>"""
_DIVE_SCRIPT_TEMPLATE = """
            try {{
              document.querySelector("#{display_id}").data = {jsonstr};
            }} catch (e) {{
              console.log("#{display_id} is not rendered yet.")
            }}"""
_DIVE_HTML_TEMPLATE = _CSS + """
            <script src="https://cdnjs.cloudflare.com/ajax/libs/webcomponentsjs/1.3.3/webcomponents-lite.js"></script>
            <link rel="import" href="https://raw.githubusercontent.com/PAIR-code/facets/1.0.0/facets-dist/facets-jupyter.html">
            <facets-dive sprite-image-width="{sprite_size}" sprite-image-height="{sprite_size}" id="{display_id}" height="600"></facets-dive>
            <script>
              document.querySelector("#{display_id}").data = {jsonstr};
            </script>"""
_OVERVIEW_SCRIPT_TEMPLATE = """
              try {{
                document.querySelector("#{display_id}").protoInput = "{protostr}";
              }} catch (e) {{
                console.log("#{display_id} is not rendered yet.")
              }}"""
_OVERVIEW_HTML_TEMPLATE = _CSS + """
            <script src="https://cdnjs.cloudflare.com/ajax/libs/webcomponentsjs/1.3.3/webcomponents-lite.js"></script>
            <link rel="import" href="https://raw.githubusercontent.com/PAIR-code/facets/1.0.0/facets-dist/facets-jupyter.html">
            <facets-overview id="{display_id}"></facets-overview>
            <script>
              document.querySelector("#{display_id}").protoInput = "{protostr}";
            </script>"""
_DATAFRAME_PAGINATION_TEMPLATE = _CSS + """
            <link rel="stylesheet" href="https://cdn.datatables.net/1.10.20/css/jquery.dataTables.min.css">
            {dataframe_html}
            <script>
              function dataframe_as_datatable() {{
                var scripts_to_load = [];
                if (typeof jQuery == "undefined") {{
                  scripts_to_load.push("https://ajax.googleapis.com/ajax/libs/jquery/2.2.4/jquery.min.js");
                  scripts_to_load.push("https://cdn.datatables.net/1.10.20/js/jquery.dataTables.min.js");
                }} else if (typeof jQuery.fn.DataTable == "undefined") {{
                  scripts_to_load.push("https://cdn.datatables.net/1.10.20/js/jquery.dataTables.min.js");
                }}
                scripts_to_load.forEach(function(src) {{
                  var script = document.createElement('script');
                  script.src = src;
                  document.head.appendChild(script)
                }});
                if (scripts_to_load.length == 0) {{
                  jQuery(document).ready(function($) {{
                    $("#{table_id}").DataTable({{
                      columnDefs: [
                        {{
                          targets: "_all",
                          className: "dt-left"
                        }},
                        {{
                          "targets": 0,
                          "width": "10px",
                          "title": ""
                        }}
                      ]
                    }});
                  }});
                }}
              }}
              dataframe_as_datatable();
            </script>"""


def visualize(
    pcoll,
    dynamic_plotting_interval=None,
    include_window_info=False,
    display_facets=False):
  """Visualizes the data of a given PCollection. Optionally enables dynamic
  plotting with interval in seconds if the PCollection is being produced by a
  running pipeline or the pipeline is streaming indefinitely. The function
  always returns immediately and is asynchronous when dynamic plotting is on.

  If dynamic plotting enabled, the visualization is updated continuously until
  the pipeline producing the PCollection is in an end state. The visualization
  would be anchored to the notebook cell output area. The function
  asynchronously returns a handle to the visualization job immediately. The user
  could manually do::

    # In one notebook cell, enable dynamic plotting every 1 second:
    handle = visualize(pcoll, dynamic_plotting_interval=1)
    # Visualization anchored to the cell's output area.
    # In a different cell:
    handle.stop()
    # Will stop the dynamic plotting of the above visualization manually.
    # Otherwise, dynamic plotting ends when pipeline is not running anymore.

  If dynamic_plotting is not enabled (by default), None is returned.

  If include_window_info is True, the data will include window information,
  which consists of the event timestamps, windows, and pane info.

  If display_facets is True, the facets widgets will be rendered. Otherwise, the
  facets widgets will not be rendered.

  The function is experimental. For internal use only; no
  backwards-compatibility guarantees.
  """
  if not _pcoll_visualization_ready:
    return None
  pv = PCollectionVisualization(
      pcoll,
      include_window_info=include_window_info,
      display_facets=display_facets)
  if ie.current_env().is_in_notebook:
    pv.display()
  else:
    pv.display_plain_text()
    # We don't want to do dynamic plotting if there is no notebook frontend.
    return None

  if dynamic_plotting_interval:
    # Disables the verbose logging from timeloop.
    logging.getLogger('timeloop').disabled = True
    tl = Timeloop()

    def dynamic_plotting(pcoll, pv, tl, include_window_info, display_facets):
      @tl.job(interval=timedelta(seconds=dynamic_plotting_interval))
      def continuous_update_display():  # pylint: disable=unused-variable
        # Always creates a new PCollVisualization instance when the
        # PCollection materialization is being updated and dynamic
        # plotting is in-process.
        # PCollectionVisualization created at this level doesn't need dynamic
        # plotting interval information when instantiated because it's already
        # in dynamic plotting logic.
        updated_pv = PCollectionVisualization(
            pcoll,
            include_window_info=include_window_info,
            display_facets=display_facets)
        updated_pv.display(updating_pv=pv)
        if ie.current_env().is_terminated(pcoll.pipeline):
          try:
            tl.stop()
          except RuntimeError:
            # The job can only be stopped once. Ignore excessive stops.
            pass

      tl.start()
      return tl

    return dynamic_plotting(pcoll, pv, tl, include_window_info, display_facets)
  return None


class PCollectionVisualization(object):
  """A visualization of a PCollection.

  The class relies on creating a PipelineInstrument w/o actual instrument to
  access current interactive environment for materialized PCollection data at
  the moment of self instantiation through cache.
  """
  def __init__(self, pcoll, include_window_info=False, display_facets=False):
    assert _pcoll_visualization_ready, (
        'Dependencies for PCollection visualization are not available. Please '
        'use `pip install apache-beam[interactive]` to install necessary '
        'dependencies and make sure that you are executing code in an '
        'interactive environment such as a Jupyter notebook.')
    assert isinstance(
        pcoll,
        pvalue.PCollection), ('pcoll should be apache_beam.pvalue.PCollection')
    self._pcoll = pcoll
    # This allows us to access cache key and other meta data about the pipeline
    # whether it's the pipeline defined in user code or a copy of that pipeline.
    # Thus, this module doesn't need any other user input but the PCollection
    # variable to be visualized. It then automatically figures out the pipeline
    # definition, materialized data and the pipeline result for the execution
    # even if the user never assigned or waited the result explicitly.
    # With only the constructor of PipelineInstrument, any interactivity related
    # pre-process or instrument is not triggered for performance concerns.
    self._pin = instr.PipelineInstrument(pcoll.pipeline)
    self._cache_key = self._pin.cache_key(self._pcoll)
    self._dive_display_id = 'facets_dive_{}_{}'.format(
        self._cache_key, id(self))
    self._overview_display_id = 'facets_overview_{}_{}'.format(
        self._cache_key, id(self))
    self._df_display_id = 'df_{}_{}'.format(self._cache_key, id(self))
    # Whether the visualization should include window info.
    self._include_window_info = include_window_info
    # Whether facets widgets should be displayed.
    self._display_facets = display_facets

    pcoll_id = self._pin.pcolls_to_pcoll_id[str(pcoll)]
    self._pcoll_var = self._pin.cacheable_var_by_pcoll_id(pcoll_id)

  def display_plain_text(self):
    """Displays a head sample of the normalized PCollection data.

    This function is used when the ipython kernel is not connected to a
    notebook frontend such as when running ipython in terminal or in unit tests.
    It's a visualization in terminal-like UI, not a function to retrieve data
    for programmatically usages.
    """
    # Double check if the dependency is ready in case someone mistakenly uses
    # the function.
    if _pcoll_visualization_ready:
      elements = _to_element_list(self._cache_key)
      data = pcoll_to_df(
          elements,
          self._pcoll.element_type,
          prefix=self._pcoll_var,
          include_window_info=self._include_window_info)
      # Displays a data-table with at most 25 entries from the head.
      data_sample = data.head(25)
      display(data_sample)

  def display(self, updating_pv=None):
    """Displays the visualization through IPython.

    Args:
      updating_pv: A PCollectionVisualization object. When provided, the
        display_id of each visualization part will inherit from the initial
        display of updating_pv and only update that visualization web element
        instead of creating new ones.

    The visualization has 3 parts: facets-dive, facets-overview and paginated
    data table. Each part is assigned an auto-generated unique display id
    (the uniqueness is guaranteed throughout the lifespan of the PCollection
    variable).
    """
    elements = _to_element_list(self._cache_key)
    data = pcoll_to_df(
        elements,
        self._pcoll.element_type,
        prefix=self._pcoll_var,
        include_window_info=self._include_window_info)
    # String-ify the dictionaries for display because elements of type dict
    # cannot be ordered.
    data = data.applymap(lambda x: str(x) if isinstance(x, dict) else x)
    if updating_pv:
      # Only updates when data is not empty. Otherwise, consider it a bad
      # iteration and noop since there is nothing to be updated.
      if data.empty:
        _LOGGER.debug('Skip a visualization update due to empty data.')
      else:
        self._display_dataframe(data, updating_pv._df_display_id)
        if self._display_facets:
          self._display_dive(data, updating_pv._dive_display_id)
          self._display_overview(data, updating_pv._overview_display_id)
    else:
      self._display_dataframe(data)
      if self._display_facets:
        self._display_dive(data)
        self._display_overview(data)

  def _display_dive(self, data, update=None):
    sprite_size = 32 if len(data.index) > 50000 else 64

    jsonstr = data.to_json(orient='records', default_handler=str)
    if update:
      script = _DIVE_SCRIPT_TEMPLATE.format(display_id=update, jsonstr=jsonstr)
      display_javascript(Javascript(script))
    else:
      html = _DIVE_HTML_TEMPLATE.format(
          display_id=self._dive_display_id,
          jsonstr=jsonstr,
          sprite_size=sprite_size)
      display(HTML(html))

  def _display_overview(self, data, update=None):
    if not data.empty and self._include_window_info:
      data = data.drop(['event_time', 'windows', 'pane_info'], axis=1)

    gfsg = GenericFeatureStatisticsGenerator()
    proto = gfsg.ProtoFromDataFrames([{'name': 'data', 'table': data}])
    protostr = base64.b64encode(proto.SerializeToString()).decode('utf-8')
    if update:
      script = _OVERVIEW_SCRIPT_TEMPLATE.format(
          display_id=update, protostr=protostr)
      display_javascript(Javascript(script))
    else:
      html = _OVERVIEW_HTML_TEMPLATE.format(
          display_id=self._overview_display_id, protostr=protostr)
      display(HTML(html))

  def _display_dataframe(self, data, update=None):

    def event_time_formatter(event_time_us):
      options = ie.current_env().options
      to_tz = options.display_timezone
      return (datetime.datetime.utcfromtimestamp(event_time_us / 1000000)
              .replace(tzinfo=tz.tzutc())
              .astimezone(to_tz)
              .strftime(options.display_timestamp_format))

    def windows_formatter(windows):
      result = []
      for w in windows:
        if isinstance(w, GlobalWindow):
          result.append(str(w))
        elif isinstance(w, IntervalWindow):
          # First get the duration in terms of hours, minutes, seconds, and
          # micros.
          duration = w.end.micros - w.start.micros
          duration_secs = duration // 1000000
          hours, remainder = divmod(duration_secs, 3600)
          minutes, seconds = divmod(remainder, 60)
          micros = (duration - duration_secs * 1000000) % 1000000

          # Construct the duration string. Try and write the string in such a
          # way that minimizes the amount of characters written.
          duration = ''
          if hours:
            duration += '{}h '.format(hours)

          if minutes or (hours and seconds):
            duration += '{}m '.format(minutes)

          if seconds:
            if micros:
              duration += '{}.{:06}s'.format(seconds, micros)
            else:
              duration += '{}s'.format(seconds)

          options = ie.current_env().options
          to_tz = options.display_timezone
          start = event_time_formatter(w.start.micros)

          result.append('{} ({})'.format(start, duration))

      return ','.join(result)

    def pane_info_formatter(pane_info):
      from apache_beam.utils.windowed_value import PaneInfo
      from apache_beam.utils.windowed_value import PaneInfoTiming
      assert isinstance(pane_info, PaneInfo)

      result = 'Pane {}'.format(pane_info.index)
      timing_info = '{}{}'.format(
          'Final ' if pane_info.is_last else '',
          PaneInfoTiming.to_string(pane_info.timing).lower().capitalize()
          if pane_info.timing in (PaneInfoTiming.EARLY, PaneInfoTiming.LATE)
          else '')

      if timing_info:
        result += ': ' + timing_info

      return result


    table_id = 'table_{}'.format(update if update else self._df_display_id)
    html = _DATAFRAME_PAGINATION_TEMPLATE.format(
        dataframe_html=data.to_html(notebook=True, table_id=table_id,
        formatters={
            'event_time': event_time_formatter,
            'windows': windows_formatter,
            'pane_info': pane_info_formatter
        }),
        table_id=table_id)
    if update:
      update_display(HTML(html), display_id=update)
    else:
      display(HTML(html), display_id=self._df_display_id)


def _to_element_list(cache_key):
  pcoll_list = iter([])
  try:
    if ie.current_env().cache_manager().exists('full', cache_key):
      pcoll_list, _ = ie.current_env().cache_manager().read('full', cache_key)
  # pylint: disable=bare-except
  except:
    _LOGGER.debug(sys.exc_info())
    # If the read errors out for some reason, be resilient to it and return
    # empty data.
    pcoll_list = iter([])

  output = []
  while True:
    # pylint: disable=bare-except
    try:
      el = next(pcoll_list)
      if isinstance(el, TestStreamPayload.Event):
        if (el.HasField('watermark_event') or
            el.HasField('processing_time_event')):
          continue

        cache = ie.current_env().cache_manager()
        for tv in el.element_event.elements:
          coder = cache.load_pcoder('full', cache_key)
          val = coder.decode(tv.encoded_element)
          if not isinstance(val, WindowedValue):
            val = WindowedValue(**val)
          output.append(val)
      else:
        if not isinstance(el, WindowedValue):
          el = WindowedValue(**el)
        output.append(el)

    except StopIteration:
      break

    except:
      _LOGGER.debug(sys.exc_info())

  return output
