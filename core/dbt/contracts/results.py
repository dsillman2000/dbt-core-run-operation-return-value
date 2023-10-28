import threading

from dbt.contracts.graph.unparsed import FreshnessThreshold
from dbt.contracts.graph.nodes import CompiledNode, SourceDefinition, ResultNode
from dbt.contracts.util import (
    BaseArtifactMetadata,
    ArtifactMixin,
    VersionedSchema,
    Replaceable,
    schema_version,
)
from dbt.exceptions import DbtInternalError
from dbt.events.functions import fire_event
from dbt.events.types import TimingInfoCollected
from dbt.events.contextvars import get_node_info
from dbt.events.helpers import datetime_to_json_string
from dbt.logger import TimingProcessor
from dbt.utils import lowercase, cast_to_str, cast_to_int
from dbt.dataclass_schema import dbtClassMixin, StrEnum

import agate

from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Union,
)

from dbt.clients.system import write_json


@dataclass
class TimingInfo(dbtClassMixin):
    name: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def begin(self):
        self.started_at = datetime.utcnow()

    def end(self):
        self.completed_at = datetime.utcnow()

    def to_msg_dict(self):
        msg_dict = {"name": self.name}
        if self.started_at:
            msg_dict["started_at"] = datetime_to_json_string(self.started_at)
        if self.completed_at:
            msg_dict["completed_at"] = datetime_to_json_string(self.completed_at)
        return msg_dict


# This is a context manager
class collect_timing_info:
    def __init__(self, name: str, callback: Callable[[TimingInfo], None]) -> None:
        self.timing_info = TimingInfo(name=name)
        self.callback = callback

    def __enter__(self):
        self.timing_info.begin()

    def __exit__(self, exc_type, exc_value, traceback):
        self.timing_info.end()
        self.callback(self.timing_info)
        # Note: when legacy logger is removed, we can remove the following line
        with TimingProcessor(self.timing_info):
            fire_event(
                TimingInfoCollected(
                    timing_info=self.timing_info.to_msg_dict(), node_info=get_node_info()
                )
            )


class RunningStatus(StrEnum):
    Started = "started"
    Compiling = "compiling"
    Executing = "executing"


class NodeStatus(StrEnum):
    Success = "success"
    Error = "error"
    Fail = "fail"
    Warn = "warn"
    Skipped = "skipped"
    Pass = "pass"
    RuntimeErr = "runtime error"


class RunStatus(StrEnum):
    Success = NodeStatus.Success
    Error = NodeStatus.Error
    Skipped = NodeStatus.Skipped


class TestStatus(StrEnum):
    __test__ = False
    Pass = NodeStatus.Pass
    Error = NodeStatus.Error
    Fail = NodeStatus.Fail
    Warn = NodeStatus.Warn
    Skipped = NodeStatus.Skipped


class FreshnessStatus(StrEnum):
    Pass = NodeStatus.Pass
    Warn = NodeStatus.Warn
    Error = NodeStatus.Error
    RuntimeErr = NodeStatus.RuntimeErr


@dataclass
class BaseResult(dbtClassMixin):
    status: Union[RunStatus, TestStatus, FreshnessStatus]
    timing: List[TimingInfo]
    thread_id: str
    execution_time: float
    adapter_response: Dict[str, Any]
    message: Optional[str]
    failures: Optional[int]
    return_value: Optional[Any]

    @classmethod
    def __pre_deserialize__(cls, data):
        data = super().__pre_deserialize__(data)
        if "message" not in data:
            data["message"] = None
        if "failures" not in data:
            data["failures"] = None
        if "return_value" not in data:
            data["return_value"] = None
        return data

    def to_msg_dict(self):
        msg_dict = {
            "status": str(self.status),
            "message": cast_to_str(self.message),
            "thread": self.thread_id,
            "execution_time": self.execution_time,
            "num_failures": cast_to_int(self.failures),
            "timing_info": [ti.to_msg_dict() for ti in self.timing],
            "adapter_response": self.adapter_response,
            "return_value": self.return_value,
        }
        return msg_dict


@dataclass
class NodeResult(BaseResult):
    node: ResultNode


@dataclass
class RunResult(NodeResult):
    agate_table: Optional[agate.Table] = field(
        default=None, metadata={"serialize": lambda x: None, "deserialize": lambda x: None}
    )

    @property
    def skipped(self):
        return self.status == RunStatus.Skipped

    @classmethod
    def from_node(cls, node: ResultNode, status: RunStatus, message: Optional[str]):
        thread_id = threading.current_thread().name
        return RunResult(
            status=status,
            thread_id=thread_id,
            execution_time=0,
            timing=[],
            message=message,
            node=node,
            adapter_response={},
            failures=None,
            return_value=None,
        )


@dataclass
class ExecutionResult(dbtClassMixin):
    results: Sequence[BaseResult]
    elapsed_time: float

    def __len__(self):
        return len(self.results)

    def __iter__(self):
        return iter(self.results)

    def __getitem__(self, idx):
        return self.results[idx]


@dataclass
class RunResultsMetadata(BaseArtifactMetadata):
    dbt_schema_version: str = field(
        default_factory=lambda: str(RunResultsArtifact.dbt_schema_version)
    )


@dataclass
class RunResultOutput(BaseResult):
    unique_id: str
    compiled: Optional[bool]
    compiled_code: Optional[str]
    relation_name: Optional[str]


def process_run_result(result: RunResult) -> RunResultOutput:

    compiled = isinstance(result.node, CompiledNode)

    return RunResultOutput(
        unique_id=result.node.unique_id,
        status=result.status,
        timing=result.timing,
        thread_id=result.thread_id,
        execution_time=result.execution_time,
        message=result.message,
        adapter_response=result.adapter_response,
        failures=result.failures,
        compiled=result.node.compiled if compiled else None,  # type:ignore
        compiled_code=result.node.compiled_code if compiled else None,  # type:ignore
        relation_name=result.node.relation_name if compiled else None,  # type:ignore
        return_value=result.return_value,
    )


@dataclass
class RunExecutionResult(
    ExecutionResult,
):
    results: Sequence[RunResult]
    args: Dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def write(self, path: str):
        writable = RunResultsArtifact.from_execution_results(
            results=self.results,
            elapsed_time=self.elapsed_time,
            generated_at=self.generated_at,
            args=self.args,
        )
        writable.write(path)


@dataclass
@schema_version("run-results", 5)
class RunResultsArtifact(ExecutionResult, ArtifactMixin):
    results: Sequence[RunResultOutput]
    args: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_execution_results(
        cls,
        results: Sequence[RunResult],
        elapsed_time: float,
        generated_at: datetime,
        args: Dict,
    ):
        processed_results = [
            process_run_result(result) for result in results if isinstance(result, RunResult)
        ]
        meta = RunResultsMetadata(
            dbt_schema_version=str(cls.dbt_schema_version),
            generated_at=generated_at,
        )
        return cls(metadata=meta, results=processed_results, elapsed_time=elapsed_time, args=args)

    def write(self, path: str):
        write_json(path, self.to_dict(omit_none=False))


# due to issues with typing.Union collapsing subclasses, this can't subclass
# PartialResult


@dataclass
class SourceFreshnessResult(NodeResult):
    node: SourceDefinition
    status: FreshnessStatus
    max_loaded_at: datetime
    snapshotted_at: datetime
    age: float

    @property
    def skipped(self):
        return False


class FreshnessErrorEnum(StrEnum):
    runtime_error = "runtime error"


@dataclass
class SourceFreshnessRuntimeError(dbtClassMixin):
    unique_id: str
    error: Optional[Union[str, int]]
    status: FreshnessErrorEnum


@dataclass
class SourceFreshnessOutput(dbtClassMixin):
    unique_id: str
    max_loaded_at: datetime
    snapshotted_at: datetime
    max_loaded_at_time_ago_in_s: float
    status: FreshnessStatus
    criteria: FreshnessThreshold
    adapter_response: Dict[str, Any]
    timing: List[TimingInfo]
    thread_id: str
    execution_time: float


@dataclass
class PartialSourceFreshnessResult(NodeResult):
    status: FreshnessStatus

    @property
    def skipped(self):
        return False


FreshnessNodeResult = Union[PartialSourceFreshnessResult, SourceFreshnessResult]
FreshnessNodeOutput = Union[SourceFreshnessRuntimeError, SourceFreshnessOutput]


def process_freshness_result(result: FreshnessNodeResult) -> FreshnessNodeOutput:
    unique_id = result.node.unique_id
    if result.status == FreshnessStatus.RuntimeErr:
        return SourceFreshnessRuntimeError(
            unique_id=unique_id,
            error=result.message,
            status=FreshnessErrorEnum.runtime_error,
        )

    # we know that this must be a SourceFreshnessResult
    if not isinstance(result, SourceFreshnessResult):
        raise DbtInternalError(
            "Got {} instead of a SourceFreshnessResult for a "
            "non-error result in freshness execution!".format(type(result))
        )
    # if we're here, we must have a non-None freshness threshold
    criteria = result.node.freshness
    if criteria is None:
        raise DbtInternalError(
            "Somehow evaluated a freshness result for a source that has no freshness criteria!"
        )
    return SourceFreshnessOutput(
        unique_id=unique_id,
        max_loaded_at=result.max_loaded_at,
        snapshotted_at=result.snapshotted_at,
        max_loaded_at_time_ago_in_s=result.age,
        status=result.status,
        criteria=criteria,
        adapter_response=result.adapter_response,
        timing=result.timing,
        thread_id=result.thread_id,
        execution_time=result.execution_time,
    )


@dataclass
class FreshnessMetadata(BaseArtifactMetadata):
    dbt_schema_version: str = field(
        default_factory=lambda: str(FreshnessExecutionResultArtifact.dbt_schema_version)
    )


@dataclass
class FreshnessResult(ExecutionResult):
    metadata: FreshnessMetadata
    results: Sequence[FreshnessNodeResult]

    @classmethod
    def from_node_results(
        cls,
        results: List[FreshnessNodeResult],
        elapsed_time: float,
        generated_at: datetime,
    ):
        meta = FreshnessMetadata(generated_at=generated_at)
        return cls(metadata=meta, results=results, elapsed_time=elapsed_time)

    def write(self, path):
        FreshnessExecutionResultArtifact.from_result(self).write(path)


@dataclass
@schema_version("sources", 3)
class FreshnessExecutionResultArtifact(
    ArtifactMixin,
    VersionedSchema,
):
    metadata: FreshnessMetadata
    results: Sequence[FreshnessNodeOutput]
    elapsed_time: float

    @classmethod
    def from_result(cls, base: FreshnessResult):
        processed = [process_freshness_result(r) for r in base.results]
        return cls(
            metadata=base.metadata,
            results=processed,
            elapsed_time=base.elapsed_time,
        )


Primitive = Union[bool, str, float, None]
PrimitiveDict = Dict[str, Primitive]

CatalogKey = NamedTuple(
    "CatalogKey", [("database", Optional[str]), ("schema", str), ("name", str)]
)


@dataclass
class StatsItem(dbtClassMixin):
    id: str
    label: str
    value: Primitive
    include: bool
    description: Optional[str] = None


StatsDict = Dict[str, StatsItem]


@dataclass
class ColumnMetadata(dbtClassMixin):
    type: str
    index: int
    name: str
    comment: Optional[str] = None


ColumnMap = Dict[str, ColumnMetadata]


@dataclass
class TableMetadata(dbtClassMixin):
    type: str
    schema: str
    name: str
    database: Optional[str] = None
    comment: Optional[str] = None
    owner: Optional[str] = None


@dataclass
class CatalogTable(dbtClassMixin, Replaceable):
    metadata: TableMetadata
    columns: ColumnMap
    stats: StatsDict
    # the same table with two unique IDs will just be listed two times
    unique_id: Optional[str] = None

    def key(self) -> CatalogKey:
        return CatalogKey(
            lowercase(self.metadata.database),
            self.metadata.schema.lower(),
            self.metadata.name.lower(),
        )


@dataclass
class CatalogMetadata(BaseArtifactMetadata):
    dbt_schema_version: str = field(
        default_factory=lambda: str(CatalogArtifact.dbt_schema_version)
    )


@dataclass
class CatalogResults(dbtClassMixin):
    nodes: Dict[str, CatalogTable]
    sources: Dict[str, CatalogTable]
    errors: Optional[List[str]] = None
    _compile_results: Optional[Any] = None

    def __post_serialize__(self, dct):
        dct = super().__post_serialize__(dct)
        if "_compile_results" in dct:
            del dct["_compile_results"]
        return dct


@dataclass
@schema_version("catalog", 1)
class CatalogArtifact(CatalogResults, ArtifactMixin):
    metadata: CatalogMetadata

    @classmethod
    def from_results(
        cls,
        generated_at: datetime,
        nodes: Dict[str, CatalogTable],
        sources: Dict[str, CatalogTable],
        compile_results: Optional[Any],
        errors: Optional[List[str]],
    ) -> "CatalogArtifact":
        meta = CatalogMetadata(generated_at=generated_at)
        return cls(
            metadata=meta,
            nodes=nodes,
            sources=sources,
            errors=errors,
            _compile_results=compile_results,
        )
