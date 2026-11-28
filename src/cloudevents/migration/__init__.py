#  Copyright 2018-Present The CloudEvents Authors
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Migration tooling for moving HTTP-ingested CloudEvents onto mixed-generation
Kafka subscribers (core SDK and the ``cloudevents.v1.kafka`` compatibility
layer).

The pre-publish check entry point :func:`check_http` evaluates the original
HTTP event against a receiver/link manifest and returns the minimum-cost
feasible wiring, complete with every per-hop Kafka message that can be replayed
through each receiver's original SDK entry.
"""

from cloudevents.migration.publish_check import (
    BINARY,
    COMPAT,
    CORE,
    STRUCTURED,
    CheckResult,
    DatacontenttypeRequirement,
    DataRequirement,
    ExtensionRequirement,
    HandoffConflict,
    HopRecord,
    Link,
    Node,
    Plan,
    Requirement,
    Topology,
    build_default_topology,
    check_event,
    check_http,
    replay_hop,
)

__all__ = [
    "BINARY",
    "COMPAT",
    "CORE",
    "STRUCTURED",
    "CheckResult",
    "DatacontenttypeRequirement",
    "DataRequirement",
    "ExtensionRequirement",
    "HandoffConflict",
    "HopRecord",
    "Link",
    "Node",
    "Plan",
    "Requirement",
    "Topology",
    "build_default_topology",
    "check_event",
    "check_http",
    "replay_hop",
]
