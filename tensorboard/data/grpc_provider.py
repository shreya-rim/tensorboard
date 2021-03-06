# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""A data provider that talks to a gRPC server."""

import contextlib

import grpc

from tensorboard.util import timing
from tensorboard import errors
from tensorboard.data import provider
from tensorboard.data.proto import data_provider_pb2
from tensorboard.data.proto import data_provider_pb2_grpc


def make_stub(channel):
    """Wraps a gRPC channel with a service stub."""
    return data_provider_pb2_grpc.TensorBoardDataProviderStub(channel)


class GrpcDataProvider(provider.DataProvider):
    """Data provider that talks over gRPC."""

    def __init__(self, addr, stub):
        """Initializes a GrpcDataProvider.

        Args:
          addr: String address of the remote peer. Used cosmetically for
            data location.
          stub: `data_provider_pb2_grpc.TensorBoardDataProviderStub`
            value. See `make_stub` to construct one from a channel.
        """
        self._addr = addr
        self._stub = stub

    def data_location(self, ctx, *, experiment_id):
        return "grpc://%s" % (self._addr,)

    def list_plugins(self, ctx, *, experiment_id):
        req = data_provider_pb2.ListPluginsRequest()
        req.experiment_id = experiment_id
        with _translate_grpc_error():
            res = self._stub.ListPlugins(req)
        return [p.name for p in res.plugins]

    def list_runs(self, ctx, *, experiment_id):
        req = data_provider_pb2.ListRunsRequest()
        req.experiment_id = experiment_id
        with _translate_grpc_error():
            res = self._stub.ListRuns(req)
        return [
            provider.Run(
                run_id=run.name,
                run_name=run.name,
                start_time=run.start_time,
            )
            for run in res.runs
        ]

    @timing.log_latency
    def list_scalars(
        self, ctx, *, experiment_id, plugin_name, run_tag_filter=None
    ):
        with timing.log_latency("build request"):
            req = data_provider_pb2.ListScalarsRequest()
            req.experiment_id = experiment_id
            req.plugin_filter.plugin_name = plugin_name
            _populate_rtf(run_tag_filter, req.run_tag_filter)
        with timing.log_latency("_stub.ListScalars"):
            with _translate_grpc_error():
                res = self._stub.ListScalars(req)
        with timing.log_latency("build result"):
            result = {}
            for run_entry in res.runs:
                tags = {}
                result[run_entry.run_name] = tags
                for tag_entry in run_entry.tags:
                    ts = tag_entry.metadata
                    tags[tag_entry.tag_name] = provider.ScalarTimeSeries(
                        max_step=ts.max_step,
                        max_wall_time=ts.max_wall_time,
                        plugin_content=ts.summary_metadata.plugin_data.content,
                        description=ts.summary_metadata.summary_description,
                        display_name=ts.summary_metadata.display_name,
                    )
            return result

    @timing.log_latency
    def read_scalars(
        self,
        ctx,
        *,
        experiment_id,
        plugin_name,
        downsample=None,
        run_tag_filter=None,
    ):
        with timing.log_latency("build request"):
            req = data_provider_pb2.ReadScalarsRequest()
            req.experiment_id = experiment_id
            req.plugin_filter.plugin_name = plugin_name
            _populate_rtf(run_tag_filter, req.run_tag_filter)
            req.downsample.num_points = downsample
        with timing.log_latency("_stub.ReadScalars"):
            with _translate_grpc_error():
                res = self._stub.ReadScalars(req)
        with timing.log_latency("build result"):
            result = {}
            for run_entry in res.runs:
                tags = {}
                result[run_entry.run_name] = tags
                for tag_entry in run_entry.tags:
                    series = []
                    tags[tag_entry.tag_name] = series
                    d = tag_entry.data
                    for (step, wt, value) in zip(d.step, d.wall_time, d.value):
                        pt = provider.ScalarDatum(
                            step=step,
                            wall_time=wt,
                            value=value,
                        )
                        series.append(pt)
            return result


@contextlib.contextmanager
def _translate_grpc_error():
    try:
        yield
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.INVALID_ARGUMENT:
            raise errors.InvalidArgumentError(e.details())
        if e.code() == grpc.StatusCode.NOT_FOUND:
            raise errors.NotFoundError(e.details())
        if e.code() == grpc.StatusCode.PERMISSION_DENIED:
            raise errors.PermissionDeniedError(e.details())
        raise


def _populate_rtf(run_tag_filter, rtf_proto):
    """Copies `run_tag_filter` into `rtf_proto`."""
    if run_tag_filter is None:
        return
    if run_tag_filter.runs is not None:
        rtf_proto.runs.names[:] = sorted(run_tag_filter.runs)
    if run_tag_filter.tags is not None:
        rtf_proto.tags.names[:] = sorted(run_tag_filter.tags)
