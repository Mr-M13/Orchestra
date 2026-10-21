"""Provider-independent Orchestra contracts.

This package deliberately contains no execution runtime, network service or
credential handling.  It describes agents, tasks, grants, reports and
verification so future execution authorities can share one vocabulary without
coupling the core model to a particular model provider or coding harness.
"""

from orchestra.contracts import (
    AgentManifest,
    CapabilityClaim,
    Concurrency,
    CostCharacteristics,
    CredentialReference,
    ExecutionGrant,
    ExecutionReport,
    FileScope,
    IsolationRequirements,
    NetworkPolicy,
    ResourceLimits,
    TaskSpec,
    ToolRequirement,
    VerificationRecord,
)

__all__ = [
    "AgentManifest",
    "CapabilityClaim",
    "Concurrency",
    "CostCharacteristics",
    "CredentialReference",
    "ExecutionGrant",
    "ExecutionReport",
    "FileScope",
    "IsolationRequirements",
    "NetworkPolicy",
    "ResourceLimits",
    "TaskSpec",
    "ToolRequirement",
    "VerificationRecord",
]

__version__ = "0.1.0"
