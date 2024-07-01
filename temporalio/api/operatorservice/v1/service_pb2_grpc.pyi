"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
The MIT License

Copyright (c) 2020 Temporal Technologies Inc.  All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import abc

import grpc

import temporalio.api.operatorservice.v1.request_response_pb2

class OperatorServiceStub:
    """OperatorService API defines how Temporal SDKs and other clients interact with the Temporal server
    to perform administrative functions like registering a search attribute or a namespace.
    APIs in this file could be not compatible with Temporal Cloud, hence it's usage in SDKs should be limited by
    designated APIs that clearly state that they shouldn't be used by the main Application (Workflows & Activities) framework.
    (-- Search Attribute --)
    """

    def __init__(self, channel: grpc.Channel) -> None: ...
    AddSearchAttributes: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.AddSearchAttributesRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.AddSearchAttributesResponse,
    ]
    """AddSearchAttributes add custom search attributes.

    Returns ALREADY_EXISTS status code if a Search Attribute with any of the specified names already exists
    Returns INTERNAL status code with temporalio.api.errordetails.v1.SystemWorkflowFailure in Error Details if registration process fails,
    """
    RemoveSearchAttributes: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.RemoveSearchAttributesRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.RemoveSearchAttributesResponse,
    ]
    """RemoveSearchAttributes removes custom search attributes.

    Returns NOT_FOUND status code if a Search Attribute with any of the specified names is not registered
    """
    ListSearchAttributes: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.ListSearchAttributesRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.ListSearchAttributesResponse,
    ]
    """ListSearchAttributes returns comprehensive information about search attributes."""
    DeleteNamespace: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.DeleteNamespaceRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.DeleteNamespaceResponse,
    ]
    """DeleteNamespace synchronously deletes a namespace and asynchronously reclaims all namespace resources."""
    AddOrUpdateRemoteCluster: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.AddOrUpdateRemoteClusterRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.AddOrUpdateRemoteClusterResponse,
    ]
    """AddOrUpdateRemoteCluster adds or updates remote cluster."""
    RemoveRemoteCluster: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.RemoveRemoteClusterRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.RemoveRemoteClusterResponse,
    ]
    """RemoveRemoteCluster removes remote cluster."""
    ListClusters: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.ListClustersRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.ListClustersResponse,
    ]
    """ListClusters returns information about Temporal clusters."""
    GetNexusEndpoint: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.GetNexusEndpointRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.GetNexusEndpointResponse,
    ]
    """Get a registered Nexus endpoint by ID. The returned version can be used for optimistic updates."""
    CreateNexusEndpoint: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.CreateNexusEndpointRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.CreateNexusEndpointResponse,
    ]
    """Create a Nexus endpoint. This will fail if an endpoint with the same name is already registered with a status of
    ALREADY_EXISTS.
    Returns the created endpoint with its initial version. You may use this version for subsequent updates.
    """
    UpdateNexusEndpoint: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.UpdateNexusEndpointRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.UpdateNexusEndpointResponse,
    ]
    """Optimistically update a Nexus endpoint based on provided version as obtained via the `GetNexusEndpoint` or
    `ListNexusEndpointResponse` APIs. This will fail with a status of FAILED_PRECONDITION if the version does not
    match.
    Returns the updated endpoint with its updated version. You may use this version for subsequent updates. You don't
    need to increment the version yourself. The server will increment the version for you after each update.
    """
    DeleteNexusEndpoint: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.DeleteNexusEndpointRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.DeleteNexusEndpointResponse,
    ]
    """Delete an incoming Nexus service by ID."""
    ListNexusEndpoints: grpc.UnaryUnaryMultiCallable[
        temporalio.api.operatorservice.v1.request_response_pb2.ListNexusEndpointsRequest,
        temporalio.api.operatorservice.v1.request_response_pb2.ListNexusEndpointsResponse,
    ]
    """List all Nexus endpoints for the cluster, sorted by ID in ascending order. Set page_token in the request to the
    next_page_token field of the previous response to get the next page of results. An empty next_page_token
    indicates that there are no more results. During pagination, a newly added service with an ID lexicographically
    earlier than the previous page's last endpoint's ID may be missed.
    """

class OperatorServiceServicer(metaclass=abc.ABCMeta):
    """OperatorService API defines how Temporal SDKs and other clients interact with the Temporal server
    to perform administrative functions like registering a search attribute or a namespace.
    APIs in this file could be not compatible with Temporal Cloud, hence it's usage in SDKs should be limited by
    designated APIs that clearly state that they shouldn't be used by the main Application (Workflows & Activities) framework.
    (-- Search Attribute --)
    """

    @abc.abstractmethod
    def AddSearchAttributes(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.AddSearchAttributesRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.AddSearchAttributesResponse:
        """AddSearchAttributes add custom search attributes.

        Returns ALREADY_EXISTS status code if a Search Attribute with any of the specified names already exists
        Returns INTERNAL status code with temporalio.api.errordetails.v1.SystemWorkflowFailure in Error Details if registration process fails,
        """
    @abc.abstractmethod
    def RemoveSearchAttributes(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.RemoveSearchAttributesRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.RemoveSearchAttributesResponse:
        """RemoveSearchAttributes removes custom search attributes.

        Returns NOT_FOUND status code if a Search Attribute with any of the specified names is not registered
        """
    @abc.abstractmethod
    def ListSearchAttributes(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.ListSearchAttributesRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.ListSearchAttributesResponse:
        """ListSearchAttributes returns comprehensive information about search attributes."""
    @abc.abstractmethod
    def DeleteNamespace(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.DeleteNamespaceRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.DeleteNamespaceResponse:
        """DeleteNamespace synchronously deletes a namespace and asynchronously reclaims all namespace resources."""
    @abc.abstractmethod
    def AddOrUpdateRemoteCluster(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.AddOrUpdateRemoteClusterRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.AddOrUpdateRemoteClusterResponse:
        """AddOrUpdateRemoteCluster adds or updates remote cluster."""
    @abc.abstractmethod
    def RemoveRemoteCluster(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.RemoveRemoteClusterRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.RemoveRemoteClusterResponse:
        """RemoveRemoteCluster removes remote cluster."""
    @abc.abstractmethod
    def ListClusters(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.ListClustersRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.ListClustersResponse:
        """ListClusters returns information about Temporal clusters."""
    @abc.abstractmethod
    def GetNexusEndpoint(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.GetNexusEndpointRequest,
        context: grpc.ServicerContext,
    ) -> (
        temporalio.api.operatorservice.v1.request_response_pb2.GetNexusEndpointResponse
    ):
        """Get a registered Nexus endpoint by ID. The returned version can be used for optimistic updates."""
    @abc.abstractmethod
    def CreateNexusEndpoint(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.CreateNexusEndpointRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.CreateNexusEndpointResponse:
        """Create a Nexus endpoint. This will fail if an endpoint with the same name is already registered with a status of
        ALREADY_EXISTS.
        Returns the created endpoint with its initial version. You may use this version for subsequent updates.
        """
    @abc.abstractmethod
    def UpdateNexusEndpoint(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.UpdateNexusEndpointRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.UpdateNexusEndpointResponse:
        """Optimistically update a Nexus endpoint based on provided version as obtained via the `GetNexusEndpoint` or
        `ListNexusEndpointResponse` APIs. This will fail with a status of FAILED_PRECONDITION if the version does not
        match.
        Returns the updated endpoint with its updated version. You may use this version for subsequent updates. You don't
        need to increment the version yourself. The server will increment the version for you after each update.
        """
    @abc.abstractmethod
    def DeleteNexusEndpoint(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.DeleteNexusEndpointRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.DeleteNexusEndpointResponse:
        """Delete an incoming Nexus service by ID."""
    @abc.abstractmethod
    def ListNexusEndpoints(
        self,
        request: temporalio.api.operatorservice.v1.request_response_pb2.ListNexusEndpointsRequest,
        context: grpc.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.request_response_pb2.ListNexusEndpointsResponse:
        """List all Nexus endpoints for the cluster, sorted by ID in ascending order. Set page_token in the request to the
        next_page_token field of the previous response to get the next page of results. An empty next_page_token
        indicates that there are no more results. During pagination, a newly added service with an ID lexicographically
        earlier than the previous page's last endpoint's ID may be missed.
        """

def add_OperatorServiceServicer_to_server(
    servicer: OperatorServiceServicer, server: grpc.Server
) -> None: ...
