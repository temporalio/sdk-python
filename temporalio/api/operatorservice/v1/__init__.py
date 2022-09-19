from .request_response_pb2 import (
    AddOrUpdateRemoteClusterRequest,
    AddOrUpdateRemoteClusterResponse,
    AddSearchAttributesRequest,
    AddSearchAttributesResponse,
    DeleteNamespaceRequest,
    DeleteNamespaceResponse,
    DeleteWorkflowExecutionRequest,
    DeleteWorkflowExecutionResponse,
    DescribeClusterRequest,
    DescribeClusterResponse,
    ListClusterMembersRequest,
    ListClusterMembersResponse,
    ListClustersRequest,
    ListClustersResponse,
    ListSearchAttributesRequest,
    ListSearchAttributesResponse,
    RemoveRemoteClusterRequest,
    RemoveRemoteClusterResponse,
    RemoveSearchAttributesRequest,
    RemoveSearchAttributesResponse,
)

__all__ = [
    "AddOrUpdateRemoteClusterRequest",
    "AddOrUpdateRemoteClusterResponse",
    "AddSearchAttributesRequest",
    "AddSearchAttributesResponse",
    "DeleteNamespaceRequest",
    "DeleteNamespaceResponse",
    "DeleteWorkflowExecutionRequest",
    "DeleteWorkflowExecutionResponse",
    "DescribeClusterRequest",
    "DescribeClusterResponse",
    "ListClusterMembersRequest",
    "ListClusterMembersResponse",
    "ListClustersRequest",
    "ListClustersResponse",
    "ListSearchAttributesRequest",
    "ListSearchAttributesResponse",
    "RemoveRemoteClusterRequest",
    "RemoveRemoteClusterResponse",
    "RemoveSearchAttributesRequest",
    "RemoveSearchAttributesResponse",
]

# gRPC is optional
try:
    import grpc

    from .service_pb2_grpc import (
        OperatorServiceServicer,
        OperatorServiceStub,
        add_OperatorServiceServicer_to_server,
    )

    __all__.extend(
        [
            "OperatorServiceServicer",
            "OperatorServiceStub",
            "add_OperatorServiceServicer_to_server",
        ]
    )
except ImportError:
    pass
