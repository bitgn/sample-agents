from typing import Annotated, List, Literal, Union

from annotated_types import Ge, Le, MaxLen, MinLen
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Vault context — extracted from tree + AGENTS.MD in prephase (SGR step)
# ---------------------------------------------------------------------------

class VaultContext(BaseModel):
    """Dynamically discovered vault structure. Replaces any hardcoded paths."""
    inbox_dirs: List[str] = Field(
        default_factory=list,
        description="Directories where new/incoming items arrive (read-mostly)",
    )
    capture_dirs: List[str] = Field(
        default_factory=list,
        description="Directories for raw captured content",
    )
    cards_dirs: List[str] = Field(
        default_factory=list,
        description="Directories for distilled notes/cards",
    )
    threads_dirs: List[str] = Field(
        default_factory=list,
        description="Directories for threads/ongoing discussions",
    )
    template_prefixes: List[str] = Field(
        default_factory=lambda: ["_"],
        description="Filename prefixes that mark template files — never delete",
    )
    readonly_during_cleanup: List[str] = Field(
        default_factory=list,
        description="Directories that must NOT be touched during card/thread cleanup tasks",
    )
    notes: str = Field(
        default="",
        description="Key file naming conventions and vault-specific rules",
    )


class ReportTaskCompletion(BaseModel):
    tool: Literal["report_completion"]
    completed_steps_laconic: List[str]
    message: str
    grounding_refs: List[str] = Field(default_factory=list)
    outcome: Literal[
        "OUTCOME_OK",
        "OUTCOME_DENIED_SECURITY",
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
        "OUTCOME_ERR_INTERNAL",
    ]


class Req_Tree(BaseModel):
    tool: Literal["tree"]
    level: int = Field(2, description="max tree depth, 0 means unlimited")
    root: str = Field("", description="tree root, empty means repository root")


class Req_Context(BaseModel):
    tool: Literal["context"]


class Req_Find(BaseModel):
    tool: Literal["find"]
    name: str
    root: str = "/"
    kind: Literal["all", "files", "dirs"] = "all"
    limit: Annotated[int, Ge(1), Le(20)] = 10


class Req_Search(BaseModel):
    tool: Literal["search"]
    pattern: str
    limit: Annotated[int, Ge(1), Le(20)] = 10
    root: str = "/"


class Req_List(BaseModel):
    tool: Literal["list"]
    path: str = "/"


class Req_Read(BaseModel):
    tool: Literal["read"]
    path: str
    number: bool = Field(False, description="return 1-based line numbers")
    start_line: int = Field(0, description="1-based inclusive linum; 0 == from the first line")
    end_line: int = Field(0, description="1-based inclusive linum; 0 == through the last line")


class Req_Write(BaseModel):
    tool: Literal["write"]
    path: str
    content: str
    start_line: int = Field(0, description="1-based inclusive line number; 0 keeps whole-file overwrite behavior")
    end_line: int = Field(0, description="1-based inclusive line number; 0 means through the last line for ranged writes")


class Req_Delete(BaseModel):
    tool: Literal["delete"]
    path: str


class Req_MkDir(BaseModel):
    tool: Literal["mkdir"]
    path: str


class Req_Move(BaseModel):
    tool: Literal["move"]
    from_name: str
    to_name: str


class NextStep(BaseModel):
    current_state: str
    plan_remaining_steps_brief: Annotated[List[str], MinLen(1), MaxLen(5)] = Field(
        ...,
        description="briefly explain the next useful steps",
    )
    done_operations: List[str] = Field(
        default_factory=list,
        description="Accumulated list of ALL confirmed write/delete/move operations completed so far in this task (e.g. 'WRITTEN: /path', 'DELETED: /path'). Never omit previously listed entries.",
    )
    task_completed: bool
    # AICODE-NOTE: Keep this union aligned with the public PCM runtime surface
    # plus the local stop action. PCM currently lacks a public completion RPC, so
    # `report_completion` ends the sample loop locally and `EndTrial` still grades
    # only the runtime events that the harness persisted.
    function: Union[
        ReportTaskCompletion,
        Req_Context,
        Req_Tree,
        Req_Find,
        Req_Search,
        Req_List,
        Req_Read,
        Req_Write,
        Req_Delete,
        Req_MkDir,
        Req_Move,
    ] = Field(..., description="execute the first remaining step")
